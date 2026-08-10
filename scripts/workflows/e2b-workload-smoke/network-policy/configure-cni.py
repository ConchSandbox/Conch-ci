#!/usr/bin/env python3

import argparse
import ipaddress
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Configure deterministic DNS and refill control for Conch CNI."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--nameserver", required=True)
    parser.add_argument("--control-dir", required=True, type=Path)
    parser.add_argument("--control-plugin", required=True)
    parser.add_argument("--real-bridge", required=True, type=Path)
    return parser.parse_args()


def normalized_nameserver(value: str) -> str:
    address = ipaddress.ip_address(value)
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError(f"DNS nameserver must be an IPv4 address: {value!r}")
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        raise ValueError(f"unsupported DNS nameserver address: {address}")
    return str(address)


def configure(
    config_path: Path,
    nameserver: str,
    control_dir: Path,
    control_plugin: str,
    real_bridge: Path,
) -> None:
    if not config_path.is_file():
        raise FileNotFoundError(f"CNI config does not exist: {config_path}")
    if not control_dir.is_absolute() or not control_dir.is_dir():
        raise ValueError(f"CNI control directory must be an absolute directory: {control_dir}")
    if not real_bridge.is_absolute() or not real_bridge.is_file():
        raise ValueError(f"real CNI bridge must be an absolute file: {real_bridge}")
    if not control_plugin or "/" in control_plugin:
        raise ValueError(f"invalid CNI control plugin name: {control_plugin!r}")

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid CNI JSON in {config_path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"CNI config must contain a JSON object: {config_path}")

    dns = config.get("dns", {})
    if not isinstance(dns, dict):
        raise ValueError(f"CNI dns field must contain a JSON object: {config_path}")
    dns["nameservers"] = [normalized_nameserver(nameserver)]
    config["dns"] = dns
    config["type"] = control_plugin
    config["conchCIControlDir"] = str(control_dir)
    config["conchCIRealBridge"] = str(real_bridge)

    config_path.write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    args = parse_args()
    configure(
        args.config,
        args.nameserver,
        args.control_dir,
        args.control_plugin,
        args.real_bridge,
    )


if __name__ == "__main__":
    main()
