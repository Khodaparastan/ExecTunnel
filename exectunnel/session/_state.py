"""Per-connection pending state tracked by ``TunnelSession`` during CONN_OPEN/ACK.

This module is intentionally minimal — it declares only the data structures
shared between :mod:`_dispatcher` and :mod:`_receiver` so neither module
depends on the other.
"""

import asyncio
from dataclasses import dataclass
from enum import StrEnum


class AckStatus(StrEnum):
    """Result values for the per-connection ACK future.

    Each member is used as the resolved value of an
    ``asyncio.Future[AckStatus]`` and as a ``status`` label in metrics.
    Always match on the enum member, never on the raw string value, so that
    metric label strings remain self-documenting in dashboards.

    Attributes:
        OK:               The agent acknowledged the connection successfully.
        TIMEOUT:          The ACK was not received within the configured timeout.
        WS_CLOSED:        The WebSocket closed before the ACK arrived.
        AGENT_ERROR:      The agent sent an ``ERROR`` frame for this connection.
        AGENT_CLOSED:     The agent sent ``CONN_CLOSE`` before ``CONN_ACK``.
        WS_SEND_TIMEOUT:  A WebSocket send timed out before the ACK.
        PRE_ACK_OVERFLOW: The pre-ACK receive buffer was exhausted.
        LIBRARY_ERROR:    An ``ExecTunnelError`` was raised during connect.
        UNEXPECTED_ERROR: An unhandled exception occurred during connect.
    """

    OK = "ok"
    TIMEOUT = "timeout"
    WS_CLOSED = "ws_closed"
    AGENT_ERROR = "agent_error"
    AGENT_CLOSED = "agent_closed"
    WS_SEND_TIMEOUT = "ws_send_timeout"
    PRE_ACK_OVERFLOW = "pre_ack_overflow"
    LIBRARY_ERROR = "library_error"
    UNEXPECTED_ERROR = "unexpected_error"


@dataclass(frozen=True, slots=True)
class PendingConnect:
    """Tracks one in-flight ``CONN_OPEN`` awaiting ``CONN_ACK``.

    Created by :class:`~exectunnel.session._dispatcher.RequestDispatcher`
    when a ``CONN_OPEN`` frame is sent and removed when the matching
    ``CONN_ACK``, ``ERROR``, or ``CONN_CLOSE`` frame arrives via
    :class:`~exectunnel.session._receiver.FrameReceiver`.

    Attributes:
        host:       Destination hostname or IP address.
        port:       Destination TCP port.
        ack_future: Future resolved with an :class:`AckStatus` when the
                    agent responds to the ``CONN_OPEN``.
    """

    host: str
    port: int
    ack_future: asyncio.Future[AckStatus]
