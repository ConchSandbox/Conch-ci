#!/usr/bin/env python3
"""Safely extract regular-file/directory-only tar archives."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath


class ArchiveError(ValueError):
    pass


def _member_path(member: tarfile.TarInfo) -> PurePosixPath | None:
    name = member.name
    if not name or name.startswith("/"):
        raise ArchiveError(f"unsafe archive member name: {name!r}")
    normalized = name[:-1] if member.isdir() and name.endswith("/") else name
    if "\\" in normalized:
        raise ArchiveError(f"archive member contains a backslash: {name!r}")
    if normalized == "." and member.isdir():
        return None
    normalized = normalized.removeprefix("./")
    if any(segment in {"", ".", ".."} for segment in normalized.split("/")):
        raise ArchiveError(f"unsafe archive member path: {name!r}")
    path = PurePosixPath(normalized)
    if member.mode & 0o6000:
        raise ArchiveError(f"setuid/setgid archive member is forbidden: {name}")
    if not (member.isfile() or member.isdir()):
        raise ArchiveError(f"non-regular archive member is forbidden: {name}")
    return path


def extract_selected(archive: Path, destination: Path, selected: dict[str, str]) -> None:
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members: dict[str, tarfile.TarInfo] = {}
            for member in bundle.getmembers():
                relative = _member_path(member)
                if relative is None:
                    continue
                normalized = relative.as_posix()
                if normalized in members:
                    raise ArchiveError(f"duplicate archive member: {normalized}")
                members[normalized] = member
            for archive_path, output_name in selected.items():
                member = members.get(archive_path)
                if member is None or not member.isfile():
                    raise ArchiveError(f"required archive member missing: {archive_path}")
                source = bundle.extractfile(member)
                if source is None:
                    raise ArchiveError(f"cannot read archive member: {archive_path}")
                output = destination / output_name
                output.parent.mkdir(parents=True, exist_ok=True)
                with source, output.open("wb") as stream:
                    shutil.copyfileobj(source, stream)
                os.chmod(output, 0o755)
    except tarfile.TarError as exc:
        raise ArchiveError(f"invalid archive {archive}: {exc}") from exc


def extract_source_archive(archive: Path, destination: Path) -> Path:
    top_levels: set[str] = set()
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                relative = _member_path(member)
                if relative is None:
                    continue
                top_levels.add(relative.parts[0])
                target = destination.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    os.chmod(target, 0o755)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise ArchiveError(f"cannot read archive member: {member.name}")
                with source, target.open("wb") as stream:
                    shutil.copyfileobj(source, stream)
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
    except tarfile.TarError as exc:
        raise ArchiveError(f"invalid archive {archive}: {exc}") from exc
    if len(top_levels) != 1:
        raise ArchiveError(f"source archive must contain exactly one top-level directory: {top_levels}")
    source_root = destination / next(iter(top_levels))
    if not source_root.is_dir():
        raise ArchiveError("source archive top-level entry is not a directory")
    return source_root


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--expect-top", required=True)
    args = parser.parse_args()
    destination = args.destination.resolve()
    cleanup_destination = False
    try:
        if destination in {Path("/"), Path.home()}:
            raise ArchiveError(f"unsafe extraction destination: {destination}")
        cleanup_destination = True
        destination.mkdir(parents=True, exist_ok=True)
        source_root = extract_source_archive(args.archive, destination)
        if source_root.name != args.expect_top:
            raise ArchiveError(
                f"archive top-level directory is {source_root.name!r}, "
                f"expected {args.expect_top!r}"
            )
        print(source_root)
        return 0
    except (OSError, ArchiveError) as exc:
        if cleanup_destination and destination.exists():
            shutil.rmtree(destination)
        print(f"safe extraction failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
