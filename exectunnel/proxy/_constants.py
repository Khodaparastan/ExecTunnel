"""Numeric tunables for the SOCKS5 proxy layer.

This module is the single source of truth for every proxy-layer magic
number. It re-imports :data:`exectunnel.protocol.constants.MAX_TCP_UDP_PORT`
rather than redefining it, so the valid port range is authoritative in
exactly one place.
"""

from __future__ import annotations

from typing import Final

from exectunnel.protocol.constants import (
    MAX_TCP_UDP_PORT as _PROTOCOL_MAX_TCP_UDP_PORT,
)
from exectunnel.protocol.constants import (
    MAX_UDP_DATA_PAYLOAD_BYTES as _PROTOCOL_MAX_UDP_DATA_PAYLOAD_BYTES,
)

__all__ = [
    "DEFAULT_DROP_WARN_INTERVAL",
    "DEFAULT_HANDSHAKE_TIMEOUT",
    "DEFAULT_HOST",
    "DEFAULT_LISTEN_BACKLOG",
    "DEFAULT_MAX_CONCURRENT_HANDSHAKES",
    "DEFAULT_PORT",
    "DEFAULT_QUEUE_PUT_TIMEOUT",
    "DEFAULT_REQUEST_QUEUE_CAPACITY",
    "DEFAULT_UDP_BIND_HOST",
    "DEFAULT_MAX_UDP_PAYLOAD_BYTES",
    "DEFAULT_UDP_QUEUE_CAPACITY",
    "DEFAULT_WRITER_CLOSE_TIMEOUT",
    "MAX_TCP_UDP_PORT",
    "MAX_UDP_PAYLOAD_BYTES",
    "MAX_TUNNEL_UDP_PAYLOAD_BYTES",
    "SOCKS5_VERSION",
]

#: Re-export of :data:`exectunnel.protocol.constants.MAX_TCP_UDP_PORT`.
MAX_TCP_UDP_PORT: Final[int] = _PROTOCOL_MAX_TCP_UDP_PORT

#: Maximum UDP payload size: ``65535 − 20 (IPv4 header) − 8 (UDP header)``.
MAX_UDP_PAYLOAD_BYTES: Final[int] = 65_507
MAX_TUNNEL_UDP_PAYLOAD_BYTES: Final[int] = min(
    MAX_UDP_PAYLOAD_BYTES, _PROTOCOL_MAX_UDP_DATA_PAYLOAD_BYTES
)
#: Default maximum UDP payload size.
DEFAULT_MAX_UDP_PAYLOAD_BYTES: Final[int] = MAX_TUNNEL_UDP_PAYLOAD_BYTES
#: SOCKS5 protocol version byte (RFC 1928 §3).
SOCKS5_VERSION: Final[int] = 0x05

DEFAULT_HANDSHAKE_TIMEOUT: Final[float] = 30.0
DEFAULT_REQUEST_QUEUE_CAPACITY: Final[int] = 256
DEFAULT_UDP_QUEUE_CAPACITY: Final[int] = 2_048
DEFAULT_DROP_WARN_INTERVAL: Final[int] = 1_000
DEFAULT_QUEUE_PUT_TIMEOUT: Final[float] = 5.0
DEFAULT_WRITER_CLOSE_TIMEOUT: Final[float] = 5.0
DEFAULT_LISTEN_BACKLOG: Final[int] = 512
DEFAULT_MAX_CONCURRENT_HANDSHAKES: Final[int] = 1_024
DEFAULT_HOST: Final[str] = "127.0.0.1"
DEFAULT_PORT: Final[int] = 1080
DEFAULT_UDP_BIND_HOST: Final[str] = "127.0.0.1"
