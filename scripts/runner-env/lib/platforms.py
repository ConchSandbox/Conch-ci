"""Supported native runner platforms; no installation or host mutation."""

from __future__ import annotations

import os


ARCHITECTURES = ("arm64", "amd64")
OS_RELEASES = {
    ("openEuler", "24.03", "openEuler 24.03 (LTS-SP3)"),
}


def host_architecture() -> str:
    machine = os.uname().machine
    try:
        return {"aarch64": "arm64", "x86_64": "amd64"}[machine]
    except KeyError as exc:
        raise ValueError(f"unsupported architecture: {machine}") from exc


def platform_receipt(architecture: str, os_release: dict[str, str]) -> dict[str, str]:
    release = tuple(os_release.get(key, "") for key in ("ID", "VERSION_ID", "PRETTY_NAME"))
    supported_release = release[:2] == ("ubuntu", "26.04") or release in OS_RELEASES
    if architecture not in ARCHITECTURES or not supported_release:
        raise ValueError(f"unsupported runner platform: {architecture}, {release!r}")
    return dict(zip(
        ("architecture", "os_id", "os_version_id", "os_pretty_name"),
        (architecture, *release),
    ))


def same_platform(recorded: dict[str, str], current: dict[str, str]) -> bool:
    """Ubuntu's display name may change without changing its platform identity."""
    if recorded.get("os_id") == current.get("os_id") == "ubuntu":
        recorded = {key: value for key, value in recorded.items() if key != "os_pretty_name"}
        current = {key: value for key, value in current.items() if key != "os_pretty_name"}
    return recorded == current


if __name__ == "__main__":
    print(host_architecture())
