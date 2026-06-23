#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

MAX_CONCURRENCY = 1000
DEFAULT_SINGLE_ITERATIONS = 5
POST_CREATE_WORKERS = 64
SANITY_MARKER = "conch-startup-ok"
SANITY_COMMAND = "printf '%s\\n' conch-startup-ok"


def parse_concurrency(value: str, *, max_value: int = MAX_CONCURRENCY) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"concurrency must be a positive integer, got {value!r}") from exc
    if str(value).strip() != str(parsed):
        raise ValueError(f"concurrency must be a positive integer, got {value!r}")
    if parsed < 1:
        raise ValueError(f"concurrency must be at least 1, got {parsed}")
    if parsed > max_value:
        raise ValueError(f"concurrency must be no greater than {max_value}, got {parsed}")
    return parsed


def percentile(values: Iterable[float], percent: float) -> Optional[float]:
    sorted_values = sorted(float(value) for value in values)
    if not sorted_values:
        return None
    if percent <= 0:
        return sorted_values[0]
    if percent >= 100:
        return sorted_values[-1]

    rank = (len(sorted_values) - 1) * (percent / 100.0)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    weight = rank - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def rounded(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 6)


def error_payload(exc: BaseException) -> Dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }


def new_phase(status: str = "skipped") -> Dict[str, Any]:
    return {
        "status": status,
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "error": None,
    }


def update_overall_status(sample: Dict[str, Any]) -> None:
    phases: List[str] = []
    for phase in ("create", "validation", "cleanup"):
        if sample[phase]["status"] == "failed":
            phases.append(phase)
    sample["status"] = "failed" if phases else "success"
    sample["failure_phase"] = "+".join(phases)


def base_sample(scenario: str, sample_index: int, sandbox_id: str, use_snapshot: bool) -> Dict[str, Any]:
    return {
        "scenario": scenario,
        "sample_index": sample_index,
        "sandbox_id": sandbox_id,
        "use_snapshot": use_snapshot,
        "status": "failed",
        "failure_phase": "",
        "started_at": utc_now(),
        "finished_at": None,
        "create": new_phase(),
        "validation": new_phase(),
        "cleanup": new_phase(),
    }


def make_sandbox_id(scenario: str, sample_index: int) -> str:
    scenario_token = "".join(ch if ch.isalnum() else "_" for ch in scenario)
    return f"sandbox_perf_{scenario_token}_{sample_index}_{uuid.uuid4().hex[:10]}"


def import_sandbox_class():
    from conch import Sandbox

    return Sandbox


def create_sandbox(
    sandbox_class,
    *,
    config_path: str,
    image_name: str,
    namespace: str,
    sandbox_id: str,
    use_snapshot: bool,
    vcpu_num: int,
    ram_mb: int,
):
    return sandbox_class.create(
        config_path=config_path,
        image_name=image_name,
        namespace=namespace,
        sandbox_id=sandbox_id,
        use_snapshot=use_snapshot,
        vcpu_num=vcpu_num,
        ram_mb=ram_mb,
    )


def run_create_phase(
    sandbox_class,
    sample: Dict[str, Any],
    *,
    config_path: str,
    image_name: str,
    namespace: str,
    use_snapshot: bool,
    vcpu_num: int,
    ram_mb: int,
):
    sample["create"]["status"] = "running"
    sample["create"]["started_at"] = utc_now()
    start = time.perf_counter()
    sample["_create_start_monotonic"] = start
    try:
        sandbox = create_sandbox(
            sandbox_class,
            config_path=config_path,
            image_name=image_name,
            namespace=namespace,
            sandbox_id=sample["sandbox_id"],
            use_snapshot=use_snapshot,
            vcpu_num=vcpu_num,
            ram_mb=ram_mb,
        )
    except Exception as exc:
        end = time.perf_counter()
        sample["create"]["status"] = "failed"
        sample["create"]["finished_at"] = utc_now()
        sample["create"]["duration_seconds"] = rounded(end - start)
        sample["create"]["latency_seconds"] = rounded(end - start)
        sample["create"]["error"] = error_payload(exc)
        sample["_create_end_monotonic"] = end
        sample["finished_at"] = utc_now()
        update_overall_status(sample)
        return sample, None

    end = time.perf_counter()
    sample["create"]["status"] = "success"
    sample["create"]["finished_at"] = utc_now()
    sample["create"]["duration_seconds"] = rounded(end - start)
    sample["create"]["latency_seconds"] = rounded(end - start)
    sample["_create_end_monotonic"] = end
    return sample, sandbox


