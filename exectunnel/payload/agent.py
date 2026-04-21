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
_MAX_PORT: int = 65_535

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
    """Append one dispatch-latency sample (bounded, thread-safe)."""
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
    """Like ``socket.create_connection`` but uses the shared DNS cache."""
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
    with contextlib.suppress(OSError, ValueError):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)


def _install_termination_handlers() -> None:
    def _handle(_signum: int, _frame: object | None) -> None:
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
    __slots__ = ("_inner", "_dead")

    def __init__(self, inner: object) -> None:
        self._inner = inner
        # ``threading.Event`` rather than plain bool — gives cross-thread
        # ordering guarantees (and a cheap wait primitive if ever needed).
        self._dead = threading.Event()

    def write(self, s: str) -> int:
        if self._dead.is_set():
            return 0
        try:
            return self._inner.write(s)  # type: ignore[union-attr]
        except OSError:
            self._dead.set()
            return 0

    def flush(self) -> None:
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
        """Force the stdout into the dead state (e.g. on hard deadline)."""
        self._dead.set()


def _b64encode(data: bytes) -> str:
    """URL-safe base64 encode with no padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
    """URL-safe base64 decode — re-adds padding stripped by :func:`_b64encode`.

    Raises:
        ValueError: If *s* is not valid base64url.
    """
    padding = (4 - len(s) % 4) % 4
    try:
        return base64.urlsafe_b64decode(s + "=" * padding)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64url: {s!r}") from exc


def _log(level: str, msg: str, *args: object) -> None:
    """Write agent diagnostics to stderr without polluting the frame channel."""
    lvl = _LOG_LEVELS.get(level.lower(), 30)
    if lvl < _LOG_LEVEL:
        return
    text = msg % args if args else msg
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write(f"{ts} {level.upper():7s} agent: {text}\n")
    sys.stderr.flush()


def _make_frame(*parts: str) -> str:
    """Construct a frame string from its colon-separated parts."""
    return f"{_FRAME_PREFIX}{':'.join(parts)}{_FRAME_SUFFIX}"


def _make_error_frame(conn_id: str, reason: str) -> str:
    """Construct an ERROR frame with a base64url-encoded reason string."""
    return _make_frame("ERROR", conn_id, _b64encode(reason.encode()))


def _parse_host_port(
    raw: str,
    frame_id: str,
    frame_type: str,
) -> tuple[str, int] | None:
    """Parse a ``[host]:port`` or ``host:port`` string into ``(host, port)``."""
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
    if port <= 0 or port > 65_535:
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
    _STOP: object = object()

    def __init__(self) -> None:
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
        """Enqueue a control frame — never blocks, never drops."""
        if self._stopping or self._out.is_dead:
            return
        self._ctrl.put(line)

    def emit_data(self, line: str) -> None:
        """Enqueue a data frame with periodic liveness and shutdown checks.

        Observes the module-level ``_TERMINATE`` flag so signal handlers can
        unblock workers stuck on a full queue even if ``is_dead`` has not yet
        flipped (e.g. before the first blocked write discovers the closed
        kubectl channel).

        Applies a hard deadline (``_EMIT_DATA_HARD_DEADLINE_SECS``) after
        which stdout is force-marked dead so this worker — and all other
        workers blocked on a full data queue — can unblock.  Protects
        against silently-wedged writer channels (e.g. half-dead TCP) where
        neither an OSError nor a signal ever arrives.
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
        """Signal the writer thread to flush remaining frames and exit."""
        if self._stopping:
            return
        self._stopping = True
        self._ctrl.put(self._STOP)
        with contextlib.suppress(queue.Full):
            self._data.put_nowait(self._STOP)
        self._thread.join(timeout=_WRITER_JOIN_TIMEOUT_SECS)

    @property
    def data_queue_size(self) -> int:
        return self._data.qsize()

    @property
    def is_dead(self) -> bool:
        return self._out.is_dead

    def _run(self) -> None:
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
        self._dispatcher = dispatcher
        self._interval = interval_secs
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="stats-sampler"
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # Daemon thread — best effort join; do not block shutdown.
        self._thread.join(timeout=0.5)

    def _snapshot(self) -> dict[str, object]:
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
    """Emit a control frame — always delivered before any pending data frame."""
    if _writer is not None:
        _writer.emit_ctrl(line)
    else:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _emit_data(line: str) -> None:
    """Emit a data frame — blocks the calling thread when the stdout queue is full."""
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
    if _writer is None:
        return False
    return _writer.data_queue_size >= _STDOUT_DATA_QUEUE_BACKPRESSURE_THRESHOLD


