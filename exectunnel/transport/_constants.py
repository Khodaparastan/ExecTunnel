"""Numeric tunables for :mod:`exectunnel.transport`.

All values are module-level :class:`typing.Final` so they can be freely
inlined by the interpreter and easily audited in code review. Constants
derived from the protocol layer are computed at import time from
:data:`exectunnel.protocol.MAX_TUNNEL_FRAME_CHARS` so there is exactly one source
of truth for the wire-format budget.

Attributes:
    MAX_DATA_CHUNK_BYTES: Maximum raw byte count that can be carried in a
        single ``DATA`` frame without exceeding the protocol frame budget
        after base64url encoding (no padding). Computed as
        ``(MAX_TUNNEL_FRAME_CHARS - FRAME_OVERHEAD_BYTES) * 3 // 4``.
    MAX_UDP_DATA_CHUNK_BYTES: Maximum raw byte count for one ``UDP_DATA``
        frame. ``UDP_DATA`` has four more message-type characters than
        ``DATA``, so its safe payload budget is slightly smaller.
    FRAME_OVERHEAD_BYTES: Fixed per-frame overhead for a standard
        ``DATA`` frame: ``len(FRAME_PREFIX) + len("DATA") + 2×len(":") +
        len(conn_id) + len(FRAME_SUFFIX)`` = ``14 + 4 + 2 + 25 + 3 = 48``.
    WRITER_CLOSE_TIMEOUT_SECS: Upper bound on the ``wait_closed()`` phase
        of TCP writer teardown.
    DOWNSTREAM_BATCH_SIZE: Maximum number of inbound queue items drained
        per ``StreamWriter.drain()`` call in the TCP downstream task.
    MIN_PRE_ACK_BUFFER_CAP: Minimum accepted value for the per-connection
        pre-ACK buffer cap. The buffer must hold at least one full socket
        read.
"""

from __future__ import annotations

from typing import Final

from exectunnel.defaults import Defaults
from exectunnel.protocol import (
    DATA_FRAME_OVERHEAD_CHARS,
    MAX_DATA_PAYLOAD_BYTES,
    MAX_UDP_DATA_PAYLOAD_BYTES,
    UDP_DATA_FRAME_OVERHEAD_CHARS,
)

__all__ = [
    "DOWNSTREAM_BATCH_SIZE",
    "FRAME_OVERHEAD_BYTES",
    "MAX_DATA_CHUNK_BYTES",
    "MAX_UDP_DATA_CHUNK_BYTES",
    "MIN_PRE_ACK_BUFFER_CAP",
    "WRITER_CLOSE_TIMEOUT_SECS",
]

# Fixed per-frame overhead for a DATA frame:
#   FRAME_PREFIX   "<<<EXECTUNNEL:"  → 14
#   msg_type       "DATA"            →  4
#   separators     ":" × 2           →  2
#   conn_id        "c" + 24 hex      → 25
#   FRAME_SUFFIX   ">>>"             →  3
#                                    = 48
FRAME_OVERHEAD_BYTES: Final[int] = DATA_FRAME_OVERHEAD_CHARS
UDP_FRAME_OVERHEAD_BYTES: Final[int] = UDP_DATA_FRAME_OVERHEAD_CHARS
# The base64url expansion ratio is 4 output chars per 3 input bytes (no
# padding on the wire). Invert to size the raw-byte budget.
MAX_DATA_CHUNK_BYTES: Final[int] = MAX_DATA_PAYLOAD_BYTES
MAX_UDP_DATA_CHUNK_BYTES: Final[int] = MAX_UDP_DATA_PAYLOAD_BYTES

WRITER_CLOSE_TIMEOUT_SECS: Final[float] = 5.0
DOWNSTREAM_BATCH_SIZE: Final[int] = 16
MIN_PRE_ACK_BUFFER_CAP: Final[int] = Defaults.PIPE_READ_CHUNK_BYTES

# Production invariant: the per-read chunk size configured in defaults must
# never exceed the protocol's per-frame byte budget. ``assert`` is stripped
# by ``python -O``, so a plain ``raise`` is used instead.
if Defaults.PIPE_READ_CHUNK_BYTES > MAX_DATA_CHUNK_BYTES:
    raise RuntimeError(
        f"Defaults.PIPE_READ_CHUNK_BYTES ({Defaults.PIPE_READ_CHUNK_BYTES}) "
        f"exceeds the protocol maximum of {MAX_DATA_CHUNK_BYTES} bytes per "
        "DATA chunk. Adjust Defaults.PIPE_READ_CHUNK_BYTES or increase "
        "MAX_TUNNEL_FRAME_CHARS in exectunnel.protocol.constants."
    )
if MAX_UDP_DATA_CHUNK_BYTES <= 0:
    raise RuntimeError(
        "MAX_UDP_DATA_CHUNK_BYTES computed to a non-positive value. "
        "Increase MAX_TUNNEL_FRAME_CHARS or reduce UDP frame overhead."
    )