def validate_sandbox(sample: Dict[str, Any], sandbox) -> Dict[str, Any]:
    if sandbox is None:
        sample["validation"] = new_phase("skipped")
        update_overall_status(sample)
        return sample

    sample["validation"]["status"] = "running"
    sample["validation"]["started_at"] = utc_now()
    sample["validation"]["command"] = SANITY_COMMAND
    start = time.perf_counter()
    try:
        result = sandbox.execute(cmd="sh", args=["-c", SANITY_COMMAND])
        exit_code = getattr(result, "exit_code", None)
        stdout = getattr(result, "stdout", "")
        stderr = getattr(result, "stderr", "")
        sample["validation"]["exit_code"] = exit_code
        sample["validation"]["stdout"] = stdout[:4000] if isinstance(stdout, str) else repr(stdout)[:4000]
        sample["validation"]["stderr"] = stderr[:4000] if isinstance(stderr, str) else repr(stderr)[:4000]
        if exit_code != 0:
            raise RuntimeError(f"sanity command exited with {exit_code}")
        if SANITY_MARKER not in str(stdout):
            raise RuntimeError(f"sanity command output missing marker {SANITY_MARKER!r}")
        sample["validation"]["status"] = "success"
    except Exception as exc:
        sample["validation"]["status"] = "failed"
        sample["validation"]["error"] = error_payload(exc)
    finally:
        sample["validation"]["finished_at"] = utc_now()
        sample["validation"]["duration_seconds"] = rounded(time.perf_counter() - start)
        update_overall_status(sample)
    return sample


def cleanup_sandbox(sample: Dict[str, Any], sandbox) -> Dict[str, Any]:
    if sandbox is None:
        sample["cleanup"] = new_phase("skipped")
        update_overall_status(sample)
        return sample

    sample["cleanup"]["status"] = "running"
    sample["cleanup"]["started_at"] = utc_now()
    start = time.perf_counter()
    try:
        sandbox.delete()
        sample["cleanup"]["status"] = "success"
    except Exception as exc:
        sample["cleanup"]["status"] = "failed"
        sample["cleanup"]["error"] = error_payload(exc)
    finally:
        sample["cleanup"]["finished_at"] = utc_now()
        sample["cleanup"]["duration_seconds"] = rounded(time.perf_counter() - start)
        sample["finished_at"] = utc_now()
        update_overall_status(sample)
    return sample


def strip_private_fields(samples: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cleaned = []
    for sample in samples:
        item = dict(sample)
        item.pop("_create_start_monotonic", None)
        item.pop("_create_end_monotonic", None)
        cleaned.append(item)
    return cleaned


def run_single_scenario(
    sandbox_class,
    *,
    scenario: str,
    iterations: int,
    use_snapshot: bool,
    config_path: str,
    image_name: str,
    namespace: str,
    vcpu_num: int,
    ram_mb: int,
) -> List[Dict[str, Any]]:
    samples = []
    for index in range(1, iterations + 1):
        sample = base_sample(scenario, index, make_sandbox_id(scenario, index), use_snapshot)
        sample, sandbox = run_create_phase(
            sandbox_class,
            sample,
            config_path=config_path,
            image_name=image_name,
            namespace=namespace,
            use_snapshot=use_snapshot,
            vcpu_num=vcpu_num,
            ram_mb=ram_mb,
        )
        if sandbox is not None:
            validate_sandbox(sample, sandbox)
            cleanup_sandbox(sample, sandbox)
        samples.append(sample)
    return samples


def run_snapshot_concurrent_scenario(
    sandbox_class,
    *,
    concurrency: int,
    config_path: str,
    image_name: str,
    namespace: str,
    vcpu_num: int,
    ram_mb: int,
) -> Tuple[List[Dict[str, Any]], Optional[float]]:
    scenario = "snapshot_concurrent"
    samples = [
        base_sample(scenario, index, make_sandbox_id(scenario, index), True)
        for index in range(1, concurrency + 1)
    ]
    sandboxes: Dict[str, Any] = {}

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                run_create_phase,
                sandbox_class,
                sample,
                config_path=config_path,
                image_name=image_name,
                namespace=namespace,
                use_snapshot=True,
                vcpu_num=vcpu_num,
                ram_mb=ram_mb,
            ): sample
            for sample in samples
        }
        for future in as_completed(futures):
            sample, sandbox = future.result()
            if sandbox is not None:
                sandboxes[sample["sandbox_id"]] = sandbox

    create_starts = [
        sample.get("_create_start_monotonic")
        for sample in samples
        if sample.get("_create_start_monotonic") is not None
    ]
    create_ends = [
        sample.get("_create_end_monotonic")
        for sample in samples
        if sample.get("_create_end_monotonic") is not None
    ]
    makespan = max(create_ends) - min(create_starts) if create_starts and create_ends else None

    post_workers = max(1, min(POST_CREATE_WORKERS, len(sandboxes)))
    if sandboxes:
        by_id = {sample["sandbox_id"]: sample for sample in samples}
        with ThreadPoolExecutor(max_workers=post_workers) as executor:
            futures = [
                executor.submit(validate_sandbox, by_id[sandbox_id], sandbox)
                for sandbox_id, sandbox in sandboxes.items()
            ]
            for future in as_completed(futures):
                future.result()

        with ThreadPoolExecutor(max_workers=post_workers) as executor:
            futures = [
                executor.submit(cleanup_sandbox, by_id[sandbox_id], sandbox)
                for sandbox_id, sandbox in sandboxes.items()
            ]
            for future in as_completed(futures):
                future.result()

    for sample in samples:
        if sample["create"]["status"] == "failed":
            sample["validation"] = new_phase("skipped")
            sample["cleanup"] = new_phase("skipped")
            sample["finished_at"] = sample["create"]["finished_at"]
            update_overall_status(sample)

    return samples, rounded(makespan)


