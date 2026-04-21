"""``exectunnel.transport`` — frame I/O and connection lifecycle layer.

This package owns everything between the raw WebSocket send callable and the
local socket (TCP or UDP). It does not know about the SOCKS5 wire protocol
(``proxy`` layer), DNS resolution, or session orchestration (``session`` layer).

Public surface
--------------
* :class:`WsSendCallable`   — structural ``Protocol`` for the injected send
  callable. Import this to annotate injection points in the session layer.
* :class:`TransportHandler` — structural ``Protocol`` satisfied by both
  concrete handlers. Use this to type the session layer's handler registries
  without importing concrete classes.
* :class:`TcpConnection`    — bridges one local TCP stream to one agent-side
  TCP connection via the WebSocket frame protocol.
* :class:`UdpFlow`          — bridges one SOCKS5 UDP ASSOCIATE flow through
  the tunnel.

``TcpRegistry`` and ``UdpRegistry`` type aliases are importable from this
package but are intentionally excluded from ``__all__``. The preferred
annotation style is the explicit ``dict[str, TcpConnection]`` /
``dict[str, UdpFlow]`` form, which requires no additional import.
"""

from __future__ import annotations

from ._types import TcpRegistry, TransportHandler, UdpRegistry, WsSendCallable
from .tcp import TcpConnection
from .udp import UdpFlow

__all__ = [
    "WsSendCallable",
    "TransportHandler",
    "TcpConnection",
    "UdpFlow",
]
