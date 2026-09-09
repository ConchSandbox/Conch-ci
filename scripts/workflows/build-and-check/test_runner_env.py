#!/usr/bin/env python3
"""Portable regression tests; never install tools or invoke privileged commands."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/runner-env"))
import runner_env as env
from ids import kernel_build_id, rootfs_build_id
from lock import ValidationError, load_lock, validate_lock
from platforms import host_architecture, platform_receipt

UBUNTU = {"ID": "ubuntu", "VERSION_ID": "26.04", "PRETTY_NAME": "Ubuntu 26.04 LTS"}
OPENEULER = {
    "ID": "openEuler", "VERSION_ID": "24.03", "PRETTY_NAME": "openEuler 24.03 (LTS-SP3)",
}


class PlatformTests(unittest.TestCase):
    def test_supported_hosts(self):
        for machine, architecture, release in (
            ("aarch64", "arm64", OPENEULER), ("x86_64", "amd64", UBUNTU),
        ):
            with self.subTest(machine=machine), patch.object(
                os, "uname", return_value=SimpleNamespace(machine=machine)
            ), patch.object(env, "parse_os_release", return_value=release), patch.object(
                env.shutil, "which", return_value="/test/tool"
            ), patch.object(env.Path, "exists", return_value=True), patch.object(
                env.os, "access", return_value=True
            ), patch.object(env, "filesystem_available", return_value=True), patch.object(env, "run") as run:
                self.assertEqual(env.verify_baseline(), platform_receipt(architecture, release))
                run.assert_any_call(["docker", "info"], capture=True)
                run.assert_any_call(["sudo", "-n", "true"], capture=True)

    def test_unsupported_platforms_fail_before_commands(self):
        for machine, release in (("riscv64", UBUNTU), ("x86_64", {**UBUNTU, "VERSION_ID": "99"})):
            with self.subTest(machine=machine, release=release), patch.object(
                os, "uname", return_value=SimpleNamespace(machine=machine)
            ), patch.object(env, "parse_os_release", return_value=release), patch.object(env, "run") as run:
                with self.assertRaises(env.BaselineError):
                    env.verify_baseline()
                run.assert_not_called()

    def test_missing_docker_still_fails(self):
        with patch.object(env, "host_architecture", return_value="amd64"), patch.object(
            env, "parse_os_release", return_value=UBUNTU
        ), patch.object(env.shutil, "which", side_effect=lambda name: None if name == "docker" else "/tool"):
            with self.assertRaisesRegex(env.BaselineError, "missing host baseline command.*docker"):
                env.verify_baseline()

    def test_sudo_still_required(self):
        def run(command, **kwargs):
            if command == ["sudo", "-n", "true"]:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, "")

        with patch.object(env, "host_architecture", return_value="amd64"), patch.object(
            env, "parse_os_release", return_value=UBUNTU
        ), patch.object(env.shutil, "which", return_value="/tool"), patch.object(
            env.Path, "exists", return_value=True
        ), patch.object(env.os, "access", return_value=True), patch.object(
            env, "filesystem_available", return_value=True
        ), patch.object(env, "run", side_effect=run):
            with self.assertRaisesRegex(env.BaselineError, "passwordless sudo"):
                env.verify_baseline()

    def test_native_elf_rejects_other_architecture(self):
        for architecture, correct, incorrect in (
            ("amd64", "x86-64", "ARM aarch64"), ("arm64", "ARM aarch64", "x86-64"),
        ):
            with self.subTest(architecture=architecture), patch.object(env, "host_architecture", return_value=architecture):
                with patch.object(env, "run", return_value=SimpleNamespace(stdout=f"ELF 64-bit {correct}")):
                    env.validate_native_elf(Path("/test/executable"))
                with patch.object(env, "run", return_value=SimpleNamespace(stdout=f"ELF 64-bit {incorrect}")):
                    with self.assertRaises(env.RunnerEnvError):
                        env.validate_native_elf(Path("/test/executable"))


class LockTests(unittest.TestCase):
    def setUp(self):
        self.lock = load_lock(ROOT / "runner-env.lock.yaml")

    def test_checksums_and_download_urls_match_architecture(self):
        expected_suffixes = {
            "arm64": {
                "cloud_hypervisor": "cloud-hypervisor-static-aarch64",
                "buildkit": "buildkit-v0.32.0.linux-arm64.tar.gz",
                "distribution_registry": "registry_3.1.1_linux_arm64.tar.gz",
                "cni_plugins": "cni-plugins-linux-arm64-v1.9.1.tgz",
            },
            "amd64": {
                "cloud_hypervisor": "cloud-hypervisor-static",
                "buildkit": "buildkit-v0.32.0.linux-amd64.tar.gz",
                "distribution_registry": "registry_3.1.1_linux_amd64.tar.gz",
                "cni_plugins": "cni-plugins-linux-amd64-v1.9.1.tgz",
            },
        }
        for architecture, suffixes in expected_suffixes.items():
            with patch.object(env, "host_architecture", return_value=architecture):
                for component, suffix in suffixes.items():
                    item = self.lock["managed_components"][component]
                    self.assertTrue(env.source_url(component, item["version"]).endswith("/" + suffix))
                    self.assertTrue(env.archive_name(component, item["version"]).endswith(suffix))
                    self.assertEqual(env.download_sha256(component, item), item["sha256"][architecture])
                    self.assertNotEqual(item["sha256"]["arm64"], item["sha256"]["amd64"])
                erofs = self.lock["managed_components"]["erofs_utils"]
                self.assertEqual(env.download_sha256("erofs_utils", erofs), erofs["sha256"])

    def test_missing_unknown_and_invalid_architecture_checksums_rejected(self):
        for digest in ({"arm64": "a" * 64}, {"arm64": "a" * 64, "amd64": "bad"},
                       {"arm64": "a" * 64, "amd64": "b" * 64, "riscv64": "c" * 64}, "a" * 64):
            lock = copy.deepcopy(self.lock)
            lock["managed_components"]["buildkit"]["sha256"] = digest
            with self.subTest(digest=digest), self.assertRaises(ValidationError):
                validate_lock(lock)


class ArtifactTests(unittest.TestCase):
    def test_rootfs_manifest_selects_requested_architecture(self):
        script = (ROOT / "scripts/runner-env/jobs/ensure-rootfs.sh").read_text()
        validator = script.split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            for architecture in ("arm64", "amd64"):
                variables = {
                    "ROOTFS_MANIFEST": str(manifest_path), "ROOTFS_BUILD_ID": "a" * 64,
                    "ROOTFS_CONCH_COMMIT": "b" * 40, "ROOTFS_SOURCE_REPOSITORY": "https://github.com/ConchSandbox/Conch.git",
                    "ROOTFS_PLATFORM": f"linux/{architecture}", "ROOTFS_SCRIPT_SHA256": "c" * 64,
                    "ROOTFS_DOCKERFILE": "Dockerfile", "ROOTFS_INDEX_DIGEST": "sha256:" + "d" * 64,
                }
                annotations = dict(zip(
                    ("build-id", "conch-commit", "source-repository", "platform", "script-sha256", "dockerfile"),
                    (variables[key] for key in ("ROOTFS_BUILD_ID", "ROOTFS_CONCH_COMMIT", "ROOTFS_SOURCE_REPOSITORY",
                                               "ROOTFS_PLATFORM", "ROOTFS_SCRIPT_SHA256", "ROOTFS_DOCKERFILE")),
                ))
                descriptor = {"platform": {"os": "linux", "architecture": architecture}, "digest": "sha256:" + "e" * 64}
                manifest = {
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "annotations": {"io.conch.rootfs." + key: value for key, value in annotations.items()},
                    "manifests": [descriptor],
                }
                manifest_path.write_text(json.dumps(manifest))
                result = subprocess.run([sys.executable, "-c", validator], env={**os.environ, **variables}, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), descriptor["digest"])
                descriptor["platform"]["architecture"] = "amd64" if architecture == "arm64" else "arm64"
                manifest_path.write_text(json.dumps(manifest))
                result = subprocess.run([sys.executable, "-c", validator], env={**os.environ, **variables}, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("expected one linux/", result.stderr)

    def test_ids_separate_platforms_and_recipes(self):
        args = ("a" * 40, "b" * 64, "c" * 64)
        arm = kernel_build_id(*args, "arm64", "d" * 64)
        self.assertNotEqual(arm, kernel_build_id(*args, "amd64", "d" * 64))
        self.assertNotEqual(arm, kernel_build_id(*args, "arm64", "e" * 64))
        rootfs = ("a" * 40, "https://github.com/ConchSandbox/Conch.git", "b" * 64, "Dockerfile")
        self.assertNotEqual(rootfs_build_id("linux/arm64", *rootfs), rootfs_build_id("linux/amd64", *rootfs))

    def test_kernel_cache_metadata_rejects_wrong_platform(self):
        architecture = host_architecture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "conch"
            source.mkdir()
            config = source / "config/oe-kernel" / ("x86" if architecture == "amd64" else "aarch") / ".config"
            config.parent.mkdir(parents=True)
            config.write_text("CONFIG_X86_64=y\nCONFIG_PVH=y\n" if architecture == "amd64" else "CONFIG_ARM64=y\n")
            for command in (["git", "init", "-q", str(source)], ["git", "-C", str(source), "add", "."],
                            ["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                             "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"]):
                subprocess.run(command, check=True, capture_output=True)
            commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
            output = root / "outputs"
            process_env = {**os.environ, "GITHUB_OUTPUT": str(output), "PYTHONDONTWRITEBYTECODE": "1"}
            script = str(ROOT / "scripts/runner-env/jobs/build-kernel.sh")
            arguments = ["--conch-source", str(source), "--conch-commit", commit, "--cache-root", str(root / "cache")]
            subprocess.run([script, "plan", *arguments], env=process_env, check=True, capture_output=True)
            outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(outputs["kernel-platform"], architecture)
            self.assertTrue(outputs["kernel-artifact-name"].startswith(f"kernel-{architecture}-"))
            cache = Path(outputs["cache-dir"])
            cache.mkdir(parents=True)
            data = b"test kernel" * 110000
            for name in ("Image", "bzImage"):
                (cache / name).write_bytes(data)
            lock = load_lock(ROOT / "runner-env.lock.yaml")["job_build_inputs"]
            metadata = {
                "schema_version": 3, "build_id": outputs["kernel-build-id"],
                "source_commit": lock["kernel_commit"], "source_archive_sha256": lock["kernel_archive_sha256"],
                "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
                "recipe_sha256": hashlib.sha256(Path(script).read_bytes()).hexdigest(),
                "platform": architecture, "native_output": "bzImage" if architecture == "amd64" else "Image",
                "workflow_alias": "bzImage", "format": "x86_64 bzImage" if architecture == "amd64" else "ARM64 Image",
                "conch_commit": commit, "sha256": hashlib.sha256(data).hexdigest(),
            }
            (cache / "kernel-metadata.json").write_text(json.dumps(metadata))
            # Simulate file(1), leaving all digest and metadata validation real.
            tools = root / "tools"
            tools.mkdir()
            file_command = tools / "file"
            file_command.write_text("#!/bin/sh\necho 'Linux kernel x86 boot executable bzImage ARM64'\n")
            file_command.chmod(0o755)
            process_env["PATH"] = str(tools) + os.pathsep + os.environ["PATH"]
            subprocess.run([script, "ensure", *arguments, "--cache-hit", "true"], env=process_env, check=True, capture_output=True)
            metadata["platform"] = "arm64" if architecture == "amd64" else "amd64"
            (cache / "kernel-metadata.json").write_text(json.dumps(metadata))
            result = subprocess.run([script, "ensure", *arguments, "--cache-hit", "true"], env=process_env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("kernel metadata mismatch for platform", result.stderr)


if __name__ == "__main__":
    unittest.main()
