#!/usr/bin/env python3
"""
exectunnel agent — runs inside the pod via ``exec``.

Usage
-----
  python3 /tmp/exectunnel_agent.py                 # tunnel mode (SOCKS5 proxy)
  python3 /tmp/exectunnel_agent.py <host> <port>   # portforward mode (fixed target)

Frame protocol (newline-terminated)
------------------------------------
local → agent::

    <<<EXECTUNNEL:CONN_OPEN:cN:host:port>>>     tunnel mode
    <<<EXECTUNNEL:CONN_OPEN:cN>>>               portforward mode (uses fixed target)
    <<<EXECTUNNEL:DATA:cN:base64url>>>
    <<<EXECTUNNEL:CONN_CLOSE:cN>>>
    <<<EXECTUNNEL:UDP_OPEN:uN:[host]:port>>>
    <<<EXECTUNNEL:UDP_DATA:uN:base64url>>>
    <<<EXECTUNNEL:UDP_CLOSE:uN>>>
    <<<EXECTUNNEL:KEEPALIVE>>>                  silently ignored

agent → local::

    <<<EXECTUNNEL:AGENT_READY>>>
    <<<EXECTUNNEL:CONN_ACK:cN>>>
    <<<EXECTUNNEL:DATA:cN:base64url>>>
    <<<EXECTUNNEL:CONN_CLOSE:cN>>>
    <<<EXECTUNNEL:ERROR:cN:base64url_reason>>>
    <<<EXECTUNNEL:UDP_DATA:uN:base64url>>>
    <<<EXECTUNNEL:UDP_CLOSE:uN>>>
    <<<EXECTUNNEL:STATS:base64url_json>>>   # optional observability snapshot

Encoding
--------
All binary payloads (DATA, UDP_DATA, ERROR) use URL-safe base64 with no
padding (``urlsafe_b64encode(...).rstrip(b"=")``) — consistent with the
client-side ``encode_data_frame`` / ``decode_binary_payload`` helpers.

Design notes
------------
* Self-contained — no third-party deps; runs on any bare Python 3.12+ pod.
* TCP and UDP workers use threads with a self-pipe to avoid blocking the
  stdin-reader thread.
* All TCP sends use a per-chunk write loop with ``select``-based blocking so
  a slow remote cannot stall the entire thread.
* Stdout is serialised by a single ``_FrameWriter`` daemon thread that owns
  a ``_FaultTolerantStdout`` wrapper.  Control frames go into an unbounded
  ``SimpleQueue`` (always drained first); DATA / UDP_DATA frames go into a
  bounded ``Queue`` (cap ``_STDOUT_DATA_QUEUE_CAP``).
* ``SIGPIPE`` is ignored so the process does not crash when the kubectl exec
  channel closes while the writer is mid-send.
* ``_FaultTolerantStdout`` catches ``OSError`` / ``BrokenPipeError`` at the
  Python ``io`` level before the C layer can print
  ``"socket.send() raised exception."`` to stderr.

Backpressure model
------------------
The stdout data queue is bounded (``_STDOUT_DATA_QUEUE_CAP``).  When it is
full, ``TcpConnectionWorker._io_loop`` skips ``sock.recv()`` for that
iteration, allowing the OS TCP receive buffer to fill.  This propagates
backpressure all the way back to the remote sender (CDN / target service)
without blocking the stdin-reader thread or the writer thread.

The backpressure threshold is ``_STDOUT_DATA_QUEUE_BACKPRESSURE_RATIO`` of
the queue capacity (default 0.75).  Pausing recv at 75% full gives the
writer thread time to drain before the queue is completely full, preventing
the EPIPE that would occur if the queue overflowed and the writer blocked.

Protocol alignment
------------------
* ``CONN_ACK`` is emitted immediately after a successful TCP connect.
* ``CONN_CLOSE`` / ``UDP_CLOSE`` are always emitted in ``finally`` blocks.
* ``ERROR`` frames are emitted for: connect failure, inbound saturation,
  corrupt base64 payload.
* ``KEEPALIVE`` frames are silently discarded.
* Unknown frame types are logged at DEBUG and ignored — forward-compatible.
* Duplicate ``CONN_OPEN`` / ``UDP_OPEN`` emit ``ERROR`` / ``UDP_CLOSE``.
* Registry eviction is performed exclusively by ``on_close`` callbacks.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import errno
import json as _json
import os
import queue
import re as _re
import select
import signal
import socket
import sys
import termios
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol


class _WritableStream(Protocol):
    """Minimal protocol for the object wrapped by :class:`_FaultTolerantStdout`.

    Only the two methods actually invoked by the writer are declared, keeping
    the surface area small and the static type-checker happy without forcing
    a concrete ``TextIO`` dependency (``sys.stdout`` is reassignable in tests).
    """

    def write(self, s: str, /) -> int: ...

    def flush(self) -> None: ...


_FRAME_PREFIX: str = "<<<EXECTUNNEL:"
_FRAME_SUFFIX: str = ">>>"
_AGENT_VERSION: str = "1"
_LOG_LEVELS: dict[str, int] = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
}
_LOG_LEVEL: int = _LOG_LEVELS.get(
    os.getenv("EXECTUNNEL_AGENT_LOG_LEVEL", "warning").lower(),
    30,
)

# ── Tuning constants ──────────────────────────────────────────────────────────

# Maximum queued inbound chunks before a TCP connection is declared saturated.
_MAX_TCP_INBOUND_CHUNKS: int = 1_024

# Global hard cap on concurrent TCP workers the agent will accept.  Guards
# against runaway/adversarial CONN_OPEN storms that could exhaust the pod's
# thread/FD budget.  Additional opens beyond the cap are rejected with an
# ERROR frame.
_MAX_TCP_WORKERS: int = int(os.getenv("EXECTUNNEL_AGENT_MAX_TCP_WORKERS", "2048"))

# Hard per-call deadline for ``_FrameWriter.emit_data`` to avoid permanent
# wedge when the writer channel is silently half-dead (no OSError raised
# yet so ``is_dead`` has not flipped).  After this deadline the writer is
# force-marked dead so all workers unblock.
_EMIT_DATA_HARD_DEADLINE_SECS: float = float(
    os.getenv("EXECTUNNEL_AGENT_EMIT_DATA_DEADLINE_SECS", "30.0")
)

# Maximum queued inbound datagrams before a UDP flow starts dropping.
_MAX_UDP_INBOUND_DGRAMS: int = 2_048

# Bounded stdout data-frame queue capacity.
_STDOUT_DATA_QUEUE_CAP: int = 2_048

# Backpressure threshold: pause TCP recv when data queue exceeds this fraction.
_STDOUT_DATA_QUEUE_BACKPRESSURE_RATIO: float = 0.75

# Derived threshold in items — computed once at module load.
_STDOUT_DATA_QUEUE_BACKPRESSURE_THRESHOLD: int = int(
    _STDOUT_DATA_QUEUE_CAP * _STDOUT_DATA_QUEUE_BACKPRESSURE_RATIO
)

# Maximum data frames drained per writer-loop iteration (fairness cap).
_WRITER_DATA_BATCH_SIZE: int = 64

# TCP connect timeout in seconds.
_TCP_CONNECT_TIMEOUT_SECS: float = 8.0

# TCP receive chunk size in bytes.
_TCP_RECV_CHUNK_BYTES: int = 4_096

# Maximum UDP datagram size (theoretical IPv4/IPv6 maximum).
_MAX_UDP_DGRAM_BYTES: int = 65_535
_MAX_TCP_UDP_PORT: int = 65_535

# Log a UDP drop warning every N drops.
_UDP_DROP_WARN_EVERY: int = 1_000

# select() timeout — 10 ms balances interactive latency vs CPU overhead.
_SELECT_TIMEOUT_SECS: float = 0.01

# Writer control-queue poll timeout — longer than select to reduce busy-polling.
_WRITER_CTRL_POLL_SECS: float = 0.05

# Writer thread join timeout on shutdown.
_WRITER_JOIN_TIMEOUT_SECS: float = 5.0

# How long to wait for the remote to close after SHUT_WR (half-close).
_HALF_CLOSE_DEADLINE_SECS: float = 30.0

# select() timeout used in _send_all_nonblocking() while waiting for a slow
# remote TCP peer to become writable.
_SEND_WRITABLE_TIMEOUT_SECS: float = 5.0

# Maximum inbound frame length accepted by dispatch().
_MAX_INBOUND_FRAME_LEN: int = 8_192
_MIN_PARTS_WITH_CONN_ID: int = 2
_MIN_PARTS_WITH_PAYLOAD: int = 3
_HOST_PORT_PART_INDEX: int = 2
_ARGC_FIXED_TARGET: int = 3

# Regex pattern for valid connection / flow IDs.
_ID_RE: _re.Pattern[str] = _re.compile(r"^[cu][0-9a-f]{24}$")

# Timeout for each Queue.put() attempt in emit_data().
_EMIT_DATA_PUT_TIMEOUT_SECS: float = 1.0

# Grace period for worker threads to emit final frames during shutdown.
_WORKER_SHUTDOWN_GRACE_SECS: float = 3.0

# Maximum number of entries in the per-process DNS cache.  The agent is
# normally short-lived so expiry is unnecessary, but a hard cap prevents
# unbounded growth under pathological fan-out (many distinct hosts).
_DNS_CACHE_MAX_ENTRIES: int = 4_096

_TERMINATE = False

# ── Observability counters (Step 2 — measurement framework) ──────────────────
# Module-level atomic-ish counters. Plain ints are safe to read/write under
# CPython's GIL for our purposes (sampler only observes monotonic snapshots;
# small races on the *rate* computation are acceptable — we never rely on
# byte-for-byte accuracy, only on trend).
_TX_BYTES_TOTAL: int = 0
_RX_BYTES_TOTAL: int = 0
_FRAMES_TX_TOTAL: int = 0
_FRAMES_RX_TOTAL: int = 0

# Bounded ring-buffer of recent dispatch latencies (seconds, float).
# Used by the stats sampler to compute p50/p95 for the last sampling window.
_DISPATCH_SAMPLES_MAX: int = 1_024
_DISPATCH_SAMPLES: deque[float] = deque(maxlen=_DISPATCH_SAMPLES_MAX)
_DISPATCH_SAMPLES_LOCK: threading.Lock = threading.Lock()

# Stats sampling interval (seconds). 1 Hz matches the client bench harness.
_STATS_SAMPLE_INTERVAL_SECS: float = float(
    os.getenv("EXECTUNNEL_AGENT_STATS_INTERVAL_SECS", "1.0")
)
# Feature flag — default ON; set to "0" to disable STATS emission entirely
# for sessions that must stay byte-identical to the pre-observability agent.
_STATS_ENABLED: bool = os.getenv("EXECTUNNEL_AGENT_STATS_ENABLED", "1") != "0"


def _record_dispatch_sample(seconds: float) -> None:
    """Record one dispatch latency observation for the stats sampler.

    Samples are stored in a bounded :class:`collections.deque` and protected
    by a lock so concurrent dispatches and the sampler thread see consistent
    snapshots. When the deque is full the oldest sample is discarded.

    Args:
        seconds: Wall-clock duration spent inside
            :meth:`_Dispatcher._dispatch_inner` for a single frame.
    """
    with _DISPATCH_SAMPLES_LOCK:
        _DISPATCH_SAMPLES.append(seconds)


class _DnsCache:
    """Thread-safe DNS cache with per-key deduplication and an LRU cap.

    Only the first caller for a given ``(host, port, socktype)`` key blocks
    on ``getaddrinfo``; concurrent callers wait on a per-key lock.

    Entries never expire, but the cache is bounded by
    ``_DNS_CACHE_MAX_ENTRIES`` and evicts oldest entries (and their
    deduplication locks) when the cap is exceeded.
    """

    __slots__ = ("_cache", "_key_locks", "_mu", "_max_entries")

    def __init__(self, max_entries: int = _DNS_CACHE_MAX_ENTRIES) -> None:
        # ``dict`` preserves insertion order (Py 3.7+), which is what we use
        # for cheap LRU eviction.  We *re-insert* on each hit so the most
        # recently used key is at the end.
        self._cache: dict[tuple[str, int, int], list[tuple]] = {}
        self._key_locks: dict[tuple[str, int, int], threading.Lock] = {}
        self._mu = threading.Lock()
        self._max_entries = max_entries

    def _touch_locked(self, key: tuple[str, int, int]) -> None:
        """Move *key* to the MRU end of the ordered dict (caller holds ``_mu``)."""
        value = self._cache.pop(key, None)
        if value is not None:
            self._cache[key] = value

    def _evict_if_needed_locked(self) -> None:
        while len(self._cache) > self._max_entries:
            oldest_key, _ = next(iter(self._cache.items()))
            self._cache.pop(oldest_key, None)
            self._key_locks.pop(oldest_key, None)

    def getaddrinfo(
        self,
        host: str,
        port: int,
        *,
        socktype: int = 0,
    ) -> list[tuple]:
        """Return cached :func:`socket.getaddrinfo` results for ``(host, port, socktype)``.

        On cache miss exactly one caller performs the real lookup while
        concurrent callers wait on a per-key lock, avoiding a thundering
        herd of parallel ``getaddrinfo`` syscalls for the same destination.

        Args:
            host: Hostname or literal IP address.
            port: Destination port number.
            socktype: Socket type filter passed through to
                :func:`socket.getaddrinfo` (``0`` means "any").

        Returns:
            A list of ``(family, type, proto, canonname, sockaddr)`` tuples
            exactly as returned by :func:`socket.getaddrinfo`.

        Raises:
            socket.gaierror: Propagated from the underlying lookup when
                resolution fails.
        """
        key = (host, port, socktype)

        with self._mu:
            cached = self._cache.get(key)
            if cached is not None:
                self._touch_locked(key)
                return cached
            key_lock = self._key_locks.get(key)
            if key_lock is None:
                key_lock = threading.Lock()
                self._key_locks[key] = key_lock

        with key_lock:
            with self._mu:
                cached = self._cache.get(key)
                if cached is not None:
                    self._touch_locked(key)
                    return cached

            infos = socket.getaddrinfo(host, port, type=socktype)

            with self._mu:
                self._cache[key] = infos
                self._evict_if_needed_locked()
            return infos


def _create_connection_cached(
    dns_cache: _DnsCache,
    host: str,
    port: int,
    timeout: float,
) -> socket.socket:
    """Open a TCP connection to ``host:port`` using *dns_cache* for resolution.

    Functionally equivalent to :func:`socket.create_connection` but with two
    differences: address resolution is served from :class:`_DnsCache` so
    repeated opens for the same target avoid redundant ``getaddrinfo``
    syscalls, and each candidate address is tried in turn with the supplied
    *timeout* applied per attempt.

    Args:
        dns_cache: Shared DNS cache used for the initial ``getaddrinfo``.
        host: Destination hostname or literal IP address.
        port: Destination port number.
        timeout: Per-attempt connect timeout in seconds.

    Returns:
        A connected, blocking :class:`socket.socket` instance.

    Raises:
        OSError: When resolution yields no candidates or every candidate
            fails to connect — the last underlying error is chained.
    """
    infos = dns_cache.getaddrinfo(host, port, socktype=socket.SOCK_STREAM)
    if not infos:
        raise OSError(f"getaddrinfo returned no results for {host!r}:{port}")

    last_exc: OSError | None = None
    for family, socktype, proto, _, addr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(addr)
            return sock
        except OSError as exc:
            last_exc = exc
            with contextlib.suppress(OSError):
                sock.close()

    raise last_exc or OSError(f"could not connect to {host}:{port}")


def _install_sigpipe_handler() -> None:
    """Ignore ``SIGPIPE`` so writes to a closed stdout do not kill the process.

    The agent's stdout is the kubectl-exec channel; when the client tears it
    down the kernel would otherwise deliver ``SIGPIPE`` mid-write and abort
    the process before the fault-tolerant stdout wrapper has a chance to
    observe the ``EPIPE`` at the Python layer.

    Failures to install the handler (unusual signal environments, restricted
    sandboxes) are silently tolerated.
    """
    with contextlib.suppress(OSError, ValueError):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)


def _install_termination_handlers() -> None:
    """Install ``SIGTERM`` / ``SIGINT`` handlers for prompt, ordered shutdown.

    The handler sets the module-level ``_TERMINATE`` flag and closes the
    stdin file descriptor, which unblocks the stdin-reader loop stuck inside
    a C-level ``read()`` call and lets :func:`main` proceed to ordered
    teardown (dispatcher → stats sampler → writer).

    Handler installation failures are silently ignored to keep the agent
    usable in restricted environments where signal setup is disallowed.
    """

    def _handle(_signum: int, _frame: object | None) -> None:
        """Signal handler: request termination and unblock the stdin reader.

        Args:
            _signum: Signal number (unused).
            _frame: Current stack frame at interruption (unused).
        """
        global _TERMINATE  # noqa: PLW0603
        _TERMINATE = True
        # The stdin reader loop iterates ``for raw_line in sys.stdin`` which
        # blocks inside a C-level ``read()``; a bare ``_TERMINATE`` flag is
        # only inspected between lines.  Closing the underlying fd unblocks
        # the pending read and propagates EOF to the dispatcher, letting
        # ``main()`` shut down promptly on SIGTERM / SIGINT.
        with contextlib.suppress(OSError, ValueError, AttributeError):
            fd = sys.stdin.fileno()
            # ``os.close`` rather than ``sys.stdin.close`` avoids re-entrant
            # buffer flush inside the signal handler.
            os.close(fd)

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(OSError, ValueError):
            signal.signal(sig, _handle)


class _FaultTolerantStdout:
    """Write wrapper that latches into a "dead" state on the first ``OSError``.

    Protects the writer thread from the C-level ``"socket.send() raised
    exception."`` stderr noise that CPython prints when stdout is a broken
    pipe. Once latched dead, subsequent writes and flushes become no-ops so
    callers observe a uniform terminal state rather than sporadic errors.

    Thread-safety: backed by :class:`threading.Event` for the dead flag,
    giving cross-thread visibility without explicit locks. Concurrent writes
    on the wrapped stream are not serialised by this class — the agent's
    :class:`_FrameWriter` is the sole producer.

    Attributes:
        _inner: The underlying writable stream (typically ``sys.stdout``).
        _dead: Event set the first time a write or flush raised ``OSError``.
    """

    __slots__ = ("_inner", "_dead")

    def __init__(self, inner: _WritableStream) -> None:
        """Wrap *inner* with fault-tolerant write semantics.

        Args:
            inner: Object implementing :class:`_WritableStream` (typically
                ``sys.stdout``) that receives the actual writes.
        """
        self._inner: _WritableStream = inner
        # ``threading.Event`` rather than plain bool — gives cross-thread
        # ordering guarantees (and a cheap wait primitive if ever needed).
        self._dead = threading.Event()

    def write(self, s: str) -> int:
        """Write *s* to the wrapped stream or become dead on first ``OSError``.

        Args:
            s: String to write; must already include any required newline.

        Returns:
            Number of characters written as reported by the underlying
            stream, or ``0`` when the wrapper is (or has just become) dead.
        """
        if self._dead.is_set():
            return 0
        try:
            return self._inner.write(s)
        except OSError:
            self._dead.set()
            return 0

    def flush(self) -> None:
        """Flush the wrapped stream, latching dead on ``OSError``.

        A no-op once the wrapper has already been marked dead.
        """
        if self._dead.is_set():
            return
        try:
            self._inner.flush()
        except OSError:
            self._dead.set()

    @property
    def is_dead(self) -> bool:
        """``True`` once any write or flush has raised ``OSError``."""
        return self._dead.is_set()

    def mark_dead(self) -> None:
        """Force the wrapper into the dead state.

        Used by :meth:`_FrameWriter.emit_data` when the per-call hard
        deadline elapses so that every worker blocked on the full data queue
        observes ``is_dead`` and unwinds cleanly, even though no ``OSError``
        has yet been raised by the underlying stream.
        """
        self._dead.set()


def _b64encode(data: bytes) -> str:
    """URL-safe base64-encode *data* with trailing ``=`` padding stripped.

    Mirrors the client-side ``encode_data_frame`` / ``decode_binary_payload``
    helpers so the agent and the session layer remain byte-compatible.

    Args:
        data: Raw bytes to encode.

    Returns:
        ASCII string containing the URL-safe base64 representation of
        *data* without any ``=`` padding characters.
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
    """URL-safe base64-decode *s*, re-adding padding stripped by :func:`_b64encode`.

    Args:
        s: Unpadded URL-safe base64 string produced by :func:`_b64encode`
            or any compatible encoder.

    Returns:
        The decoded raw bytes.

    Raises:
        ValueError: If *s* is not a valid URL-safe base64 string.
    """
    padding = (4 - len(s) % 4) % 4
    try:
        return base64.urlsafe_b64decode(s + "=" * padding)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64url: {s!r}") from exc


