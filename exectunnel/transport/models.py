"""Transport-layer data models."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(slots=True)
class PendingConnectState:
    """
    Tracks one in-flight CONN_OPEN that has not yet received CONN_ACK.
    """

    host: str
    # The asyncio Future resolved when CONN_ACK arrives.
    ack_future: asyncio.Future[str]
