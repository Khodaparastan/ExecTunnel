"""Per-connection pending state tracked by TunnelSession during CONN_OPEN/ACK."""

import asyncio
from dataclasses import dataclass
from enum import StrEnum


class AckStatus(StrEnum):
    """Result values for the per-connection ACK future.

    Used as ``asyncio.Future[AckStatus]`` results and as ``ack_status``
    labels in metrics.  Always match on the enum member, never on the raw
    string value.

    Values deliberately match their names so that metric label strings are
    self-documenting in dashboards.
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
    """Tracks one in-flight ``CONN_OPEN`` that has not yet received ``CONN_ACK``.

    ``ack_future`` is resolved by ``FrameReceiver._dispatch_frame`` when the
    matching ``CONN_ACK`` or ``ERROR`` / ``CONN_CLOSE`` frame arrives.
    """

    host: str
    port: int
    ack_future: asyncio.Future[AckStatus]