def _log(level: str, msg: str, *args: object) -> None:
    """Write agent diagnostics to stderr without polluting the frame channel.

    The stdout channel is reserved for the frame protocol; all operational
    logging (warnings, debug traces, errors) is emitted to stderr, prefixed
    with an ISO-8601 UTC timestamp and the severity label.

    Args:
        level: Severity name — one of ``"debug"``, ``"info"``, ``"warning"``
            or ``"error"`` (case-insensitive). Unknown levels default to
            ``warning`` priority.
        msg: ``%``-style format string.
        *args: Arguments interpolated into *msg* when non-empty.
    """
    lvl = _LOG_LEVELS.get(level.lower(), 30)
    if lvl < _LOG_LEVEL:
        return
    text = msg % args if args else msg
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write(f"{ts} {level.upper():7s} agent: {text}\n")
    sys.stderr.flush()


def _make_frame(*parts: str) -> str:
    """Build a protocol frame line from its colon-separated *parts*.

    Args:
        *parts: Frame type followed by each field value, already encoded as
            text (IDs, base64url payloads, etc.).

    Returns:
        A single string of the form ``<<<EXECTUNNEL:p1:p2:...>>>`` — ready
        to be written to stdout followed by a newline.
    """
    return f"{_FRAME_PREFIX}{':'.join(parts)}{_FRAME_SUFFIX}"


