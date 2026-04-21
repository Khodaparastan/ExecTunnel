"""Shared types for the ``exectunnel.transport`` package."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .tcp import TcpConnection
    from .udp import UdpFlow

__all__ = [
    "TcpRegistry",
    "TransportHandler",
    "UdpRegistry",
    "WsSendCallable",
]


@runtime_checkable
class WsSendCallable(Protocol):
    """Structural type for the WebSocket send callable injected into handlers.

    Implementations accept a newline-terminated frame string and two
    keyword-only flow-control flags:

    * ``must_queue`` — block until enqueued under backpressure; ignored when
      ``control=True``.
    * ``control``    — priority frame that bypasses flow-control ordering;
      when ``True``, ``must_queue`` is ignored.
    """

    def __call__(
        self,
        frame: str,
        *,
        must_queue: bool = False,
        control: bool = False,
    ) -> Coroutine[Any, Any, None]: ...


class TransportHandler(Protocol):
    """Structural protocol satisfied by both ``TcpConnection`` and ``UdpFlow``.

    Provides the ``session`` layer with a uniform interface for typing handler
    registries without importing concrete classes.

    Note:
        Handler-specific methods (e.g. ``TcpConnection.abort``,
        ``UdpFlow.send_datagram``) require type-narrowing to the concrete
        class before use.

        ``on_remote_closed()`` must remain synchronous on all concrete
        implementations — the session layer calls it from a synchronous
        dispatch path and cannot ``await`` it.
    """

    @property
    def is_closed(self) -> bool:
        """``True`` once the handler has been fully torn down."""
        ...

    @property
    def drop_count(self) -> int:
        """Total number of inbound chunks or datagrams dropped."""
        ...

    def on_remote_closed(self) -> None:
        """Signal that the remote agent has closed its side of the flow."""
        ...


type TcpRegistry = dict[str, TcpConnection]
type UdpRegistry = dict[str, UdpFlow]
