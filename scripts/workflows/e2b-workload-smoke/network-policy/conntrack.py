#!/usr/bin/env python3
"""Query replied IPv4 UDP conntrack entries in the current network namespace."""

from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import struct
import sys
import time


# Linux UAPI: netlink.h, nfnetlink_conntrack.h, nf_conntrack_common.h.
NETLINK_NETFILTER = 12
CT_NEW = 1 << 8
CT_GET = CT_NEW | 1
NLM_F_REQUEST = 1
NLM_F_DUMP = 0x300
NLM_F_DUMP_INTR = 0x10
NLMSG_NOOP = 1
NLMSG_ERROR = 2
NLMSG_DONE = 3
CTA_TUPLE_ORIG = 1
CTA_STATUS = 3
CTA_TUPLE_IP = 1
CTA_TUPLE_PROTO = 2
CTA_IP_V4_DST = 2
CTA_PROTO_NUM = 1
CTA_PROTO_SRC_PORT = 2
CTA_PROTO_DST_PORT = 3
IPS_SEEN_REPLY = 1 << 1
HEADER = struct.Struct("=IHHII")


def attributes(data: bytes) -> dict[int, bytes]:
    result = {}
    offset = 0
    while offset < len(data):
        if len(data) - offset < 4:
            raise ValueError("truncated conntrack attribute header")
        length, kind = struct.unpack_from("=HH", data, offset)
        if length < 4 or offset + length > len(data):
            raise ValueError("invalid conntrack attribute length")
        kind &= 0x3FFF  # Strip NLA_F_NESTED and NLA_F_NET_BYTEORDER.
        if kind in result:
            raise ValueError(f"duplicate conntrack attribute {kind}")
        result[kind] = data[offset + 4:offset + length]
        offset += (length + 3) & ~3
    return result


def field(values: dict[int, bytes], kind: int, size: int) -> bytes:
    value = values.get(kind)
    if value is None or len(value) != size:
        raise ValueError(f"missing or invalid conntrack attribute {kind}")
    return value


def matches_replied_udp(
    message: bytes, destination: bytes, source_port: int, destination_port: int
) -> bool:
    if len(message) < 4 or message[:2] != bytes((socket.AF_INET, 0)):
        raise ValueError("invalid IPv4 conntrack message")
    values = attributes(message[4:])
    try:
        original = attributes(values[CTA_TUPLE_ORIG])
        protocol = attributes(original[CTA_TUPLE_PROTO])
        addresses = attributes(original[CTA_TUPLE_IP])
    except KeyError as exc:
        raise ValueError("incomplete conntrack original tuple") from exc
    status = int.from_bytes(field(values, CTA_STATUS, 4), "big")
    if field(protocol, CTA_PROTO_NUM, 1) != bytes((socket.IPPROTO_UDP,)):
        return False
    # SEEN_REPLY is equivalent to the absence of procfs's UNREPLIED marker.
    # Replied UDP flows need not have the stronger ASSURED flag.
    return (
        bool(status & IPS_SEEN_REPLY)
        and field(addresses, CTA_IP_V4_DST, 4) == destination
        and int.from_bytes(field(protocol, CTA_PROTO_SRC_PORT, 2), "big") == source_port
        and int.from_bytes(field(protocol, CTA_PROTO_DST_PORT, 2), "big") == destination_port
    )


def check_error(payload: bytes) -> None:
    if len(payload) < 4:
        raise ValueError("truncated Netlink error")
    error = struct.unpack_from("=i", payload)[0]
    if error < 0:
        raise OSError(-error, os.strerror(-error))
    if error > 0:
        raise ValueError(f"invalid Netlink error code: {error}")


def has_replied_udp(
    destination: ipaddress.IPv4Address, source_port: int, destination_port: int
) -> bool:
    deadline = time.monotonic() + 5
    sequence = 1
    found = False
    # nsenter starts this process in the slot namespace before this socket opens.
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_NETFILTER) as channel:
        channel.settimeout(5)
        channel.bind((0, 0))
        payload = struct.pack("!BBH", socket.AF_INET, 0, 0)
        request = HEADER.pack(
            HEADER.size + len(payload), CT_GET, NLM_F_REQUEST | NLM_F_DUMP, sequence, 0
        ) + payload
        channel.sendto(request, (0, 0))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("conntrack Netlink dump timed out")
            channel.settimeout(remaining)
            data, _, receive_flags, sender = channel.recvmsg(1 << 20)
            if not data or receive_flags & socket.MSG_TRUNC or sender[0] != 0:
                raise ValueError("invalid or truncated conntrack Netlink response")
            offset = 0
            while offset < len(data):
                if len(data) - offset < HEADER.size:
                    raise ValueError("truncated Netlink header")
                length, kind, flags, reply_sequence, _ = HEADER.unpack_from(data, offset)
                if length < HEADER.size or offset + length > len(data):
                    raise ValueError("invalid Netlink message length")
                if reply_sequence != sequence or flags & NLM_F_DUMP_INTR:
                    raise ValueError("unexpected or interrupted conntrack Netlink dump")
                payload = data[offset + HEADER.size:offset + length]
                offset += (length + 3) & ~3
                if kind == NLMSG_DONE:
                    if payload:
                        check_error(payload)
                    # Consume the whole dump before accepting an absent tuple.
                    return found
                if kind == NLMSG_ERROR:
                    check_error(payload)
                elif kind == CT_NEW:
                    matched = matches_replied_udp(
                        payload, destination.packed, source_port, destination_port
                    )
                    found = found or matched
                elif kind != NLMSG_NOOP:
                    raise ValueError(f"unexpected conntrack Netlink message type: {kind}")


def port(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination-ip", type=ipaddress.IPv4Address, required=True)
    parser.add_argument("--source-port", type=port, required=True)
    parser.add_argument("--destination-port", type=port, required=True)
    args = parser.parse_args()
    try:
        found = has_replied_udp(args.destination_ip, args.source_port, args.destination_port)
    except (OSError, ValueError) as exc:
        print(f"conntrack query failed: {exc}", file=sys.stderr)
        return 1
    print("true" if found else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
