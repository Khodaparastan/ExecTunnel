"""Shared numeric constants for the SOCKS5 proxy layer.

All values are pure data — no imports from other exectunnel sub-packages.
This module is the single source of truth for every proxy-layer magic number,
including ``MAX_UDP_PAYLOAD_BYTES`` which is used by both ``_wire`` and
``udp_relay``.
"""

from __future__ import annotations

__all__: list[str] = [
    "DEFAULT_HANDSHAKE_TIMEOUT",
    "DEFAULT_QUEUE_CAPACITY",
    "DROP_WARN_INTERVAL",
    "LOOPBACK_ADDRS",
    "MAX_UDP_PAYLOAD_BYTES",
]

# Maximum UDP payload accepted from the SOCKS5 client.
# 65507 = 65535 − 20 (IPv4 header) − 8 (UDP header).
# Anything larger is physically impossible over standard IPv4/UDP.
MAX_UDP_PAYLOAD_BYTES: int = 65_507

# Default SOCKS5 handshake timeout in seconds.
DEFAULT_HANDSHAKE_TIMEOUT: float = 30.0

# Default capacity of the inbound datagram queue per UdpRelay instance.
DEFAULT_QUEUE_CAPACITY: int = 2_048

# Log a warning every N drops to avoid log flooding on a saturated relay.
DROP_WARN_INTERVAL: int = 100

# Addresses considered loopback — binding to anything else triggers a warning.
LOOPBACK_ADDRS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})
