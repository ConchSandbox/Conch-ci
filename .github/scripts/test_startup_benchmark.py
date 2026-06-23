from pathlib import Path
import unittest

import startup_benchmark


class FakeExecution:
    exit_code = 0
    stdout = "conch-startup-ok\n"
    stderr = ""


class FakeSandbox:
    created = []
    deleted = []

    def __init__(self, sandbox_id, use_snapshot):
        self.sandbox_id = sandbox_id
        self.use_snapshot = use_snapshot

    @classmethod
    def create(cls, **kwargs):
        sandbox = cls(kwargs["sandbox_id"], kwargs["use_snapshot"])
        cls.created.append(sandbox)
        return sandbox

    def execute(self, **_kwargs):
        return FakeExecution()

    def delete(self):
        self.deleted.append(self.sandbox_id)
        return True


class StartupBenchmarkTest(unittest.TestCase):
    def setUp(self):
        FakeSandbox.created = []
        FakeSandbox.deleted = []

    def test_parse_concurrency_accepts_positive_integer_up_to_limit(self):
        self.assertEqual(startup_benchmark.parse_concurrency("1"), 1)
        self.assertEqual(startup_benchmark.parse_concurrency("1000"), 1000)

    def test_parse_concurrency_rejects_invalid_values(self):
        for value in ("", "0", "-1", "1001", "1.5", "abc"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    startup_benchmark.parse_concurrency(value)

    def test_percentile_uses_linear_interpolation(self):
        values = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(startup_benchmark.percentile(values, 0), 1.0)
        self.assertEqual(startup_benchmark.percentile(values, 50), 2.5)
        self.assertEqual(startup_benchmark.percentile(values, 100), 4.0)

    def test_summarize_samples_reports_latency_stats_and_failure_phases(self):
        samples = [
            {
                "status": "success",
                "create": {"status": "success", "latency_seconds": 1.0},
                "validation": {"status": "success"},
                "cleanup": {"status": "success"},
            },
            {
                "status": "failed",
                "create": {"status": "success", "latency_seconds": 3.0},
                "validation": {"status": "failed"},
                "cleanup": {"status": "success"},
            },
            {
                "status": "failed",
                "create": {"status": "failed", "latency_seconds": 2.0},
                "validation": {"status": "skipped"},
                "cleanup": {"status": "skipped"},
            },
        ]

        summary = startup_benchmark.summarize_samples("snapshot_single", samples)

        self.assertEqual(summary["scenario"], "snapshot_single")
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["success_count"], 1)
        self.assertEqual(summary["failure_count"], 2)
        self.assertEqual(summary["failure_phases"]["create"], 1)
        self.assertEqual(summary["failure_phases"]["validation"], 1)
        self.assertEqual(summary["latency"]["count"], 2)
        self.assertEqual(summary["latency"]["min_seconds"], 1.0)
        self.assertEqual(summary["latency"]["p50_seconds"], 2.0)
        self.assertEqual(summary["latency"]["max_seconds"], 3.0)

    def test_benchmark_scenarios_run_through_sdk_surface(self):
        cold = startup_benchmark.run_single_scenario(
            FakeSandbox,
            scenario="cold_single",
            iterations=1,
            use_snapshot=False,
            config_path="/tmp/sdk-config.yaml",
            image_name="localhost:5000/conch/snapshot:v1",
            namespace="test",
            vcpu_num=2,
            ram_mb=2048,
        )
        snapshot, makespan = startup_benchmark.run_snapshot_concurrent_scenario(
            FakeSandbox,
            concurrency=2,
            config_path="/tmp/sdk-config.yaml",
            image_name="localhost:5000/conch/snapshot:v1",
            namespace="test",
            vcpu_num=2,
            ram_mb=2048,
        )

        self.assertEqual([sample["status"] for sample in cold], ["success"])
        self.assertEqual([sample["use_snapshot"] for sample in cold], [False])
        self.assertEqual([sample["status"] for sample in snapshot], ["success", "success"])
        self.assertEqual([sample["use_snapshot"] for sample in snapshot], [True, True])
        self.assertIsNotNone(makespan)
        self.assertEqual(len(FakeSandbox.created), 3)
        self.assertEqual(len(FakeSandbox.deleted), 3)

    def test_workflow_uses_guest_agent_static_tap_network(self):
        repo_root = Path(__file__).resolve().parents[2]
        workflow = repo_root.joinpath(".github/workflows/startup-performance.yml").read_text()

        self.assertIn("tap_ip: 192.168.100.2", workflow)
        self.assertIn("tap_mask: 24", workflow)
        self.assertIn(
            'cache_material="${CONCH_SNAPSHOT_FORMAT_VERSION}|${CONCH_E2B_ROOTFS_PLATFORM_SUFFIX}|${conch_commit}|${rootfs_digest}|${kernel_digest}|${ALPINE_VERSION}"',
            workflow,
        )
        self.assertNotIn("|${initrd_digest}\"", workflow)
        self.assertNotIn("CONCH_STARTUP_VALIDATE_IN_NETNS", workflow)


if __name__ == "__main__":
    unittest.main()
