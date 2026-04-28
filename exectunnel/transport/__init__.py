"""``exectunnel.transport`` — frame I/O and connection lifecycle layer.

This package owns everything between the raw WebSocket send callable and
the local socket (TCP or UDP). It does not know about the SOCKS5 wire
protocol (``proxy`` layer), DNS resolution, or session orchestration
(``session`` layer).

Public surface
--------------
* :class:`WsSendCallable` — structural :class:`~typing.Protocol` for the
  injected send callable. Import this to annotate injection points in
  the session layer.
* :class:`TransportHandler` — structural :class:`~typing.Protocol`
  satisfied by both concrete handlers. Use this to type the session
  layer's handler registries without importing concrete classes.
* :class:`TcpConnection` — bridges one local TCP stream to one
  agent-side TCP connection via the WebSocket frame protocol.
* :class:`UdpFlow` — bridges one SOCKS5 UDP ASSOCIATE flow through the
  tunnel.
* :data:`MAX_DATA_CHUNK_BYTES` — authoritative upper bound on the raw
  byte count of a single ``DATA`` frame, derived from the protocol
  frame budget. Exposed so upstream producers can size their reads
  against one source of truth.

:data:`TcpRegistry` and :data:`UdpRegistry` type aliases are importable
from this package but are intentionally excluded from ``__all__``. The
preferred annotation style is the explicit ``dict[str, TcpConnection]``
/ ``dict[str, UdpFlow]`` form, which requires no additional import.

Internal module layout
----------------------
For maintenance, the package is split into focused sub-modules:

* :mod:`exectunnel.transport._constants` — numeric tunables.
* :mod:`exectunnel.transport._errors`    — structured task-exception
  logging.
* :mod:`exectunnel.transport._types`     — :class:`WsSendCallable`,
  :class:`TransportHandler`, registry aliases.
* :mod:`exectunnel.transport._validation` — :func:`require_bytes`.
* :mod:`exectunnel.transport._waiting`   — :func:`wait_first` helper.
* :mod:`exectunnel.transport.tcp`        — :class:`TcpConnection`.
* :mod:`exectunnel.transport.udp`        — :class:`UdpFlow`.

Callers must import exclusively from ``exectunnel.transport``; the
sub-module boundaries are an implementation detail and are not part of
the stability contract.
"""

from __future__ import annotations

from ._constants import MAX_DATA_CHUNK_BYTES, MAX_UDP_DATA_CHUNK_BYTES
from ._types import TcpRegistry, TransportHandler, UdpRegistry, WsSendCallable
from .tcp import TcpConnection
from .udp import UdpFlow

__all__ = [
    "MAX_DATA_CHUNK_BYTES",
    "MAX_UDP_DATA_CHUNK_BYTES",
    "TcpConnection",
    "TransportHandler",
    "UdpFlow",
    "WsSendCallable",
]
