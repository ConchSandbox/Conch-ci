"""Supported native runner platforms; no installation or host mutation."""

from __future__ import annotations

import os


ARCHITECTURES = ("arm64", "amd64")
OS_RELEASES = {
    ("openEuler", "24.03", "openEuler 24.03 (LTS-SP3)"),
    ("ubuntu", "26.04", "Ubuntu 26.04 LTS"),
}


def host_architecture() -> str:
    machine = os.uname().machine
    try:
        return {"aarch64": "arm64", "x86_64": "amd64"}[machine]
    except KeyError as exc:
        raise ValueError(f"unsupported architecture: {machine}") from exc


def platform_receipt(architecture: str, os_release: dict[str, str]) -> dict[str, str]:
    release = tuple(os_release.get(key, "") for key in ("ID", "VERSION_ID", "PRETTY_NAME"))
    if architecture not in ARCHITECTURES or release not in OS_RELEASES:
        raise ValueError(f"unsupported runner platform: {architecture}, {release!r}")
    return dict(zip(
        ("architecture", "os_id", "os_version_id", "os_pretty_name"),
        (architecture, *release),
    ))


if __name__ == "__main__":
    print(host_architecture())
