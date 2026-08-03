#!/usr/bin/env python3
"""Read and strictly validate the runner environment lock."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


COMPONENTS = ("cloud_hypervisor", "buildkit", "erofs_utils", "cni_plugins")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
EXACT_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9.-]*)?")
FLOATING_RE = re.compile(r"^[<>=~^!*]|latest|snapshot|nightly", re.IGNORECASE)


class ValidationError(ValueError):
    pass


def parse_mapping(path: Path) -> dict[str, Any]:
    """Parse the mapping-and-scalar-only YAML subset used by the lock."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-2, root)]
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw:
            raise ValidationError(f"{path}:{line_number}: tabs are not allowed")
        indent = len(raw) - len(raw.lstrip(" "))
        match = re.fullmatch(r"([A-Za-z0-9_-]+):(?: +(.*))?", raw[indent:])
        if indent % 2 or not match:
            raise ValidationError(f"{path}:{line_number}: invalid mapping entry")
        key, scalar = match.groups()
        while indent <= stack[-1][0]:
            stack.pop()
        if indent != stack[-1][0] + 2:
            raise ValidationError(f"{path}:{line_number}: invalid indentation")
        parent = stack[-1][1]
        if key in parent:
            raise ValidationError(f"{path}:{line_number}: duplicate key {key!r}")
        if not scalar:
            parent[key] = {}
            stack.append((indent, parent[key]))
        elif scalar.startswith(("[", "{", "&", "*", "!", "|", ">")) or " #" in scalar:
            raise ValidationError(f"{path}:{line_number}: unsupported YAML construct")
        elif scalar.startswith("\""):
            try:
                parent[key] = json.loads(scalar)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"{path}:{line_number}: invalid quoted string") from exc
            if not isinstance(parent[key], str):
                raise ValidationError(f"{path}:{line_number}: expected string")
        elif scalar.startswith("'"):
            if len(scalar) < 2 or not scalar.endswith("'"):
                raise ValidationError(f"{path}:{line_number}: invalid quoted string")
            parent[key] = scalar[1:-1].replace("''", "'")
        elif re.fullmatch(r"0|[1-9][0-9]*", scalar):
            parent[key] = int(scalar)
        elif scalar.lower() in {"null", "~", "true", "false"}:
            raise ValidationError(f"{path}:{line_number}: null and boolean values are forbidden")
        else:
            parent[key] = scalar
    return root


def exact_object(value: Any, location: str, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{location}: expected object")
    missing = set(fields) - set(value)
    unknown = set(value) - set(fields)
    if missing:
        raise ValidationError(f"{location}: missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValidationError(f"{location}: unknown fields: {', '.join(sorted(unknown))}")
    return value


def string(value: Any, location: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or (pattern and not pattern.fullmatch(value)):
        raise ValidationError(f"{location}: invalid string value {value!r}")
    return value


def validate_download(value: Any, location: str) -> None:
    item = exact_object(value, location, ("version", "sha256"))
    version = string(item["version"], f"{location}.version")
    string(item["sha256"], f"{location}.sha256", SHA256_RE)
    if FLOATING_RE.search(version):
        raise ValidationError(f"{location}.version: floating version is forbidden")


def validate_version(value: Any, location: str) -> None:
    item = exact_object(value, location, ("version",))
    version = string(item["version"], f"{location}.version")
    if not EXACT_VERSION_RE.fullmatch(version) or FLOATING_RE.search(version):
        raise ValidationError(f"{location}.version: expected exact version")


def validate_lock(value: Any) -> dict[str, Any]:
    lock = exact_object(value, "$", ("schema_version", "managed_components", "job_build_inputs"))
    if lock["schema_version"] != 1 or isinstance(lock["schema_version"], bool):
        raise ValidationError("$.schema_version: expected integer 1")

    components = exact_object(lock["managed_components"], "$.managed_components", COMPONENTS)
    for name in COMPONENTS:
        validate_download(components[name], f"$.managed_components.{name}")

    inputs = exact_object(
        lock["job_build_inputs"],
        "$.job_build_inputs",
        ("go_toolchain", "kernel_commit", "kernel_archive_sha256"),
    )
    validate_version(inputs["go_toolchain"], "$.job_build_inputs.go_toolchain")
    string(inputs["kernel_commit"], "$.job_build_inputs.kernel_commit", COMMIT_RE)
    string(
        inputs["kernel_archive_sha256"],
        "$.job_build_inputs.kernel_archive_sha256",
        SHA256_RE,
    )
    return lock


def load_lock(path: Path) -> dict[str, Any]:
    return validate_lock(parse_mapping(path))


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    get = subparsers.add_parser("get")
    get.add_argument("path")
    args = parser.parse_args()
    try:
        value: Any = load_lock(Path(__file__).resolve().parents[3] / "runner-env.lock.yaml")
        if args.command == "get":
            for segment in args.path.split("."):
                if not isinstance(value, dict) or segment not in value:
                    raise ValidationError(f"unknown lock path: {args.path}")
                value = value[segment]
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise ValidationError(f"lock path is not a scalar: {args.path}")
            print(value)
        return 0
    except (OSError, ValidationError) as exc:
        print(f"lock error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
