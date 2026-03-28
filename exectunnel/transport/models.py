"""Transport-layer data models."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(slots=True)
class PendingConnectState:
    """
    Tracks one in-flight CONN_OPEN that has not yet received CONN_ACK.

    Renamed from ``PendingConnect`` to disambiguate the dataclass (which
    represents a *state* snapshot) from the action of connecting.
    """

    host: str
    # The asyncio Future resolved when CONN_ACK arrives.
    ack_future: asyncio.Future[str]


# Backward-compat alias.
PendingConnect = PendingConnectState
