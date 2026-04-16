"""
``exectunnel.transport`` — frame I/O and connection lifecycle layer.

Responsibility
--------------
This package owns everything between the raw WebSocket send callable and the
local socket (TCP or UDP).  It does **not** know about:

* The SOCKS5 wire protocol (that is the ``proxy`` layer's concern).
* DNS resolution or session orchestration (that is the ``session`` layer).
* Frame encoding details beyond calling the typed helpers from
  :mod:`exectunnel.protocol` (always imported from the package root).

Public surface
--------------

.. list-table::
   :widths: 30 70

   * - :class:`WsSendCallable`
     - Structural ``Protocol`` for the WebSocket send callable injected into
       both handlers.  Import this to type-annotate injection points in the
       ``session`` layer.
   * - :class:`TransportHandler`
     - Structural ``Protocol`` satisfied by both :class:`TcpConnection` and
       :class:`UdpFlow`.  Use this to type the session layer's handler
       registries without importing concrete classes.
   * - :class:`TcpConnection`
     - Bridges one local TCP stream to one agent-side TCP connection via the
       WebSocket frame protocol.  Owns upstream/downstream copy tasks,
       pre-ACK buffering, half-close semantics, and cleanup.
   * - :class:`UdpFlow`
     - Bridges one SOCKS5 UDP ASSOCIATE flow through the tunnel.  Owns
       inbound queue, open/close handshake, and datagram send/recv.

Registry type aliases
---------------------
:data:`TcpRegistry` and :data:`UdpRegistry` are importable from this package
for use at session-layer wiring time when a named alias improves readability.
They are not listed in ``__all__`` because the preferred annotation style at
most call sites is the explicit ``dict[str, TcpConnection]`` /
``dict[str, UdpFlow]`` form, which is more immediately readable and does not
require an additional import.

Internal modules (not part of the public API)
---------------------------------------------
* ``_types.py``      — shared ``Protocol`` types and registry type aliases.
* ``_validation.py`` — payload validation utilities shared across handlers.
* ``tcp.py``         — :class:`TcpConnection` implementation.
* ``udp.py``         — :class:`UdpFlow` implementation.

Layer contract
--------------
The ``session`` layer is the only permitted consumer of this package's public
surface.  No other layer should import from ``exectunnel.transport`` directly.
"""

from __future__ import annotations

from ._types import (
    TcpRegistry,
    TransportHandler,
    UdpRegistry,
    WsSendCallable,
)
from .tcp import TcpConnection
from .udp import UdpFlow

__all__ = [
    # Protocols — import these to annotate injection points and registries.
    "WsSendCallable",
    "TransportHandler",
    # Concrete handlers.
    "TcpConnection",
    "UdpFlow",
]

# Registry aliases — part of the public surface for callers that prefer named
# aliases over explicit `dict[str, TcpConnection]` / `dict[str, UdpFlow]`.
__all__ += ["TcpRegistry", "UdpRegistry"]
