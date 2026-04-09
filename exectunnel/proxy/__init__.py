"""Proxy layer: SOCKS5 server, request handling, and UDP relay.

Public types:

* :class:`Socks5ServerConfig` — immutable validated config.
* :class:`Socks5Server`       — async accept loop yielding requests.
* :class:`Socks5Request`      — one completed SOCKS5 handshake.
* :class:`UdpRelay`           — UDP datagram relay for ``UDP_ASSOCIATE``.
"""

from __future__ import annotations

from .config import Socks5ServerConfig
from .request import Socks5Request
from .server import Socks5Server
from .udp_relay import UdpRelay

__all__: list[str] = [
    "Socks5ServerConfig",
    "Socks5Request",
    "Socks5Server",
    "UdpRelay",
]
