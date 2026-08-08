#!/usr/bin/env python3
"""Initialize conch-init through the Cloud Hypervisor vsock proxy."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import struct
import time
from typing import Any


PROTOCOL_VERSION = 1
MAX_PAYLOAD_SIZE = 16 << 10
ATTEMPT_TIMEOUT_SECONDS = 5
READY_TIMEOUT_SECONDS = 120
RETRY_SECONDS = 1


class InitRejected(RuntimeError):
    """Raised when conch-init reports a non-retryable initialization error."""


def required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is required")
    return value


def build_init_request() -> dict[str, Any]:
    guest_ip = ipaddress.ip_address(required_env("CONCH_INIT_GUEST_IP"))
    gateway_interface = ipaddress.ip_interface(required_env("CONCH_INIT_NET_CIDR"))
    if guest_ip.version != 4 or gateway_interface.version != 4:
        raise ValueError("conch-init smoke network must use IPv4")
    if guest_ip not in gateway_interface.network:
        raise ValueError(
            f"guest IP {guest_ip} is outside gateway network "
            f"{gateway_interface.network}"
        )
    if guest_ip == gateway_interface.ip:
        raise ValueError("guest IP and gateway must be different")

    return {
        "version": PROTOCOL_VERSION,
        "sandboxID": required_env("CONCH_INIT_SANDBOX_ID"),
        "agentToken": required_env("CONCH_INIT_TOKEN"),
        "network": {
            "guestIP": str(guest_ip),
            "prefixLength": gateway_interface.network.prefixlen,
            "gateway": str(gateway_interface.ip),
            "dns": {},
        },
    }


def encode_frame(value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, separators=(",", ":")).encode()
    if not 1 <= len(payload) <= MAX_PAYLOAD_SIZE:
        raise ValueError(
            f"frame payload is {len(payload)} bytes, maximum is {MAX_PAYLOAD_SIZE}"
        )
    return struct.pack(">I", len(payload)) + payload


def receive_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError(f"connection closed with {remaining} bytes left to read")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def receive_frame(sock: socket.socket) -> dict[str, Any]:
    (size,) = struct.unpack(">I", receive_exact(sock, 4))
    if not 1 <= size <= MAX_PAYLOAD_SIZE:
        raise ValueError(
            f"frame payload size {size} is outside [1, {MAX_PAYLOAD_SIZE}]"
        )
    response = json.loads(receive_exact(sock, size))
    if not isinstance(response, dict):
        raise ValueError("initialization response must be a JSON object")
    return response


def exchange_init(request: dict[str, Any]) -> None:
    socket_path = required_env("CONCH_INIT_VSOCK_SOCKET")
    port = int(required_env("CONCH_INIT_VSOCK_PORT"))
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid vsock port: {port}")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(ATTEMPT_TIMEOUT_SECONDS)
        sock.connect(socket_path)
        sock.sendall(f"CONNECT {port}\n".encode())
        proxy_response = sock.recv(64)
        if b"OK" not in proxy_response:
            raise RuntimeError(
                f"VMM vsock proxy did not acknowledge CONNECT: {proxy_response!r}"
            )

        sock.sendall(encode_frame(request))
        response = receive_frame(sock)

    print(json.dumps(response, sort_keys=True))
    if response.get("version") != PROTOCOL_VERSION:
        raise RuntimeError(
            f"agent protocol version {response.get('version')!r}, "
            f"want {PROTOCOL_VERSION}"
        )
    if response.get("status") == "ready":
        return

    detail = (
        f"agent reported {response.get('status')!r}: "
        f"{response.get('errorCode', '')} {response.get('message', '')}"
    ).rstrip()
    if response.get("retryable") is not True:
        raise InitRejected(detail)
    raise RuntimeError(detail)


def main() -> None:
    request = build_init_request()
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            exchange_init(request)
            return
        except InitRejected as exc:
            raise SystemExit(f"conch-init rejected initialization: {exc}") from exc
        except Exception as exc:
            last_error = exc
        time.sleep(RETRY_SECONDS)

    raise SystemExit(f"timed out waiting for conch-init READY: {last_error}")


if __name__ == "__main__":
    main()
