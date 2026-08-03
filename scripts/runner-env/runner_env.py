#!/usr/bin/env python3
"""Install and verify the Conch self-hosted runner environment."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from archive import ArchiveError, extract_selected  # noqa: E402
from ids import repository_environment_id  # noqa: E402
from lock import ValidationError, load_lock  # noqa: E402


EXIT_USAGE_OR_SCHEMA = 2
EXIT_BASELINE = 3
EXIT_DRIFT = 4
EXIT_INSTALL = 5
EXIT_LOCK_TIMEOUT = 6
LOCK_TIMEOUT_SECONDS = 300

LOCK_PATH = REPO_ROOT / "runner-env.lock.yaml"
EROFS_UTILS_BUILD_RECIPE = SCRIPT_DIR / "jobs/prepare-erofs-utils.sh"

COMPONENTS = ("buildkit", "cloud_hypervisor", "cni_plugins", "erofs_utils")
COMPONENT_FILES = {
    "cloud_hypervisor": ("bin/cloud-hypervisor",),
    "buildkit": ("bin/buildctl", "bin/buildkitd", "bin/buildkit-runc"),
    "erofs_utils": ("bin/mkfs.erofs",),
    "cni_plugins": ("bin/cni/bridge", "bin/cni/host-local", "bin/cni/loopback"),
}

REASON_ORDER = {
    "missing",
    "version-mismatch",
    "digest-mismatch",
    "mode-mismatch",
    "owner-mismatch",
    "configuration-mismatch",
    "build-recipe-mismatch",
    "runtime-mismatch",
    "dependency-declaration-mismatch",
}


class RunnerEnvError(RuntimeError):
    exit_code = EXIT_INSTALL


class BaselineError(RunnerEnvError):
    exit_code = EXIT_BASELINE


class DriftError(RunnerEnvError):
    exit_code = EXIT_DRIFT


class LockTimeoutError(RunnerEnvError):
    exit_code = EXIT_LOCK_TIMEOUT


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def erofs_utils_build_recipe_sha256() -> str:
    return sha256_file(EROFS_UTILS_BUILD_RECIPE)


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def parse_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw or raw.startswith("#"):
            continue
        if "=" not in raw:
            raise BaselineError(f"{path}:{line_number}: malformed os-release entry")
        key, raw_value = raw.split("=", 1)
        if key in values:
            raise BaselineError(f"{path}:{line_number}: duplicate os-release field {key}")
        if not key or not all(character.isupper() or character.isdigit() or character == "_" for character in key):
            raise BaselineError(f"{path}:{line_number}: invalid os-release field {key!r}")
        try:
            parsed = shlex.split(raw_value, posix=True)
        except ValueError as exc:
            raise BaselineError(f"{path}:{line_number}: invalid os-release value") from exc
        if len(parsed) != 1:
            raise BaselineError(f"{path}:{line_number}: invalid os-release scalar")
        values[key] = parsed[0]
    return values


def filesystem_available(name: str, path: Path = Path("/proc/filesystems")) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BaselineError(f"cannot read host filesystem capabilities from {path}: {exc}") from exc
    return any(fields and fields[-1] == name for fields in (line.split() for line in lines))


def verify_baseline() -> dict[str, str]:
    if os.uname().machine != "aarch64":
        raise BaselineError(f"unsupported architecture: {os.uname().machine}; Phase 1 requires aarch64")

    os_release = parse_os_release()
    expected = {
        "ID": "openEuler",
        "VERSION_ID": "24.03",
        "PRETTY_NAME": "openEuler 24.03 (LTS-SP3)",
    }
    for key, expected_value in expected.items():
        actual = os_release.get(key)
        if actual != expected_value:
            raise BaselineError(f"os-release {key}={actual!r}, expected {expected_value!r}")

    required_commands = (
        "autoconf",
        "automake",
        "awk",
        "bash",
        "bc",
        "bison",
        "cpio",
        "curl",
        "docker",
        "file",
        "find",
        "flex",
        "flock",
        "g++",
        "gcc",
        "git",
        "gzip",
        "install",
        "libtoolize",
        "make",
        "openssl",
        "pahole",
        "perl",
        "pkg-config",
        "python3",
        "rsync",
        "sed",
        "sha256sum",
        "tar",
        "xz",
    )
    missing = [name for name in required_commands if shutil.which(name) is None]
    if missing:
        raise BaselineError(f"missing host baseline command(s): {', '.join(missing)}")

    python_version = sys.version_info
    if python_version < (3, 10):
        raise BaselineError(f"python3 >= 3.10 is required, got {python_version.major}.{python_version.minor}")

    kvm = Path("/dev/kvm")
    if not kvm.exists() or not os.access(kvm, os.R_OK | os.W_OK):
        raise BaselineError("/dev/kvm must exist and be readable/writable by the runner user")

    if not filesystem_available("erofs"):
        raise BaselineError(
            "EROFS filesystem support is not loaded; machine initialization must load it "
            "(for example, with modprobe erofs)"
        )

    try:
        run(["docker", "info"], capture=True)
    except subprocess.CalledProcessError as exc:
        raise BaselineError(f"Docker daemon is unavailable to the runner user: {exc.stdout}") from exc
    try:
        run(["sudo", "-n", "true"], capture=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise BaselineError("passwordless sudo required by E2E runtime operations is unavailable") from exc
    admin_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    try:
        run(
            [
                "sudo",
                "-n",
                "env",
                f"PATH={admin_path}",
                "sh",
                "-c",
                "command -v ip >/dev/null && command -v iptables >/dev/null",
            ],
            capture=True,
        )
    except subprocess.CalledProcessError as exc:
        raise BaselineError(
            "host network commands ip and iptables must be available to E2E runtime operations"
        ) from exc

    for package in ("uuid", "liblz4", "libzstd", "zlib"):
        try:
            run(["pkg-config", "--exists", package], capture=True)
        except subprocess.CalledProcessError as exc:
            raise BaselineError(f"host build dependency unavailable through pkg-config: {package}") from exc

    return {
        "architecture": "arm64",
        "os_id": expected["ID"],
        "os_version_id": expected["VERSION_ID"],
        "os_pretty_name": expected["PRETTY_NAME"],
    }


def _reject_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for segment in path.parts[1:]:
        current /= segment
        if current.exists() or current.is_symlink():
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise BaselineError(f"managed path contains a symbolic link: {current}")


def tool_paths(*, create: bool) -> dict[str, Path]:
    raw = os.environ.get("RUNNER_TOOL_CACHE", "")
    if not raw:
        raise BaselineError("RUNNER_TOOL_CACHE must be set by the Actions runner")
    tool_cache = Path(raw)
    if not tool_cache.is_absolute():
        raise BaselineError("RUNNER_TOOL_CACHE must be an absolute path")
    normalized = Path(os.path.normpath(str(tool_cache)))
    # Actions runners may intentionally expose _work through a runner-owned
    # symlink. Resolve the injected tool-cache path before applying the
    # no-symlink rule to anything Conch-ci creates below it.
    resolved_candidate = normalized.resolve(strict=False)
    if resolved_candidate == Path("/") or resolved_candidate == Path.home():
        raise BaselineError(f"unsafe RUNNER_TOOL_CACHE: {resolved_candidate}")
    _reject_symlink_ancestors(resolved_candidate)
    if create:
        resolved_candidate.mkdir(parents=True, exist_ok=True)
        resolved_candidate = resolved_candidate.resolve(strict=True)
        if not os.access(resolved_candidate, os.R_OK | os.W_OK | os.X_OK):
            raise BaselineError(
                f"RUNNER_TOOL_CACHE is not writable by the runner user: {resolved_candidate}"
            )
    elif resolved_candidate.exists():
        resolved_candidate = resolved_candidate.resolve(strict=True)

    root = resolved_candidate / "conch-ci"
    paths = {
        "tool_cache": resolved_candidate,
        "root": root,
        "bin": root / "bin",
        "cache": root / "cache/downloads",
        "state_dir": root / "state",
        "state": root / "state/runner-env-state.json",
        "dirty": root / "state/runner-env-dirty.json",
        "locks": root / "locks",
        "lock": root / "locks/runner-env.lock",
        "staging": root / "staging",
    }
    if create:
        for key in ("root", "bin", "cache", "state_dir", "locks", "staging"):
            paths[key].mkdir(parents=True, exist_ok=True)
        os.chmod(paths["state_dir"], 0o700)
        os.chmod(paths["locks"], 0o700)
        _reject_symlink_ancestors(paths["root"])
    return paths


@contextlib.contextmanager
def local_lock(lock_path: Path, *, shared: bool, create: bool) -> Iterator[None]:
    if not lock_path.exists() and not create:
        raise DriftError(f"runner environment lock is missing: {lock_path}")
    flags = os.O_RDWR | (os.O_CREAT if create else 0)
    descriptor = os.open(lock_path, flags, 0o600)
    os.chmod(lock_path, 0o600)
    operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise LockTimeoutError(f"timed out waiting for runner environment lock: {lock_path}")
            time.sleep(0.25)
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def source_url(component: str, version: str) -> str:
    if component == "cloud_hypervisor":
        return (
            "https://github.com/ConchSandbox/cloud-hypervisor/releases/download/"
            f"{version}/cloud-hypervisor-static-aarch64"
        )
    if component == "buildkit":
        return (
            "https://github.com/moby/buildkit/releases/download/"
            f"{version}/buildkit-{version}.linux-arm64.tar.gz"
        )
    if component == "erofs_utils":
        clean = version.removeprefix("v")
        return (
            "https://git.kernel.org/pub/scm/linux/kernel/git/xiang/erofs-utils.git/snapshot/"
            f"erofs-utils-{clean}.tar.gz"
        )
    if component == "cni_plugins":
        return (
            "https://github.com/containernetworking/plugins/releases/download/"
            f"{version}/cni-plugins-linux-arm64-{version}.tgz"
        )
    raise RunnerEnvError(f"unknown managed component: {component}")


def archive_name(component: str, version: str) -> str:
    clean = version.replace("/", "_")
    suffix = {
        "cloud_hypervisor": "cloud-hypervisor-static-aarch64",
        "buildkit": f"buildkit-{version}.linux-arm64.tar.gz",
        "erofs_utils": f"erofs-utils-{version.removeprefix('v')}.tar.gz",
        "cni_plugins": f"cni-plugins-linux-arm64-{version}.tgz",
    }[component]
    return f"{component}-{clean}-{suffix}"


def download_verified(url: str, expected_sha256: str, destination: Path) -> Path:
    if destination.is_file() and sha256_file(destination) == expected_sha256:
        print(f"[runner-env] using verified download cache: {destination.name}")
        return destination
    if destination.exists():
        destination.unlink()

    temporary = destination.with_name(f".{destination.name}.part-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        run(
            [
                "curl",
                "--proto",
                "=https",
                "--tlsv1.2",
                "--fail",
                "--location",
                "--retry",
                "3",
                "--retry-all-errors",
                "--output",
                str(temporary),
                url,
            ]
        )
        actual = sha256_file(temporary)
        if actual != expected_sha256:
            raise RunnerEnvError(
                f"download digest mismatch for {url}: expected {expected_sha256}, got {actual}"
            )
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        return destination
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def component_smoke_commands(component: str, root: Path) -> list[list[str]]:
    return {
        "cloud_hypervisor": [[str(root / "bin/cloud-hypervisor"), "--version"]],
        "buildkit": [
            [str(root / "bin/buildctl"), "--version"],
            [str(root / "bin/buildkitd"), "--version"],
            [str(root / "bin/buildkit-runc"), "--version"],
        ],
        "erofs_utils": [[str(root / "bin/mkfs.erofs"), "--version"]],
        "cni_plugins": [[str(root / "bin/cni/bridge"), "--version"]],
    }[component]


def smoke_component(component: str, root: Path) -> str:
    output: list[str] = []
    for command in component_smoke_commands(component, root):
        try:
            result = run(command, capture=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise DriftError(f"{component} smoke test failed: {' '.join(command)}: {exc}") from exc
        text = (result.stdout or "").strip()
        output.append(text or Path(command[0]).name)
    return " | ".join(output)


def validate_arm64_elf(path: Path) -> None:
    result = run(["file", str(path)], capture=True)
    description = (result.stdout or "").strip()
    if "ELF 64-bit" not in description or "ARM aarch64" not in description:
        raise RunnerEnvError(f"expected ARM64 ELF executable at {path}: {description}")


def atomic_install(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestors(target.parent)
    temporary = target.with_name(f".{target.name}.new-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        os.chmod(temporary, 0o755)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def install_component(component: str, declaration: dict[str, str], paths: dict[str, Path]) -> None:
    version = declaration["version"]
    expected_sha256 = declaration["sha256"]
    url = source_url(component, version)
    archive = download_verified(
        url,
        expected_sha256,
        paths["cache"] / archive_name(component, version),
    )
    staging_root = Path(tempfile.mkdtemp(prefix=f"{component}-", dir=paths["staging"]))
    output_root = staging_root / "output"
    output_root.mkdir()
    try:
        if component == "cloud_hypervisor":
            output = output_root / "bin/cloud-hypervisor"
            output.parent.mkdir(parents=True)
            shutil.copyfile(archive, output)
            os.chmod(output, 0o755)
        elif component == "buildkit":
            extract_selected(
                archive,
                output_root / "bin",
                {
                    "bin/buildctl": "buildctl",
                    "bin/buildkitd": "buildkitd",
                    "bin/buildkit-runc": "buildkit-runc",
                },
            )
        elif component == "erofs_utils":
            run(
                [
                    str(SCRIPT_DIR / "jobs/prepare-erofs-utils.sh"),
                    "--archive",
                    str(archive),
                    "--work-dir",
                    str(staging_root / "source"),
                    "--prefix",
                    str(output_root),
                ]
            )
        elif component == "cni_plugins":
            extract_selected(
                archive,
                output_root / "bin/cni",
                {
                    "bridge": "bridge",
                    "host-local": "host-local",
                    "loopback": "loopback",
                },
            )
        else:
            raise RunnerEnvError(f"unknown component: {component}")

        for relative in COMPONENT_FILES[component]:
            staged = output_root / relative
            if not staged.is_file() or staged.is_symlink():
                raise RunnerEnvError(f"{component} did not stage a regular file: {relative}")
            if not os.access(staged, os.X_OK):
                raise RunnerEnvError(f"{component} did not stage an executable file: {relative}")
            if component != "erofs_utils":
                validate_arm64_elf(staged)
        if component != "erofs_utils":
            for command in component_smoke_commands(component, output_root):
                run(command)

        for relative in COMPONENT_FILES[component]:
            staged = output_root / relative
            atomic_install(staged, paths["root"] / relative)
    finally:
        shutil.rmtree(staging_root)


def expected_component_receipt(
    component: str,
    declaration: dict[str, str],
    paths: dict[str, Path],
) -> dict[str, Any]:
    files = []
    for relative in COMPONENT_FILES[component]:
        path = paths["root"] / relative
        metadata = path.stat()
        files.append(
            {
                "path": str(path),
                "sha256": sha256_file(path),
                "owner_uid": metadata.st_uid,
                "mode": stat.S_IMODE(metadata.st_mode),
            }
        )
    receipt = {
        "declared_version": declaration["version"],
        "version_output": smoke_component(component, paths["root"]),
        "source_url": source_url(component, declaration["version"]),
        "archive_sha256": declaration["sha256"],
        "files": files,
    }
    if component == "erofs_utils":
        receipt["build_recipe_sha256"] = erofs_utils_build_recipe_sha256()
    return receipt


def validate_state(value: Any) -> dict[str, Any]:
    expected_fields = {
        "schema_version",
        "environment_id",
        "platform",
        "install_root",
        "components",
        "last_changed_at",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ValidationError("state: invalid top-level fields")
    environment_id = value["environment_id"]
    if (
        value["schema_version"] != 1
        or isinstance(value["schema_version"], bool)
        or not isinstance(environment_id, str)
        or len(environment_id) != 64
        or any(character not in "0123456789abcdef" for character in environment_id)
    ):
        raise ValidationError("state: invalid schema version or environment ID")
    expected_platform = {
        "architecture": "arm64",
        "os_id": "openEuler",
        "os_version_id": "24.03",
        "os_pretty_name": "openEuler 24.03 (LTS-SP3)",
    }
    if value["platform"] != expected_platform or not isinstance(value["install_root"], str):
        raise ValidationError("state: invalid platform or install root")
    components = value["components"]
    if not isinstance(components, dict) or set(components) != set(COMPONENTS):
        raise ValidationError("state: invalid components")
    receipt_fields = {
        "declared_version",
        "version_output",
        "source_url",
        "archive_sha256",
        "files",
    }
    for name, receipt in components.items():
        expected_receipt_fields = set(receipt_fields)
        if name == "erofs_utils":
            expected_receipt_fields.add("build_recipe_sha256")
        if not isinstance(receipt, dict) or set(receipt) != expected_receipt_fields:
            raise ValidationError(f"state: invalid {name} receipt")
        if not isinstance(receipt["files"], list):
            raise ValidationError(f"state: invalid {name} file receipts")
        if name == "erofs_utils":
            recipe_sha256 = receipt["build_recipe_sha256"]
            if (
                not isinstance(recipe_sha256, str)
                or len(recipe_sha256) != 64
                or any(character not in "0123456789abcdef" for character in recipe_sha256)
            ):
                raise ValidationError("state: invalid erofs_utils build recipe digest")
    if not isinstance(value["last_changed_at"], str):
        raise ValidationError("state: invalid change timestamp")
    return value


def load_state(paths: dict[str, Path], *, strict: bool) -> dict[str, Any] | None:
    if not paths["state"].is_file():
        return None
    try:
        return validate_state(json.loads(paths["state"].read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        if strict:
            raise DriftError(f"invalid runner environment state receipt: {exc}") from exc
        print(f"[runner-env] warning: invalid state receipt will be rebuilt: {exc}", file=sys.stderr)
        return None


def inspect_component(
    component: str,
    declaration: dict[str, str],
    receipt: dict[str, Any] | None,
    paths: dict[str, Path],
) -> set[str]:
    if receipt is None:
        return {"missing"}
    reasons: set[str] = set()
    if receipt.get("declared_version") != declaration["version"]:
        reasons.add("version-mismatch")
    if receipt.get("archive_sha256") != declaration["sha256"]:
        reasons.add("digest-mismatch")
    if receipt.get("source_url") != source_url(component, declaration["version"]):
        reasons.add("configuration-mismatch")
    if (
        component == "erofs_utils"
        and receipt.get("build_recipe_sha256") != erofs_utils_build_recipe_sha256()
    ):
        reasons.add("build-recipe-mismatch")

    expected_paths = [str(paths["root"] / relative) for relative in COMPONENT_FILES[component]]
    file_receipts = receipt.get("files", [])
    by_path = {item.get("path"): item for item in file_receipts if isinstance(item, dict)}
    if sorted(by_path) != sorted(expected_paths):
        reasons.add("configuration-mismatch")
    for expected_path in expected_paths:
        file_receipt = by_path.get(expected_path)
        path = Path(expected_path)
        if file_receipt is None or not path.is_file() or path.is_symlink():
            reasons.add("missing")
            continue
        metadata = path.stat()
        if sha256_file(path) != file_receipt.get("sha256"):
            reasons.add("digest-mismatch")
        if stat.S_IMODE(metadata.st_mode) != 0o755 or file_receipt.get("mode") != 0o755:
            reasons.add("mode-mismatch")
        if metadata.st_uid != os.getuid() or file_receipt.get("owner_uid") != os.getuid():
            reasons.add("owner-mismatch")
    return reasons


def operation_for(component: str, reasons: set[str], *, status: str) -> dict[str, Any]:
    if "missing" in reasons:
        operation = "install"
    elif reasons & {"version-mismatch", "digest-mismatch", "build-recipe-mismatch"}:
        operation = "replace"
    elif reasons & {"mode-mismatch", "owner-mismatch"}:
        operation = "repair"
    else:
        operation = "reconfigure"
    unknown = reasons - REASON_ORDER
    if unknown:
        raise RunnerEnvError(f"internal error: unknown operation reason(s): {unknown}")
    return {
        "component": component,
        "operation": operation,
        "reasons": sorted(reasons),
        "status": status,
    }


def plan_operations(
    lock: dict[str, Any],
    environment_id: str,
    state: dict[str, Any] | None,
    paths: dict[str, Path],
    *,
    status: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    component_state = state.get("components", {}) if state else {}
    for component in COMPONENTS:
        reasons = inspect_component(
            component,
            lock["managed_components"][component],
            component_state.get(component),
            paths,
        )
        if reasons:
            operations.append(operation_for(component, reasons, status=status))
    if (
        state is not None
        and state.get("environment_id") != environment_id
        and not any(operation["component"] == "ci_dependency_metadata" for operation in operations)
    ):
        operations.append(
            operation_for(
                "ci_dependency_metadata",
                {"dependency-declaration-mismatch"},
                status=status,
            )
        )
    if paths["dirty"].exists() and not operations:
        operations.append(
            operation_for(
                "ci_dependency_metadata",
                {"runtime-mismatch"},
                status=status,
            )
        )
    return sorted(operations, key=lambda item: item["component"].encode("ascii"))


def canonical_operations(operations: list[dict[str, Any]]) -> str:
    return json.dumps(operations, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def atomic_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_dirty(paths: dict[str, Path], environment_id: str, operations: list[dict[str, Any]]) -> None:
    atomic_json(
        paths["dirty"],
        {
            "environment_id": environment_id,
            "operations": operations,
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
    )


def verify_unlocked(
    lock: dict[str, Any],
    environment_id: str,
    platform: dict[str, str],
    paths: dict[str, Path],
) -> dict[str, Any]:
    if paths["dirty"].exists():
        raise DriftError(
            f"runner environment has a dirty marker: {paths['dirty']}; run approved ensure"
        )
    state = load_state(paths, strict=True)
    if state is None:
        raise DriftError(f"runner environment state receipt is missing: {paths['state']}")
    if state["environment_id"] != environment_id:
        raise DriftError(
            "runner environment declaration changed: "
            f"state={state['environment_id']} expected={environment_id}; run approved ensure"
        )
    if state["platform"] != platform:
        raise DriftError(f"runner platform receipt mismatch: {state['platform']} != {platform}")
    if state["install_root"] != str(paths["root"]):
        raise DriftError(
            f"runner install root mismatch: {state['install_root']} != {paths['root']}"
        )
    for component in COMPONENTS:
        reasons = inspect_component(
            component,
            lock["managed_components"][component],
            state["components"].get(component),
            paths,
        )
        if reasons:
            raise DriftError(f"{component} drift detected: {', '.join(sorted(reasons))}")
        smoke_component(component, paths["root"])
    return state


def write_action_outputs(
    *,
    paths: dict[str, Path] | None,
) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    values = {
        "binary-dir": str(paths["bin"]) if paths else "",
        "cloud-hypervisor-path": str(paths["root"] / "bin/cloud-hypervisor") if paths else "",
    }
    with Path(output_path).open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise RunnerEnvError(f"action output contains a newline: {key}")
            stream.write(f"{key}={value}\n")


def write_summary(environment_id: str, changed: bool, operations: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with Path(summary_path).open("a", encoding="utf-8") as stream:
        stream.write("### Conch runner environment\n\n")
        stream.write(f"- Environment ID: `{environment_id}`\n")
        stream.write(f"- Persistent state changed: `{str(changed).lower()}`\n")
        stream.write(f"- Operations: `{operations}`\n")


def ensure(lock: dict[str, Any], environment_id: str) -> None:
    platform = verify_baseline()
    paths = tool_paths(create=True)

    with local_lock(paths["lock"], shared=False, create=True):
        state = load_state(paths, strict=False)
        operations = plan_operations(
            lock,
            environment_id,
            state,
            paths,
            status="executed",
        )

        if not operations:
            operations_json = "[]"
            print(f"[runner-env] environment-id={environment_id}")
            print("[runner-env] all managed components are unchanged")
            write_action_outputs(
                paths=paths,
            )
            write_summary(environment_id, False, operations_json)
            return

        write_dirty(paths, environment_id, operations)
        for operation in operations:
            component = operation["component"]
            if component == "ci_dependency_metadata":
                continue
            print(
                f"[runner-env] {operation['operation']} {component}: "
                f"{','.join(operation['reasons'])}"
            )
            if operation["operation"] in {"install", "replace"}:
                install_component(component, lock["managed_components"][component], paths)
            elif operation["operation"] == "repair":
                for relative in COMPONENT_FILES[component]:
                    target = paths["root"] / relative
                    if target.exists() and target.stat().st_uid == os.getuid():
                        os.chmod(target, 0o755)
                    else:
                        install_component(component, lock["managed_components"][component], paths)
                        break

        components = {
            component: expected_component_receipt(
                component,
                lock["managed_components"][component],
                paths,
            )
            for component in COMPONENTS
        }
        new_state = {
            "schema_version": 1,
            "environment_id": environment_id,
            "platform": platform,
            "install_root": str(paths["root"]),
            "components": components,
            "last_changed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        validate_state(new_state)
        atomic_json(paths["state"], new_state)
        paths["dirty"].unlink()
        verify_unlocked(lock, environment_id, platform, paths)

    operations_json = canonical_operations(operations)
    changed = bool(operations)
    print(f"[runner-env] environment-id={environment_id}")
    print(f"[runner-env] operations={operations_json}")
    write_action_outputs(
        paths=paths,
    )
    write_summary(environment_id, changed, operations_json)


def verify_command(lock: dict[str, Any], environment_id: str) -> None:
    platform = verify_baseline()
    paths = tool_paths(create=False)
    if not paths["root"].is_dir():
        raise DriftError(f"runner environment is not installed: {paths['root']}")
    with local_lock(paths["lock"], shared=True, create=False):
        verify_unlocked(lock, environment_id, platform, paths)
    print(f"[runner-env] environment-id={environment_id}")
    print("[runner-env] verification succeeded")
    write_action_outputs(
        paths=paths,
    )
    write_summary(environment_id, False, "[]")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the locked Conch self-hosted runner environment.",
        epilog=(
            "Exit codes: 2 usage/schema, 3 host baseline, 4 environment drift, "
            "5 install/runtime failure, 6 lock timeout."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("ensure")
    subparsers.add_parser("verify")
    subparsers.add_parser("print-id")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        lock = load_lock(LOCK_PATH)
        environment_id = repository_environment_id(REPO_ROOT)
        if args.command == "print-id":
            print(environment_id)
            write_action_outputs(
                paths=None,
            )
        elif args.command == "ensure":
            ensure(lock, environment_id)
        elif args.command == "verify":
            verify_command(lock, environment_id)
        else:
            parser.error(f"unsupported command: {args.command}")
        return 0
    except ValidationError as exc:
        print(f"runner-env schema error: {exc}", file=sys.stderr)
        return EXIT_USAGE_OR_SCHEMA
    except RunnerEnvError as exc:
        print(f"runner-env error: {exc}", file=sys.stderr)
        return exc.exit_code
    except subprocess.CalledProcessError as exc:
        print(f"runner-env command failed ({exc.returncode}): {exc.cmd}", file=sys.stderr)
        if exc.stdout:
            print(exc.stdout, file=sys.stderr)
        return EXIT_INSTALL
    except (OSError, json.JSONDecodeError, ArchiveError) as exc:
        print(f"runner-env failure: {exc}", file=sys.stderr)
        return EXIT_INSTALL


if __name__ == "__main__":
    raise SystemExit(main())
