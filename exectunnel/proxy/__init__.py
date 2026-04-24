"""ExecTunnel SOCKS5 proxy layer.

This package implements the SOCKS5 server (RFC 1928) that accepts local
client connections, negotiates the handshake, and yields completed
:class:`TCPRelay` objects to the session layer for tunnel relay.

Typical usage::

    from exectunnel.proxy import Socks5Server, Socks5ServerConfig

    async with Socks5Server(Socks5ServerConfig(port=1080)) as server:
        async for request in server:
            asyncio.create_task(session_layer.handle(request))

Public surface
--------------
* :class:`Socks5Server` — the async accept loop.
* :class:`Socks5ServerConfig` — immutable, self-validating configuration.
* :class:`TCPRelay` — one completed SOCKS5 handshake, ready for relay.
* :class:`UDPRelay` — per-session UDP relay bound on loopback.

Internal module layout
----------------------
For maintenance, the package is split into focused sub-modules:

* :mod:`exectunnel.proxy._constants`    — numeric tunables.
* :mod:`exectunnel.proxy._wire`         — pure, sync SOCKS5 wire codec.
* :mod:`exectunnel.proxy._io`           — async stream helpers.
* :mod:`exectunnel.proxy._udp_protocol` — asyncio datagram adapter.
* :mod:`exectunnel.proxy._handshake`    — SOCKS5 negotiation state machine.
* :mod:`exectunnel.proxy._errors`       — handshake exception dispatch.
* :mod:`exectunnel.proxy.config`        — :class:`Socks5ServerConfig`.
* :mod:`exectunnel.proxy.tcp_relay`     — :class:`TCPRelay`.
* :mod:`exectunnel.proxy.udp_relay`     — :class:`UDPRelay`.
* :mod:`exectunnel.proxy.server`        — :class:`Socks5Server`.

Callers must import exclusively from ``exectunnel.proxy``; the
sub-module boundaries are an implementation detail and are not part of
the stability contract.
"""

from __future__ import annotations

from .config import Socks5ServerConfig
from .server import Socks5Server
from .tcp_relay import TCPRelay
from .udp_relay import UDPRelay

__all__ = [
    "Socks5Server",
    "Socks5ServerConfig",
    "TCPRelay",
    "UDPRelay",
]
