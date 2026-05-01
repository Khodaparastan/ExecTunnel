"""Shared constants for the ``exectunnel.session`` package.

All magic numbers, string literals, and tuples that are referenced by more
than one module — or that benefit from a single authoritative definition —
live here.  Import-only module: no classes, no functions, no side effects.
"""

import re
from typing import Final

from exectunnel.defaults import Defaults

# ── Bootstrap: fence / marker protocol ───────────────────────────────────────

FENCE_PREFIX: Final[str] = "EXECTUNNEL_FENCE"
"""Prefix used to construct unique fence markers for fenced commands."""

MARKER_PREFIX: Final[str] = "EXECTUNNEL_EXISTS"
"""Prefix for file-existence probe markers."""

MARKER_YES: Final[str] = f"{MARKER_PREFIX}:1"
"""Marker emitted by the remote shell when the probed file exists."""

MARKER_NO: Final[str] = f"{MARKER_PREFIX}:0"
"""Marker emitted by the remote shell when the probed file does not exist."""

# ── Bootstrap: delivery ───────────────────────────────────────────────────────

VALID_DELIVERY_MODES: Final[frozenset[str]] = frozenset({"upload", "fetch"})
"""Accepted values for ``TunnelConfig.bootstrap_delivery``."""

UPLOAD_PROGRESS_LOG_INTERVAL: Final[int] = 50
"""Log a progress message every N chunks during base64 upload."""

# ── Bootstrap: timeouts ───────────────────────────────────────────────────────

FENCE_TIMEOUT_SECS: Final[float] = 30.0
"""Maximum seconds to wait for a command fence marker."""

MIN_FENCE_TIMEOUT_SECS: Final[float] = 2.0
"""Minimum fence timeout used for short probes (e.g. Python interpreter check)."""

# ── Bootstrap: buffering ──────────────────────────────────────────────────────

MAX_STASH_LINES: Final[int] = Defaults.BOOTSTRAP_MAX_STASH_LINES
"""Maximum lines buffered in the bootstrapper stash deque.
"""

# ── Bootstrap: Python interpreter candidates ─────────────────────────────────

PYTHON_CANDIDATES: Final[tuple[str, ...]] = (
    "python3.13",
    "python3.12",
    "python3.11",
    "python3",
    "python",
)
"""Ordered list of Python interpreter names probed on the remote pod.

Includes ``python3.13`` first to match the py313+ deployment target.
"""
MIN_REMOTE_PYTHON_VERSION: Final[tuple[int, int]] = (3, 11)
"""Minimum Python version required by the Python pod agent."""
UPLOAD_FENCE_EVERY_CHUNKS: Final[int] = Defaults.UPLOAD_FENCE_EVERY_CHUNKS
"""Fence every N upload chunks to avoid overrunning raw PTY input buffers.
"""

# ── WebSocket decode ──────────────────────────────────────────────────────────

WS_DECODE_ERRORS: Final[str] = "replace"
"""Unicode error handler used when decoding raw WebSocket bytes."""

# ── Dispatcher: concurrency ───────────────────────────────────────────────────

HOST_SEMAPHORE_CAPACITY: Final[int] = 4_096
"""Maximum number of per-host semaphore entries in ``_HostGateRegistry``."""

UDP_ACTIVE_FLOWS_CAP: Final[int] = 256
"""Maximum simultaneous UDP flows per ``UDP_ASSOCIATE`` session."""

PIPE_WRITER_CLOSE_TIMEOUT_SECS: Final[float] = 5.0
"""Seconds to wait for a ``StreamWriter.wait_closed()`` call in ``_pipe``."""

DIRECT_CONNECT_TIMEOUT_SECS: Final[float] = 15.0
"""Timeout for direct (excluded-host) TCP connection attempts."""

# ── Receiver: protocol ────────────────────────────────────────────────────────

UNEXPECTED_NO_CONN_ID_FRAMES: Final[frozenset[str]] = frozenset({
    "AGENT_READY",
    "KEEPALIVE",
})
"""Frame types that must never arrive from the agent after bootstrap."""

WS_CLOSE_CODE_PROTOCOL_ERROR: Final[int] = 1002
"""WebSocket close code for protocol-level errors."""

WS_CLOSE_CODE_INTERNAL_ERROR: Final[int] = 1011
"""WebSocket close code for agent-side internal errors."""

# ── Sender ────────────────────────────────────────────────────────────────────

STOP_GRACE_TIMEOUT_SECS: Final[float] = 5.0
"""Seconds to wait for the send loop to drain before force-cancelling."""

# ── Shared ────────────────────────────────────────────────────────────────────

LOG_TRUNCATE_LEN: Final[int] = 120
"""Maximum characters included when truncating strings in log messages."""

UDP_MAX_DATAGRAM_SIZE: Final[int] = 65_535
"""Maximum UDP datagram size in bytes."""

# ── Shared: host key sanitisation ─────────────────────────────────────────────

SAFE_HOST_RE: Final[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9_.\-]")
"""Compiled pattern for sanitising host strings used in asyncio task names.

Single authoritative definition — used by both ``session.py`` and
``_dispatcher.py`` to avoid duplicated compilation.
"""

# ── Routing: default IPv6 exclusions ─────────────────────────────────────────

DEFAULT_EXCLUDE_IPV6_CIDRS: Final[tuple[str, ...]] = (
    "::1/128",  # IPv6 loopback
    "fc00::/7",  # Unique Local Addresses (RFC 4193)
    "fe80::/10",  # Link-local
)
"""IPv6 networks excluded from tunnelling by default."""