def _disable_echo() -> None:
    """Disable terminal echo on stdin so shell output does not pollute the channel."""
    try:
        fd = sys.stdin.fileno()
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~(termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        _log("debug", "stdin is not a tty; skipping echo disable")


def _send_all_nonblocking(sock: socket.socket, data: bytes) -> bool:
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
    def __init__(
        self,
        conn_id: str,
        host: str,
        port: int,
        dns_cache: _DnsCache,
        on_close: Callable[[], None] | None = None,
    ) -> None:
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
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout=timeout)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def feed(self, data: bytes) -> None:
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
        self._closed = True
        self._wake()

    def _run(self) -> None:
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
    def __init__(
        self,
        flow_id: str,
        host: str,
        port: int,
        dns_cache: _DnsCache,
        on_close: Callable[[], None] | None = None,
    ) -> None:
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
        with self._pipe_lock:
            if self._pipe_closed:
                return
            with contextlib.suppress(OSError):
                os.write(self._notify_w, b"\x00")

    def _close_pipe(self) -> None:
        with self._pipe_lock:
            if self._pipe_closed:
                return
            self._pipe_closed = True
            with contextlib.suppress(OSError):
                os.close(self._notify_r)
            with contextlib.suppress(OSError):
                os.close(self._notify_w)

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout=timeout)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def feed(self, data: bytes) -> None:
        """Enqueue *data* to be sent to the remote UDP peer.

        Silently drops when the inbound queue is full — UDP semantics.
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
        """Signal the IO loop to exit after draining pending sends."""
        self._closed = True
        self._wake()

    def _run(self) -> None:
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
                            _log(
                                "debug",
                                "udp flow %s send transient error %s; "
                                "datagram dropped (send_blocks=%d)",
                                fid,
                                errno.errorcode.get(exc.errno, exc.errno),
                                self._send_block_count,
                            )
                        continue
                    return


class _Dispatcher:
    def __init__(
        self,
        fixed_host: str | None,
        fixed_port: int | None,
    ) -> None:
        self._fixed_host = fixed_host
        self._fixed_port = fixed_port

        self._conn_map: dict[str, TcpConnectionWorker] = {}
        self._udp_map: dict[str, UdpFlowWorker] = {}
        self._conn_lock = threading.Lock()
        self._udp_lock = threading.Lock()
        self._dns_cache = _DnsCache()

    def dispatch(self, line: str) -> None:
        """Parse and dispatch one raw frame line."""
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
        """Return current ``(tcp_workers, udp_workers)`` count.

        Used by the stats sampler to report concurrency in the STATS frame.
        """
        with self._conn_lock:
            tcp = len(self._conn_map)
        with self._udp_lock:
            udp = len(self._udp_map)
        return tcp, udp

    def shutdown(self) -> None:
        """Signal all workers to close and wait briefly for clean teardown.

        Called by ``main()`` before stopping the writer so that workers have
        a chance to emit final ``CONN_CLOSE`` / ``UDP_CLOSE`` frames while
        the writer is still alive.
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
        elif len(parts) >= 3:
            result = _parse_host_port(parts[_HOST_PORT_PART_INDEX], cid, "CONN_OPEN")
            if result is None:
                _emit_ctrl(
                    _make_error_frame(cid, f"invalid CONN_OPEN host:port: {parts[_HOST_PORT_PART_INDEX]!r}")
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
        if len(parts) < _MIN_PARTS_WITH_CONN_ID:
            return
        cid = parts[1]
        with self._conn_lock:
            worker = self._conn_map.get(cid)
        if worker is not None:
            worker.signal_eof()

    def _on_udp_open(self, parts: list[str]) -> None:
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
        if len(parts) < _MIN_PARTS_WITH_CONN_ID:
            return
        fid = parts[1]
        with self._udp_lock:
            flow = self._udp_map.get(fid)
        if flow is not None:
            flow.close()

    def run(self) -> None:
        """Read stdin line-by-line and dispatch each frame until EOF."""
        for raw_line in sys.stdin:
            if _TERMINATE:
                break
            self.dispatch(raw_line.rstrip("\n\r"))
        _log("info", "stdin EOF — dispatcher exiting")


def main() -> None:
    """Parse arguments, initialise the writer, and run the dispatcher."""
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
        if fixed_port <= 0 or fixed_port > _MAX_PORT:
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
