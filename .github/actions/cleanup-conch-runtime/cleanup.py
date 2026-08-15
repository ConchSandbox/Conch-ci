#!/usr/bin/env python3
"""Release Conch processes, CNI state, mounts, and namespace handles."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MOUNT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
NETWORK_NAMESPACE_DIR = Path("/run/conch/netns")
NETWORK_NAMESPACE_RE = re.compile(r"^slot-([0-9]+)$")
CNI_CACHE_FILE_RE = re.compile(r"^conch-bridge-conch-slot-([0-9]+)-eth0$")
CNI_COMMENT_RE = re.compile(
    r'^name: "conch-bridge" id: "conch-slot-([0-9]+)"$'
)
CNI_CHAIN_RE = re.compile(r"^CNI-[0-9A-Fa-f]+$")
FIRST_SLOT_ID = 2
MAX_SLOT_ID_EXCLUSIVE = 4002
CNI_CACHE_KIND = "cniCacheV1"
CNI_NETWORK_NAME = "conch-bridge"
CNI_INTERFACE_NAME = "eth0"
CNI_CONTAINER_PREFIX = "conch-slot-"
CNI_BRIDGE_NAME = "cni-conch0"
DEFAULT_CNI_DATA_DIR = "/var/lib/conch/cni/networks"
TAP_INTERFACE_NAME = "tap0"
# Keep this distinct from argparse's exit status 2 so the action can safely
# distinguish "reported residue, fallback succeeded" from invocation errors.
RESIDUAL_EXIT_STATUS = 42


@dataclass(frozen=True)
class CNIAttachment:
    slot_id: int
    container_id: str
    netns: str
    plugin_config: dict[str, Any]
    cni_args: str
    cache_path: Path


@dataclass(frozen=True)
class ResidualResources:
    """Dynamic resources Conch should remove during graceful shutdown."""

    forced_daemon_pids: tuple[int, ...]
    child_process_pids: tuple[int, ...]
    cni_cache_entries: tuple[str, ...]
    network_namespaces: tuple[str, ...]
    bridge_ports: tuple[str, ...]
    host_tap_present: bool
    nat_rule_count: int
    forward_rule_count: int
    workdir_mounts: tuple[str, ...]

    def found(self) -> bool:
        return any(
            (
                self.forced_daemon_pids,
                self.child_process_pids,
                self.cni_cache_entries,
                self.network_namespaces,
                self.bridge_ports,
                self.host_tap_present,
                self.nat_rule_count,
                self.forward_rule_count,
                self.workdir_mounts,
            )
        )


def process_start_time(pid: int) -> int | None:
    """Read Linux /proc stat field 22 for PID-reuse-safe process identity."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    end = data.rfind(")")
    if end < 0:
        return None
    fields = data[end + 2 :].split()
    if len(fields) < 20:
        return None
    if fields[0] == "Z":
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def process_command(pid: int) -> tuple[str, bytes] | None:
    try:
        comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    return comm, cmdline


def runtime_processes(workdir: Path, *, daemon: bool) -> dict[int, int]:
    """Find Conch processes tied to this job runtime."""
    workdir_variants = {os.fsencode(workdir), os.fsencode(workdir.resolve())}
    workdir_prefixes = {
        variant + os.fsencode(os.sep) for variant in workdir_variants
    }
    processes: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        command = process_command(pid)
        if command is None:
            continue
        comm, cmdline = command
        arguments = cmdline.split(b"\0")
        executable = Path(os.fsdecode(arguments[0])).name if arguments[0] else ""
        if not any(
            argument in workdir_variants
            or any(prefix in argument for prefix in workdir_prefixes)
            for argument in arguments
        ):
            continue
        if daemon:
            matches = comm == "conchd"
        else:
            matches = (
                executable in {"cloud-hypervisor", "stratovirt"}
                and b"conch.sandbox_id=" in cmdline
            ) or executable == "virtiofsd"
        if not matches:
            continue
        start_time = process_start_time(pid)
        if start_time is not None:
            processes[pid] = start_time
    return processes


def other_conchd_processes(workdir: Path) -> list[int]:
    """Return live conchd PIDs that do not belong to this runtime."""
    scoped = set(runtime_processes(workdir, daemon=True))
    processes: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        command = process_command(pid)
        if (
            command is not None
            and command[0] == "conchd"
            and pid not in scoped
            and process_start_time(pid) is not None
        ):
            processes.append(pid)
    return sorted(processes)


