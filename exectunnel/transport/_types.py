"""
Shared types for the ``exectunnel.transport`` package.

This module is the single source of truth for:

* :class:`WsSendCallable`  — structural ``Protocol`` for the WebSocket send
  callable injected into both :class:`~exectunnel.transport.tcp.TcpConnection`
  and :class:`~exectunnel.transport.udp.UdpFlow`.

* :class:`TransportHandler` — structural ``Protocol`` that both concrete
  handlers satisfy, allowing the ``session`` layer to type its registries
  uniformly without importing concrete handler classes.

* ``TcpRegistry`` / ``UdpRegistry`` — type aliases for the shared handler
  registries passed into each handler on construction.

Design notes
------------
* All types are defined here to avoid circular imports: ``tcp.py`` and
  ``udp.py`` both import from ``_types.py``; ``_types.py`` imports nothing
  from within the transport package.

* ``WsSendCallable`` is a ``@runtime_checkable`` ``Protocol`` so that
  injection points can be validated with ``isinstance()`` in tests and at
  session-layer wiring time.

* ``TransportHandler`` is intentionally *not* ``@runtime_checkable`` — the
  structural check would require inspecting async methods at runtime, which
  is fragile. Use static type checking (mypy / pyright) instead.
"""

from collections.abc import Coroutine
from typing import Any, Protocol, runtime_checkable


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
        site, but ``control=True`` always takes precedence.
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
    directly is declared here.  Additional handler-specific methods (e.g.
    ``UdpFlow.feed``, ``TcpConnection.feed_async``) are accessed through the
    the concrete types within their respective modules.
    """

    @property
    def is_closed(self) -> bool:
        """``True`` once the handler has been fully torn down."""
        ...

    @property
    def drop_count(self) -> int:
        """Total number of inbound chunks/datagrams dropped."""
        ...

    def close_remote(self) -> None:
        """Signal that the remote agent has closed its side."""
        ...


# ── Registry type aliases ─────────────────────────────────────────────────────

# Forward references are intentional — concrete handler classes are defined
# in tcp.py and udp.py which both depend on this module, not the other way
# around.  The aliases are used for annotation purposes only; no runtime
# isinstance() checks are performed against them.
#
# Using `type` statement (PEP 695, Python 3.12+) for all aliases.

type TcpRegistry = dict[str, Any]  # narrowed to dict[str, TcpConnection] at use site
type UdpRegistry = dict[str, Any]  # narrowed to dict[str, UdpFlow] at use site
