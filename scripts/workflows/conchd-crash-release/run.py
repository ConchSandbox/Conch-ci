#!/usr/bin/env python3
"""Exercise conchd startup cleanup after an ungraceful daemon exit."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from conch import Sandbox


NETWORK_NAMESPACE_DIR = Path("/run/conch/netns")
BOOT_NAMESPACE = "conch"
MOUNT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
SOCKET_NAME_RE = re.compile(r"^[0-9a-f]{16}\.sock(?:\.serial)?$")
NETWORK_NAMESPACE_RE = re.compile(r"^slot-[0-9]+$")


def log(message: str) -> None:
    print(f"[conchd-crash-release] {message}", flush=True)


def require_absolute_safe_path(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise RuntimeError(f"{label} must be an absolute non-root path: {path}")
    resolved = path.resolve()
    if resolved == Path("/"):
        raise RuntimeError(f"{label} must be an absolute non-root path: {path}")
    return resolved


def wait_for(
    description: str,
    predicate: Callable[[], bool],
    timeout: float = 120,
    interval: float = 0.5,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # diagnostics are included in the timeout error
            last_error = exc
        time.sleep(interval)
    detail = f": {last_error}" if last_error is not None else ""
    raise RuntimeError(f"timed out waiting for {description}{detail}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise RuntimeError(f"invalid crash-release manifest: {path}")
    return value


def read_pid_file(path: Path) -> int:
    value = path.read_text(encoding="utf-8").strip()
    if not value.isdigit() or int(value) <= 1:
        raise RuntimeError(f"invalid PID file {path}: {value!r}")
    return int(value)


def process_info(pid: int) -> dict[str, Any] | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    end = stat.rfind(")")
    if end < 0:
        return None
    fields = stat[end + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        start_time = int(fields[19])
    except ValueError:
        return None
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        cmdline = b""
    return {
        "pid": pid,
        "start_time": start_time,
        "state": fields[0],
        "cmdline": cmdline,
    }


def process_identity(info: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "pid": info["pid"],
        "start_time": info["start_time"],
    }


def same_process(identity: dict[str, Any], zombies_are_alive: bool = False) -> bool:
    current = process_info(int(identity["pid"]))
    if current is None or current["start_time"] != identity["start_time"]:
        return False
    return zombies_are_alive or current["state"] != "Z"


def find_vmm_processes(sandbox_id: str) -> list[dict[str, Any]]:
    marker = os.fsencode(f"conch.sandbox_id={sandbox_id}")
    matches: list[dict[str, Any]] = []
    for entry in sorted(Path("/proc").iterdir(), key=lambda item: item.name):
        if not entry.name.isdigit():
            continue
        info = process_info(int(entry.name))
        if info is None:
            continue
        cmdline = info["cmdline"]
        if marker not in cmdline:
            continue
        if b"cloud-hypervisor" not in cmdline and b"stratovirt" not in cmdline:
            continue
        matches.append(process_identity(info, "vmm"))
    return matches


def decode_mount_path(value: str) -> str:
    return MOUNT_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), value)


def mount_targets() -> set[str]:
    targets: set[str] = set()
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 5:
            targets.add(decode_mount_path(fields[4]))
    return targets


def path_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"path": str(path), "device": stat.st_dev, "inode": stat.st_ino}


def same_path_identity(identity: dict[str, Any]) -> bool:
    try:
        current = Path(identity["path"]).stat()
    except FileNotFoundError:
        return False
    return current.st_dev == identity["device"] and current.st_ino == identity["inode"]


def network_namespaces() -> list[dict[str, Any]]:
    if not NETWORK_NAMESPACE_DIR.exists():
        return []
    return [
        path_identity(path)
        for path in sorted(NETWORK_NAMESPACE_DIR.iterdir())
        if NETWORK_NAMESPACE_RE.fullmatch(path.name)
    ]


def cni_network_state(work_dir: Path) -> tuple[str, Path]:
    config_dir = work_dir / "cni" / "net.d"
    for path in sorted(config_dir.glob("*.conf")):
        document = json.loads(path.read_text(encoding="utf-8"))
        name = document.get("name")
        if isinstance(name, str) and name:
            state_dir = require_absolute_safe_path(
                work_dir / "state" / "cni" / "networks",
                "CNI network state directory",
            )
            return name, state_dir
    raise RuntimeError(f"no named CNI config found below {config_dir}")


def cni_allocations(state_root: Path, network_name: str) -> list[str]:
    state_dir = state_root / network_name
    if not state_dir.exists():
        return []
    allocations: list[str] = []
    for entry in state_dir.iterdir():
        try:
            ipaddress.ip_address(entry.name)
        except ValueError:
            continue
        allocations.append(entry.name)
    return sorted(allocations, key=ipaddress.ip_address)


def socket_paths(work_dir: Path) -> list[str]:
    runtime_dir = work_dir / "work"
    if not runtime_dir.exists():
        return []
    return sorted(
        str(path)
        for path in runtime_dir.rglob("*")
        if SOCKET_NAME_RE.fullmatch(path.name)
    )


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP/1.1 over a Unix socket; conchd serves no TCP listener."""

    def __init__(self, socket_path: str, timeout: float = 2) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def http_status(unix_socket: str, path: str) -> int | None:
    """Status of GET path, or None when conchd is unreachable."""
    connection = _UnixHTTPConnection(unix_socket)
    try:
        connection.request("GET", path)
        return connection.getresponse().status
    except OSError:
        return None
    finally:
        connection.close()


