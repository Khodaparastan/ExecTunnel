"""Wire-level constants for the ExecTunnel protocol.

This module is the single source of truth for every byte-level value the
protocol layer relies on:

* Frame delimiters (``FRAME_PREFIX``, ``FRAME_SUFFIX``).
* The agent-ready sentinel (``READY_FRAME``).
* Size ceilings (``MAX_TUNNEL_FRAME_CHARS``).
* Reserved port values (``PORT_UNSPECIFIED``).
* The closed sets of message-type strings and their role classification
  (requires payload, forbids payload, carries no conn_id, …).

Nothing in this module performs I/O, imports from siblings other than
:mod:`exectunnel.protocol.ids`, or has any runtime side effects.

Note:
    ``STATS`` is listed in :data:`VALID_MSG_TYPES` and
    :data:`NO_CONN_ID_WITH_PAYLOAD_TYPES`. It is session-scoped, carries a
    base64url JSON payload, and is encoded through
    :func:`exectunnel.protocol.encoders.encode_stats_frame`.
"""

from __future__ import annotations

import os
from typing import Final

from .ids import SESSION_CONN_ID as _SESSION_CONN_ID

__all__ = [
    "DATA_FRAME_OVERHEAD_CHARS",
    "FRAME_PREFIX",
    "FRAME_SUFFIX",
    "KEEPALIVE_FRAME",
    "MAX_DATA_PAYLOAD_BYTES",
    "MAX_TUNNEL_FRAME_CHARS",
    "MAX_UDP_DATA_PAYLOAD_BYTES",
    "MAX_TCP_UDP_PORT",
    "MIN_TCP_UDP_PORT",
    "NO_CONN_ID_TYPES",
    "NO_CONN_ID_WITH_PAYLOAD_TYPES",
    "PAYLOAD_FORBIDDEN_TYPES",
    "PAYLOAD_PREVIEW_LEN",
    "PAYLOAD_REQUIRED_TYPES",
    "PORT_UNSPECIFIED",
    "READY_FRAME",
    "SESSION_CONN_ID",
    "UDP_DATA_FRAME_OVERHEAD_CHARS",
    "VALID_MSG_TYPES",
]

# ── Frame delimiters ──────────────────────────────────────────────────────────

FRAME_PREFIX: Final[str] = "<<<EXECTUNNEL:"
"""Opening delimiter of every tunnel frame on the wire."""

FRAME_SUFFIX: Final[str] = ">>>"
"""Closing delimiter of every tunnel frame on the wire."""

# ── Size and range limits ─────────────────────────────────────────────────────

#: Maximum accepted frame length in characters, excluding the trailing newline.
#:
#: Derivation of the safe DATA payload budget::
#:
#:     available = MAX_TUNNEL_FRAME_CHARS
#:               - len(FRAME_PREFIX)        # 14
#:               - len("DATA")              #  4
#:               - 2 * len(":")             #  2
#:               - len(conn_id)             # 25
#:               - len(FRAME_SUFFIX)        #  3
#:               = 8192 - 48
#:               = 8144 base64url characters
#:     max raw bytes = floor(8144 * 3 / 4) = 6108 bytes
DEFAULT_MAX_TUNNEL_FRAME_CHARS: Final[int] = 262_144
MAX_TUNNEL_FRAME_CHARS: Final[int] = int(
    os.getenv("EXECTUNNEL_MAX_TUNNEL_FRAME_CHARS", DEFAULT_MAX_TUNNEL_FRAME_CHARS)
)

_CONN_FLOW_ID_CHARS: Final[int] = 25

DATA_FRAME_OVERHEAD_CHARS: Final[int] = (
    len(FRAME_PREFIX) + len("DATA") + 2 + _CONN_FLOW_ID_CHARS + len(FRAME_SUFFIX)
)

UDP_DATA_FRAME_OVERHEAD_CHARS: Final[int] = (
    len(FRAME_PREFIX) + len("UDP_DATA") + 2 + _CONN_FLOW_ID_CHARS + len(FRAME_SUFFIX)
)


