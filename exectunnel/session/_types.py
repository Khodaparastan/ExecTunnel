"""Type aliases and type parameter definitions for the ``exectunnel.session`` package.

Centralising these avoids circular imports and keeps implementation modules
free of boilerplate type machinery.

Uses PEP 695 ``type`` statement syntax (Python 3.13+) for generic type
parameters and ``TypeAlias`` for callable aliases.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

# ── Generic type parameters ───────────────────────────────────────────────────

KT = TypeVar("KT")
"""Key type parameter for :class:`~exectunnel.session._lru.LruDict`."""

VT = TypeVar("VT")
"""Value type parameter for :class:`~exectunnel.session._lru.LruDict`."""

DefaultT = TypeVar("DefaultT")
"""Default-value type parameter for :class:`~exectunnel.session._lru.LruDict`."""

# ── Callable aliases ──────────────────────────────────────────────────────────

type AgentStatsCallable = Callable[[dict[str, Any]], None]
"""Signature of the optional listener invoked for each STATS snapshot.

The listener receives a decoded snapshot ``dict`` emitted by the remote agent
roughly once per second.  It must not block or raise — exceptions are logged
and suppressed by the receiver.

Production sessions leave this unset; the measurement framework registers a
listener that aggregates snapshots into benchmark reports.
"""

type ReconnectCallable = Callable[[str], Awaitable[None]]
"""Signature of the optional callback used to request a session reconnect.
The single argument is a short human-readable reason string.

Retained because the RX-liveness watchdog needs the same signature to surface
``inbound_liveness_timeout`` to :meth:`TunnelSession._request_reconnect`.
The dispatcher no longer uses it.
"""

type MarkAgentRxCallable = Callable[[], None]
"""Signature of the callback fired by the receiver on each inbound WebSocket
chunk to refresh the shared RX-liveness timestamp owned by
:class:`~exectunnel.session.session.TunnelSession`.

Replaces the previous activity-tracker indirection: the timestamp is now consumed by the
RX-liveness watchdog, not by the dispatcher's removed ACK-health heuristic.
"""