def _make_error_frame(conn_id: str, reason: str) -> str:
    """Build an ``ERROR`` frame carrying a human-readable *reason*.

    Args:
        conn_id: Connection or flow identifier the error refers to.
        reason: Free-form explanation; encoded as UTF-8 and then base64url
            with padding stripped to satisfy the frame grammar.

    Returns:
        A fully-formatted ``ERROR`` frame ready to be emitted.
    """
    return _make_frame("ERROR", conn_id, _b64encode(reason.encode()))


def _parse_host_port(
    raw: str,
    frame_id: str,
    frame_type: str,
) -> tuple[str, int] | None:
    """Parse a ``host:port`` (or bracketed ``[host]:port``) target string.

    Accepts both bare-host and bracketed forms, the latter required for
    literal IPv6 addresses. On failure the reason is logged at DEBUG level
    with the originating frame identifier so operators can correlate malformed
    input with its client-side emitter.

    Args:
        raw: Target field as received from the client.
        frame_id: Connection or flow ID the raw target belongs to — used
            only for diagnostic logging.
        frame_type: Frame type name (e.g. ``"CONN_OPEN"``) used only for
            diagnostic logging.

    Returns:
        A ``(host, port)`` tuple on success, or ``None`` when *raw* is
        malformed, the port is non-numeric, or the port is outside the
        ``1..65535`` range.
    """
    if raw.startswith("["):
        bracket_end = raw.find("]")
        if bracket_end == -1 or raw[bracket_end + 1 : bracket_end + 2] != ":":
            _log(
                "debug",
                "malformed bracketed host in %s payload for %s: %r",
                frame_type,
                frame_id,
                raw,
            )
            return None
        host = raw[1:bracket_end]
        port_str = raw[bracket_end + 2 :]
    else:
        host, sep, port_str = raw.rpartition(":")
        if not sep or not host:
            _log(
                "debug",
                "invalid %s payload for %s: %r",
                frame_type,
                frame_id,
                raw,
            )
            return None

    if not port_str:
        _log(
            "debug",
            "missing port in %s payload for %s: %r",
            frame_type,
            frame_id,
            raw,
        )
        return None

    try:
        port = int(port_str)
    except ValueError:
        _log(
            "debug",
            "invalid %s port for %s: %r",
            frame_type,
            frame_id,
            port_str,
        )
        return None
    if port <= 0 or port > _MAX_TCP_UDP_PORT:
        _log(
            "debug",
            "out-of-range %s port for %s: %d",
            frame_type,
            frame_id,
            port,
        )
        return None
    return host, port


