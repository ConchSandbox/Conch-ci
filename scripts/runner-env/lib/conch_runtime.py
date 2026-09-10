#!/usr/bin/env python3
"""Locate a short Conch work directory and keep its job ownership explicit."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat


ROOT_RE = re.compile(r"/tmp/conch-ci-run-[0-9a-f]{32}")


def runtime_root(workdir: Path) -> Path:
    if (
        not workdir.is_absolute()
        or workdir != Path(os.path.normpath(str(workdir)))
        or workdir.resolve() == Path("/")
        or workdir.is_symlink()
    ):
        raise RuntimeError(f"unsafe Conch job directory: {workdir}")
    digest = hashlib.sha256(os.fsencode(workdir.resolve())).hexdigest()[:32]
    return Path("/tmp") / f"conch-ci-run-{digest}"


def runtime_owner(root: Path) -> Path:
    if not ROOT_RE.fullmatch(str(root)):
        raise RuntimeError(f"invalid short Conch runtime path: {root}")
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError(f"Conch runtime must be a private real directory: {root}")
    marker = root / ".owner"
    marker_info = marker.lstat()
    if not stat.S_ISREG(marker_info.st_mode) or marker_info.st_uid != info.st_uid:
        raise RuntimeError(f"unsafe Conch runtime ownership record: {marker}")
    contents = marker.read_text(encoding="utf-8")
    if contents.count("\n") != 1 or not contents.endswith("\n"):
        raise RuntimeError(f"invalid Conch runtime ownership record: {marker}")
    owner = Path(contents[:-1])
    if runtime_root(owner) != root or not owner.is_dir() or owner.stat().st_uid != info.st_uid:
        raise RuntimeError(f"Conch runtime ownership mismatch: {root}")
    return owner


def runtime_work_path(workdir: Path) -> Path:
    root = runtime_root(workdir)
    if root.exists() or root.is_symlink():
        if runtime_owner(root).resolve() != workdir.resolve():
            raise RuntimeError(f"Conch runtime belongs to another job: {root}")
        target = root / "work"
        if not target.is_dir() or target.is_symlink():
            raise RuntimeError(f"unsafe Conch runtime work directory: {target}")
        return target
    # Retain cleanup/restart support for runtimes created before this change.
    legacy = workdir / "work"
    if legacy.is_symlink():
        raise RuntimeError(f"unsafe legacy Conch work directory: {legacy}")
    return legacy if legacy.is_dir() else root / "work"


def create_runtime(workdir: Path) -> Path:
    if not workdir.is_dir() or workdir.stat().st_uid != os.geteuid():
        raise RuntimeError(f"Conch job directory must belong to the runner: {workdir}")
    root = runtime_root(workdir)
    root.mkdir(mode=0o700)  # Refuse to take over any existing directory or symlink.
    try:
        marker = root / ".owner"
        marker.write_text(str(workdir) + "\n", encoding="utf-8")
        marker.chmod(0o600)
        (root / "work").mkdir(mode=0o700)
    except BaseException:
        shutil.rmtree(root)
        raise
    return root / "work"


def remove_runtime(workdir: Path) -> None:
    """Remove a validated runtime after its processes and mounts are cleaned up."""
    root = runtime_root(workdir)
    if not root.exists() and not root.is_symlink():
        return
    runtime_work_path(workdir)
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 5:
            target = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
            if target == str(root) or target.startswith(str(root) + "/"):
                raise RuntimeError(f"refusing to delete mounted Conch runtime: {target}")
    shutil.rmtree(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "path", "remove"))
    parser.add_argument("workdir", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "create":
            print(create_runtime(args.workdir))
        elif args.command == "path":
            print(runtime_work_path(args.workdir))
        else:
            remove_runtime(args.workdir)
    except (OSError, RuntimeError) as exc:
        parser.exit(2, f"Conch runtime path error: {exc}\n")
