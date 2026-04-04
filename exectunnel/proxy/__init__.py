"""Proxy layer: SOCKS5 server, request handling, and UDP relay.

Implements the SOCKS5 wire protocol (RFC 1928 / RFC 1929) and exposes three
primary public types:

* :class:`Socks5Server`  — async SOCKS5 accept loop; enqueues negotiated requests.
* :class:`Socks5Request` — one completed SOCKS5 handshake, ready for data relay.
* :class:`UdpRelay`      — UDP datagram relay for ``UDP_ASSOCIATE`` sessions.

Layer contract
--------------
**Knows about:** SOCKS5 wire protocol, UDP relay, ``exectunnel.protocol`` enums,
``exectunnel.exceptions``.

**Does not know about:** WebSocket, frame encoding, session management,
``exectunnel.transport``, ``exectunnel.config``, ``exectunnel.observability``.
"""

from __future__ import annotations

from exectunnel.proxy.request import Socks5Request
from exectunnel.proxy.server import Socks5Server
from exectunnel.proxy.udp_relay import UdpRelay

__all__: list[str] = [
    "Socks5Request",
    "Socks5Server",
    "UdpRelay",
]
