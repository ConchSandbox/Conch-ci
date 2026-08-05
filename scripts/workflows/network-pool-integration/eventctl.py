#!/usr/bin/env python3
"""Wait for and assert events emitted by the controllable CNI wrapper."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


class EventError(RuntimeError):
    pass


def load_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EventError(f"cannot read CNI event log {path}: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EventError(f"invalid JSON in {path}:{line_number}: {exc}") from exc
        if not isinstance(event, dict):
            raise EventError(f"event in {path}:{line_number} must be an object")
        events.append(event)
    return events


def matching_events(
    events: list[dict[str, Any]],
    *,
    command: str,
    phase: str,
    outcome: str | None,
) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if event.get("command") == command
        and event.get("phase") == phase
        and (outcome is None or event.get("outcome") == outcome)
    ]


def command_wait(args: argparse.Namespace) -> None:
    deadline = time.monotonic() + args.timeout
    while True:
        matched = matching_events(
            load_events(args.path),
            command=args.command,
            phase=args.phase,
            outcome=args.outcome,
        )
        if len(matched) >= args.count:
            print(f"observed {len(matched)} {args.command} {args.phase} event(s)")
            return
        if time.monotonic() >= deadline:
            raise EventError(
                f"timed out waiting for {args.count} {args.command} {args.phase} "
                f"event(s), observed {len(matched)}"
            )
        time.sleep(0.02)


def command_count(args: argparse.Namespace) -> None:
    matched = matching_events(
        load_events(args.path),
        command=args.command,
        phase=args.phase,
        outcome=args.outcome,
    )
    if len(matched) != args.count:
        raise EventError(
            f"{args.command} {args.phase} event count is {len(matched)}, want {args.count}"
        )


def command_sequence(args: argparse.Namespace) -> None:
    expected = args.outcomes.split(",") if args.outcomes else []
    matched = matching_events(
        load_events(args.path),
        command=args.command,
        phase=args.phase,
        outcome=None,
    )
    attempts = [event.get("attempt") for event in matched]
    expected_attempts = list(range(1, len(expected) + 1))
    if attempts != expected_attempts:
        raise EventError(f"{args.command} attempts are {attempts}, want {expected_attempts}")
    actual = [event.get("outcome") for event in matched]
    if actual != expected:
        raise EventError(f"{args.command} outcome sequence is {actual}, want {expected}")


def event_by_attempt(events: list[dict[str, Any]], attempt: int) -> dict[str, Any]:
    for event in events:
        if event.get("attempt") == attempt:
            return event
    raise EventError(f"missing CNI ADD start event for attempt {attempt}")


def retry_gap(events: list[dict[str, Any]], earlier: int, later: int) -> float:
    earlier_event = event_by_attempt(events, earlier)
    later_event = event_by_attempt(events, later)
    try:
        return (int(later_event["time_unix_nano"]) - int(earlier_event["time_unix_nano"])) / 1e9
    except (KeyError, TypeError, ValueError) as exc:
        raise EventError("CNI retry event has an invalid time_unix_nano") from exc


def command_retry(args: argparse.Namespace) -> None:
    starts = matching_events(
        load_events(args.path),
        command="ADD",
        phase="start",
        outcome=None,
    )
    expected = ["fail", "fail", "fail", "fail", "pass", "fail", "fail", "pass"]
    attempts = [event.get("attempt") for event in starts]
    outcomes = [event.get("outcome") for event in starts]
    if attempts != list(range(1, 9)) or outcomes != expected:
        raise EventError(
            f"retry ADD events are attempts={attempts} outcomes={outcomes}, "
            f"want attempts 1..8 and outcomes {expected}"
        )

    first = retry_gap(starts, 3, 4)
    second = retry_gap(starts, 4, 5)
    reset_first = retry_gap(starts, 6, 7)
    reset_second = retry_gap(starts, 7, 8)
    if first < 0.75:
        raise EventError(f"first retry gap is {first:.3f}s, want at least 0.75s")
    if second < 1.75 or second < first + 0.5:
        raise EventError(f"retry gaps did not grow: first={first:.3f}s second={second:.3f}s")
    if reset_first < 0.75 or reset_first >= 3.0:
        raise EventError(f"retry gap did not reset after success: {reset_first:.3f}s")
    if reset_second < 1.75 or reset_second < reset_first + 0.5:
        raise EventError(
            f"retry gaps after reset did not grow: first={reset_first:.3f}s "
            f"second={reset_second:.3f}s"
        )
    print(
        "retry gaps: "
        f"first={first:.3f}s second={second:.3f}s "
        f"reset_first={reset_first:.3f}s reset_second={reset_second:.3f}s"
    )


def add_event_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path", type=Path)
    parser.add_argument("--command", required=True, choices=("ADD", "CHECK", "DEL"))
    parser.add_argument("--phase", required=True, choices=("start", "finish"))
    parser.add_argument("--outcome", choices=("pass", "fail", "block"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)

    wait_parser = commands.add_parser("wait")
    add_event_filters(wait_parser)
    wait_parser.add_argument("--count", required=True, type=int)
    wait_parser.add_argument("--timeout", type=float, default=60.0)
    wait_parser.set_defaults(run=command_wait)

    count_parser = commands.add_parser("count")
    add_event_filters(count_parser)
    count_parser.add_argument("--count", required=True, type=int)
    count_parser.set_defaults(run=command_count)

    sequence_parser = commands.add_parser("sequence")
    sequence_parser.add_argument("path", type=Path)
    sequence_parser.add_argument("--command", required=True, choices=("ADD", "CHECK", "DEL"))
    sequence_parser.add_argument("--phase", required=True, choices=("start", "finish"))
    sequence_parser.add_argument("--outcomes", required=True)
    sequence_parser.set_defaults(run=command_sequence)

    retry_parser = commands.add_parser("retry")
    retry_parser.add_argument("path", type=Path)
    retry_parser.set_defaults(run=command_retry)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.run(args)
    except EventError as exc:
        print(f"event assertion failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
