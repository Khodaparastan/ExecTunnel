"""Shared numeric constants for the SOCKS5 proxy layer.

All values are pure data — no imports from other exectunnel sub-packages.
This module is the single source of truth for every proxy-layer magic number,
including ``MAX_UDP_PAYLOAD_BYTES`` which is used by both ``_wire`` and
``udp_relay``.
"""

from __future__ import annotations

# Maximum UDP payload accepted from the SOCKS5 client.
# 65507 = 65535 − 20 (IPv4 header) − 8 (UDP header).
MAX_UDP_PAYLOAD_BYTES: int = 65_507

# Default SOCKS5 handshake timeout in seconds.
DEFAULT_HANDSHAKE_TIMEOUT: float = 30.0

# Default capacity of the inbound datagram queue per UdpRelay instance.
# Per-server: max buffered, fully-negotiated SOCKS5 requests.
DEFAULT_REQUEST_QUEUE_CAPACITY: int = 256

# Per UdpRelay: max inbound datagrams before drop.
DEFAULT_UDP_QUEUE_CAPACITY: int = 2_048

# Log a warning every N drops to avoid log flooding on a saturated relay.
DEFAULT_DROP_WARN_INTERVAL: int = 1_000

# Maximum seconds to wait when enqueueing a completed handshake before
# dropping the connection with a GENERAL_FAILURE reply.
DEFAULT_QUEUE_PUT_TIMEOUT: float = 5.0

DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 1080