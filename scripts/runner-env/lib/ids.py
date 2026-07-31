#!/usr/bin/env python3
"""Stable IDs for runner environments and CI build artifacts."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import struct
from pathlib import Path
from typing import Iterable


ENV_PREFIX = b"conch-runner-env-id-v1\0"
SAFE_PATH_RE = re.compile(rb"[A-Za-z0-9._/-]+")


class IdError(ValueError):
    pass


def _sized(digest: object, value: bytes) -> None:
    digest.update(struct.pack(">Q", len(value)))
    digest.update(value)


def framed_digest(prefix: bytes, fields: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    digest.update(prefix + b"\0")
    for name, value in fields:
        _sized(digest, name.encode("ascii"))
        _sized(digest, value.encode("ascii"))
    return digest.hexdigest()


def kernel_build_id(source_commit: str, config_sha256: str, platform: str) -> str:
    return framed_digest(
        b"conch-kernel-build-id-v1",
        [
            ("source_commit", source_commit),
            ("config_sha256", config_sha256),
            ("platform", platform),
        ],
    )


def rootfs_build_id(
    platform: str,
    conch_commit: str,
    script_sha256: str,
    dockerfile: str,
) -> str:
    return framed_digest(
        b"conch-rootfs-build-id-v1",
        [
            ("platform", platform),
            ("conch_commit", conch_commit),
            ("script_sha256", script_sha256),
            ("dockerfile", dockerfile),
        ],
    )


def environment_digest(entries: Iterable[tuple[bytes, int, bytes]]) -> str:
    digest = hashlib.sha256()
    digest.update(ENV_PREFIX)
    previous: bytes | None = None
    for path, mode, content in sorted(entries):
        segments = path.split(b"/")
        if (
            not SAFE_PATH_RE.fullmatch(path)
            or any(segment in {b"", b".", b".."} for segment in segments)
            or path == previous
        ):
            raise IdError(f"invalid or duplicate environment ID path: {path!r}")
        previous = path
        _sized(digest, path)
        digest.update(struct.pack(">I", mode & 0o7777))
        _sized(digest, content)
    return digest.hexdigest()


def repository_environment_id(repo_root: Path) -> str:
    paths = [repo_root / "runner-env.lock.yaml"]
    for tree in (
        repo_root / "scripts/runner-env",
        repo_root / ".github/actions/ensure-runner-environment",
    ):
        if not tree.is_dir():
            raise IdError(f"missing environment ID input directory: {tree}")
        for current_root, directories, files in os.walk(tree, followlinks=False):
            current = Path(current_root)
            for name in directories:
                if (current / name).is_symlink():
                    raise IdError(f"symbolic link is forbidden: {current / name}")
            paths.extend(current / name for name in files)

    entries = []
    for path in paths:
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise IdError(f"missing environment ID input: {path}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise IdError(f"environment ID input is not a regular file: {path}")
        entries.append(
            (
                path.relative_to(repo_root).as_posix().encode("ascii"),
                stat.S_IMODE(metadata.st_mode),
                path.read_bytes(),
            )
        )
    return environment_digest(entries)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="kind", required=True)
    kernel = subparsers.add_parser("kernel")
    kernel.add_argument("--source-commit", required=True)
    kernel.add_argument("--config-sha256", required=True)
    kernel.add_argument("--platform", required=True, choices=("arm64", "amd64"))
    rootfs = subparsers.add_parser("rootfs")
    rootfs.add_argument("--platform", required=True)
    rootfs.add_argument("--conch-commit", required=True)
    rootfs.add_argument("--script-sha256", required=True)
    rootfs.add_argument("--dockerfile", required=True)
    args = parser.parse_args()

    if args.kind == "kernel":
        print(kernel_build_id(args.source_commit, args.config_sha256, args.platform))
    else:
        print(
            rootfs_build_id(
                args.platform,
                args.conch_commit,
                args.script_sha256,
                args.dockerfile,
            )
        )


if __name__ == "__main__":
    main()
