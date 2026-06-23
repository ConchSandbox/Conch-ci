from pathlib import Path
import os
import tempfile
import unittest

import startup_benchmark


class FakeExecution:
    exit_code = 0
    stdout = "conch-startup-ok\n"
    stderr = ""


class FakeSandbox:
    created = []
    deleted = []
    executed = []

    def __init__(self, sandbox_id, image_name, use_snapshot):
        self.sandbox_id = sandbox_id
        self.image_name = image_name
        self.use_snapshot = use_snapshot

    @classmethod
    def create(cls, **kwargs):
        sandbox = cls(kwargs["sandbox_id"], kwargs["image_name"], kwargs["use_snapshot"])
        cls.created.append(sandbox)
        return sandbox

    def execute(self, **_kwargs):
        self.executed.append(self.sandbox_id)
        return FakeExecution()

    def delete(self):
        self.deleted.append(self.sandbox_id)
        return True


class StartupBenchmarkTest(unittest.TestCase):
    def setUp(self):
        FakeSandbox.created = []
        FakeSandbox.deleted = []
        FakeSandbox.executed = []

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
            image_name="localhost:5000/conch/boot:v1",
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
        self.assertEqual(
            [sandbox.image_name for sandbox in FakeSandbox.created],
            [
                "localhost:5000/conch/boot:v1",
                "localhost:5000/conch/snapshot:v1",
                "localhost:5000/conch/snapshot:v1",
            ],
        )
        self.assertEqual(len(FakeSandbox.deleted), 3)
        self.assertEqual(len(FakeSandbox.executed), 3)

    def test_skip_validation_counts_create_and_cleanup_success(self):
        samples = startup_benchmark.run_single_scenario(
            FakeSandbox,
            scenario="cold_single",
            iterations=1,
            use_snapshot=False,
            validate=False,
            config_path="/tmp/sdk-config.yaml",
            image_name="localhost:5000/conch/boot:v1",
            namespace="test",
            vcpu_num=2,
            ram_mb=2048,
        )

        self.assertEqual(samples[0]["status"], "success")
        self.assertEqual(samples[0]["validation"]["status"], "skipped")
        self.assertEqual(samples[0]["validation"]["reason"], "post-create validation disabled")
        self.assertEqual(len(FakeSandbox.created), 1)
        self.assertEqual(len(FakeSandbox.deleted), 1)
        self.assertEqual(FakeSandbox.executed, [])

    def test_write_outputs_leaves_github_step_summary_to_workflow(self):
        sample = {
            "scenario": "snapshot_single",
            "sample_index": 1,
            "sandbox_id": "sandbox-1",
            "use_snapshot": True,
            "status": "success",
            "failure_phase": "",
            "started_at": "2026-06-23T00:00:00.000Z",
            "finished_at": "2026-06-23T00:00:01.000Z",
            "create": {"status": "success", "latency_seconds": 0.1},
            "validation": {"status": "success"},
            "cleanup": {"status": "success"},
        }
        scenario_summary = startup_benchmark.summarize_samples("snapshot_single", [sample])

        with tempfile.TemporaryDirectory() as tmp:
            results_dir = Path(tmp, "results")
            step_summary = Path(tmp, "github-step-summary.md")
            step_summary.write_text("existing\n", encoding="utf-8")
            old_step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
            os.environ["GITHUB_STEP_SUMMARY"] = str(step_summary)
            try:
                startup_benchmark.write_outputs(
                    results_dir=results_dir,
                    samples=[sample],
                    scenario_summaries=[scenario_summary],
                    parameters={
                        "concurrency": 1,
                        "single_iterations": 1,
                        "vcpu_num": 2,
                        "ram_mb": 2048,
                    },
                )
            finally:
                if old_step_summary is None:
                    os.environ.pop("GITHUB_STEP_SUMMARY", None)
                else:
                    os.environ["GITHUB_STEP_SUMMARY"] = old_step_summary

            self.assertTrue(results_dir.joinpath("summary.md").exists())
            self.assertEqual(step_summary.read_text(encoding="utf-8"), "existing\n")

    def test_workflow_uses_guest_agent_static_tap_network(self):
        repo_root = Path(__file__).resolve().parents[2]
        workflow = repo_root.joinpath(".github/workflows/startup-performance.yml").read_text()

        self.assertIn("CONCH_REF: ${{ github.event.inputs.conch_ref || 'dev' }}", workflow)
        self.assertIn("CONCH_SNAPSHOT_FORMAT_VERSION: dev-v1", workflow)
        self.assertIn("default_vmm: \"$CONCH_E2B_VMM_NAME\"", workflow)
        self.assertNotIn("./bin/conch convert", workflow)
        self.assertIn('--image-name "$CONCH_BENCHMARK_BOOT_IMAGE"', workflow)
        self.assertIn('--snapshot-image-name "$CONCH_BENCHMARK_SNAPSHOT_IMAGE"', workflow)
        self.assertIn("tap_ip: 192.168.100.2", workflow)
        self.assertIn("tap_mask: 24", workflow)
        self.assertIn(
            'cache_material="${CONCH_SNAPSHOT_FORMAT_VERSION}|${CONCH_E2B_ROOTFS_PLATFORM_SUFFIX}|${conch_commit}|${rootfs_digest}|${kernel_digest}|${ALPINE_VERSION}"',
            workflow,
        )
        self.assertNotIn("|${initrd_digest}\"", workflow)
        self.assertNotIn("CONCH_STARTUP_VALIDATE_IN_NETNS", workflow)
        self.assertIn("conch_cli() {", workflow)
        self.assertIn('sudo -n env \\', workflow)
        self.assertIn("conch_cli build \\", workflow)
        self.assertIn("conch_cli snapshot export \\", workflow)
        self.assertIn("sudo -n buildah unmount --all", workflow)
        self.assertIn("xargs -r sudo -n buildah rmi -f", workflow)
        self.assertIn("--skip-validation", workflow)


if __name__ == "__main__":
    unittest.main()