class _FrameWriter:
    """Serialise all frames onto stdout from a single daemon thread.

    The writer owns a :class:`_FaultTolerantStdout` wrapper and two internal
    queues:

    * Control queue (unbounded :class:`queue.SimpleQueue`) for frames that
      must not be dropped or reordered relative to their logical event
      (``CONN_ACK``, ``CONN_CLOSE``, ``ERROR``, ``UDP_CLOSE``, ``STATS``).
      These are always drained *before* data frames within an iteration.
    * Data queue (bounded :class:`queue.Queue`) for ``DATA`` / ``UDP_DATA``
      payloads. The bound is :data:`_STDOUT_DATA_QUEUE_CAP`; workers use it
      for backpressure (see module docstring, "Backpressure model").

    The writer exits cleanly when :meth:`stop` is called or when
    :class:`_FaultTolerantStdout` latches dead after an ``OSError``.

    Attributes:
        _STOP: Sentinel object enqueued to signal "drain and exit".
    """

    _STOP: object = object()

    def __init__(self) -> None:
        """Create the queues and immediately start the writer daemon thread."""
        self._out = _FaultTolerantStdout(sys.stdout)
        self._ctrl: queue.SimpleQueue[str | object] = queue.SimpleQueue()
        self._data: queue.Queue[str | object] = queue.Queue(
            maxsize=_STDOUT_DATA_QUEUE_CAP
        )
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="stdout-writer",
        )
        self._thread.start()

    def emit_ctrl(self, line: str) -> None:
        """Enqueue a control frame; never blocks and never drops.

        Control frames are written onto the unbounded control queue so that
        protocol-critical events (``CONN_ACK`` / ``CONN_CLOSE`` / ``ERROR``
        / ``UDP_CLOSE`` / ``STATS``) are never lost even under backpressure.
        Calls made after :meth:`stop` or once stdout is dead are silently
        dropped — no downstream consumer can possibly observe them.

        Args:
            line: Fully-formatted frame string without trailing newline.
        """
        if self._stopping or self._out.is_dead:
            return
        self._ctrl.put(line)

    def emit_data(self, line: str) -> None:
        """Enqueue a data frame, respecting liveness and a hard deadline.

        Polls the bounded data queue with :data:`_EMIT_DATA_PUT_TIMEOUT_SECS`
        per attempt so the calling worker observes shutdown signals promptly:

        * ``_TERMINATE`` — set by the SIGTERM/SIGINT handler.
        * ``self._stopping`` — set by :meth:`stop`.
        * ``self._out.is_dead`` — set once stdout has raised ``OSError``.

        If none of those fire but the queue stays full for the entire
        :data:`_EMIT_DATA_HARD_DEADLINE_SECS` window, the writer is
        force-marked dead. This guards against a silently half-dead TCP
        channel where no error and no signal ever arrive but the consumer
        has stopped reading.

        Args:
            line: Fully-formatted ``DATA`` / ``UDP_DATA`` frame string
                without trailing newline.
        """
        deadline = time.monotonic() + _EMIT_DATA_HARD_DEADLINE_SECS
        while not self._out.is_dead and not self._stopping and not _TERMINATE:
            try:
                self._data.put(line, timeout=_EMIT_DATA_PUT_TIMEOUT_SECS)
                return
            except queue.Full:
                if time.monotonic() >= deadline:
                    _log(
                        "warning",
                        "emit_data: hard deadline %.1fs exceeded — marking stdout dead",
                        _EMIT_DATA_HARD_DEADLINE_SECS,
                    )
                    self._out.mark_dead()
                    return
                continue

    def stop(self) -> None:
        """Request an ordered shutdown of the writer thread.

        Enqueues the stop sentinel on both queues so whichever loop the
        writer is currently servicing can observe it, then joins the thread
        with :data:`_WRITER_JOIN_TIMEOUT_SECS` as an upper bound. Idempotent.
        """
        if self._stopping:
            return
        self._stopping = True
        self._ctrl.put(self._STOP)
        with contextlib.suppress(queue.Full):
            self._data.put_nowait(self._STOP)
        self._thread.join(timeout=_WRITER_JOIN_TIMEOUT_SECS)

    @property
    def data_queue_size(self) -> int:
        """Current depth of the bounded data queue (sampled, may race)."""
        return self._data.qsize()

    @property
    def is_dead(self) -> bool:
        """``True`` once the underlying stdout wrapper has latched dead."""
        return self._out.is_dead

    def _run(self) -> None:
        """Writer-thread main loop: drain control then data queues in turn.

        The loop prioritises control frames — on every iteration it waits up
        to :data:`_WRITER_CTRL_POLL_SECS` for a control item, flushes any
        immediately-available follow-up control frames, and only then drains
        up to :data:`_WRITER_DATA_BATCH_SIZE` data frames to keep latency
        bounded for both classes. Exits on the stop sentinel or once stdout
        becomes dead.
        """
        out = self._out
        ctrl = self._ctrl
        data = self._data
        stop = self._STOP
        batch_size = _WRITER_DATA_BATCH_SIZE

        while True:
            if out.is_dead:
                return

            try:
                item = ctrl.get(timeout=_WRITER_CTRL_POLL_SECS)
            except queue.Empty:
                item = None

            if item is stop:
                self._drain_remaining(out, ctrl, data, stop)
                return

            if item is not None and isinstance(item, str):
                out.write(item + "\n")
                if out.is_dead:
                    return
                while True:
                    try:
                        nxt = ctrl.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is stop:
                        out.flush()
                        ctrl.put(stop)
                        break
                    if isinstance(nxt, str):
                        out.write(nxt + "\n")
                        if out.is_dead:
                            return

            batch = 0
            while batch < batch_size:
                if out.is_dead:
                    return
                try:
                    d = data.get_nowait()
                except queue.Empty:
                    break
                if d is stop:
                    ctrl.put(stop)
                    break
                if isinstance(d, str):
                    out.write(d + "\n")
                batch += 1

            if item is not None or batch > 0:
                out.flush()

    @staticmethod
    def _drain_remaining(
        out: _FaultTolerantStdout,
        ctrl: queue.SimpleQueue[str | object],
        data: queue.Queue[str | object],
        stop: object,
    ) -> None:
        """Best-effort flush of both queues after the stop sentinel is seen.

        Non-blocking: drains whatever is already queued and returns. Any
        stop sentinels encountered mid-drain are skipped so remaining real
        frames still reach stdout before the writer exits.

        Args:
            out: Fault-tolerant stdout wrapper.
            ctrl: Control-frame queue.
            data: Data-frame queue.
            stop: Sentinel object identifying "exit" entries in the queues.
        """
        while True:
            if out.is_dead:
                return
            try:
                c = ctrl.get_nowait()
            except queue.Empty:
                break
            if c is stop:
                continue
            if isinstance(c, str):
                out.write(c + "\n")

        while True:
            if out.is_dead:
                return
            try:
                d = data.get_nowait()
            except queue.Empty:
                break
            if d is stop:
                continue
            if isinstance(d, str):
                out.write(d + "\n")

        out.flush()


_writer: _FrameWriter | None = None


class _StatsSampler:
    """Periodic emitter of ``STATS`` control frames.

    Runs as a daemon thread. Every ``_STATS_SAMPLE_INTERVAL_SECS`` it
    builds a JSON snapshot of the module-level observability counters,
    base64url-encodes it, and emits a ``STATS`` frame via the existing
    control-frame lane (``_emit_ctrl``) so it inherits the same FIFO +
    priority semantics as ``CONN_ACK`` / ``CONN_CLOSE``.

    Frame format (new type, forward-compatible for old clients which
    fall through to the ``unknown frame type ignored`` path)::

        <<<EXECTUNNEL:STATS:<base64url-no-padding-JSON>>>>

    The decoded JSON is a single snapshot carrying byte/frame totals,
    stdout-queue depth, worker counts, and dispatch latency percentiles
    computed from the bounded sample ring. The client aggregates many
    snapshots into percentile distributions at report-generation time.
    """

    def __init__(
        self,
        dispatcher: _Dispatcher,
        interval_secs: float = _STATS_SAMPLE_INTERVAL_SECS,
    ) -> None:
        """Create the sampler (thread is not started until :meth:`start`).

        Args:
            dispatcher: Dispatcher instance queried each tick for worker
                counts included in the snapshot payload.
            interval_secs: Interval in seconds between consecutive STATS
                emissions; defaults to :data:`_STATS_SAMPLE_INTERVAL_SECS`.
        """
        self._dispatcher = dispatcher
        self._interval = interval_secs
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="stats-sampler"
        )

    def start(self) -> None:
        """Start the sampler daemon thread."""
        self._thread.start()

    def stop(self) -> None:
        """Request sampler shutdown and best-effort join.

        The sampler thread is a daemon, so process exit is not gated on it;
        the short join timeout merely gives it a chance to emit a final
        snapshot cleanly before the writer is stopped.
        """
        self._stop.set()
        # Daemon thread — best effort join; do not block shutdown.
        self._thread.join(timeout=0.5)

    def _snapshot(self) -> dict[str, object]:
        """Build a single JSON-serialisable snapshot of current counters.

        Latency percentiles are derived from a sorted copy of the bounded
        dispatch-sample ring. ``data_queue_depth`` reflects the writer's
        queue size at the instant of the call (sampled, may race).

        Returns:
            A ``dict`` ready to be serialised by :func:`json.dumps` and
            base64url-encoded into the payload of a ``STATS`` frame.
        """
        with _DISPATCH_SAMPLES_LOCK:
            samples = list(_DISPATCH_SAMPLES)
        samples.sort()
        n = len(samples)

        def pct(p: float) -> float:
            if n == 0:
                return 0.0
            idx = min(n - 1, int(p * n))
            return samples[idx]

        tcp_count, udp_count = self._dispatcher.worker_counts()
        data_queue_depth = _writer.data_queue_size if _writer is not None else 0
        return {
            "ts": time.time(),
            "agent_version": _AGENT_VERSION,
            "tx_bytes_total": _TX_BYTES_TOTAL,
            "rx_bytes_total": _RX_BYTES_TOTAL,
            "frames_tx_total": _FRAMES_TX_TOTAL,
            "frames_rx_total": _FRAMES_RX_TOTAL,
            "data_queue_depth": data_queue_depth,
            "data_queue_cap": _STDOUT_DATA_QUEUE_CAP,
            "tcp_worker_count": tcp_count,
            "udp_worker_count": udp_count,
            "dispatch_ms_p50": pct(0.50) * 1000.0,
            "dispatch_ms_p95": pct(0.95) * 1000.0,
            "dispatch_ms_p99": pct(0.99) * 1000.0,
            "dispatch_samples": n,
        }

    def _run(self) -> None:
        """Sampler-thread main loop.

        Waits one interval before the first emission so the leading
        ``STATS`` frame does not race with ``AGENT_READY`` on session
        bring-up; thereafter emits once per interval until stop is
        requested or ``_TERMINATE`` is set. Exceptions inside the loop are
        captured and logged so the sampler never crashes the agent.
        """
        # Stagger the first emission so it does not race with AGENT_READY.
        if self._stop.wait(timeout=self._interval):
            return
        while not self._stop.is_set() and not _TERMINATE:
            try:
                snap = self._snapshot()
                payload = _b64encode(_json.dumps(snap, separators=(",", ":")).encode())
                _emit_ctrl(_make_frame("STATS", payload))
            except Exception as exc:  # noqa: BLE001 — sampler must never crash
                _log("debug", "stats sampler error: %s", exc)
            if self._stop.wait(timeout=self._interval):
                return


_stats_sampler: _StatsSampler | None = None


def _emit_ctrl(line: str) -> None:
    """Emit a control frame through the shared :class:`_FrameWriter`.

    If the writer has not yet been initialised (e.g. during early startup
    before :func:`main` installs it) the frame is written directly to
    stdout so boot-time diagnostics are not lost.

    Args:
        line: Fully-formatted frame string without trailing newline.
    """
    if _writer is not None:
        _writer.emit_ctrl(line)
    else:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _emit_data(line: str) -> None:
    """Emit a data frame, blocking the caller when the data queue is full.

    Also bumps the module-level byte and frame counters used by the stats
    sampler; the byte total includes the trailing newline so it reflects
    actual stdout traffic, not just payload size.

    Args:
        line: Fully-formatted ``DATA`` / ``UDP_DATA`` frame string.
    """
    # Count bytes before the frame is enqueued. We count the wire-level
    # frame length (including marker + base64 overhead) so the number
    # reflects real stdout traffic, not raw payload.
    global _TX_BYTES_TOTAL, _FRAMES_TX_TOTAL  # noqa: PLW0603
    _TX_BYTES_TOTAL += len(line) + 1  # +1 for newline
    _FRAMES_TX_TOTAL += 1
    if _writer is not None:
        _writer.emit_data(line)
    else:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _stdout_backpressure_active() -> bool:
    """Return whether TCP workers should pause ``recv()`` this iteration.

    Returns:
        ``True`` when the writer's data queue is at or above the
        :data:`_STDOUT_DATA_QUEUE_BACKPRESSURE_THRESHOLD` watermark, in
        which case TCP workers leave their remote sockets out of the
        ``select()`` readable set so kernel receive buffers absorb the
        backpressure all the way to the remote sender.
    """
    if _writer is None:
        return False
    return _writer.data_queue_size >= _STDOUT_DATA_QUEUE_BACKPRESSURE_THRESHOLD


