#!/usr/bin/env python3

import argparse
import ipaddress
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add a deterministic DNS nameserver to a Conch CNI config."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--nameserver", required=True)
    return parser.parse_args()


def normalized_nameserver(value: str) -> str:
    address = ipaddress.ip_address(value)
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError(f"DNS nameserver must be an IPv4 address: {value!r}")
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        raise ValueError(f"unsupported DNS nameserver address: {address}")
    return str(address)


def configure(config_path: Path, nameserver: str) -> None:
    if not config_path.is_file():
        raise FileNotFoundError(f"CNI config does not exist: {config_path}")

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

    config_path.write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    args = parse_args()
    configure(args.config, args.nameserver)


if __name__ == "__main__":
    main()
