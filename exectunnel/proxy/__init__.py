"""Proxy layer: SOCKS5 server, request handling, and UDP relay.

This package implements the SOCKS5 wire protocol (RFC 1928) and exposes three
primary public types:

* :class:`Socks5Server` — async SOCKS5 accept loop that yields requests.
* :class:`Socks5Request` — represents a single negotiated SOCKS5 handshake.
* :class:`UdpRelay` — UDP datagram relay for ``UDP_ASSOCIATE`` sessions.

Layer contract:
    **Knows about:** SOCKS5 wire protocol, UDP relay, protocol enums.
    **Does not know about:** WebSocket, frame encoding, session management.
"""

from exectunnel.proxy.request import Socks5Request
from exectunnel.proxy.server import Socks5Server
from exectunnel.proxy.udp_relay import UdpRelay

__all__ = [
    "Socks5Request",
    "Socks5Server",
    "UdpRelay",
]
