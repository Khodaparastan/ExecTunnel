"""
Shared types for the ``exectunnel.transport`` package.

This module is the single source of truth for:

* :class:`WsSendCallable`   — structural ``Protocol`` for the WebSocket send
  callable injected into both :class:`~exectunnel.transport.tcp.TcpConnection`
  and :class:`~exectunnel.transport.udp.UdpFlow`.

* :class:`TransportHandler` — structural ``Protocol`` that both concrete
  handlers satisfy, allowing the ``session`` layer to type its handler
  registries uniformly without importing concrete handler classes, which would
  create cross-layer coupling.

* ``TcpRegistry`` / ``UdpRegistry`` — type aliases for the shared handler
  registries passed into each handler on construction.

Design notes
------------
* All types are defined here to avoid circular imports: ``tcp.py`` and
  ``udp.py`` both import from ``_types.py``; ``_types.py`` imports nothing
  from within the transport package.

* ``WsSendCallable`` is ``@runtime_checkable`` so that injection points can
  be validated with ``isinstance()`` in tests and at session-layer wiring time.

* ``TransportHandler`` is intentionally *not* ``@runtime_checkable`` — the
  structural check would require inspecting async methods at runtime, which is
  fragile. Use static type checking (mypy / pyright) instead.

* Registry aliases use ``type`` statement (Python 3.12+) with string forward
  references. The ``TYPE_CHECKING`` guard ensures the concrete handler imports
  are erased at runtime, preventing the circular import that would otherwise
  occur. The ``type`` statement itself is lazily evaluated, so forward
  references resolve correctly at static-analysis time without any runtime cost.
"""

from __future__ import annotations

from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from exectunnel.transport.tcp import TcpConnection
    from exectunnel.transport.udp import UdpFlow

__all__ = [
    "WsSendCallable",
    "TransportHandler",
    "TcpRegistry",
    "UdpRegistry",
]


# ── WebSocket send callable ───────────────────────────────────────────────────


@runtime_checkable
class WsSendCallable(Protocol):
    """Structural type for the WebSocket send callable injected into handlers.

    Implementations must accept:

    * ``frame``      — the newline-terminated frame string to send.
    * ``must_queue`` — if ``True``, block until the frame is enqueued even
                       when the send queue is under backpressure.
                       Ignored when ``control=True``.
    * ``control``    — if ``True``, the frame is a priority control frame
                       that bypasses normal flow-control ordering.
                       When ``control=True``, ``must_queue`` is ignored and
                       the frame is enqueued immediately.

    Note:
        ``must_queue`` and ``control`` are not mutually exclusive at the call
        site, but ``control=True`` always takes precedence over ``must_queue``.
    """

    def __call__(
        self,
        frame: str,
        *,
        must_queue: bool = False,
        control: bool = False,
    ) -> Coroutine[Any, Any, None]: ...


# ── Shared handler protocol ───────────────────────────────────────────────────


class TransportHandler(Protocol):
    """Structural protocol satisfied by both ``TcpConnection`` and ``UdpFlow``.

    Provides the ``session`` layer with a uniform interface for typing its
    handler registries without importing concrete handler classes, which would
    create cross-layer coupling.

    Only the subset of the interface that the ``session`` layer needs to call
    through the *generic* registry path is declared here.  Handler-specific
    methods (e.g. ``TcpConnection.feed_async``, ``UdpFlow.send_datagram``)
    are accessed after type-narrowing to the concrete class within the session
    layer's dispatch logic.

    Naming convention
    -----------------
    ``on_remote_closed()`` is the canonical name for the agent-initiated
    teardown signal on **both** concrete handlers.  The session layer must
    call this method — never any deprecated alias.
    """

    @property
    def is_closed(self) -> bool:
        """``True`` once the handler has been fully torn down."""
        ...

    @property
    def drop_count(self) -> int:
        """Total number of inbound chunks/datagrams dropped."""
        ...

    def on_remote_closed(self) -> None:
        """Signal that the remote agent has closed its side of the flow."""
        ...


# ── Registry type aliases ─────────────────────────────────────────────────────
#
# Defined with the ``type`` statement (Python 3.12+) so they are lazily
# evaluated — forward references to TcpConnection / UdpFlow resolve at
# static-analysis time without triggering a circular import at runtime.
# The TYPE_CHECKING guard above ensures the concrete imports are erased
# entirely at runtime.

type TcpRegistry = dict[str, "TcpConnection"]
type UdpRegistry = dict[str, "UdpFlow"]
