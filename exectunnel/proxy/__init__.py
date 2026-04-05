"""Proxy layer: SOCKS5 server, request handling, and UDP relay.

Implements the SOCKS5 wire protocol (RFC 1928 / RFC 1929) and exposes four
public types:

* :class:`Socks5ServerConfig` — immutable server configuration; validated at
  construction time before any socket is opened.
* :class:`Socks5Server`       — async SOCKS5 accept loop; yields negotiated
  requests via ``async for``.
* :class:`Socks5Request`      — one completed SOCKS5 handshake, ready for
  data relay.
* :class:`UdpRelay`           — UDP datagram relay for ``UDP_ASSOCIATE``
  sessions.  Exposed for type-checking by the session layer; the session layer
  should access relay instances via :attr:`Socks5Request.udp_relay` rather
  than constructing them directly.

Layer contract
--------------
**Knows about:** SOCKS5 wire protocol (RFC 1928 / RFC 1929), UDP relay,
``exectunnel.protocol`` enums (``AddrType``, ``AuthMethod``, ``Cmd``,
``Reply``), ``exectunnel.exceptions``.

**Does not know about:** WebSocket, frame encoding, session management,
``exectunnel.transport``, ``exectunnel.session``, ``exectunnel.config``,
``exectunnel.observability``, ``exectunnel._stream``.

Canonical usage
---------------
::

    from exectunnel.proxy import Socks5ServerConfig, Socks5Server

    async def run() -> None:
        cfg = Socks5ServerConfig(host="127.0.0.1", port=1080)
        async with Socks5Server(cfg) as server:
            async for req in server:
                asyncio.create_task(handle(req))
"""

from __future__ import annotations

from exectunnel.proxy.config import Socks5ServerConfig
from exectunnel.proxy.request import Socks5Request
from exectunnel.proxy.server import Socks5Server
from exectunnel.proxy.udp_relay import UdpRelay

__all__: list[str] = [
    "Socks5ServerConfig",
    "Socks5Request",
    "Socks5Server",
    "UdpRelay",
]
