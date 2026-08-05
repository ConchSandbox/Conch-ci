#!/usr/bin/env python3
"""Controllable CNI wrapper used by Conch network integration tests."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


CONTROL_DIR_FIELD = "conchCIControlDir"
REAL_BRIDGE_ENV = "CONCH_CNI_REAL_BRIDGE"
EVENT_LOG_ENV = "CONCH_CNI_EVENT_LOG"
VALID_OUTCOMES = {"pass", "fail", "block"}


class ControlError(RuntimeError):
    pass


def load_json_object(path: Path, *, missing_ok: bool = False) -> dict[str, Any]:
    if missing_ok and not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlError(f"JSON value in {path} must be an object")
    return value


def write_json_object(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def append_event(
    control_dir: Path,
    *,
    command: str,
    phase: str,
    outcome: str,
    attempt: int,
    exit_code: int | None = None,
) -> None:
    event: dict[str, Any] = {
        "command": command,
        "phase": phase,
        "outcome": outcome,
        "attempt": attempt,
        "time_unix_nano": time.time_ns(),
        "container_id": os.environ.get("CNI_CONTAINERID", ""),
        "netns": os.environ.get("CNI_NETNS", ""),
    }
    if exit_code is not None:
        event["exit_code"] = exit_code
    payload = (json.dumps(event, sort_keys=True) + "\n").encode()
    event_paths = [control_dir / "events.jsonl"]
    archive = os.environ.get(EVENT_LOG_ENV, "")
    if archive:
        archive_path = Path(archive)
        if archive_path.is_absolute() and archive_path not in event_paths:
            event_paths.append(archive_path)
    for event_path in event_paths:
        descriptor = os.open(
            event_path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)


def select_outcome(control_dir: Path, command: str) -> tuple[int, str]:
    lock_path = control_dir / "lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        plan = load_json_object(control_dir / "plan.json")
        state = load_json_object(control_dir / "state.json", missing_ok=True)
        counters = state.setdefault("counters", {})
        if not isinstance(counters, dict):
            raise ControlError("state counters must be an object")
        attempt = int(counters.get(command, 0)) + 1
        counters[command] = attempt
        write_json_object(control_dir / "state.json", state)

        outcomes = plan.get("outcomes", {})
        defaults = plan.get("defaults", {})
        if not isinstance(outcomes, dict) or not isinstance(defaults, dict):
            raise ControlError("plan outcomes and defaults must be objects")
        sequence = outcomes.get(command, [])
        if not isinstance(sequence, list) or not all(isinstance(item, str) for item in sequence):
            raise ControlError(f"plan outcomes for {command} must be a string array")
        default = defaults.get(command, "pass")
        if not isinstance(default, str):
            raise ControlError(f"plan default for {command} must be a string")
        outcome = sequence[attempt - 1] if attempt <= len(sequence) else default
        if outcome not in VALID_OUTCOMES:
            raise ControlError(f"unsupported outcome for {command} attempt {attempt}: {outcome}")
        append_event(
            control_dir,
            command=command,
            phase="start",
            outcome=outcome,
            attempt=attempt,
        )
        return attempt, outcome


def finish_event(
    control_dir: Path,
    *,
    command: str,
    outcome: str,
    attempt: int,
    exit_code: int,
) -> None:
    lock_path = control_dir / "lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        append_event(
            control_dir,
            command=command,
            phase="finish",
            outcome=outcome,
            attempt=attempt,
            exit_code=exit_code,
        )


def cni_error(message: str, code: int = 11) -> int:
    json.dump({"code": code, "msg": message}, sys.stdout)
    sys.stdout.write("\n")
    return 1


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

    raw_config = sys.stdin.buffer.read()
    try:
        config = json.loads(raw_config)
    except json.JSONDecodeError as exc:
        return cni_error(f"invalid CNI configuration: {exc}", code=7)
    if not isinstance(config, dict):
        return cni_error("CNI configuration must be an object", code=7)

    raw_control_dir = config.get(CONTROL_DIR_FIELD, "")
    if not isinstance(raw_control_dir, str) or not raw_control_dir:
        return cni_error(f"missing CNI configuration field {CONTROL_DIR_FIELD}", code=7)
    control_dir = Path(raw_control_dir)
    if not control_dir.is_absolute() or not control_dir.is_dir():
        return cni_error(f"invalid CNI control directory: {control_dir}", code=7)

    try:
        attempt, outcome = select_outcome(control_dir, command)
    except ControlError as exc:
        return cni_error(str(exc), code=7)

    if outcome == "block":
        gate = control_dir / f"release-{command}-{attempt}"
        while not gate.exists():
            time.sleep(0.02)
    if outcome == "fail":
        finish_event(
            control_dir,
            command=command,
            outcome=outcome,
            attempt=attempt,
            exit_code=1,
        )
        return cni_error(f"injected {command} failure at attempt {attempt}")

    real_bridge = Path(os.environ.get(REAL_BRIDGE_ENV, ""))
    if not real_bridge.is_absolute() or not os.access(real_bridge, os.X_OK):
        finish_event(
            control_dir,
            command=command,
            outcome=outcome,
            attempt=attempt,
            exit_code=1,
        )
        return cni_error(f"{REAL_BRIDGE_ENV} does not name an executable bridge plugin", code=7)

    delegated_config = dict(config)
    delegated_config.pop(CONTROL_DIR_FIELD, None)
    delegated_config["type"] = "bridge"
    completed = subprocess.run(
        [str(real_bridge)],
        input=json.dumps(delegated_config, separators=(",", ":")).encode(),
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    finish_event(
        control_dir,
        command=command,
        outcome=outcome,
        attempt=attempt,
        exit_code=completed.returncode,
    )
    sys.stdout.buffer.write(completed.stdout)
    sys.stderr.buffer.write(completed.stderr)
    return completed.returncode


def main() -> None:
    try:
        raise SystemExit(run())
    except ControlError as exc:
        raise SystemExit(cni_error(str(exc), code=7)) from exc


if __name__ == "__main__":
    main()
