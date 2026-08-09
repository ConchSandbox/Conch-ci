#!/usr/bin/env python3
"""CNI bridge wrapper that can pause warm-pool refills after initial prefill."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path


CONTROL_DIR_FIELD = "conchCIControlDir"
REAL_BRIDGE_FIELD = "conchCIRealBridge"
RELEASE_FILE = "release-refill"
BLOCKED_FILE = "refill-blocked"


def cni_error(message: str, code: int = 11) -> int:
    json.dump({"code": code, "msg": message}, sys.stdout)
    sys.stdout.write("\n")
    return 1


def next_add_attempt(control_dir: Path) -> int:
    lock_path = control_dir / "lock"
    counter_path = control_dir / "add-count"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            count = int(counter_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            count = 0
        count += 1
        temporary = counter_path.with_name(f".{counter_path.name}.{os.getpid()}.tmp")
        temporary.write_text(f"{count}\n", encoding="utf-8")
        os.replace(temporary, counter_path)
        return count


def run() -> int:
    command = os.environ.get("CNI_COMMAND", "")
    if command == "VERSION":
        json.dump(
            {
                "cniVersion": "1.0.0",
                "supportedVersions": ["0.4.0", "1.0.0"],
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 0
    if command not in {"ADD", "CHECK", "DEL"}:
        return cni_error(f"unsupported CNI command: {command!r}", code=4)

    try:
        config = json.load(sys.stdin.buffer)
    except json.JSONDecodeError as exc:
        return cni_error(f"invalid CNI configuration: {exc}", code=7)
    if not isinstance(config, dict):
        return cni_error("CNI configuration must be an object", code=7)

    control_dir = Path(str(config.get(CONTROL_DIR_FIELD, "")))
    real_bridge = Path(str(config.get(REAL_BRIDGE_FIELD, "")))
    if not control_dir.is_absolute() or not control_dir.is_dir():
        return cni_error(f"invalid CNI control directory: {control_dir}", code=7)
    if not real_bridge.is_absolute() or not os.access(real_bridge, os.X_OK):
        return cni_error(f"invalid real CNI bridge plugin: {real_bridge}", code=7)

    if command == "ADD" and next_add_attempt(control_dir) > 1:
        release_path = control_dir / RELEASE_FILE
        (control_dir / BLOCKED_FILE).touch()
        while not release_path.exists():
            time.sleep(0.02)

    delegated_config = dict(config)
    delegated_config.pop(CONTROL_DIR_FIELD, None)
    delegated_config.pop(REAL_BRIDGE_FIELD, None)
    delegated_config["type"] = "bridge"
    completed = subprocess.run(
        [str(real_bridge)],
        input=json.dumps(delegated_config, separators=(",", ":")).encode(),
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    sys.stdout.buffer.write(completed.stdout)
    sys.stderr.buffer.write(completed.stderr)
    return completed.returncode


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