def terminate_processes(
    processes: dict[int, int], timeout: float
) -> tuple[int, ...]:
    """Stop captured processes, rechecking start time before every signal."""
    for pid, start_time in processes.items():
        if process_start_time(pid) != start_time:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(
            process_start_time(pid) == start_time
            for pid, start_time in processes.items()
        ):
            break
        time.sleep(0.1)

    forced: list[int] = []
    for pid, start_time in processes.items():
        if process_start_time(pid) != start_time:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            forced.append(pid)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not any(
            process_start_time(pid) == start_time
            for pid, start_time in processes.items()
        ):
            break
        time.sleep(0.1)
    return tuple(sorted(forced))


def decode_mount_path(value: str) -> str:
    """Decode the octal escapes used for mount points in Linux mountinfo."""
    return MOUNT_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), value)


def mount_targets() -> set[str]:
    mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    targets: set[str] = set()
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) >= 5:
            targets.add(decode_mount_path(fields[4]))
    return targets


def workdir_mount_targets(workdir: Path) -> set[str]:
    """Return mounts at or below workdir without relying on path traversal."""
    workdir_text = str(workdir.resolve())
    return {
        target
        for target in mount_targets()
        if target == workdir_text or target.startswith(workdir_text + os.sep)
    }


def cni_cache_entry_paths(workdir: Path) -> list[Path]:
    results_dir = workdir / "state" / "cni" / "results"
    try:
        return sorted(results_dir.iterdir(), key=lambda path: path.name)
    except FileNotFoundError:
        return []


def bridge_port_names() -> list[str]:
    bridge_ports = Path(f"/sys/class/net/{CNI_BRIDGE_NAME}/brif")
    try:
        return sorted(path.name for path in bridge_ports.iterdir())
    except FileNotFoundError:
        return []


def unmount_workdir(workdir: Path) -> None:
    """Unmount child mounts before parents so nested snapshot layouts unwind."""
    targets = workdir_mount_targets(workdir)
    for target in sorted(
        targets,
        key=lambda value: (value.count(os.sep), len(value)),
        reverse=True,
    ):
        subprocess.run(["umount", "--", target], check=True)


def validate_slot_id(slot_id: int) -> None:
    if not FIRST_SLOT_ID <= slot_id < MAX_SLOT_ID_EXCLUSIVE:
        raise RuntimeError(
            f"slot ID {slot_id} is outside supported range "
            f"[{FIRST_SLOT_ID}, {MAX_SLOT_ID_EXCLUSIVE})"
        )


def parse_json_object(data: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid {description}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{description} must contain a JSON object")
    return value


def plugin_config_from_network(
    network: dict[str, Any], expected_data_dir: Path
) -> dict[str, Any]:
    """Build the single plugin input used by current Conch/libcni."""
    name = network.get("name")
    cni_version = network.get("cniVersion")
    if name != CNI_NETWORK_NAME or not isinstance(cni_version, str) or not cni_version:
        raise RuntimeError("CNI config has an unexpected name or cniVersion")

    plugins = network.get("plugins")
    if plugins is None:
        plugin: Any = dict(network)
    elif isinstance(plugins, list) and len(plugins) == 1:
        plugin = dict(plugins[0]) if isinstance(plugins[0], dict) else None
    else:
        plugin = None
    if not isinstance(plugin, dict):
        raise RuntimeError("CNI config must contain exactly one plugin")
    if plugin.get("type") != "bridge" or plugin.get("bridge") != CNI_BRIDGE_NAME:
        raise RuntimeError("CNI config is not the Conch bridge network")
    ipam = plugin.get("ipam")
    if not isinstance(ipam, dict) or ipam.get("type") != "host-local":
        raise RuntimeError("Conch bridge config must use host-local IPAM")
    expected = str(expected_data_dir)
    if ipam.get("dataDir") not in {DEFAULT_CNI_DATA_DIR, expected}:
        raise RuntimeError(
            f"host-local dataDir is not a supported Conch source or runtime path: "
            f"{ipam.get('dataDir')!r}"
        )
    ipam = dict(ipam)
    ipam["dataDir"] = expected
    plugin["ipam"] = ipam

    plugin["name"] = name
    plugin["cniVersion"] = cni_version
    return plugin


def encode_cni_args(raw_args: Any) -> str:
    if raw_args in (None, []):
        return ""
    if not isinstance(raw_args, list):
        raise RuntimeError("cached cniArgs must be a list")
    values: list[str] = []
    for pair in raw_args:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(value, str) for value in pair)
            or ";" in pair[0]
            or "=" in pair[0]
            or ";" in pair[1]
        ):
            raise RuntimeError("cached cniArgs contains an invalid pair")
        values.append(f"{pair[0]}={pair[1]}")
    return ";".join(values)