def sandbox_path(sandbox_id: str) -> str:
    return f"/api/v1/sandboxes/{urllib.parse.quote(sandbox_id, safe='')}"


def wait_agent_health(sandbox: Sandbox, timeout: float = 240) -> None:
    def healthy() -> bool:
        return sandbox.client.health_check().get("status") == "OK"

    wait_for(f"sandbox {sandbox.sandbox_id} agent health", healthy, timeout=timeout, interval=2)


def create_sandbox(template_id: str, sandbox_id: str) -> Sandbox:
    from conch import Sandbox

    log(f"creating sandbox {sandbox_id}")
    sandbox = Sandbox.create(
        template_id=template_id,
        sandbox_id=sandbox_id,
        vcpu_num=2,
        vcpu_max=2,
        ram_mb=2048,
    )
    wait_agent_health(sandbox)
    return sandbox


def start_volume_fixture(
    work_dir: Path,
    logical_work_dir: Path,
    fixture_root: Path,
    sandbox_id: str,
) -> dict[str, Any]:
    volume_source = fixture_root / "volume-source"
    volume_source.mkdir(parents=True, exist_ok=False)
    marker = volume_source / "host-data.txt"
    marker.write_text("preserve-host-volume-data\n", encoding="utf-8")

    # Mountinfo reports the resolved host path, while Conch matches stale
    # virtiofsd processes against the runtime path written to its config.
    volume_runtime = work_dir / "work" / "sandboxes"
    process_runtime = logical_work_dir / "work" / "sandboxes"
    sandbox_runtime = volume_runtime / sandbox_id
    volume_dir = sandbox_runtime / "volume"
    mount_target = volume_dir / "0"
    mount_target.mkdir(parents=True, exist_ok=False)
    (volume_dir / "config.json").write_text(
        '{"version":1,"mounts":[{"index":0,"path":"/crash-release"}]}\n',
        encoding="utf-8",
    )
    subprocess.run(["mount", "--bind", str(volume_source), str(mount_target)], check=True)

    fake_log = fixture_root / "virtiofsd-fixture.log"
    output = fake_log.open("ab", buffering=0)
    code = "import time; time.sleep(3600)"
    process = subprocess.Popen(
        ["virtiofsd", "-c", code, str(process_runtime)],
        executable=sys.executable,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    output.close()

    def fixture_ready() -> bool:
        info = process_info(process.pid)
        return (
            info is not None
            and info["state"] != "Z"
            and b"virtiofsd" in info["cmdline"]
            and os.fsencode(str(process_runtime)) in info["cmdline"]
            and str(mount_target) in mount_targets()
        )

    wait_for("stale volume fixture", fixture_ready, timeout=10, interval=0.1)
    info = process_info(process.pid)
    if info is None:
        raise RuntimeError("volume fixture process exited unexpectedly")
    return {
        "process": process_identity(info, "virtiofsd-fixture"),
        "process_runtime_dir": str(process_runtime),
        "runtime_dir": str(volume_runtime),
        "sandbox_runtime_dir": str(sandbox_runtime),
        "mount_target": str(mount_target),
        "source_dir": str(volume_source),
        "source_marker": str(marker),
    }


def prepare(args: argparse.Namespace) -> None:
    logical_work_dir = args.work_dir
    work_dir = require_absolute_safe_path(logical_work_dir, "work directory")
    manifest_path = require_absolute_safe_path(args.manifest, "manifest")
    fixture_root = require_absolute_safe_path(args.fixture_root, "fixture root")
    config_path = work_dir / "config.yaml"
    if not work_dir.is_dir() or not config_path.is_file():
        raise RuntimeError(f"prepared conchd runtime is missing below {work_dir}")
    if manifest_path.exists() or fixture_root.exists():
        raise RuntimeError("crash-release fixture paths must not already exist")
    fixture_root.mkdir(parents=True)

    if http_status(args.unix_socket, "/health") != 204:
        raise RuntimeError("conchd health endpoint is not ready")
    sandbox = create_sandbox(args.template_id, args.sandbox_id)
    if http_status(args.unix_socket, sandbox_path(args.sandbox_id)) != 200:
        raise RuntimeError("created sandbox is absent from the control plane")

    wait_for(
        "a replenished warm network slot",
        lambda: len(network_namespaces()) >= 2,
        timeout=120,
    )
    network_name, cni_state_dir = cni_network_state(work_dir)
    wait_for(
        "two CNI allocations for the sandbox and warm slot",
        lambda: len(cni_allocations(cni_state_dir, network_name)) >= 2,
        timeout=120,
    )
    wait_for(
        "sandbox VMM process",
        lambda: bool(find_vmm_processes(args.sandbox_id)),
        timeout=30,
    )

    boot_dir = work_dir / "work" / "snapshot" / BOOT_NAMESPACE / args.sandbox_id
    wait_for("sandbox boot layout", boot_dir.exists, timeout=30)
    wait_for("sandbox VMM sockets", lambda: bool(socket_paths(work_dir)), timeout=30)
    volume = start_volume_fixture(
        work_dir,
        logical_work_dir,
        fixture_root,
        args.sandbox_id,
    )

    service_pid = read_pid_file(work_dir / "work" / "conchd.pid")
    service_info = process_info(service_pid)
    if service_info is None:
        raise RuntimeError("conchd service process is missing")
    launcher_pid = read_pid_file(work_dir / "conchd.pid")
    launcher_info = process_info(launcher_pid)
    if launcher_info is None:
        raise RuntimeError("conchd launcher process is missing")

    sentinel = work_dir / "crash-release-runtime-sentinel"
    sentinel.write_text("runtime-must-be-reused\n", encoding="utf-8")
    boot_prefix = str(boot_dir) + os.sep
    boot_mounts = sorted(
        target
        for target in mount_targets()
        if target == str(boot_dir) or target.startswith(boot_prefix)
    )
    if not boot_mounts:
        raise RuntimeError("sandbox boot layout has no mounted paths to test")
    manifest = {
        "schema": 1,
        "unix_socket": args.unix_socket,
        "boot_dir": str(boot_dir),
        "boot_mounts": boot_mounts,
        "cni_allocations_before_crash": cni_allocations(
            cni_state_dir,
            network_name,
        ),
        "cni_network_name": network_name,
        "cni_network_state_dir": str(cni_state_dir),
        "config_path": str(config_path),
        "config_sha256": sha256_file(config_path),
        "fixture_root": str(fixture_root),
        "launcher_process": process_identity(launcher_info, "conchd-launcher"),
        "network_namespaces": network_namespaces(),
        "sandbox_id": args.sandbox_id,
        "sentinel": str(sentinel),
        "service_process": process_identity(service_info, "conchd"),
        "socket_paths": socket_paths(work_dir),
        "template_id": args.template_id,
        "vmm_processes": find_vmm_processes(args.sandbox_id),
        "volume": volume,
        "work_dir": str(work_dir),
    }
    if not manifest["vmm_processes"]:
        raise RuntimeError("no sandbox VMM process was captured")
    if not manifest["network_namespaces"]:
        raise RuntimeError("no network namespace was captured")
    write_json(manifest_path, manifest)
    log(f"prepared crash-release manifest at {manifest_path}")


def crash(args: argparse.Namespace) -> None:
    manifest = read_json(args.manifest)
    service = manifest["service_process"]
    if not same_process(service, zombies_are_alive=True):
        raise RuntimeError("recorded conchd service process is no longer running")

    log(f"sending SIGKILL to conchd pid {service['pid']}")
    os.kill(int(service["pid"]), signal.SIGKILL)
    wait_for(
        "conchd service process to be reaped",
        lambda: not same_process(service, zombies_are_alive=True),
        timeout=30,
        interval=0.1,
    )
    launcher = manifest["launcher_process"]
    wait_for(
        "conchd launcher process to exit",
        lambda: not same_process(launcher, zombies_are_alive=True),
        timeout=30,
        interval=0.1,
    )
    wait_for(
        "conchd API to become unavailable",
        lambda: http_status(manifest["unix_socket"], "/health") is None,
        timeout=10,
        interval=0.2,
    )

    if not any(same_process(identity) for identity in manifest["vmm_processes"]):
        raise RuntimeError("SIGKILL did not leave a stale VMM process to release")
    if not same_process(manifest["volume"]["process"]):
        raise RuntimeError("stale volume fixture process exited before restart")
    if not Path(manifest["boot_dir"]).exists():
        raise RuntimeError("sandbox boot layout disappeared before restart")
    if not set(manifest["boot_mounts"]).issubset(mount_targets()):
        raise RuntimeError("sandbox boot mounts disappeared before restart")
    if not any(same_path_identity(item) for item in manifest["network_namespaces"]):
        raise RuntimeError("network namespaces disappeared before restart")
    if manifest["volume"]["mount_target"] not in mount_targets():
        raise RuntimeError("stale volume bind mount disappeared before restart")

    manifest["crashed"] = True
    manifest["crashed_at_unix"] = int(time.time())
    write_json(args.manifest, manifest)
    log("confirmed resources remain after the ungraceful daemon exit")


def assert_sandbox_absent(unix_socket: str, sandbox_id: str) -> bool:
    return http_status(unix_socket, sandbox_path(sandbox_id)) == 404


def verify(args: argparse.Namespace) -> None:
    manifest = read_json(args.manifest)
    if manifest.get("crashed") is not True:
        raise RuntimeError("manifest does not record a completed SIGKILL phase")
    work_dir = Path(manifest["work_dir"])
    config_path = Path(manifest["config_path"])
    if sha256_file(config_path) != manifest["config_sha256"]:
        raise RuntimeError("conchd config changed instead of being reused")
    if Path(manifest["sentinel"]).read_text(encoding="utf-8") != "runtime-must-be-reused\n":
        raise RuntimeError("runtime sentinel is missing or changed")

    unix_socket = manifest["unix_socket"]
    sandbox_id = manifest["sandbox_id"]
    wait_for(
        "restarted conchd health",
        lambda: http_status(unix_socket, "/health") == 204,
        timeout=30,
    )
    wait_for(
        "stale sandbox state record removal",
        lambda: assert_sandbox_absent(unix_socket, sandbox_id),
        timeout=30,
    )
    wait_for(
        "stale VMM process termination",
        lambda: not any(same_process(item) for item in manifest["vmm_processes"]),
        timeout=30,
    )
    wait_for(
        "stale volume process termination",
        lambda: not same_process(manifest["volume"]["process"]),
        timeout=30,
    )
    wait_for(
        "stale VMM socket removal",
        lambda: not any(Path(path).exists() for path in manifest["socket_paths"]),
        timeout=30,
    )
    wait_for(
        "stale boot layout removal",
        lambda: not Path(manifest["boot_dir"]).exists(),
        timeout=30,
    )
    wait_for(
        "stale boot mount removal",
        lambda: not set(manifest["boot_mounts"]) & mount_targets(),
        timeout=30,
    )
    wait_for(
        "stale volume mount removal",
        lambda: manifest["volume"]["mount_target"] not in mount_targets(),
        timeout=30,
    )
    wait_for(
        "stale volume runtime removal",
        lambda: not Path(manifest["volume"]["sandbox_runtime_dir"]).exists(),
        timeout=30,
    )
    if Path(manifest["volume"]["source_marker"]).read_text(encoding="utf-8") != "preserve-host-volume-data\n":
        raise RuntimeError("volume cleanup removed or changed host source data")

    # Namespace inode numbers can be reused after teardown. Verify recovery by
    # requiring the restarted warm pool and its CNI state to converge instead.
    network_name = manifest["cni_network_name"]
    cni_state_dir = require_absolute_safe_path(
        Path(manifest["cni_network_state_dir"]),
        "CNI network state directory",
    )
    wait_for(
        "warm network pool convergence",
        lambda: len(network_namespaces()) == 1,
        timeout=120,
    )
    wait_for(
        "warm CNI allocation convergence",
        lambda: len(cni_allocations(cni_state_dir, network_name)) == 1,
        timeout=120,
    )
    replacement = create_sandbox(manifest["template_id"], sandbox_id)
    replacement.delete()
    wait_for(
        "replacement sandbox deletion",
        lambda: assert_sandbox_absent(unix_socket, sandbox_id),
        timeout=30,
    )
    wait_for(
        "replacement VMM termination",
        lambda: not find_vmm_processes(sandbox_id),
        timeout=30,
    )
    wait_for(
        "replacement boot layout removal",
        lambda: not Path(manifest["boot_dir"]).exists(),
        timeout=30,
    )
    wait_for(
        "warm pool stabilization after replacement deletion",
        lambda: len(network_namespaces()) == 1
        and len(cni_allocations(cni_state_dir, network_name)) == 1,
        timeout=120,
    )

    manifest["verified"] = True
    manifest["verified_at_unix"] = int(time.time())
    manifest["network_namespaces_after"] = network_namespaces()
    manifest["cni_allocations_after"] = cni_allocations(cni_state_dir, network_name)
    write_json(args.manifest, manifest)
    log("crash release and same-ID reuse verified")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--work-dir", type=Path, required=True)
    prepare_parser.add_argument("--manifest", type=Path, required=True)
    prepare_parser.add_argument("--fixture-root", type=Path, required=True)
    prepare_parser.add_argument("--unix-socket", required=True)
    prepare_parser.add_argument("--template-id", required=True)
    prepare_parser.add_argument("--sandbox-id", required=True)
    prepare_parser.set_defaults(handler=prepare)

    for name, handler in (("crash", crash), ("verify", verify)):
        command_parser = subparsers.add_parser(name)
        command_parser.add_argument("--manifest", type=Path, required=True)
        command_parser.set_defaults(handler=handler)

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
