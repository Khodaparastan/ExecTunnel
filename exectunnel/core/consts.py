from __future__ import annotations

import ipaddress
from enum import IntEnum

# ── Frame protocol ────────────────────────────────────────────────────────────

FRAME_PREFIX = "<<<EXECTUNNEL:"
FRAME_SUFFIX = ">>>"
READY_FRAME = "<<<EXECTUNNEL:AGENT_READY>>>"

# Maximum queued frames per connection before back-pressure kicks in.
QUEUE_CAP = 256

# Size of base64-encoded chunks sent during bootstrap.
# 200 chars is safely under most shell input-buffer limits (512 bytes POSIX
# minimum) and avoids splitting multi-byte UTF-8 sequences in the b64 alphabet.
BOOTSTRAP_CHUNK_SIZE = 200

# Read chunk size used when piping raw TCP streams (CONNECT relay).
PIPE_CHUNK_SIZE = 4096

# ── SOCKS5 enums ──────────────────────────────────────────────────────────────


class AuthMethod(IntEnum):
    NO_AUTH = 0x00
    NO_ACCEPT = 0xFF


class Cmd(IntEnum):
    CONNECT = 0x01
    BIND = 0x02
    UDP_ASSOCIATE = 0x03


class AddrType(IntEnum):
    IPV4 = 0x01
    DOMAIN = 0x03
    IPV6 = 0x04


class Reply(IntEnum):
    SUCCESS = 0x00
    GENERAL_FAILURE = 0x01
    NOT_ALLOWED = 0x02
    NET_UNREACHABLE = 0x03
    HOST_UNREACHABLE = 0x04
    REFUSED = 0x05
    CMD_NOT_SUPPORTED = 0x07
    ADDR_NOT_SUPPORTED = 0x08


# ── Default tunnel exclusions (RFC 1918 + loopback) ───────────────────────────
# Stored as plain strings so callers choose how to parse them.
# Use ``default_exclusion_networks()`` to get parsed IPv4Network objects.

DEFAULT_EXCLUDE_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
)


def default_exclusion_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Return the default RFC1918 + loopback exclusion list as network objects."""
    return [ipaddress.ip_network(cidr, strict=False) for cidr in DEFAULT_EXCLUDE_CIDRS]


# Legacy alias kept so existing internal imports don't break during the
# refactor; prefer ``default_exclusion_networks()`` in new code.
_DEFAULT_EXCLUDE = [
    ipaddress.ip_network(cidr, strict=False) for cidr in DEFAULT_EXCLUDE_CIDRS
]

# ── Connection / ACK hardening ────────────────────────────────────────────────

ACK_TIMEOUT_WARN_EVERY = 10
ACK_TIMEOUT_WINDOW_SECS = 60.0
ACK_TIMEOUT_RECONNECT_THRESHOLD = 10
CONNECT_MAX_PENDING = 128
CONNECT_MAX_PENDING_PER_HOST = 16
CONNECT_MAX_PENDING_CF = 1
PRE_ACK_BUFFER_CAP_BYTES = 256 * 1024
CONNECT_FAILURE_WARN_EVERY = 10
CONNECT_PACE_CF_MS = 120
CONNECT_PACE_JITTER_CAP_SECS = 0.02
UDP_QUEUE_CAP = 256
UDP_WARN_EVERY = 100
UDP_PUMP_POLL_TIMEOUT_SECS = 1.0
UDP_DIRECT_RECV_TIMEOUT_SECS = 2.0
HANDSHAKE_TIMEOUT_SECS = 10.0

# ── Bridge / WebSocket defaults ───────────────────────────────────────────────

WS_PING_INTERVAL = 30
WS_PING_TIMEOUT = 10
WS_SEND_TIMEOUT_SECS = 30.0
WS_SEND_QUEUE_CAP = 512
WS_RECONNECT_MAX_RETRIES = 5
WS_RECONNECT_BASE_DELAY_SECS = 1.0
WS_RECONNECT_MAX_DELAY_SECS = 30.0

# ── SOCKS5 / tunnel defaults ──────────────────────────────────────────────────

SOCKS_DEFAULT_HOST = "127.0.0.1"
SOCKS_DEFAULT_PORT = 1080
DNS_LOCAL_PORT = 5300
READY_TIMEOUT_SECS = 15.0
CONN_ACK_TIMEOUT_SECS = 30.0

# ── DNS ───────────────────────────────────────────────────────────────────────

DNS_UPSTREAM_PORT = 53
DNS_QUERY_TIMEOUT_SECS = 5.0
DNS_MAX_INFLIGHT = 256

# ── Bootstrap timing ─────────────────────────────────────────────────────────

BOOTSTRAP_STTY_DELAY_SECS = 0.2
BOOTSTRAP_RM_DELAY_SECS = 0.05
BOOTSTRAP_DECODE_DELAY_SECS = 0.1
BOOTSTRAP_DIAG_MAX_LINES = 20

# ── Send / metrics ────────────────────────────────────────────────────────────

SEND_DROP_LOG_EVERY = 100
METRICS_REPORT_INTERVAL_SECS = 60.0

# ── WebSocket ─────────────────────────────────────────────────────────────────

WS_CLOSE_CODE_UNHEALTHY = 1011
