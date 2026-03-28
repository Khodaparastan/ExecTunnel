"""
Backward-compat re-exports from protocol/ and utility functions.

All new code should import from exectunnel.protocol.frames,
exectunnel.protocol.ids, and use load_agent_b64/is_host_excluded from here
(or the future exectunnel.transport.utils).
"""
from __future__ import annotations

import base64
import importlib.resources
import ipaddress
import socket

from exectunnel.protocol.frames import (
    encode_conn_open_frame,
    encode_data_frame,
    encode_frame,
    encode_udp_close_frame,
    encode_udp_data_frame,
    encode_udp_open_frame,
    parse_frame,
)
from exectunnel.protocol.ids import new_conn_id, new_flow_id

__all__ = [
    "encode_conn_open_frame",
    "encode_data_frame",
    "encode_frame",
    "encode_udp_close_frame",
    "encode_udp_data_frame",
    "encode_udp_open_frame",
    "is_host_excluded",
    "load_agent_b64",
    "make_udp_socket",
    "new_conn_id",
    "new_flow_id",
    "parse_frame",
]


def load_agent_b64() -> str:
    """Load ``payload/agent.py`` from package resources and return as a base64 string."""
    pkg = importlib.resources.files("exectunnel")
    agent_bytes = (pkg / "payload/agent.py").read_bytes()
    return base64.b64encode(agent_bytes).decode()


def is_host_excluded(
    host: str, exclusions: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> bool:
    """Return True if *host* (an IP string) falls within any exclusion network."""
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in exclusions)
    except ValueError:
        return False  # domain names are never excluded — resolved remotely


def make_udp_socket(host: str) -> socket.socket:
    """Create a UDP socket with the correct address family for *host*."""
    try:
        addr = ipaddress.ip_address(host)
        family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
    except ValueError:
        family = socket.AF_INET  # default for domain names (unusual in excluded set)
    return socket.socket(family, socket.SOCK_DGRAM)