def latency_stats(latencies: List[float]) -> Dict[str, Optional[float]]:
    return {
        "count": len(latencies),
        "min_seconds": rounded(min(latencies)) if latencies else None,
        "p50_seconds": rounded(percentile(latencies, 50)),
        "p90_seconds": rounded(percentile(latencies, 90)),
        "p95_seconds": rounded(percentile(latencies, 95)),
        "p99_seconds": rounded(percentile(latencies, 99)),
        "max_seconds": rounded(max(latencies)) if latencies else None,
    }


def summarize_samples(
    scenario: str,
    samples: List[Dict[str, Any]],
    *,
    makespan_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    failure_phases = {"create": 0, "validation": 0, "cleanup": 0}
    for sample in samples:
        for phase in failure_phases:
            if sample.get(phase, {}).get("status") == "failed":
                failure_phases[phase] += 1

    successful_latencies = [
        float(sample["create"]["latency_seconds"])
        for sample in samples
        if sample.get("create", {}).get("status") == "success"
        and sample.get("create", {}).get("latency_seconds") is not None
    ]
    success_count = sum(1 for sample in samples if sample.get("status") == "success")
    return {
        "scenario": scenario,
        "count": len(samples),
        "success_count": success_count,
        "failure_count": len(samples) - success_count,
        "failure_phases": failure_phases,
        "latency": latency_stats(successful_latencies),
        "makespan_seconds": makespan_seconds,
    }


def env_metadata() -> Dict[str, str]:
    keys = [
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_SHA",
        "RUNNER_ARCH",
        "CONCH_REPOSITORY",
        "CONCH_REF",
        "CONCH_COMMIT",
        "CONCH_E2B_ROOTFS_IMAGE",
        "CONCH_BENCHMARK_SNAPSHOT_IMAGE",
        "CONCH_STARTUP_SNAPSHOT_CACHE_KEY",
        "CONCH_STARTUP_SNAPSHOT_CACHE_HIT",
        "CONCH_KERNEL_DIGEST",
        "CONCH_INITRD_DIGEST",
        "CONCH_SNAPSHOT_FORMAT_VERSION",
        "CONCH_NAMESPACE",
    ]
    return {key: os.environ.get(key, "") for key in keys}


def format_seconds(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}"


def write_markdown_summary(summary: Dict[str, Any]) -> str:
    lines = [
        "# Conch sandbox startup benchmark",
        "",
        "Create latency is measured around `Sandbox.create(...)` only. Rootfs build, image conversion, image pull/unpack, conchd startup, validation, and cleanup are excluded from latency statistics.",
        "",
        f"- Snapshot image: `{summary['metadata'].get('CONCH_BENCHMARK_SNAPSHOT_IMAGE', '')}`",
        f"- Snapshot cache hit: `{summary['metadata'].get('CONCH_STARTUP_SNAPSHOT_CACHE_HIT', '')}`",
        f"- Concurrency: `{summary['parameters']['concurrency']}`",
        "",
        "| Scenario | Samples | Success | Failure | min | p50 | p90 | p95 | p99 | max | Makespan |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for scenario in summary["scenarios"]:
        latency = scenario["latency"]
        lines.append(
            "| {scenario} | {count} | {success} | {failure} | {min} | {p50} | {p90} | {p95} | {p99} | {max} | {makespan} |".format(
                scenario=scenario["scenario"],
                count=scenario["count"],
                success=scenario["success_count"],
                failure=scenario["failure_count"],
                min=format_seconds(latency["min_seconds"]),
                p50=format_seconds(latency["p50_seconds"]),
                p90=format_seconds(latency["p90_seconds"]),
                p95=format_seconds(latency["p95_seconds"]),
                p99=format_seconds(latency["p99_seconds"]),
                max=format_seconds(latency["max_seconds"]),
                makespan=format_seconds(scenario.get("makespan_seconds")),
            )
        )
    lines.extend(
        [
            "",
            "The first version reports slow latency without enforcing thresholds. Create, validation, cleanup, preparation, pull, or unpack failures fail the job.",
            "",
        ]
    )
    return "\n".join(lines)


def write_outputs(
    *,
    results_dir: Path,
    samples: List[Dict[str, Any]],
    scenario_summaries: List[Dict[str, Any]],
    parameters: Dict[str, Any],
) -> Dict[str, Any]:
    results_dir.mkdir(parents=True, exist_ok=True)
    cleaned_samples = strip_private_fields(samples)
    failed = any(sample["status"] != "success" for sample in cleaned_samples)
    summary = {
        "metadata": env_metadata(),
        "parameters": parameters,
        "measurement": {
            "create_latency": "Python-side wall time around Sandbox.create(...).",
            "excluded": [
                "rootfs build",
                "snapshot image conversion",
                "image pull/unpack",
                "conchd startup",
                "post-create sanity command",
                "sandbox cleanup",
            ],
            "readiness": "Sandbox.create(...) returning according to the Conch guest-agent ready contract.",
            "latency_threshold_gate": False,
        },
        "failed": failed,
        "scenarios": scenario_summaries,
    }

    samples_path = results_dir / "samples.jsonl"
    with samples_path.open("w", encoding="utf-8") as handle:
        for sample in cleaned_samples:
            handle.write(json.dumps(sample, sort_keys=True) + "\n")

    summary_path = results_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    markdown = write_markdown_summary(summary)
    markdown_path = results_dir / "summary.md"
    markdown_path.write_text(markdown, encoding="utf-8")

    return summary


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Conch sandbox startup latency.")
    parser.add_argument("--config-path", required=True)
    parser.add_argument("--image-name", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--concurrency", required=True)
    parser.add_argument("--single-iterations", type=int, default=DEFAULT_SINGLE_ITERATIONS)
    parser.add_argument("--vcpu-num", type=int, default=2)
    parser.add_argument("--ram-mb", type=int, default=2048)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        concurrency = parse_concurrency(args.concurrency)
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2
    if args.single_iterations < 1:
        print("::error::single iterations must be at least 1", file=sys.stderr)
        return 2

    sandbox_class = import_sandbox_class()
    all_samples: List[Dict[str, Any]] = []
    scenario_summaries: List[Dict[str, Any]] = []

    cold_samples = run_single_scenario(
        sandbox_class,
        scenario="cold_single",
        iterations=args.single_iterations,
        use_snapshot=False,
        config_path=args.config_path,
        image_name=args.image_name,
        namespace=args.namespace,
        vcpu_num=args.vcpu_num,
        ram_mb=args.ram_mb,
    )
    all_samples.extend(cold_samples)
    scenario_summaries.append(summarize_samples("cold_single", cold_samples))

    snapshot_single_samples = run_single_scenario(
        sandbox_class,
        scenario="snapshot_single",
        iterations=args.single_iterations,
        use_snapshot=True,
        config_path=args.config_path,
        image_name=args.image_name,
        namespace=args.namespace,
        vcpu_num=args.vcpu_num,
        ram_mb=args.ram_mb,
    )
    all_samples.extend(snapshot_single_samples)
    scenario_summaries.append(summarize_samples("snapshot_single", snapshot_single_samples))

    snapshot_concurrent_samples, makespan = run_snapshot_concurrent_scenario(
        sandbox_class,
        concurrency=concurrency,
        config_path=args.config_path,
        image_name=args.image_name,
        namespace=args.namespace,
        vcpu_num=args.vcpu_num,
        ram_mb=args.ram_mb,
    )
    all_samples.extend(snapshot_concurrent_samples)
    scenario_summaries.append(
        summarize_samples("snapshot_concurrent", snapshot_concurrent_samples, makespan_seconds=makespan)
    )

    summary = write_outputs(
        results_dir=Path(args.results_dir),
        samples=all_samples,
        scenario_summaries=scenario_summaries,
        parameters={
            "concurrency": concurrency,
            "single_iterations": args.single_iterations,
            "vcpu_num": args.vcpu_num,
            "ram_mb": args.ram_mb,
        },
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