def _max_binary_payload_bytes(frame_overhead_chars: int) -> int:
    available_chars = MAX_TUNNEL_FRAME_CHARS - frame_overhead_chars
    return max(0, available_chars * 3 // 4)


MAX_DATA_PAYLOAD_BYTES: Final[int] = _max_binary_payload_bytes(
    DATA_FRAME_OVERHEAD_CHARS
)

MAX_UDP_DATA_PAYLOAD_BYTES: Final[int] = _max_binary_payload_bytes(
    UDP_DATA_FRAME_OVERHEAD_CHARS
)

if MAX_DATA_PAYLOAD_BYTES <= 0 or MAX_UDP_DATA_PAYLOAD_BYTES <= 0:
    raise RuntimeError(
        "EXECTUNNEL_MAX_TUNNEL_FRAME_CHARS is too small for DATA/UDP_DATA frames."
    )

#: Number of characters of a malformed payload included in error telemetry.
PAYLOAD_PREVIEW_LEN: Final[int] = 64

#: Sentinel port value used by callers to signal "unspecified / unknown".
#:
#: The on-wire port range is ``[1, 65535]``; ``0`` is reserved and never
#: appears in a valid ``CONN_OPEN`` / ``UDP_OPEN`` frame.
PORT_UNSPECIFIED: Final[int] = 0

#: Inclusive lower bound of the valid TCP/UDP port range.
MIN_TCP_UDP_PORT: Final[int] = 1

#: Inclusive upper bound of the valid TCP/UDP port range.
MAX_TCP_UDP_PORT: Final[int] = 65_535

# ── Sentinel frames ───────────────────────────────────────────────────────────

READY_FRAME: Final[str] = f"{FRAME_PREFIX}AGENT_READY{FRAME_SUFFIX}"
"""Bare ``AGENT_READY`` comparison string (no trailing newline).

Callers that want the wire form must use
:func:`exectunnel.protocol.encoders.encode_agent_ready_frame`.
"""

KEEPALIVE_FRAME: Final[str] = f"{FRAME_PREFIX}KEEPALIVE{FRAME_SUFFIX}\n"
"""Pre-computed, newline-terminated KEEPALIVE frame."""

#: Re-export of :data:`exectunnel.protocol.ids.SESSION_CONN_ID` for convenience.
SESSION_CONN_ID: Final[str] = _SESSION_CONN_ID

# ── Message-type classification sets ──────────────────────────────────────────

VALID_MSG_TYPES: Final[frozenset[str]] = frozenset({
    "AGENT_READY",
    "CONN_OPEN",
    "CONN_ACK",
    "CONN_CLOSE",
    "DATA",
    "UDP_OPEN",
    "UDP_DATA",
    "UDP_CLOSE",
    "ERROR",
    "KEEPALIVE",
    # Session-scoped observability snapshot emitted by the agent. Carries a
    # base64url-JSON payload but no conn_id. See exectunnel/bench/_schema.py.
    "STATS",
})
"""The complete, closed set of recognised frame type strings."""

NO_CONN_ID_TYPES: Final[frozenset[str]] = frozenset({
    "AGENT_READY",
    "KEEPALIVE",
})
"""Frame types that carry neither a conn_id nor a payload."""

NO_CONN_ID_WITH_PAYLOAD_TYPES: Final[frozenset[str]] = frozenset({
    "STATS",
})
"""Frame types that carry a payload but no conn_id (session-scoped)."""

PAYLOAD_REQUIRED_TYPES: Final[frozenset[str]] = frozenset({
    "CONN_OPEN",
    "DATA",
    "UDP_OPEN",
    "UDP_DATA",
    "ERROR",
})
"""Frame types whose payload must be non-empty."""

PAYLOAD_FORBIDDEN_TYPES: Final[frozenset[str]] = frozenset({
    "CONN_ACK",
    "CONN_CLOSE",
    "UDP_CLOSE",
})
"""Frame types that must not carry any payload."""
