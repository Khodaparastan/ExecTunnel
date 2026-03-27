"""
Shared low-level utilities: frame encoding/decoding, connection IDs,
and agent-script loading.
"""

from __future__ import annotations

import base64
import importlib.resources
import ipaddress
import secrets
import socket

from exectunnel.core.consts import FRAME_PREFIX, FRAME_SUFFIX

# ── Connection / flow ID ──────────────────────────────────────────────────────


def new_conn_id() -> str:
    """Generate a unique TCP connection ID (e.g. ``c3f9a1``)."""
    return "c" + secrets.token_hex(3)


def new_flow_id() -> str:
    """Generate a unique UDP flow ID (e.g. ``ua1b2c3``)."""
    return "u" + secrets.token_hex(3)


# ── Frame codec ───────────────────────────────────────────────────────────────


def encode_frame(msg_type: str, conn_id: str, payload: str = "") -> str:
    """
    Encode a single protocol frame.

    Returns a newline-terminated string ready to be sent over the wire.
    """
    if payload:
        return f"{FRAME_PREFIX}{msg_type}:{conn_id}:{payload}{FRAME_SUFFIX}\n"
    return f"{FRAME_PREFIX}{msg_type}:{conn_id}{FRAME_SUFFIX}\n"


def encode_conn_open_frame(conn_id: str, host: str, port: int) -> str:
    """Encode a CONN_OPEN frame with host/port payload.

    IPv6 addresses are embedded as-is (e.g. ``2001:db8::1:8080``).  The agent
    parses the payload with ``rpartition(":")`` so the last colon always
    separates the port — both sides must use ``rpartition`` for this to work.
    """
    return encode_frame("CONN_OPEN", conn_id, f"{host}:{port}")


def encode_udp_open_frame(flow_id: str, host: str, port: int) -> str:
    """Encode a UDP_OPEN frame with host/port payload.

    Same IPv6 / ``rpartition`` convention as :func:`encode_conn_open_frame`.
    """
    return encode_frame("UDP_OPEN", flow_id, f"{host}:{port}")


def encode_udp_data_frame(flow_id: str, payload_b64: str) -> str:
    """Encode a UDP_DATA frame from a base64 payload string."""
    return encode_frame("UDP_DATA", flow_id, payload_b64)


def encode_udp_close_frame(flow_id: str) -> str:
    """Encode a UDP_CLOSE frame."""
    return encode_frame("UDP_CLOSE", flow_id)


def parse_frame(line: str) -> tuple[str, str, str] | None:
    """
    Parse one line into ``(msg_type, conn_id, payload)`` or return ``None``.

    ``payload`` is an empty string when absent.  The function is intentionally
    lenient — unrecognised frames are silently dropped by callers.
    """
    line = line.strip()
    if not (line.startswith(FRAME_PREFIX) and line.endswith(FRAME_SUFFIX)):
        return None
    inner = line[len(FRAME_PREFIX): -len(FRAME_SUFFIX)]
    # Split into at most 3 parts so base64 payloads with ":" are not truncated.
    parts = inner.split(":", 2)
    msg_type = parts[0]
    conn_id = parts[1] if len(parts) > 1 else ""
    payload = parts[2] if len(parts) > 2 else ""
    return msg_type, conn_id, payload


# ── Agent payload loader ──────────────────────────────────────────────────────


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
