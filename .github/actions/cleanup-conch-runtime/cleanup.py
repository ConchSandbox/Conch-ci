#!/usr/bin/env python3
"""Release Conch processes, mounts, and network namespace handles."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path


MOUNT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
NETWORK_NAMESPACE_DIR = Path("/run/conch/netns")
NETWORK_NAMESPACE_RE = re.compile(r"^(?:ns|slot)-[0-9]+$")


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
    try:
        return int(fields[19])
    except ValueError:
        return None


def runtime_processes(workdir: Path) -> dict[int, int]:
    """Find only Conch VMM and virtiofs processes tied to this job runtime."""
    workdir_bytes = os.fsencode(workdir)
    workdir_prefix = workdir_bytes + os.fsencode(os.sep)
    processes: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        arguments = cmdline.split(b"\0")
        if not any(
            argument == workdir_bytes or workdir_prefix in argument
            for argument in arguments
        ):
            continue
        is_vmm = (
            b"conch.sandbox_id=" in cmdline
            and (b"cloud-hypervisor" in cmdline or b"stratovirt" in cmdline)
        )
        if not is_vmm and b"virtiofsd" not in cmdline:
            continue
        start_time = process_start_time(pid)
        if start_time is not None:
            processes[pid] = start_time
    return processes


def terminate_processes(processes: dict[int, int]) -> None:
    """Stop captured processes, rechecking start time before every signal."""
    for pid, start_time in processes.items():
        if process_start_time(pid) != start_time:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not any(
            process_start_time(pid) == start_time
            for pid, start_time in processes.items()
        ):
            break
        time.sleep(0.1)

    for pid, start_time in processes.items():
        if process_start_time(pid) != start_time:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def decode_mount_path(value: str) -> str:
    """Decode the octal escapes used for mount points in Linux mountinfo."""
    return MOUNT_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), value)


def workdir_mount_targets(workdir: Path) -> set[str]:
    """Return mounts at or below workdir without relying on path traversal."""
    workdir_text = str(workdir)
    mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    targets: set[str] = set()
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        target = decode_mount_path(fields[4])
        if target == workdir_text or target.startswith(workdir_text + os.sep):
            targets.add(target)
    return targets


def unmount_workdir(workdir: Path) -> None:
    """Detach child mounts before parents so nested snapshot layouts unwind."""
    targets = workdir_mount_targets(workdir)
    for target in sorted(
        targets,
        key=lambda value: (value.count(os.sep), len(value)),
        reverse=True,
    ):
        subprocess.run(["umount", "-l", "--", target], check=False)


def remove_network_namespace_handles() -> None:
    """Remove Conch's bind-mounted namespace handles from its private directory."""
    try:
        paths = [
            path
            for path in NETWORK_NAMESPACE_DIR.iterdir()
            if NETWORK_NAMESPACE_RE.fullmatch(path.name)
        ]
    except (FileNotFoundError, PermissionError):
        paths = []
    for path in sorted(paths, key=lambda value: value.name):
        subprocess.run(["umount", "-l", "--", str(path)], check=False)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def cleanup(workdir: Path) -> None:
    """Release runtime resources and refuse directory deletion if mounts remain."""
    terminate_processes(runtime_processes(workdir))
    unmount_workdir(workdir)
    remove_network_namespace_handles()

    remaining_mounts = workdir_mount_targets(workdir)
    if remaining_mounts:
        raise RuntimeError(
            "refusing to delete Conch work directory with mounted paths: "
            + ", ".join(sorted(remaining_mounts))
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    workdir = args.work_dir
    if not workdir.is_absolute() or workdir == Path("/"):
        raise SystemExit(f"work directory must be an absolute non-root path: {workdir}")
    try:
        cleanup(workdir)
    except (OSError, RuntimeError) as exc:
        print(f"Conch runtime cleanup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
