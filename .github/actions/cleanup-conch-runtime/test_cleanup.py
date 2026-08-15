#!/usr/bin/env python3
"""Unit tests for the Conch runtime cleanup helper."""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("cleanup.py")
SPEC = importlib.util.spec_from_file_location("cleanup_conch_runtime", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load cleanup helper from {MODULE_PATH}")
cleanup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup
SPEC.loader.exec_module(cleanup)


def network_config(data_dir: str) -> dict[str, object]:
    return {
        "name": "conch-bridge",
        "cniVersion": "1.0.0",
        "plugins": [
            {
                "type": "bridge",
                "bridge": "cni-conch0",
                "isGateway": True,
                "ipMasq": True,
                "ipam": {
                    "type": "host-local",
                    "dataDir": data_dir,
                    "subnet": "10.12.0.0/20",
                },
            }
        ],
    }


def residual_resources(**overrides: object) -> object:
    values = {
        "forced_daemon_pids": (),
        "child_process_pids": (),
        "cni_cache_entries": (),
        "network_namespaces": (),
        "bridge_ports": (),
        "nat_rule_count": 0,
        "forward_rule_count": 0,
        "workdir_mounts": (),
    }
    values.update(overrides)
    return cleanup.ResidualResources(**values)


class CachedAttachmentTests(unittest.TestCase):
    def write_cache(self, workdir: Path, data_dir: str) -> Path:
        results = workdir / "state" / "cni" / "results"
        results.mkdir(parents=True)
        path = results / "conch-bridge-conch-slot-2-eth0"
        config = json.dumps(network_config(data_dir)).encode()
        path.write_text(
            json.dumps(
                {
                    "kind": "cniCacheV1",
                    "containerId": "conch-slot-2",
                    "config": base64.b64encode(config).decode(),
                    "ifName": "eth0",
                    "networkName": "conch-bridge",
                    "netns": "/run/conch/netns/slot-2",
                    "result": {"cniVersion": "1.0.0"},
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_loads_current_conch_libcni_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            expected_data_dir = workdir / "state" / "cni" / "networks"
            # Current Conch rewrites the parsed plugin bytes, while libcni
            # caches the original list bytes with the source-default dataDir.
            cache_path = self.write_cache(workdir, "/var/lib/conch/cni/networks")

            attachments = cleanup.load_cached_attachments(workdir)

            self.assertEqual(len(attachments), 1)
            attachment = attachments[0]
            self.assertEqual(attachment.slot_id, 2)
            self.assertEqual(attachment.cache_path, cache_path)
            self.assertEqual(attachment.plugin_config["name"], "conch-bridge")
            self.assertEqual(
                attachment.plugin_config["ipam"]["dataDir"], str(expected_data_dir)
            )
            self.assertEqual(
                attachment.plugin_config["prevResult"], {"cniVersion": "1.0.0"}
            )

    def test_rejects_cache_owned_by_another_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            self.write_cache(workdir, "/tmp/another-runtime/cni/networks")

            with self.assertRaisesRegex(RuntimeError, "not a supported Conch"):
                cleanup.load_cached_attachments(workdir)


class ConfigTests(unittest.TestCase):
    def test_runtime_config_rewrites_host_local_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            conf_dir = workdir / "cni" / "net.d"
            conf_dir.mkdir(parents=True)
            source = network_config("/var/lib/conch/cni/networks")["plugins"][0]
            (conf_dir / "10-conch.conf").write_text(
                json.dumps(
                    {
                        "name": "conch-bridge",
                        "cniVersion": "1.0.0",
                        **source,
                    }
                ),
                encoding="utf-8",
            )

            plugin = cleanup.load_runtime_plugin_config(workdir)

            self.assertEqual(
                plugin["ipam"]["dataDir"],
                str(workdir / "state" / "cni" / "networks"),
            )

    def test_cni_del_uses_current_conch_runtime_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary_dir = root / "bin"
            cni_dir = binary_dir / "cni"
            cni_dir.mkdir(parents=True)
            output = root / "invocation.json"
            bridge = cni_dir / "bridge"
            bridge.write_text(
                """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
Path(os.environ["TEST_CNI_OUTPUT"]).write_text(json.dumps({
    "command": os.environ["CNI_COMMAND"],
    "container": os.environ["CNI_CONTAINERID"],
    "netns": os.environ["CNI_NETNS"],
    "ifname": os.environ["CNI_IFNAME"],
    "path": os.environ["CNI_PATH"],
    "config": json.load(sys.stdin),
}))
""",
                encoding="utf-8",
            )
            bridge.chmod(0o755)
            plugin = {
                "name": "conch-bridge",
                "cniVersion": "1.0.0",
                "type": "bridge",
                "bridge": "cni-conch0",
            }

            with mock.patch.dict(os.environ, {"TEST_CNI_OUTPUT": str(output)}):
                cleanup.run_cni_del(binary_dir, 2, plugin, "", True)

            invocation = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(invocation["command"], "DEL")
            self.assertEqual(invocation["container"], "conch-slot-2")
            self.assertEqual(invocation["netns"], "/run/conch/netns/slot-2")
            self.assertEqual(invocation["ifname"], "eth0")
            self.assertEqual(invocation["path"], str(cni_dir))
            self.assertEqual(invocation["config"], plugin)


class IPTablesRuleTests(unittest.TestCase):
    def test_matches_only_exact_conch_cni_comment(self) -> None:
        conch = cleanup.shlex.split(
            '-A POSTROUTING -s 10.12.0.2/32 -m comment --comment '
            '"name: \\"conch-bridge\\" id: \\"conch-slot-2\\"" '
            "-j CNI-1234abcd"
        )
        other_network = cleanup.shlex.split(
            '-A POSTROUTING -s 10.13.0.2/32 -m comment --comment '
            '"name: \\"other\\" id: \\"conch-slot-2\\"" '
            "-j CNI-1234abcd"
        )

        self.assertTrue(cleanup.conch_nat_rule(conch))
        self.assertFalse(cleanup.conch_nat_rule(other_network))

    def test_matches_only_conch_bridge_forward_accept_rules(self) -> None:
        self.assertTrue(
            cleanup.conch_forward_rule(
                ["-A", "FORWARD", "-i", "cni-conch0", "-o", "eth0", "-j", "ACCEPT"]
            )
        )
        self.assertFalse(
            cleanup.conch_forward_rule(
                ["-A", "FORWARD", "-i", "docker0", "-o", "eth0", "-j", "ACCEPT"]
            )
        )
        self.assertFalse(
            cleanup.conch_forward_rule(
                ["-A", "FORWARD", "-i", "cni-conch0", "-j", "DROP"]
            )
        )


class CleanupControlFlowTests(unittest.TestCase):
    def test_host_tap_is_not_treated_as_conch_residue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                mock.patch.object(cleanup, "network_namespace_paths", return_value={}),
                mock.patch.object(cleanup, "iptables_rules", return_value=[]),
                mock.patch.object(cleanup, "cni_cache_entry_paths", return_value=[]),
                mock.patch.object(cleanup, "bridge_port_names", return_value=[]),
                mock.patch.object(cleanup, "workdir_mount_targets", return_value=set()),
                mock.patch.object(cleanup, "link_exists") as link_exists,
            ):
                resources = cleanup.detect_residual_resources(root, (), {})

            self.assertFalse(resources.found())
            link_exists.assert_not_called()

    def test_clean_shutdown_skips_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.md"
            with (
                mock.patch.object(cleanup, "runtime_processes", return_value={}),
                mock.patch.object(cleanup, "terminate_processes", return_value=()),
                mock.patch.object(cleanup, "other_conchd_processes", return_value=[]),
                mock.patch.object(
                    cleanup,
                    "detect_residual_resources",
                    return_value=residual_resources(),
                ),
                mock.patch.object(cleanup, "delete_link") as delete_link,
                mock.patch.object(cleanup, "link_exists", return_value=False),
                mock.patch.object(cleanup, "fallback_cleanup") as fallback_cleanup,
            ):
                detected = cleanup.cleanup(root / "work", root / "bin", report)

            self.assertFalse(detected)
            self.assertFalse(report.exists())
            delete_link.assert_called_once_with("cni-conch0")
            fallback_cleanup.assert_not_called()

    def test_residue_is_reported_before_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / "work"
            report = root / "report.md"
            resources = residual_resources(
                cni_cache_entries=(
                    str(workdir / "state/cni/results/conch-bridge-conch-slot-2-eth0"),
                ),
                nat_rule_count=3,
            )

            def assert_report_exists(*_args: object) -> None:
                self.assertTrue(report.exists())

            with (
                mock.patch.object(cleanup, "runtime_processes", return_value={}),
                mock.patch.object(cleanup, "terminate_processes", return_value=()),
                mock.patch.object(cleanup, "other_conchd_processes", return_value=[]),
                mock.patch.object(
                    cleanup, "detect_residual_resources", return_value=resources
                ),
                mock.patch.object(
                    cleanup,
                    "fallback_cleanup",
                    side_effect=assert_report_exists,
                ) as fallback_cleanup,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                detected = cleanup.cleanup(workdir, root / "bin", report)

            self.assertTrue(detected)
            fallback_cleanup.assert_called_once_with(workdir, root / "bin", {})
            contents = report.read_text(encoding="utf-8")
            self.assertIn("Conch teardown bug", contents)
            self.assertIn("libcni result cache", contents)
            self.assertIn("Conch CNI NAT rules", contents)
            self.assertIn("Fallback cleanup status: **succeeded**", contents)

    def test_main_uses_distinct_status_after_successful_fallback(self) -> None:
        arguments = [
            "cleanup.py",
            "normal-shutdown",
            "--work-dir",
            "/tmp/conch-work",
            "--binary-dir",
            "/tmp/conch-bin",
            "--report-file",
            "/tmp/conch-report.md",
        ]
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", arguments),
            mock.patch.object(cleanup, "cleanup", return_value=True),
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as raised,
        ):
            cleanup.main()

        self.assertEqual(raised.exception.code, cleanup.RESIDUAL_EXIT_STATUS)
        self.assertIn("::error title=Conch teardown bug detected::", output.getvalue())


class AbandonedRuntimeRecoveryTests(unittest.TestCase):
    def runtime_paths(self, root: Path) -> tuple[Path, Path, Path]:
        runner_temp = root / "runner-temp"
        runner_temp.mkdir()
        old_workdir = runner_temp / "conch-e2b-sdk-100-1"
        current_workdir = runner_temp / "conch-e2b-sdk-101-1"
        return runner_temp, old_workdir, current_workdir

    def test_recovers_dangling_sdk_symlink_from_dead_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner_temp, old_workdir, current_workdir = self.runtime_paths(root)
            old_workdir.mkdir()
            sdk_socket = root / "run/conch/conchd.sock"
            sdk_socket.parent.mkdir(parents=True)
            sdk_socket.symlink_to(old_workdir / "work/conchd.sock")
            report = runner_temp / "recovery.md"

            with (
                mock.patch.object(cleanup, "SDK_SOCKET", sdk_socket),
                mock.patch.object(cleanup, "CNI_CONF_MOUNT", root / "etc/conch"),
                mock.patch.object(cleanup, "mount_targets", return_value=set()),
                mock.patch.object(cleanup, "cleanup", return_value=False) as run_cleanup,
            ):
                recovered = cleanup.recover_abandoned_runtime(
                    runner_temp,
                    current_workdir,
                    runner_temp / "bin",
                    report,
                )

            self.assertTrue(recovered)
            self.assertFalse(sdk_socket.is_symlink())
            self.assertFalse(old_workdir.exists())
            self.assertIn(
                "Abandoned Conch CI runtime recovery",
                report.read_text(encoding="utf-8"),
            )
            run_cleanup.assert_called_once_with(
                old_workdir,
                runner_temp / "bin",
                report,
                mode=cleanup.ABANDONED_RUN_MODE,
            )

    def test_current_runtime_fixed_resource_is_not_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner_temp, _old_workdir, current_workdir = self.runtime_paths(root)
            sdk_socket = root / "run/conch/conchd.sock"
            sdk_socket.parent.mkdir(parents=True)
            sdk_socket.symlink_to(current_workdir / "work/conchd.sock")

            with (
                mock.patch.object(cleanup, "SDK_SOCKET", sdk_socket),
                mock.patch.object(cleanup, "CNI_CONF_MOUNT", root / "etc/conch"),
                mock.patch.object(cleanup, "mount_targets", return_value=set()),
            ):
                resources = cleanup.fixed_runtime_resources(
                    runner_temp, current_workdir
                )

            self.assertIsNone(resources)

    def test_rejects_live_conchd_in_abandoned_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                mock.patch.object(
                    cleanup, "runtime_processes", return_value={123: 456}
                ),
                mock.patch.object(cleanup, "terminate_processes") as terminate,
            ):
                with self.assertRaisesRegex(RuntimeError, "live conchd"):
                    cleanup.cleanup(
                        root / "work",
                        root / "bin",
                        root / "report.md",
                        mode=cleanup.ABANDONED_RUN_MODE,
                    )

            terminate.assert_not_called()

    def test_abandoned_residue_is_not_reported_as_graceful_teardown_bug(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.md"
            resources = residual_resources(nat_rule_count=1)
            with (
                mock.patch.object(cleanup, "runtime_processes", return_value={}),
                mock.patch.object(cleanup, "other_conchd_processes", return_value=[]),
                mock.patch.object(
                    cleanup, "detect_residual_resources", return_value=resources
                ),
                mock.patch.object(cleanup, "fallback_cleanup"),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                detected = cleanup.cleanup(
                    root / "work",
                    root / "bin",
                    report,
                    mode=cleanup.ABANDONED_RUN_MODE,
                )

            contents = report.read_text(encoding="utf-8")
            self.assertTrue(detected)
            self.assertIn("previous CI execution ended", contents)
            self.assertNotIn("Conch teardown bug", contents)

    def test_rejects_different_socket_and_mount_owners(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner_temp, old_workdir, current_workdir = self.runtime_paths(root)
            other_workdir = runner_temp / "conch-template-99-1"
            sdk_socket = root / "run/conch/conchd.sock"
            sdk_socket.parent.mkdir(parents=True)
            sdk_socket.symlink_to(old_workdir / "work/conchd.sock")
            cni_mount = root / "etc/conch/cni/net.d"
            cni_mount.mkdir(parents=True)
            (cni_mount / cleanup.CNI_RUNTIME_MARKER).write_text(
                f"{other_workdir}\n", encoding="utf-8"
            )

            with (
                mock.patch.object(cleanup, "SDK_SOCKET", sdk_socket),
                mock.patch.object(cleanup, "CNI_CONF_MOUNT", cni_mount),
                mock.patch.object(
                    cleanup, "mount_targets", return_value={str(cni_mount)}
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "different owners"):
                    cleanup.fixed_runtime_resources(runner_temp, current_workdir)

    def test_discovers_abandoned_cni_mount_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner_temp, old_workdir, current_workdir = self.runtime_paths(root)
            cni_mount = root / "etc/conch/cni/net.d"
            cni_mount.mkdir(parents=True)
            (cni_mount / cleanup.CNI_RUNTIME_MARKER).write_text(
                f"{old_workdir}\n", encoding="utf-8"
            )

            with (
                mock.patch.object(cleanup, "SDK_SOCKET", root / "missing/socket"),
                mock.patch.object(cleanup, "CNI_CONF_MOUNT", cni_mount),
                mock.patch.object(
                    cleanup, "mount_targets", return_value={str(cni_mount)}
                ),
            ):
                resources = cleanup.fixed_runtime_resources(
                    runner_temp, current_workdir
                )

            self.assertEqual(
                resources,
                cleanup.FixedRuntimeResources(old_workdir, False, True),
            )

    def test_rejects_marker_owner_outside_runner_temp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner_temp, _old_workdir, current_workdir = self.runtime_paths(root)
            cni_mount = root / "etc/conch/cni/net.d"
            cni_mount.mkdir(parents=True)
            (cni_mount / cleanup.CNI_RUNTIME_MARKER).write_text(
                "/tmp/conch-e2b-sdk-100-1\n", encoding="utf-8"
            )

            with (
                mock.patch.object(cleanup, "SDK_SOCKET", root / "missing/socket"),
                mock.patch.object(cleanup, "CNI_CONF_MOUNT", cni_mount),
                mock.patch.object(
                    cleanup, "mount_targets", return_value={str(cni_mount)}
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "supported layout"):
                    cleanup.fixed_runtime_resources(runner_temp, current_workdir)

    def test_cni_mount_recovery_revalidates_owner_before_unmount(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner_temp, old_workdir, _current_workdir = self.runtime_paths(root)
            cni_mount = root / "etc/conch/cni/net.d"
            resources = cleanup.FixedRuntimeResources(
                workdir=old_workdir,
                sdk_socket=False,
                cni_mount=True,
            )

            with (
                mock.patch.object(cleanup, "CNI_CONF_MOUNT", cni_mount),
                mock.patch.object(
                    cleanup, "cni_mount_owner", return_value=old_workdir
                ),
                mock.patch.object(cleanup, "mount_targets", return_value=set()),
                mock.patch.object(cleanup.subprocess, "run") as run,
            ):
                cleanup.remove_fixed_runtime_resources(resources, runner_temp)

            run.assert_called_once_with(
                ["umount", "--", str(cni_mount)], check=True
            )

    def test_recovery_failure_keeps_fixed_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner_temp, old_workdir, current_workdir = self.runtime_paths(root)
            resources = cleanup.FixedRuntimeResources(old_workdir, True, True)

            with (
                mock.patch.object(
                    cleanup, "fixed_runtime_resources", return_value=resources
                ),
                mock.patch.object(
                    cleanup, "cleanup", side_effect=RuntimeError("cleanup failed")
                ),
                mock.patch.object(
                    cleanup, "remove_fixed_runtime_resources"
                ) as remove_fixed,
            ):
                with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                    cleanup.recover_abandoned_runtime(
                        runner_temp,
                        current_workdir,
                        runner_temp / "bin",
                        runner_temp / "report.md",
                    )

            remove_fixed.assert_not_called()

    def test_main_reports_successful_abandoned_run_recovery(self) -> None:
        arguments = [
            "cleanup.py",
            "abandoned-run",
            "--runner-temp",
            "/tmp/runner",
            "--current-work-dir",
            "/tmp/runner/conch-e2b-sdk-101-1",
            "--binary-dir",
            "/tmp/conch-bin",
            "--report-file",
            "/tmp/conch-report.md",
        ]
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", arguments),
            mock.patch.object(
                cleanup, "recover_abandoned_runtime", return_value=True
            ),
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as raised,
        ):
            cleanup.main()

        self.assertEqual(raised.exception.code, cleanup.ABANDONED_RUN_EXIT_STATUS)
        self.assertIn("Recovered abandoned Conch CI runtime", output.getvalue())


if __name__ == "__main__":
    unittest.main()