def load_cached_attachments(workdir: Path) -> list[CNIAttachment]:
    paths = cni_cache_entry_paths(workdir)

    attachments: list[CNIAttachment] = []
    expected_data_dir = workdir / "state" / "cni" / "networks"
    for path in paths:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise RuntimeError(f"unsafe CNI cache entry: {path}")
        match = CNI_CACHE_FILE_RE.fullmatch(path.name)
        if match is None:
            raise RuntimeError(f"unexpected CNI cache file: {path}")
        slot_id = int(match.group(1))
        validate_slot_id(slot_id)
        cached = parse_json_object(path.read_bytes(), f"CNI cache file {path}")
        container_id = f"{CNI_CONTAINER_PREFIX}{slot_id}"
        netns = str(NETWORK_NAMESPACE_DIR / f"slot-{slot_id}")
        if (
            cached.get("kind") != CNI_CACHE_KIND
            or cached.get("containerId") != container_id
            or cached.get("ifName") != CNI_INTERFACE_NAME
            or cached.get("networkName") != CNI_NETWORK_NAME
            or cached.get("netns") != netns
        ):
            raise RuntimeError(f"CNI cache entry is not owned by slot {slot_id}: {path}")
        if cached.get("capabilityArgs") not in (None, {}):
            raise RuntimeError(f"unsupported capabilityArgs in CNI cache file: {path}")
        encoded_config = cached.get("config")
        if not isinstance(encoded_config, str):
            raise RuntimeError(f"CNI cache config is not base64 text: {path}")
        try:
            config_bytes = base64.b64decode(encoded_config, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RuntimeError(f"invalid base64 CNI config in {path}: {exc}") from exc
        network = parse_json_object(config_bytes, f"cached CNI config in {path}")
        plugin = plugin_config_from_network(network, expected_data_dir)
        result = cached.get("result")
        if result is not None:
            if not isinstance(result, dict):
                raise RuntimeError(f"cached CNI result is not an object: {path}")
            plugin["prevResult"] = result
        attachments.append(
            CNIAttachment(
                slot_id=slot_id,
                container_id=container_id,
                netns=netns,
                plugin_config=plugin,
                cni_args=encode_cni_args(cached.get("cniArgs")),
                cache_path=path,
            )
        )
    return attachments


def load_runtime_plugin_config(workdir: Path) -> dict[str, Any]:
    conf_dir = workdir / "cni" / "net.d"
    candidates = sorted(
        path
        for path in conf_dir.iterdir()
        if path.suffix in {".conf", ".conflist", ".json"}
    )
    if not candidates:
        raise RuntimeError(f"no CNI config found in {conf_dir}")
    path = candidates[0]
    if not stat.S_ISREG(path.lstat().st_mode):
        raise RuntimeError(f"unsafe CNI config file: {path}")
    network = parse_json_object(path.read_bytes(), f"CNI config {path}")
    return plugin_config_from_network(
        network, workdir / "state" / "cni" / "networks"
    )


def network_namespace_paths() -> dict[int, Path]:
    try:
        paths = sorted(NETWORK_NAMESPACE_DIR.iterdir(), key=lambda path: path.name)
    except FileNotFoundError:
        return {}
    namespaces: dict[int, Path] = {}
    for path in paths:
        if not path.name.startswith("slot-"):
            continue
        match = NETWORK_NAMESPACE_RE.fullmatch(path.name)
        if match is None:
            raise RuntimeError(f"invalid Conch network namespace name: {path}")
        if stat.S_ISLNK(path.lstat().st_mode):
            raise RuntimeError(f"unsafe Conch network namespace handle: {path}")
        slot_id = int(match.group(1))
        validate_slot_id(slot_id)
        namespaces[slot_id] = path
    return namespaces


def delete_namespace_tap(path: Path) -> None:
    if str(path) not in mount_targets():
        return
    completed = subprocess.run(
        ["nsenter", f"--net={path}", "ip", "link", "delete", TAP_INTERFACE_NAME],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0 and "does not exist" not in completed.stderr:
        print(
            f"warning: could not delete {TAP_INTERFACE_NAME} in {path}: "
            f"{completed.stderr.strip()}",
            file=sys.stderr,
        )


def run_cni_del(
    binary_dir: Path,
    slot_id: int,
    plugin_config: dict[str, Any],
    cni_args: str,
    netns_mounted: bool,
) -> None:
    cni_dir = binary_dir / "cni"
    bridge_plugin = cni_dir / "bridge"
    if not binary_dir.is_absolute() or not os.access(bridge_plugin, os.X_OK):
        raise RuntimeError(f"CNI bridge plugin is not executable: {bridge_plugin}")
    netns = NETWORK_NAMESPACE_DIR / f"slot-{slot_id}"
    environment = os.environ.copy()
    environment.update(
        {
            "CNI_COMMAND": "DEL",
            "CNI_CONTAINERID": f"{CNI_CONTAINER_PREFIX}{slot_id}",
            "CNI_NETNS": str(netns) if netns_mounted else "",
            "CNI_IFNAME": CNI_INTERFACE_NAME,
            "CNI_ARGS": cni_args,
            "CNI_PATH": str(cni_dir),
        }
    )
    completed = subprocess.run(
        [str(bridge_plugin)],
        input=json.dumps(plugin_config, separators=(",", ":")).encode(),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode(errors="replace").strip()
        stdout = completed.stdout.decode(errors="replace").strip()
        detail = stderr or stdout or f"exit status {completed.returncode}"
        raise RuntimeError(f"CNI DEL for slot {slot_id} failed: {detail}")


def iptables_rules(table: str, chain: str | None = None) -> list[list[str]]:
    command = ["iptables", "-w", "30", "-t", table, "-S"]
    if chain is not None:
        command.append(chain)
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return [shlex.split(line) for line in completed.stdout.splitlines() if line]


def rule_comment(tokens: list[str]) -> str | None:
    try:
        index = tokens.index("--comment")
    except ValueError:
        return None
    if index + 1 >= len(tokens):
        return None
    return tokens[index + 1]


def conch_nat_rule(tokens: list[str]) -> bool:
    comment = rule_comment(tokens)
    if comment is None:
        return False
    match = CNI_COMMENT_RE.fullmatch(comment)
    if match is None:
        return False
    try:
        validate_slot_id(int(match.group(1)))
    except RuntimeError:
        return False
    return len(tokens) >= 2 and tokens[0] == "-A"


def conch_forward_rule(tokens: list[str]) -> bool:
    if len(tokens) < 2 or tokens[:2] != ["-A", "FORWARD"]:
        return False
    if "-j" not in tokens or tokens[tokens.index("-j") + 1 :] != ["ACCEPT"]:
        return False
    return any(
        tokens[index : index + 2] in (["-i", CNI_BRIDGE_NAME], ["-o", CNI_BRIDGE_NAME])
        for index in range(len(tokens) - 1)
    )


def delete_iptables_rule(table: str, tokens: list[str]) -> None:
    if len(tokens) < 2 or tokens[0] != "-A":
        raise RuntimeError(f"refusing unexpected iptables rule: {tokens}")
    subprocess.run(
        ["iptables", "-w", "30", "-t", table, "-D", *tokens[1:]],
        check=True,
    )


def remove_conch_iptables_rules() -> bool:
    """Remove only rules and private chains attributable to current Conch."""
    removed = False
    nat_rules = iptables_rules("nat")
    private_chains: set[str] = set()
    for tokens in nat_rules:
        if not conch_nat_rule(tokens):
            continue
        if "-j" in tokens:
            target_index = tokens.index("-j") + 1
            if target_index < len(tokens) and CNI_CHAIN_RE.fullmatch(tokens[target_index]):
                private_chains.add(tokens[target_index])
        delete_iptables_rule("nat", tokens)
        removed = True

    current_chains = {
        tokens[1]
        for tokens in iptables_rules("nat")
        if len(tokens) == 2 and tokens[0] == "-N"
    }
    for chain in sorted(private_chains & current_chains):
        subprocess.run(
            ["iptables", "-w", "30", "-t", "nat", "-F", chain], check=True
        )
        subprocess.run(
            ["iptables", "-w", "30", "-t", "nat", "-X", chain], check=True
        )

    for tokens in iptables_rules("filter", "FORWARD"):
        if conch_forward_rule(tokens):
            delete_iptables_rule("filter", tokens)
            removed = True
    return removed


def link_exists(name: str) -> bool:
    completed = subprocess.run(
        ["ip", "link", "show", "dev", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def detect_residual_resources(
    workdir: Path,
    forced_daemon_pids: tuple[int, ...],
    child_processes: dict[int, int],
) -> ResidualResources:
    """Inspect without mutating after conchd has had a graceful exit window."""
    namespaces = network_namespace_paths()
    nat_rules = [tokens for tokens in iptables_rules("nat") if conch_nat_rule(tokens)]
    forward_rules = [
        tokens
        for tokens in iptables_rules("filter", "FORWARD")
        if conch_forward_rule(tokens)
    ]
    return ResidualResources(
        forced_daemon_pids=forced_daemon_pids,
        child_process_pids=tuple(sorted(child_processes)),
        cni_cache_entries=tuple(
            str(path) for path in cni_cache_entry_paths(workdir)
        ),
        network_namespaces=tuple(
            str(namespaces[slot_id]) for slot_id in sorted(namespaces)
        ),
        bridge_ports=tuple(bridge_port_names()),
        host_tap_present=link_exists(TAP_INTERFACE_NAME),
        nat_rule_count=len(nat_rules),
        forward_rule_count=len(forward_rules),
        workdir_mounts=tuple(sorted(workdir_mount_targets(workdir))),
    )


def markdown_value(values: tuple[object, ...]) -> str:
    text = ", ".join(str(value) for value in values)
    return text.replace("|", "\\|").replace("`", "\\`")


def residual_report(resources: ResidualResources) -> str:
    rows: list[tuple[str, str]] = []
    if resources.forced_daemon_pids:
        rows.append(
            (
                "conchd processes requiring SIGKILL",
                markdown_value(resources.forced_daemon_pids),
            )
        )
    if resources.child_process_pids:
        rows.append(
            ("VMM/virtiofs processes", markdown_value(resources.child_process_pids))
        )
    if resources.cni_cache_entries:
        rows.append(("libcni result cache", markdown_value(resources.cni_cache_entries)))
    if resources.network_namespaces:
        rows.append(("network namespaces", markdown_value(resources.network_namespaces)))
    if resources.bridge_ports:
        rows.append(("cni-conch0 ports", markdown_value(resources.bridge_ports)))
    if resources.host_tap_present:
        rows.append(("host-namespace tap", TAP_INTERFACE_NAME))
    if resources.nat_rule_count:
        rows.append(("Conch CNI NAT rules", str(resources.nat_rule_count)))
    if resources.forward_rule_count:
        rows.append(("cni-conch0 FORWARD rules", str(resources.forward_rule_count)))
    if resources.workdir_mounts:
        rows.append(("runtime mounts", markdown_value(resources.workdir_mounts)))
    lines = [
        "### Conch graceful-shutdown residual resources",
        "",
        "Conch left dynamic runtime resources after its graceful shutdown window. "
        "This is treated as a Conch teardown bug; CI started fallback cleanup.",
        "",
        "| Resource | Residue |",
        "| --- | --- |",
    ]
    lines.extend(f"| {name} | {value} |" for name, value in rows)
    lines.extend(("", "Fallback cleanup started after this inventory.", ""))
    return "\n".join(lines)


def write_report(report_file: Path, contents: str) -> None:
    if not report_file.is_absolute() or report_file.parent.resolve() == Path("/"):
        raise RuntimeError(f"report file must be below an absolute directory: {report_file}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(report_file, flags, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(contents)


def append_report(report_file: Path, contents: str) -> None:
    mode = report_file.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise RuntimeError(f"unsafe cleanup report file: {report_file}")
    flags = os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(report_file, flags)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(contents)


def delete_link(name: str) -> None:
    if link_exists(name):
        subprocess.run(["ip", "link", "delete", name], check=True)


def remove_network_namespace_handles(namespaces: dict[int, Path]) -> None:
    """Unmount and unlink Conch's fixed-path namespace handles."""
    for path in namespaces.values():
        if str(path) in mount_targets():
            subprocess.run(["umount", "--", str(path)], check=True)
        if str(path) in mount_targets():
            raise RuntimeError(f"network namespace is still mounted: {path}")
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def verify_network_cleanup() -> None:
    remaining_namespaces = network_namespace_paths()
    if remaining_namespaces:
        raise RuntimeError(
            "Conch network namespaces remain: "
            + ", ".join(str(path) for path in remaining_namespaces.values())
        )
    remaining_nat = [tokens for tokens in iptables_rules("nat") if conch_nat_rule(tokens)]
    remaining_filter = [
        tokens
        for tokens in iptables_rules("filter", "FORWARD")
        if conch_forward_rule(tokens)
    ]
    remaining_links = [
        name for name in (CNI_BRIDGE_NAME, TAP_INTERFACE_NAME) if link_exists(name)
    ]
    if remaining_nat or remaining_filter or remaining_links:
        raise RuntimeError(
            "Conch host networking remains after cleanup: "
            f"nat_rules={len(remaining_nat)}, filter_rules={len(remaining_filter)}, "
            f"links={remaining_links}"
        )


def cached_slot_ids(workdir: Path) -> set[int]:
    slots: set[int] = set()
    for path in cni_cache_entry_paths(workdir):
        match = CNI_CACHE_FILE_RE.fullmatch(path.name)
        if match is None:
            continue
        slot_id = int(match.group(1))
        validate_slot_id(slot_id)
        slots.add(slot_id)
    return slots


def fallback_cleanup(
    workdir: Path,
    binary_dir: Path,
    child_processes: dict[int, int],
) -> None:
    """Clean resources only after graceful-shutdown residue was reported."""
    terminate_processes(child_processes, timeout=5)
    remaining_runtime_processes = {
        **runtime_processes(workdir, daemon=True),
        **runtime_processes(workdir, daemon=False),
    }
    if remaining_runtime_processes:
        raise RuntimeError(
            "runtime processes survived fallback termination: "
            + ", ".join(str(pid) for pid in sorted(remaining_runtime_processes))
        )

    try:
        attachments = load_cached_attachments(workdir)
    except (OSError, RuntimeError) as exc:
        attachments = []
        print(
            f"warning: cannot replay cached CNI configuration; "
            f"using validated runtime config and fixed-name fallback: {exc}",
            file=sys.stderr,
        )
    namespaces = network_namespace_paths()
    mounted = mount_targets()

    for path in namespaces.values():
        delete_namespace_tap(path)

    failed: dict[int, str] = {}
    attached_slots: set[int] = set()
    for attachment in attachments:
        attached_slots.add(attachment.slot_id)
        try:
            run_cni_del(
                binary_dir,
                attachment.slot_id,
                attachment.plugin_config,
                attachment.cni_args,
                attachment.netns in mounted,
            )
            attachment.cache_path.unlink(missing_ok=True)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            failed[attachment.slot_id] = str(exc)

    expected_slots = set(namespaces) | cached_slot_ids(workdir)
    missing_slots = sorted(expected_slots - attached_slots)
    if missing_slots:
        try:
            fallback_config = load_runtime_plugin_config(workdir)
        except (OSError, RuntimeError) as exc:
            fallback_config = None
            for slot_id in missing_slots:
                failed[slot_id] = str(exc)
        if fallback_config is not None:
            for slot_id in missing_slots:
                try:
                    run_cni_del(
                        binary_dir,
                        slot_id,
                        fallback_config,
                        "",
                        str(namespaces.get(slot_id, "")) in mounted,
                    )
                    failed.pop(slot_id, None)
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    failed[slot_id] = str(exc)

    remove_conch_iptables_rules()

    # An iptables dependency can make the first bridge DEL fail. Retry after
    # removing only the rules that carry Conch's exact network/container tags.
    for attachment in attachments:
        if attachment.slot_id not in failed:
            continue
        try:
            run_cni_del(
                binary_dir,
                attachment.slot_id,
                attachment.plugin_config,
                attachment.cni_args,
                attachment.netns in mounted,
            )
            attachment.cache_path.unlink(missing_ok=True)
            failed.pop(attachment.slot_id, None)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            failed[attachment.slot_id] = str(exc)

    remove_network_namespace_handles(namespaces)
    delete_link(CNI_BRIDGE_NAME)
    # Current Conch creates this persistent TAP only inside a slot namespace.
    # Seeing it in the host namespace means an interrupted netns teardown moved
    # it there; no live conchd remains at this point.
    delete_link(TAP_INTERFACE_NAME)
    remove_conch_iptables_rules()
    verify_network_cleanup()

    if failed:
        for slot_id, message in sorted(failed.items()):
            print(
                f"warning: CNI DEL for slot {slot_id} did not complete; "
                f"fixed-name fallback cleanup succeeded: {message}",
                file=sys.stderr,
            )

    unmount_workdir(workdir)
    remaining_mounts = workdir_mount_targets(workdir)
    if remaining_mounts:
        raise RuntimeError(
            "refusing to delete Conch work directory with mounted paths: "
            + ", ".join(sorted(remaining_mounts))
        )


def cleanup(workdir: Path, binary_dir: Path, report_file: Path) -> bool:
    """Stop conchd, report any residue, and run fallback only when necessary."""
    forced_daemon_pids = terminate_processes(
        runtime_processes(workdir, daemon=True), timeout=30
    )
    remaining_daemons = runtime_processes(workdir, daemon=True)
    if remaining_daemons:
        raise RuntimeError(
            "conchd processes survived termination: "
            + ", ".join(str(pid) for pid in sorted(remaining_daemons))
        )

    other_daemons = other_conchd_processes(workdir)
    if other_daemons:
        raise RuntimeError(
            "refusing global Conch network inspection while another conchd is live: "
            + ", ".join(str(pid) for pid in other_daemons)
        )

    child_processes = runtime_processes(workdir, daemon=False)
    resources = detect_residual_resources(
        workdir, forced_daemon_pids, child_processes
    )
    if not resources.found():
        # The current Conch dev branch intentionally leaves the empty CNI bridge
        # after Pool.Close. It is expected host state, not a teardown bug.
        delete_link(CNI_BRIDGE_NAME)
        if link_exists(CNI_BRIDGE_NAME):
            raise RuntimeError(f"expected empty bridge remains: {CNI_BRIDGE_NAME}")
        return False

    write_report(report_file, residual_report(resources))
    print(
        "Conch left dynamic resources after graceful shutdown; "
        f"fallback cleanup started and report written to {report_file}",
        file=sys.stderr,
    )
    try:
        fallback_cleanup(workdir, binary_dir, child_processes)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        try:
            append_report(report_file, "\nFallback cleanup status: **failed**\n")
        except (OSError, RuntimeError) as report_exc:
            print(
                f"warning: cannot update cleanup report: {report_exc}",
                file=sys.stderr,
            )
        raise
    append_report(report_file, "\nFallback cleanup status: **succeeded**\n")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--binary-dir", type=Path, required=True)
    parser.add_argument("--report-file", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    workdir = args.work_dir
    binary_dir = args.binary_dir
    report_file = args.report_file
    if not workdir.is_absolute() or workdir.resolve() == Path("/"):
        raise SystemExit(f"work directory must be an absolute non-root path: {workdir}")
    if not binary_dir.is_absolute() or binary_dir.resolve() == Path("/"):
        raise SystemExit(f"binary directory must be an absolute non-root path: {binary_dir}")
    if not report_file.is_absolute() or report_file.resolve() == Path("/"):
        raise SystemExit(f"report file must be an absolute non-root path: {report_file}")
    try:
        residue_detected = cleanup(workdir, binary_dir, report_file)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Conch runtime cleanup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if residue_detected:
        print(
            "::error title=Conch teardown bug detected::Conch left dynamic "
            "runtime resources after graceful shutdown; fallback cleanup succeeded."
        )
        raise SystemExit(RESIDUAL_EXIT_STATUS)


if __name__ == "__main__":
    main()