def _disable_echo() -> None:
    """Disable terminal echo on stdin so shell output does not pollute stdout.

    When the agent is launched through ``kubectl exec`` with a TTY allocated,
    the pseudo-terminal would otherwise echo every incoming frame back out
    on stdout, corrupting the frame channel. Non-TTY stdin (e.g. when run
    via a pipe in tests) is detected via ``termios.error`` and skipped.
    """
    try:
        fd = sys.stdin.fileno()
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~(termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        _log("debug", "stdin is not a tty; skipping echo disable")


def _send_all_nonblocking(sock: socket.socket, data: bytes) -> bool:
    """Write *data* to a non-blocking socket, waiting for writability as needed.

    Uses a :func:`select.select`-based wait on ``BlockingIOError`` rather than
    blocking inside ``send()`` so the calling worker thread never stalls
    indefinitely on a slow remote peer. The per-wait timeout is
    :data:`_SEND_WRITABLE_TIMEOUT_SECS`; exceeding it or any other
    :class:`OSError` causes the function to return ``False`` so the caller
    can tear the connection down.

    Args:
        sock: Connected non-blocking TCP socket.
        data: Payload bytes to write in full.

    Returns:
        ``True`` when all bytes were transmitted; ``False`` on a closed
        peer (``sent == 0``), write-readiness timeout, or ``OSError``.
    """
    offset = 0
    total = len(data)
    while offset < total:
        try:
            sent = sock.send(data[offset:])
            if sent == 0:
                return False
            offset += sent
        except BlockingIOError:
            _, writable, _ = select.select([], [sock], [], _SEND_WRITABLE_TIMEOUT_SECS)
            if not writable:
                return False
        except OSError:
            return False
    return True


class TcpConnectionWorker:
    """Thread-backed TCP connection bridging one ``CONN_OPEN`` session.

    Each worker owns a single outbound TCP socket and a self-pipe used to
    wake its ``select()`` loop from the dispatcher thread. Inbound payload
    bytes from the client are buffered in :attr:`_inbound` (bounded by
    :data:`_MAX_TCP_INBOUND_CHUNKS`); outbound bytes from the remote peer
    are forwarded as base64url-encoded ``DATA`` frames.

    Protocol contract:
        * ``CONN_ACK`` is emitted as soon as the TCP connect succeeds.
        * ``CONN_CLOSE`` is always emitted in ``_run``'s ``finally`` block,
          unless :attr:`_suppress_close_frame` is set because an ``ERROR``
          frame has already been sent (saturation path).
        * On connect failure an ``ERROR`` frame replaces ``CONN_ACK`` and
          no ``CONN_CLOSE`` follows.

    Args:
        conn_id: Client-assigned connection identifier (matches ``_ID_RE``).
        host: Destination hostname or IP literal.
        port: Destination TCP port.
        dns_cache: Shared DNS cache for address resolution.
        on_close: Optional callback invoked from the worker thread after
            teardown, typically used by the dispatcher to evict the worker
            from its registry.
    """

    def __init__(
        self,
        conn_id: str,
        host: str,
        port: int,
        dns_cache: _DnsCache,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        """Initialise state; the worker thread is not started until :meth:`start`."""
        self.conn_id = conn_id
        self._host = host
        self._port = port
        self._dns_cache = dns_cache
        self._on_close = on_close

        self._sock: socket.socket | None = None
        self._inbound: list[bytes] = []
        self._inbound_lock = threading.Lock()
        # H1: once ``_run`` has finished and cleared ``_inbound``, any late
        # ``feed()`` call would silently leak bytes into a list that is
        # never read again.  We gate feeds on this flag (held under
        # ``_inbound_lock``) to log + drop instead.
        self._final_closed = False

        self._notify_r, self._notify_w = os.pipe()
        os.set_blocking(self._notify_r, False)
        os.set_blocking(self._notify_w, False)
        # H2: guard os.write to the self-pipe against fd-reuse after close.
        # ``_pipe_lock`` serialises write-vs-close; ``_pipe_closed`` is set
        # under the lock before the fd is released.
        self._pipe_lock = threading.Lock()
        self._pipe_closed = False

        self._closed = False
        self._saturated = False
        self._drop_count: int = 0
        # When true, ``_run`` will NOT emit a trailing ``CONN_CLOSE`` because
        # an ``ERROR`` frame was already emitted and the client has torn the
        # connection down.  Avoids noisy orphan-frame metrics on the client.
        self._suppress_close_frame = False

        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"tcp-conn-{conn_id}"
        )

    def _wake(self) -> None:
        """Write one byte to the self-pipe under the pipe lock (H2)."""
        with self._pipe_lock:
            if self._pipe_closed:
                return
            with contextlib.suppress(OSError):
                os.write(self._notify_w, b"\x00")

    def _close_pipe(self) -> None:
        """Close both ends of the self-pipe atomically with the guard flag."""
        with self._pipe_lock:
            if self._pipe_closed:
                return
            self._pipe_closed = True
            with contextlib.suppress(OSError):
                os.close(self._notify_r)
            with contextlib.suppress(OSError):
                os.close(self._notify_w)

    def start(self) -> None:
        """Start the worker thread (connect + IO loop)."""
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        """Join the worker thread.

        Args:
            timeout: Maximum seconds to wait; ``None`` waits indefinitely.
        """
        self._thread.join(timeout=timeout)

    @property
    def is_alive(self) -> bool:
        """``True`` while the worker thread has not yet terminated."""
        return self._thread.is_alive()

    def feed(self, data: bytes) -> None:
        """Hand client-originated *data* to the worker for onward TCP transmit.

        Thread-safe: called from the dispatcher thread. Applies inbound
        flow-control: once :data:`_MAX_TCP_INBOUND_CHUNKS` queued chunks
        accumulate, the worker is declared saturated — an ``ERROR`` frame
        is emitted, the connection is marked closed, and subsequent
        feeds are silently dropped. Feeds received after the worker has
        finally torn down (``_final_closed``) are logged at DEBUG.

        Args:
            data: Raw bytes decoded from a ``DATA`` frame payload.
        """
        saturated = False
        with self._inbound_lock:
            # H1: worker already torn down — log and drop so late bytes are
            # not silently leaked into a dead list.
            if self._final_closed:
                _log(
                    "debug",
                    "conn %s late feed dropped (%d bytes) — worker already closed",
                    self.conn_id,
                    len(data),
                )
                return
            if self._saturated:
                return
            if len(self._inbound) >= _MAX_TCP_INBOUND_CHUNKS:
                self._saturated = True
                self._closed = True
                saturated = True
            else:
                self._inbound.append(data)

        if saturated:
            self._suppress_close_frame = True
            _emit_ctrl(_make_error_frame(self.conn_id, "agent tcp inbound saturated"))
            _log(
                "warning",
                "conn %s inbound saturated; closing connection",
                self.conn_id,
            )
            # Wake the IO loop (if already running) so it observes ``_closed``
            # and exits promptly.
            self._wake()
            return

        self._wake()

    def signal_eof(self) -> None:
        """Request a half-close: no more inbound data, drain and send FIN.

        The IO loop observes ``_closed`` and, after the outbound queue is
        empty, performs ``shutdown(SHUT_WR)`` on the remote socket and
        waits up to :data:`_HALF_CLOSE_DEADLINE_SECS` for the remote peer
        to close its side.
        """
        self._closed = True
        self._wake()

    def _run(self) -> None:
        """Worker-thread main: connect, emit ``CONN_ACK``, run IO, clean up.

        Exactly one of ``CONN_ACK`` or an ``ERROR`` frame is emitted per
        worker lifecycle. Registry eviction is delegated to :attr:`_on_close`
        so the dispatcher remains the single source of truth for worker
        membership.
        """
        cid = self.conn_id
        try:
            sock = _create_connection_cached(
                self._dns_cache,
                self._host,
                self._port,
                timeout=_TCP_CONNECT_TIMEOUT_SECS,
            )
            sock.setblocking(False)
        except OSError as exc:
            _emit_ctrl(_make_error_frame(cid, str(exc)))
            _log("warning", "conn %s connect failed: %s", cid, exc)
            self._close_pipe()
            with self._inbound_lock:
                self._final_closed = True
                self._inbound.clear()
            if self._on_close is not None:
                self._on_close()
            return

        # H3: saturation may have fired during connect.  If so, the client
        # already received ERROR and will treat CONN_ACK as belonging to an
        # unknown connection (orphaned frame).  Skip CONN_ACK entirely —
        # ``_suppress_close_frame`` is already set, so the client sees only
        # the ERROR frame and a clean local teardown.
        if self._closed:
            _log(
                "debug",
                "conn %s already closed before CONN_ACK — suppressing ACK",
                cid,
            )
            with contextlib.suppress(OSError):
                sock.close()
            self._close_pipe()
            with self._inbound_lock:
                self._final_closed = True
                self._inbound.clear()
            if self._on_close is not None:
                self._on_close()
            return

        self._sock = sock
        _emit_ctrl(_make_frame("CONN_ACK", cid))

        try:
            self._io_loop(sock, cid)
        finally:
            # Count any client-side bytes still queued at close — the remote
            # peer closed (or we were saturated) before we could forward them.
            with self._inbound_lock:
                dropped = sum(len(c) for c in self._inbound)
                self._inbound.clear()
                # H1: mark final so late ``feed()`` calls short-circuit
                # with a log instead of silently appending to a dead list.
                self._final_closed = True
            if dropped:
                _log(
                    "debug",
                    "conn %s closed with %d bytes of undelivered client data",
                    cid,
                    dropped,
                )
            sock.close()
            self._close_pipe()
            if not self._suppress_close_frame:
                _emit_ctrl(_make_frame("CONN_CLOSE", cid))
            if self._on_close is not None:
                self._on_close()

    def _io_loop(self, sock: socket.socket, cid: str) -> None:
        """Pump bytes between *sock* and the client until either end closes.

        Uses :func:`select.select` with the remote socket and the self-pipe
        as readable sources; the remote socket is omitted from the readable
        set when :func:`_stdout_backpressure_active` is true so OS receive
        buffers can absorb backpressure upstream. Honors half-close: once
        the client has signalled EOF and all pending bytes are forwarded,
        the worker performs ``SHUT_WR`` and waits for the remote FIN up to
        :data:`_HALF_CLOSE_DEADLINE_SECS`.

        Args:
            sock: Connected non-blocking TCP socket.
            cid: Connection identifier — used to tag outbound frames.
        """
        local_shut = False
        local_shut_deadline: float | None = None

        while True:
            # When stdout backpressure is active, exclude the remote socket
            # from the readable set.  This prevents the busy-loop that would
            # occur if select() returned immediately because the socket has
            # unread data that we'd skip reading anyway.  The self-pipe is
            # always monitored so inbound data from the client can still be
            # forwarded to the remote.
            backpressure = _stdout_backpressure_active()
            read_fds: list[int | socket.socket] = [self._notify_r]
            if not backpressure:
                read_fds.append(sock)

            readable, _, errored = select.select(
                read_fds, [], [sock], _SELECT_TIMEOUT_SECS
            )

            if errored:
                break

            remote_closed = False
            if sock in readable:
                try:
                    chunk = sock.recv(_TCP_RECV_CHUNK_BYTES)
                except OSError:
                    break
                if not chunk:
                    remote_closed = True
                else:
                    _emit_data(_make_frame("DATA", cid, _b64encode(chunk)))

            if self._notify_r in readable:
                with contextlib.suppress(OSError):
                    os.read(self._notify_r, 4_096)

            if not local_shut:
                with self._inbound_lock:
                    pending, self._inbound = self._inbound, []
                for chunk in pending:
                    if not _send_all_nonblocking(sock, chunk):
                        return
            else:
                pending = []

            if remote_closed:
                if not local_shut:
                    with self._inbound_lock:
                        remaining, self._inbound = self._inbound, []
                    for chunk in remaining:
                        if not _send_all_nonblocking(sock, chunk):
                            break
                break

            if self._closed and not local_shut:
                with self._inbound_lock:
                    still_pending = bool(self._inbound)
                if not still_pending and not pending:
                    local_shut = True
                    local_shut_deadline = time.monotonic() + _HALF_CLOSE_DEADLINE_SECS
                    with contextlib.suppress(OSError):
                        sock.shutdown(socket.SHUT_WR)

            if (
                local_shut
                and local_shut_deadline is not None
                and time.monotonic() >= local_shut_deadline
            ):
                break


class UdpFlowWorker:
    """Thread-backed UDP flow bridging one ``UDP_OPEN`` session.

    Mirrors :class:`TcpConnectionWorker` for UDP semantics: a single
    connected :class:`socket.socket` of type ``SOCK_DGRAM`` is used so
    receive side-steps require no per-datagram address tracking. Inbound
    datagrams from the client are buffered up to
    :data:`_MAX_UDP_INBOUND_DGRAMS`; further datagrams are dropped per
    UDP semantics with rate-limited warnings.

    Protocol contract:
        * ``UDP_CLOSE`` is always emitted in ``_run``'s ``finally`` block
          — including on resolve or connect failure — so the client can
          deterministically reclaim the flow ID.

    Args:
        flow_id: Client-assigned flow identifier (matches ``_ID_RE``).
        host: Destination hostname or IP literal.
        port: Destination UDP port.
        dns_cache: Shared DNS cache for address resolution.
        on_close: Optional callback invoked from the worker thread after
            teardown, typically used by the dispatcher to evict the flow.
    """

    def __init__(
        self,
        flow_id: str,
        host: str,
        port: int,
        dns_cache: _DnsCache,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        """Initialise state; the worker thread is not started until :meth:`start`."""
        self._id = flow_id
        self._host = host
        self._port = port
        self._dns_cache = dns_cache
        self._on_close = on_close

        self._sock: socket.socket | None = None
        self._inbound: list[bytes] = []
        self._inbound_lock = threading.Lock()
        self._final_closed = False  # H1 parity for UDP

        self._notify_r, self._notify_w = os.pipe()
        os.set_blocking(self._notify_r, False)
        os.set_blocking(self._notify_w, False)
        # H2 parity: guard self-pipe against fd-reuse after close.
        self._pipe_lock = threading.Lock()
        self._pipe_closed = False

        self._closed = False
        self._drop_count: int = 0
        self._send_block_count: int = 0

        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"udp-flow-{flow_id}"
        )

    def _wake(self) -> None:
        """Wake the IO loop by writing one byte to the self-pipe.

        Serialised against :meth:`_close_pipe` via ``_pipe_lock`` so a
        write cannot race with fd reuse after close (H2 parity with TCP).
        """
        with self._pipe_lock:
            if self._pipe_closed:
                return
            with contextlib.suppress(OSError):
                os.write(self._notify_w, b"\x00")

    def _close_pipe(self) -> None:
        """Close both ends of the self-pipe atomically with the guard flag."""
        with self._pipe_lock:
            if self._pipe_closed:
                return
            self._pipe_closed = True
            with contextlib.suppress(OSError):
                os.close(self._notify_r)
            with contextlib.suppress(OSError):
                os.close(self._notify_w)

    def start(self) -> None:
        """Start the worker thread (resolve + connect + IO loop)."""
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        """Join the worker thread.

        Args:
            timeout: Maximum seconds to wait; ``None`` waits indefinitely.
        """
        self._thread.join(timeout=timeout)

    @property
    def is_alive(self) -> bool:
        """``True`` while the worker thread has not yet terminated."""
        return self._thread.is_alive()

    def feed(self, data: bytes) -> None:
        """Enqueue *data* for transmission to the remote UDP peer.

        Silently drops the datagram when the inbound queue is full, per
        UDP semantics, and emits a rate-limited warning every
        :data:`_UDP_DROP_WARN_EVERY` drops.

        Args:
            data: Raw datagram bytes decoded from a ``UDP_DATA`` frame.
        """
        with self._inbound_lock:
            if self._final_closed:
                return
            if len(self._inbound) >= _MAX_UDP_INBOUND_DGRAMS:
                self._drop_count += 1
                if (
                    self._drop_count == 1
                    or self._drop_count % _UDP_DROP_WARN_EVERY == 0
                ):
                    _log(
                        "warning",
                        "udp flow %s inbound saturated; dropping datagram (drops=%d)",
                        self._id,
                        self._drop_count,
                    )
                return
            self._inbound.append(data)

        self._wake()

    def close(self) -> None:
        """Request termination of the IO loop after draining pending sends."""
        self._closed = True
        self._wake()

    def _run(self) -> None:
        """Worker-thread main: resolve, connect the UDP socket, run IO, clean up.

        Emits exactly one ``UDP_CLOSE`` frame per flow lifecycle, including
        on resolution or connect failure so the client can reclaim the
        flow ID deterministically.
        """
        fid = self._id

        try:
            infos = self._dns_cache.getaddrinfo(
                self._host, self._port, socktype=socket.SOCK_DGRAM
            )
            if not infos:
                raise OSError(f"could not resolve {self._host!r}")
        except OSError as exc:
            _emit_ctrl(_make_frame("UDP_CLOSE", fid))
            _log("warning", "udp flow %s resolve failed: %s", fid, exc)
            self._close_pipe()
            with self._inbound_lock:
                self._final_closed = True
                self._inbound.clear()
            if self._on_close is not None:
                self._on_close()
            return

        sock: socket.socket | None = None
        last_exc: OSError | None = None
        for family, _, _, _, addr in infos:
            candidate = socket.socket(family, socket.SOCK_DGRAM)
            try:
                candidate.setblocking(False)
                candidate.connect(addr)
                sock = candidate
                break
            except OSError as exc:
                last_exc = exc
                with contextlib.suppress(OSError):
                    candidate.close()

        if sock is None:
            _emit_ctrl(_make_frame("UDP_CLOSE", fid))
            _log("warning", "udp flow %s open failed: %s", fid, last_exc)
            self._close_pipe()
            with self._inbound_lock:
                self._final_closed = True
                self._inbound.clear()
            if self._on_close is not None:
                self._on_close()
            return

        self._sock = sock
        try:
            self._io_loop(sock, fid)
        finally:
            sock.close()
            self._close_pipe()
            with self._inbound_lock:
                self._final_closed = True
                self._inbound.clear()
            _emit_ctrl(_make_frame("UDP_CLOSE", fid))
            if self._on_close is not None:
                self._on_close()

    def _io_loop(self, sock: socket.socket, fid: str) -> None:
        """Pump datagrams between *sock* and the client until closed.

        Transient per-datagram send errors (``ECONNREFUSED``, ``EMSGSIZE``,
        ``ENETUNREACH``, etc.) are treated as drops and the flow stays
        alive; any other :class:`OSError` indicates the socket itself is
        broken and terminates the loop.

        Args:
            sock: Connected non-blocking UDP socket.
            fid: Flow identifier — used to tag outbound ``UDP_DATA`` frames.
        """
        while not self._closed:
            readable, _, errored = select.select(
                [sock, self._notify_r], [], [sock], _SELECT_TIMEOUT_SECS
            )

            if errored:
                break

            if sock in readable:
                try:
                    chunk = sock.recv(_MAX_UDP_DGRAM_BYTES)
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                        break
                    chunk = b""
                if chunk:
                    _emit_data(_make_frame("UDP_DATA", fid, _b64encode(chunk)))

            if self._notify_r in readable:
                with contextlib.suppress(OSError):
                    os.read(self._notify_r, 4_096)

            with self._inbound_lock:
                pending, self._inbound = self._inbound, []

            for dgram in pending:
                try:
                    sock.send(dgram)
                except BlockingIOError:
                    # UDP send buffer full — drop the datagram (UDP semantics).
                    self._send_block_count += 1
                    if (
                        self._send_block_count == 1
                        or self._send_block_count % _UDP_DROP_WARN_EVERY == 0
                    ):
                        _log(
                            "debug",
                            "udp flow %s send blocked; datagram dropped "
                            "(send_blocks=%d)",
                            fid,
                            self._send_block_count,
                        )
                except OSError as exc:
                    # M4: classify UDP send errors.  ICMP unreachable
                    # (ECONNREFUSED), oversized datagram (EMSGSIZE), and
                    # transient routing issues (ENETUNREACH / EHOSTUNREACH
                    # / ENETDOWN / EHOSTDOWN) are per-datagram faults —
                    # drop the datagram and keep the flow alive.  Anything
                    # else (EBADF, ENOTCONN, ...) indicates the socket
                    # itself is broken and we must tear down.
                    transient = {
                        errno.ECONNREFUSED,
                        errno.EMSGSIZE,
                        errno.ENETUNREACH,
                        errno.EHOSTUNREACH,
                        errno.ENETDOWN,
                        errno.EHOSTDOWN,
                    }
                    if exc.errno in transient:
                        self._send_block_count += 1
                        if (
                            self._send_block_count == 1
                            or self._send_block_count % _UDP_DROP_WARN_EVERY == 0
                        ):
                            # ``errno.errorcode`` maps int → symbolic name;
                            # fall back to the raw numeric string when the
                            # platform does not expose a name for this code
                            # (or when ``exc.errno`` is ``None``).
                            err_no = exc.errno
                            err_label: str = (
                                errno.errorcode.get(err_no, str(err_no))
                                if err_no is not None
                                else "None"
                            )
                            _log(
                                "debug",
                                "udp flow %s send transient error %s; "
                                "datagram dropped (send_blocks=%d)",
                                fid,
                                err_label,
                                self._send_block_count,
                            )
                        continue
                    return


class _Dispatcher:
    """Route incoming frames from stdin to per-connection and per-flow workers.

    The dispatcher owns the authoritative registries of live
    :class:`TcpConnectionWorker` and :class:`UdpFlowWorker` instances, a
    per-process DNS cache shared by every worker, and the stdin reader
    loop. It exposes a single public :meth:`run` entry-point plus the
    :meth:`worker_counts` and :meth:`shutdown` helpers used by the stats
    sampler and :func:`main` respectively.

    Thread-safety: :attr:`_conn_map` and :attr:`_udp_map` are guarded by
    dedicated locks. Workers evict themselves from the maps through their
    ``on_close`` callbacks — the dispatcher never tears down a worker
    implicitly.

    Args:
        fixed_host: In portforward mode, the fixed destination host that
            overrides the address carried on each ``CONN_OPEN`` frame. Pass
            ``None`` for tunnel (SOCKS5) mode.
        fixed_port: Fixed destination port that pairs with *fixed_host*;
            must be provided when *fixed_host* is set.
    """

    def __init__(
        self,
        fixed_host: str | None,
        fixed_port: int | None,
    ) -> None:
        """Initialise registries, locks, and the shared DNS cache."""
        self._fixed_host = fixed_host
        self._fixed_port = fixed_port

        self._conn_map: dict[str, TcpConnectionWorker] = {}
        self._udp_map: dict[str, UdpFlowWorker] = {}
        self._conn_lock = threading.Lock()
        self._udp_lock = threading.Lock()
        self._dns_cache = _DnsCache()

    def dispatch(self, line: str) -> None:
        """Parse and dispatch one raw frame line from stdin.

        Updates the inbound byte / frame counters and records a dispatch
        latency sample so malformed, oversized, or unknown frames are still
        visible to the stats sampler.

        Args:
            line: One newline-stripped line read from stdin.
        """
        # Step 2: count inbound bytes / frames at the earliest point so the
        # sampler sees real ingress even for malformed / oversized frames.
        global _RX_BYTES_TOTAL, _FRAMES_RX_TOTAL  # noqa: PLW0603
        _RX_BYTES_TOTAL += len(line) + 1  # +1 for newline consumed by stdin iter
        _FRAMES_RX_TOTAL += 1
        dispatch_started = time.monotonic()
        try:
            self._dispatch_inner(line)
        finally:
            _record_dispatch_sample(time.monotonic() - dispatch_started)

    def _dispatch_inner(self, line: str) -> None:
        """Validate framing and route *line* to the matching handler.

        Silently ignores empty lines, oversized frames (longer than
        :data:`_MAX_INBOUND_FRAME_LEN`), frames missing the ``<<<EXECTUNNEL:``
        / ``>>>`` envelope, and unknown frame types. ``KEEPALIVE`` is
        accepted and deliberately ignored.

        Args:
            line: Raw line (newline already stripped by the caller).
        """
        line = line.strip()

        if len(line) > _MAX_INBOUND_FRAME_LEN:
            _log("debug", "oversized frame dropped (%d chars)", len(line))
            return

        # ── Proxy suffix tolerance ────────────────────────────────────────
        if line.startswith(_FRAME_PREFIX):
            suffix_pos = line.rfind(_FRAME_SUFFIX)
            if suffix_pos != -1:
                line = line[: suffix_pos + len(_FRAME_SUFFIX)]
        # ──────────────────────────────────────────────────────────────────

        if not (line.startswith(_FRAME_PREFIX) and line.endswith(_FRAME_SUFFIX)):
            return

        inner = line[len(_FRAME_PREFIX) : -len(_FRAME_SUFFIX)]
        parts = inner.split(":", 2)
        if not parts:
            return

        match parts[0]:
            case "CONN_OPEN":
                self._on_conn_open(parts)
            case "DATA":
                self._on_data(parts)
            case "CONN_CLOSE":
                self._on_conn_close(parts)
            case "UDP_OPEN":
                self._on_udp_open(parts)
            case "UDP_DATA":
                self._on_udp_data(parts)
            case "UDP_CLOSE":
                self._on_udp_close(parts)
            case "KEEPALIVE":
                pass
            case _:
                _log("debug", "unknown frame type ignored: %s", parts[0])

    def worker_counts(self) -> tuple[int, int]:
        """Return a snapshot of live worker counts.

        Returns:
            A ``(tcp_worker_count, udp_worker_count)`` tuple, used by the
            stats sampler to report concurrency in each ``STATS`` frame.
        """
        with self._conn_lock:
            tcp = len(self._conn_map)
        with self._udp_lock:
            udp = len(self._udp_map)
        return tcp, udp

    def shutdown(self) -> None:
        """Signal every worker to close and wait up to the grace period.

        Called by :func:`main` before the writer is stopped so that workers
        can emit their final ``CONN_CLOSE`` / ``UDP_CLOSE`` frames while
        the writer is still alive. Workers that fail to exit within
        :data:`_WORKER_SHUTDOWN_GRACE_SECS` are logged and left to die with
        the process (they are daemon threads).
        """
        with self._conn_lock:
            tcp_workers = list(self._conn_map.values())
        for worker in tcp_workers:
            worker.signal_eof()

        with self._udp_lock:
            udp_workers = list(self._udp_map.values())
        for flow in udp_workers:
            flow.close()

        deadline = time.monotonic() + _WORKER_SHUTDOWN_GRACE_SECS
        for worker in tcp_workers:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            worker.join(timeout=remaining)
        for flow in udp_workers:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            flow.join(timeout=remaining)

        still_alive = sum(1 for w in tcp_workers if w.is_alive) + sum(
            1 for f in udp_workers if f.is_alive
        )
        if still_alive:
            _log(
                "debug",
                "shutdown: %d worker(s) still alive after grace period — "
                "will be killed on process exit",
                still_alive,
            )

    def _on_conn_open(self, parts: list[str]) -> None:
        """Handle a ``CONN_OPEN`` frame: validate, start a TCP worker.

        In portforward mode the host/port carried by the frame is ignored
        and the fixed target is used instead. Rejects frames with malformed
        connection IDs, duplicate IDs (with an ``ERROR`` reply), and — as a
        DoS guard — opens beyond :data:`_MAX_TCP_WORKERS`.

        Args:
            parts: Frame body split on ``:`` at most twice.
        """
        if len(parts) < _MIN_PARTS_WITH_CONN_ID:
            return
        cid = parts[1]
        if not _ID_RE.match(cid):
            _log("debug", "CONN_OPEN invalid conn_id format: %r", cid)
            return

        with self._conn_lock:
            if cid in self._conn_map:
                _log("debug", "duplicate CONN_OPEN for %s — sending ERROR", cid)
                _emit_ctrl(_make_error_frame(cid, "duplicate CONN_OPEN"))
                return
            # L13: global TCP worker cap — cheap DoS defence against
            # runaway/adversarial CONN_OPEN storms.
            if len(self._conn_map) >= _MAX_TCP_WORKERS:
                _log(
                    "warning",
                    "CONN_OPEN rejected: worker cap %d reached (conn=%s)",
                    _MAX_TCP_WORKERS,
                    cid,
                )
                _emit_ctrl(
                    _make_error_frame(cid, "agent: too many concurrent connections")
                )
                return

        if self._fixed_host is not None:
            if self._fixed_port is None:
                _emit_ctrl(_make_error_frame(cid, "portforward: fixed_port not set"))
                return
            host, port = self._fixed_host, self._fixed_port
        elif len(parts) >= _MIN_PARTS_WITH_PAYLOAD:
            result = _parse_host_port(parts[_HOST_PORT_PART_INDEX], cid, "CONN_OPEN")
            if result is None:
                _emit_ctrl(
                    _make_error_frame(
                        cid,
                        f"invalid CONN_OPEN host:port: {parts[_HOST_PORT_PART_INDEX]!r}",
                    )
                )
                return
            host, port = result
        else:
            _log("debug", "CONN_OPEN missing host:port for %s", cid)
            _emit_ctrl(_make_error_frame(cid, "CONN_OPEN missing host:port"))
            return

        def on_close(conn_id: str = cid) -> None:
            with self._conn_lock:
                self._conn_map.pop(conn_id, None)

        worker = TcpConnectionWorker(
            cid, host, port, dns_cache=self._dns_cache, on_close=on_close
        )
        with self._conn_lock:
            if cid in self._conn_map:
                _log(
                    "debug",
                    "duplicate CONN_OPEN for %s (late check) — sending ERROR",
                    cid,
                )
                _emit_ctrl(_make_error_frame(cid, "duplicate CONN_OPEN"))
                return
            # L13: re-check the worker cap (late) in case concurrent opens
            # raced past the early check.
            if len(self._conn_map) >= _MAX_TCP_WORKERS:
                _log(
                    "warning",
                    "CONN_OPEN rejected (late check): worker cap %d reached (conn=%s)",
                    _MAX_TCP_WORKERS,
                    cid,
                )
                _emit_ctrl(
                    _make_error_frame(cid, "agent: too many concurrent connections")
                )
                return
            self._conn_map[cid] = worker
        worker.start()

    def _on_data(self, parts: list[str]) -> None:
        """Handle a ``DATA`` frame: decode payload and feed the TCP worker.

        Silently ignores frames referencing unknown connection IDs (the
        worker may have torn down before the client learned about it);
        malformed base64 causes an ``ERROR`` reply.

        Args:
            parts: Frame body split on ``:`` at most twice.
        """
        if len(parts) < _MIN_PARTS_WITH_PAYLOAD:
            return
        cid = parts[1]
        with self._conn_lock:
            worker = self._conn_map.get(cid)
        if worker is None:
            return
        try:
            data = _b64decode(parts[_HOST_PORT_PART_INDEX])
        except ValueError:
            _log("debug", "invalid base64url DATA for conn %s", cid)
            _emit_ctrl(_make_error_frame(cid, "invalid base64url in DATA frame"))
            return
        worker.feed(data)

    def _on_conn_close(self, parts: list[str]) -> None:
        """Handle a ``CONN_CLOSE`` frame: request a half-close on the worker.

        Args:
            parts: Frame body split on ``:`` at most twice.
        """
        if len(parts) < _MIN_PARTS_WITH_CONN_ID:
            return
        cid = parts[1]
        with self._conn_lock:
            worker = self._conn_map.get(cid)
        if worker is not None:
            worker.signal_eof()

    def _on_udp_open(self, parts: list[str]) -> None:
        """Handle a ``UDP_OPEN`` frame: validate, start a UDP flow worker.

        Malformed host/port or duplicate flow IDs cause an immediate
        ``UDP_CLOSE`` reply so the client can reclaim the ID.

        Args:
            parts: Frame body split on ``:`` at most twice.
        """
        if len(parts) < _MIN_PARTS_WITH_PAYLOAD:
            return
        fid = parts[1]
        if not _ID_RE.match(fid):
            _log("debug", "UDP_OPEN invalid flow_id format: %r", fid)
            return

        result = _parse_host_port(parts[_HOST_PORT_PART_INDEX], fid, "UDP_OPEN")
        if result is None:
            _emit_ctrl(_make_frame("UDP_CLOSE", fid))
            return
        host, port = result

        def on_close(flow_id: str = fid) -> None:
            with self._udp_lock:
                self._udp_map.pop(flow_id, None)

        flow = UdpFlowWorker(
            fid, host, port, dns_cache=self._dns_cache, on_close=on_close
        )
        with self._udp_lock:
            if fid in self._udp_map:
                _log("debug", "duplicate UDP_OPEN for %s — sending UDP_CLOSE", fid)
                _emit_ctrl(_make_frame("UDP_CLOSE", fid))
                return
            self._udp_map[fid] = flow
        flow.start()

    def _on_udp_data(self, parts: list[str]) -> None:
        """Handle a ``UDP_DATA`` frame: decode and feed the UDP flow worker.

        Frames for unknown flow IDs are ignored; malformed base64 triggers
        a ``UDP_CLOSE`` reply.

        Args:
            parts: Frame body split on ``:`` at most twice.
        """
        if len(parts) < _MIN_PARTS_WITH_PAYLOAD:
            return
        fid = parts[1]
        with self._udp_lock:
            flow = self._udp_map.get(fid)
        if flow is None:
            return
        try:
            data = _b64decode(parts[_HOST_PORT_PART_INDEX])
        except ValueError:
            _log("debug", "invalid base64url UDP_DATA for flow %s", fid)
            _emit_ctrl(_make_frame("UDP_CLOSE", fid))
            return
        flow.feed(data)

    def _on_udp_close(self, parts: list[str]) -> None:
        """Handle a ``UDP_CLOSE`` frame: request flow termination.

        Args:
            parts: Frame body split on ``:`` at most twice.
        """
        if len(parts) < _MIN_PARTS_WITH_CONN_ID:
            return
        fid = parts[1]
        with self._udp_lock:
            flow = self._udp_map.get(fid)
        if flow is not None:
            flow.close()

    def run(self) -> None:
        """Read stdin line-by-line and dispatch each frame until EOF or terminate.

        Returns when either stdin closes (client side went away) or the
        termination flag is observed between frames. Lines are passed to
        :meth:`dispatch` after stripping the trailing ``\\r\\n``.
        """
        for raw_line in sys.stdin:
            if _TERMINATE:
                break
            self.dispatch(raw_line.rstrip("\n\r"))
        _log("info", "stdin EOF — dispatcher exiting")


def main() -> None:
    """Agent entry point.

    Parses command-line arguments, installs signal handlers, initialises
    the frame writer and the stats sampler, runs the dispatcher, and
    performs an ordered teardown on exit.

    Argument handling:
        * No arguments: tunnel (SOCKS5) mode — the client supplies the
          destination on each ``CONN_OPEN`` frame.
        * Two arguments ``<host> <port>``: portforward mode — every
          ``CONN_OPEN`` is connected to the given fixed target.

    Exits with status 1 on invalid arguments (printed to stderr).
    """
    _install_sigpipe_handler()
    _install_termination_handlers()

    if len(sys.argv) == 1:
        fixed_host = None
        fixed_port = None
    elif len(sys.argv) == _ARGC_FIXED_TARGET:
        fixed_host = sys.argv[1]
        try:
            fixed_port = int(sys.argv[2])
        except ValueError:
            sys.stderr.write(f"invalid port: {sys.argv[2]!r}\n")
            sys.exit(1)
        if fixed_port <= 0 or fixed_port > _MAX_TCP_UDP_PORT:
            sys.stderr.write(f"port out of range: {fixed_port}\n")
            sys.exit(1)
    else:
        sys.stderr.write("usage: agent.py [<host> <port>]\n")
        sys.exit(1)

    for path in ("/tmp/exectunnel_agent.py", "/tmp/exectunnel_agent.b64"):  # noqa: S108
        with contextlib.suppress(OSError):
            os.unlink(path)

    global _writer, _stats_sampler  # noqa: PLW0603
    _disable_echo()
    _writer = _FrameWriter()
    # AGENT_READY stays byte-identical to v1 so existing clients'
    # ``is_ready_frame()`` predicate continues to match.  Version/feature
    # negotiation is handled implicitly: new clients recognise the
    # periodic STATS frame; old clients fall through their forward-
    # compatible "unknown frame type" path and ignore it.
    _emit_ctrl(_make_frame("AGENT_READY"))

    dispatcher = _Dispatcher(fixed_host, fixed_port)
    if _STATS_ENABLED:
        _stats_sampler = _StatsSampler(dispatcher)
        _stats_sampler.start()
    try:
        dispatcher.run()
    finally:
        if _stats_sampler is not None:
            _stats_sampler.stop()
        dispatcher.shutdown()
        if _writer is not None:
            _writer.stop()


if __name__ == "__main__":
    main()
