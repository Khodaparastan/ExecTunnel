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

Encoding
--------
All binary payloads (DATA, UDP_DATA, ERROR) use URL-safe base64 with no
padding (``urlsafe_b64encode(...).rstrip(b"=")``) — consistent with the
client-side ``encode_data_frame`` / ``decode_binary_payload`` helpers.

Design notes
------------
* Self-contained — no third-party deps; runs on any bare Python 3.11+ pod.
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

import sys

# ── Python version guard ──────────────────────────────────────────────────────
# match/case requires 3.10+; datetime.UTC requires 3.11+.
if sys.version_info < (3, 11):
    sys.stderr.write(
        f"exectunnel agent requires Python 3.11+, got {sys.version}\n"
    )
    sys.exit(1)

import base64
import binascii
import contextlib
import errno
import os
import queue
import re as _re
import select
import signal
import socket
import termios
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

# ── Protocol constants ────────────────────────────────────────────────────────

_FRAME_PREFIX: str = "<<<EXECTUNNEL:"
_FRAME_SUFFIX: str = ">>>"
_AGENT_VERSION: str = "1"

# ── Logging ───────────────────────────────────────────────────────────────────

_LOG_LEVELS: dict[str, int] = {
    "debug":   10,
    "info":    20,
    "warning": 30,
    "error":   40,
}
_LOG_LEVEL: int = _LOG_LEVELS.get(
    os.getenv("EXECTUNNEL_AGENT_LOG_LEVEL", "warning").lower(),
    30,
)

# ── Tuning constants ──────────────────────────────────────────────────────────

# Maximum queued inbound chunks before a TCP connection is declared saturated.
_MAX_TCP_INBOUND_CHUNKS: int = 1_024

# Maximum queued inbound datagrams before a UDP flow starts dropping.
_MAX_UDP_INBOUND_DGRAMS: int = 2_048

# Bounded stdout data-frame queue capacity.
_STDOUT_DATA_QUEUE_CAP: int = 2_048

# Backpressure threshold: pause TCP recv when data queue exceeds this fraction.
# At 0.75 capacity the IO loop skips sock.recv(), letting the OS TCP receive
# buffer fill and propagating backpressure to the remote sender.
# This prevents the queue from reaching 100% and causing EPIPE on the writer.
_STDOUT_DATA_QUEUE_BACKPRESSURE_RATIO: float = 0.75

# Derived threshold in items — computed once at module load.
_STDOUT_DATA_QUEUE_BACKPRESSURE_THRESHOLD: int = int(
    _STDOUT_DATA_QUEUE_CAP * _STDOUT_DATA_QUEUE_BACKPRESSURE_RATIO
)

# Maximum data frames drained per writer-loop iteration (fairness cap).
_WRITER_DATA_BATCH_SIZE: int = 64

# TCP connect timeout in seconds.
# Must be shorter than the client-side CONN_ACK_TIMEOUT_SECS (10 s) so the
# agent fails first and emits CONN_ERR before the client gives up waiting.
# 8 s = 10 s - 2 s tunnel RTT budget, leaving time for CONN_ERR to arrive.
_TCP_CONNECT_TIMEOUT_SECS: float = 8.0

# TCP receive chunk size in bytes.
_TCP_RECV_CHUNK_BYTES: int = 4_096

# Maximum UDP datagram size (theoretical IPv4/IPv6 maximum).
_MAX_UDP_DGRAM_BYTES: int = 65_535

# Log a UDP drop warning every N drops.
# 1,000 matches the client-side UDP_WARN_EVERY / DROP_WARN_INTERVAL policy
# for consistent log volume across all drop-counting paths.
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
# remote TCP peer to become writable.  5 s is generous but bounded.
_SEND_WRITABLE_TIMEOUT_SECS: float = 5.0

# Maximum inbound frame length accepted by dispatch() — mirrors the client-side
# MAX_FRAME_LEN constant in protocol/frames.py.  Frames longer than this are
# silently dropped to prevent unbounded memory allocation.
_MAX_INBOUND_FRAME_LEN: int = 8_192

# Regex pattern for valid connection / flow IDs — mirrors the client-side
# ID_RE = r"[cu][0-9a-f]{24}" in protocol/ids.py.
_ID_RE: _re.Pattern[str] = _re.compile(r"^[cu][0-9a-f]{24}$")


# ── SIGPIPE handler ───────────────────────────────────────────────────────────


def _install_sigpipe_handler() -> None:
    """Ignore SIGPIPE so the process does not crash on stdout EPIPE.

    When the kubectl exec channel closes while the writer thread is mid-send,
    the OS delivers SIGPIPE.  Ignoring it lets the writer thread catch the
    ``OSError`` at the Python level instead of crashing the process.

    No-op on platforms that do not support SIGPIPE (e.g. Windows).
    """
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    except (OSError, ValueError):
        pass


# ── Fault-tolerant stdout wrapper ─────────────────────────────────────────────


class _FaultTolerantStdout:
    """Wraps ``sys.stdout`` and catches ``OSError`` / ``BrokenPipeError`` silently.

    Python's C-level ``io.BufferedWriter`` prints
    ``"socket.send() raised exception."`` to stderr when it catches an
    ``OSError`` it cannot propagate.  By catching the exception at the Python
    level before it reaches the C layer, we prevent that message entirely.

    Once a write or flush fails, the wrapper marks itself dead and all
    subsequent calls are no-ops.  The ``_FrameWriter`` checks ``is_dead``
    and exits its loop promptly.
    """

    __slots__ = ("_inner", "_dead")

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self._dead = False

    def write(self, s: str) -> int:
        if self._dead:
            return 0
        try:
            return self._inner.write(s)  # type: ignore[union-attr]
        except OSError:
            self._dead = True
            return 0

    def flush(self) -> None:
        if self._dead:
            return
        try:
            self._inner.flush()  # type: ignore[union-attr]
        except OSError:
            self._dead = True

    @property
    def is_dead(self) -> bool:
        """``True`` once any write or flush has raised ``OSError``."""
        return self._dead


# ── Base64url helpers (no padding) ────────────────────────────────────────────


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


# ── IPv6 bracket helper ───────────────────────────────────────────────────────


def _strip_brackets(host: str) -> str:
    """Remove RFC 2732 brackets from an IPv6 address literal."""
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


# ── Logging ───────────────────────────────────────────────────────────────────


def _log(level: str, msg: str, *args: object) -> None:
    """Write agent diagnostics to stderr without polluting the frame channel."""
    lvl = _LOG_LEVELS.get(level.lower(), 30)
    if lvl < _LOG_LEVEL:
        return
    text = msg % args if args else msg
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write(f"{ts} {level.upper():7s} agent: {text}\n")
    sys.stderr.flush()


# ── Frame construction helpers ────────────────────────────────────────────────


def _make_frame(*parts: str) -> str:
    """Construct a frame string from its colon-separated parts."""
    return f"{_FRAME_PREFIX}{':'.join(parts)}{_FRAME_SUFFIX}"


def _make_error_frame(conn_id: str, reason: str) -> str:
    """Construct an ERROR frame with a base64url-encoded reason string."""
    return _make_frame("ERROR", conn_id, _b64encode(reason.encode()))


# ── Frame parsing helpers ─────────────────────────────────────────────────────


def _parse_host_port(
    raw: str,
    frame_id: str,
    frame_type: str,
) -> tuple[str, int] | None:
    """Parse a ``[host]:port`` or ``host:port`` string into ``(host, port)``."""
    host_raw, sep, port_str = raw.rpartition(":")
    if not sep or not host_raw or not port_str:
        _log("debug", "invalid %s payload for %s: %r", frame_type, frame_id, raw)
        return None
    host = _strip_brackets(host_raw)
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


# ── Frame writer ──────────────────────────────────────────────────────────────


class _FrameWriter:
    """Single daemon thread that serialises all stdout writes.

    Owns a ``_FaultTolerantStdout`` wrapper around ``sys.stdout`` so that
    ``OSError`` / ``BrokenPipeError`` from the kubectl exec channel are caught
    at the Python level before the C-layer ``io.BufferedWriter`` can print
    ``"socket.send() raised exception."`` to stderr.

    Backpressure interface
    ----------------------
    :attr:`data_queue_size` is read by ``TcpConnectionWorker._io_loop`` to
    decide whether to pause ``sock.recv()``.  When the queue exceeds
    ``_STDOUT_DATA_QUEUE_BACKPRESSURE_THRESHOLD``, the IO loop skips recv
    for that iteration, letting the OS TCP receive buffer fill and propagating
    backpressure to the remote sender.

    This prevents the queue from reaching 100% capacity, which would cause
    the writer thread to block on ``out.write()`` / ``out.flush()``, which
    would eventually cause EPIPE and kill the writer thread permanently.
    """

    _STOP: object = object()

    def __init__(self) -> None:
        self._out = _FaultTolerantStdout(sys.stdout)
        self._ctrl: queue.SimpleQueue[str | object] = queue.SimpleQueue()
        self._data: queue.Queue[str | object] = queue.Queue(
            maxsize=_STDOUT_DATA_QUEUE_CAP
        )
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="stdout-writer"
        )
        self._thread.start()

    def emit_ctrl(self, line: str) -> None:
        """Enqueue a control frame — never blocks, never drops."""
        self._ctrl.put(line)

    def emit_data(self, line: str) -> None:
        """Enqueue a data frame — blocks the caller when the queue is full.

        Backpressure propagates naturally through the kernel TCP receive
        buffer to the remote sender when ``TcpConnectionWorker._io_loop``
        pauses ``sock.recv()`` based on :attr:`data_queue_size`.
        """
        self._data.put(line)

    def stop(self) -> None:
        """Signal the writer thread to flush remaining frames and exit."""
        self._ctrl.put(self._STOP)
        with contextlib.suppress(queue.Full):
            self._data.put_nowait(self._STOP)
        self._thread.join(timeout=_WRITER_JOIN_TIMEOUT_SECS)

    @property
    def data_queue_size(self) -> int:
        """Current number of items in the data queue.

        Read by ``TcpConnectionWorker._io_loop`` to implement backpressure.
        ``qsize()`` is approximate on some platforms but accurate enough for
        a soft threshold check — we do not need exact precision here.
        """
        return self._data.qsize()

    @property
    def is_dead(self) -> bool:
        """``True`` once the stdout channel has raised ``OSError``."""
        return self._out.is_dead

    def _run(self) -> None:
        """Writer loop — runs until the stop sentinel is received or channel dies."""
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
                # Drain ALL remaining non-sentinel data frames before exiting.
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
                return

            if item is not None and isinstance(item, str):
                out.write(item + "\n")
                if out.is_dead:
                    return
                # Drain additional pending control frames first.
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

            # Drain pending data frames (bounded batch for fairness).
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


# ── Module-level writer — set in main() before any threads start ──────────────

_writer: _FrameWriter | None = None


def _emit_ctrl(line: str) -> None:
    """Emit a control frame — always delivered before any pending data frame."""
    if _writer is not None:
        _writer.emit_ctrl(line)
    else:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _emit_data(line: str) -> None:
    """Emit a data frame — blocks the calling thread when the stdout queue is full."""
    if _writer is not None:
        _writer.emit_data(line)
    else:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _stdout_backpressure_active() -> bool:
    """Return ``True`` when the stdout data queue exceeds the backpressure threshold.

    Called by ``TcpConnectionWorker._io_loop`` before each ``sock.recv()``
    to decide whether to pause reading from the TCP socket.

    When ``True``, the IO loop skips ``sock.recv()`` for that iteration,
    allowing the OS TCP receive buffer to fill.  This propagates backpressure
    to the remote sender (CDN / target service) without blocking any thread.

    Returns ``False`` when no writer is active (e.g. during testing without
    a ``_FrameWriter`` instance) so that the IO loop always reads in that case.
    """
    if _writer is None:
        return False
    return _writer.data_queue_size >= _STDOUT_DATA_QUEUE_BACKPRESSURE_THRESHOLD


# ── Terminal helpers ──────────────────────────────────────────────────────────


def _disable_echo() -> None:
    """Disable terminal echo on stdin so shell output does not pollute the channel."""
    try:
        fd = sys.stdin.fileno()
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~(termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        _log("debug", "stdin is not a tty; skipping echo disable")


# ── Non-blocking send ─────────────────────────────────────────────────────────


def _send_all_nonblocking(sock: socket.socket, data: bytes) -> bool:
    """Send all of *data* through a non-blocking *sock*.

    Returns:
        ``True`` on success, ``False`` if the socket closed or errored.
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


# ── TCP connection worker ─────────────────────────────────────────────────────


class TcpConnectionWorker:
    """Manages one TCP connection to ``(host, port)`` on behalf of *conn_id*.

    The worker thread runs a ``select()`` loop:

    * **readable socket**  → emit ``DATA`` frame to stdout (base64url, no padding)
    * **notify pipe**      → drain inbound queue → send to socket
    * **``_closed`` flag** → ``SHUT_WR`` then wait for remote FIN

    Backpressure
    ------------
    Before calling ``sock.recv()``, the IO loop checks
    ``_stdout_backpressure_active()``.  When the stdout data queue exceeds
    ``_STDOUT_DATA_QUEUE_BACKPRESSURE_THRESHOLD``, recv is skipped for that
    iteration.  The OS TCP receive buffer fills, propagating backpressure to
    the remote sender without blocking any thread.

    This prevents the stdout data queue from reaching 100% capacity, which
    would cause the writer thread to block on ``out.write()`` / ``out.flush()``,
    eventually causing EPIPE and permanently killing the writer thread.

    Half-close semantics
    --------------------
    When the client signals ``CONN_CLOSE`` (local EOF), :meth:`signal_eof`
    sets ``_closed``.  The IO loop drains any remaining inbound chunks, calls
    ``SHUT_WR``, then waits up to :data:`_HALF_CLOSE_DEADLINE_SECS` for the
    remote to close its write side before tearing down.

    Saturation
    ----------
    If the inbound queue reaches :data:`_MAX_TCP_INBOUND_CHUNKS`, the
    connection is declared saturated: ``_closed`` is set and an ``ERROR``
    frame is emitted.

    Resource cleanup
    ----------------
    Pipe FDs and the socket are always closed in ``_run``'s ``finally`` block.
    ``CONN_CLOSE`` is always emitted in the ``finally`` block.
    """

    def __init__(
        self,
        conn_id: str,
        host: str,
        port: int,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self.conn_id = conn_id
        self._host = host
        self._port = port
        self._on_close = on_close

        self._sock: socket.socket | None = None
        self._inbound: list[bytes] = []
        self._inbound_lock = threading.Lock()

        self._notify_r, self._notify_w = os.pipe()
        os.set_blocking(self._notify_r, False)
        os.set_blocking(self._notify_w, False)

        self._closed = False
        self._saturated = False
        self._drop_count: int = 0

        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"tcp-conn-{conn_id}"
        )

    def start(self) -> None:
        """Start the worker thread."""
        self._thread.start()

    def feed(self, data: bytes) -> None:
        """Enqueue *data* to be sent to the remote TCP peer.

        If the inbound queue is full, the connection is declared saturated:
        ``_closed`` is set, an ``ERROR`` frame is emitted, and all subsequent
        calls are no-ops.

        Thread-safe — called from the dispatcher thread.
        """
        saturated = False
        with self._inbound_lock:
            if self._saturated:
                return
            if len(self._inbound) >= _MAX_TCP_INBOUND_CHUNKS:
                self._saturated = True
                self._closed = True
                saturated = True
            else:
                self._inbound.append(data)

        if saturated:
            _emit_ctrl(_make_error_frame(self.conn_id, "agent tcp inbound saturated"))
            _log(
                "warning",
                "conn %s inbound saturated; closing connection",
                self.conn_id,
            )
            return

        with contextlib.suppress(OSError):
            os.write(self._notify_w, b"\x00")

    def signal_eof(self) -> None:
        """Signal that the client has sent EOF — no more data will arrive.

        Thread-safe — called from the dispatcher thread.
        """
        self._closed = True
        with contextlib.suppress(OSError):
            os.write(self._notify_w, b"\x00")

    def _run(self) -> None:
        cid = self.conn_id
        try:
            sock = socket.create_connection(
                (self._host, self._port),
                timeout=_TCP_CONNECT_TIMEOUT_SECS,
            )
            sock.setblocking(False)
        except OSError as exc:
            _emit_ctrl(_make_error_frame(cid, str(exc)))
            _log("warning", "conn %s connect failed: %s", cid, exc)
            os.close(self._notify_r)
            os.close(self._notify_w)
            if self._on_close is not None:
                self._on_close()
            return

        self._sock = sock
        # CONN_ACK emitted immediately after successful connect.
        _emit_ctrl(_make_frame("CONN_ACK", cid))

        try:
            self._io_loop(sock, cid)
        finally:
            # Always close resources regardless of how _io_loop exits.
            # CONN_CLOSE is always emitted — never omitted on any teardown path.
            sock.close()
            os.close(self._notify_r)
            os.close(self._notify_w)
            _emit_ctrl(_make_frame("CONN_CLOSE", cid))
            if self._on_close is not None:
                self._on_close()

    def _io_loop(self, sock: socket.socket, cid: str) -> None:
        """Main select loop — runs until the connection is fully closed.

        Backpressure
        ------------
        Before calling ``sock.recv()``, the loop checks
        ``_stdout_backpressure_active()``.  When the stdout data queue is
        near capacity, recv is skipped for that iteration.  The OS TCP receive
        buffer fills, propagating backpressure to the remote sender.

        This is the critical fix for the SSL handshake failure: without
        backpressure, the agent floods the stdout channel with DATA frames
        faster than the exec WebSocket can drain them, causing EPIPE, killing
        the writer thread, and truncating the TLS record stream mid-handshake.
        """
        local_shut = False
        local_shut_deadline: float | None = None

        while True:
            readable, _, errored = select.select(
                [sock, self._notify_r], [], [sock], _SELECT_TIMEOUT_SECS
            )

            if errored:
                break

            # ── Receive from remote ───────────────────────────────────────────
            remote_closed = False
            if sock in readable:
                # Backpressure check: skip recv when stdout data queue is near
                # capacity.  This lets the OS TCP receive buffer fill, which
                # propagates backpressure to the remote sender (CDN / target).
                # The select() timeout (10ms) means we re-check every 10ms,
                # so backpressure is released promptly when the queue drains.
                if _stdout_backpressure_active():
                    # Do not recv — let TCP buffer fill.
                    # Still process notify pipe and inbound sends below.
                    pass
                else:
                    try:
                        chunk = sock.recv(_TCP_RECV_CHUNK_BYTES)
                    except OSError:
                        break
                    if not chunk:
                        remote_closed = True
                    else:
                        _emit_data(_make_frame("DATA", cid, _b64encode(chunk)))

            # ── Drain notify pipe ─────────────────────────────────────────────
            if self._notify_r in readable:
                with contextlib.suppress(OSError):
                    os.read(self._notify_r, 4_096)

            # ── Send pending inbound data ─────────────────────────────────────
            if not local_shut:
                with self._inbound_lock:
                    pending, self._inbound = self._inbound, []
                for chunk in pending:
                    if not _send_all_nonblocking(sock, chunk):
                        return
            else:
                pending = []

            # ── Remote closed its write side ──────────────────────────────────
            if remote_closed:
                if not local_shut:
                    with self._inbound_lock:
                        remaining, self._inbound = self._inbound, []
                    for chunk in remaining:
                        _send_all_nonblocking(sock, chunk)
                break

            # ── Half-close: local side done sending ───────────────────────────
            if self._closed and not local_shut:
                with self._inbound_lock:
                    still_pending = bool(self._inbound)
                if not still_pending and not pending:
                    local_shut = True
                    local_shut_deadline = (
                        time.monotonic() + _HALF_CLOSE_DEADLINE_SECS
                    )
                    with contextlib.suppress(OSError):
                        sock.shutdown(socket.SHUT_WR)

            # ── Half-close deadline ───────────────────────────────────────────
            if (
                local_shut
                and local_shut_deadline is not None
                and time.monotonic() >= local_shut_deadline
            ):
                break


# ── UDP flow worker ───────────────────────────────────────────────────────────


class UdpFlowWorker:
    """UDP flow: datagrams sent to ``(host, port)``; responses forwarded back.

    UDP is best-effort — no backpressure is applied to the recv loop.
    Inbound saturation drops datagrams silently (UDP semantics).
    """

    def __init__(
        self,
        flow_id: str,
        host: str,
        port: int,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._id = flow_id
        self._host = host
        self._port = port
        self._on_close = on_close

        self._sock: socket.socket | None = None
        self._inbound: list[bytes] = []
        self._inbound_lock = threading.Lock()

        self._notify_r, self._notify_w = os.pipe()
        os.set_blocking(self._notify_r, False)
        os.set_blocking(self._notify_w, False)

        self._closed = False
        self._drop_count: int = 0

        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"udp-flow-{flow_id}"
        )

    def start(self) -> None:
        """Start the worker thread."""
        self._thread.start()

    def feed(self, data: bytes) -> None:
        """Enqueue *data* to be sent to the remote UDP peer.

        Silently drops when the inbound queue is full — UDP semantics.
        Thread-safe — called from the dispatcher thread.
        """
        with self._inbound_lock:
            if len(self._inbound) >= _MAX_UDP_INBOUND_DGRAMS:
                self._drop_count += 1
                if (
                    self._drop_count == 1
                    or self._drop_count % _UDP_DROP_WARN_EVERY == 0
                ):
                    _log(
                        "warning",
                        "udp flow %s inbound saturated; dropping datagram "
                        "(drops=%d)",
                        self._id,
                        self._drop_count,
                    )
                return
            self._inbound.append(data)

        with contextlib.suppress(OSError):
            os.write(self._notify_w, b"\x00")

    def close(self) -> None:
        """Signal the IO loop to exit after draining pending sends.

        Thread-safe — called from the dispatcher thread.
        """
        self._closed = True
        with contextlib.suppress(OSError):
            os.write(self._notify_w, b"\x00")

    def _run(self) -> None:
        fid = self._id

        try:
            infos = socket.getaddrinfo(
                self._host, self._port, type=socket.SOCK_DGRAM
            )
            if not infos:
                raise OSError(f"could not resolve {self._host!r}")
        except OSError as exc:
            _emit_ctrl(_make_frame("UDP_CLOSE", fid))
            _log("warning", "udp flow %s resolve failed: %s", fid, exc)
            os.close(self._notify_r)
            os.close(self._notify_w)
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
            os.close(self._notify_r)
            os.close(self._notify_w)
            if self._on_close is not None:
                self._on_close()
            return

        self._sock = sock
        try:
            self._io_loop(sock, fid)
        finally:
            sock.close()
            os.close(self._notify_r)
            os.close(self._notify_w)
            _emit_ctrl(_make_frame("UDP_CLOSE", fid))
            if self._on_close is not None:
                self._on_close()

    def _io_loop(self, sock: socket.socket, fid: str) -> None:
        """Main select loop — runs until ``_closed`` is set and pending sends drain."""
        # Note: datagrams enqueued via feed() after _closed is set but before
        # the loop checks the flag are silently dropped.  This is intentional
        # — UDP is best-effort and draining on close is not required.
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
                    pass
                except OSError:
                    return


# ── Dispatcher ────────────────────────────────────────────────────────────────


class _Dispatcher:
    """Owns the TCP and UDP worker registries and dispatches incoming frames.

    Registry discipline
    -------------------
    ``on_close`` callbacks are the **only** place where entries are removed
    from the registries.  Frame handlers never pop from the maps directly.
    """

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

    def dispatch(self, line: str) -> None:
        """Parse and dispatch one raw frame line."""
        if len(line) > _MAX_INBOUND_FRAME_LEN:
            _log("debug", "oversized frame dropped (%d chars)", len(line))
            return
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

    def _on_conn_open(self, parts: list[str]) -> None:
        if len(parts) < 2:
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

        if self._fixed_host is not None:
            if self._fixed_port is None:
                _emit_ctrl(_make_error_frame(cid, "portforward: fixed_port not set"))
                return
            host, port = self._fixed_host, self._fixed_port
        elif len(parts) >= 3:
            result = _parse_host_port(parts[2], cid, "CONN_OPEN")
            if result is None:
                _emit_ctrl(
                    _make_error_frame(
                        cid, f"invalid CONN_OPEN host:port: {parts[2]!r}"
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

        worker = TcpConnectionWorker(cid, host, port, on_close=on_close)
        with self._conn_lock:
            # Re-check inside the lock to close the window between the earlier
            # duplicate check and this insert.  The dispatcher is single-
            # threaded so this cannot race in practice, but the pattern is
            # correct if concurrency is ever introduced.
            if cid in self._conn_map:
                _log("debug", "duplicate CONN_OPEN for %s (late check) — sending ERROR", cid)
                _emit_ctrl(_make_error_frame(cid, "duplicate CONN_OPEN"))
                return
            self._conn_map[cid] = worker
        worker.start()

    def _on_data(self, parts: list[str]) -> None:
        if len(parts) < 3:
            return
        cid = parts[1]
        with self._conn_lock:
            worker = self._conn_map.get(cid)
        if worker is None:
            return
        try:
            data = _b64decode(parts[2])
        except ValueError:
            _log("debug", "invalid base64url DATA for conn %s", cid)
            _emit_ctrl(_make_error_frame(cid, "invalid base64url in DATA frame"))
            return
        worker.feed(data)

    def _on_conn_close(self, parts: list[str]) -> None:
        if len(parts) < 2:
            return
        cid = parts[1]
        with self._conn_lock:
            worker = self._conn_map.get(cid)
        if worker is not None:
            worker.signal_eof()

    def _on_udp_open(self, parts: list[str]) -> None:
        if len(parts) < 3:
            return
        fid = parts[1]
        if not _ID_RE.match(fid):
            _log("debug", "UDP_OPEN invalid flow_id format: %r", fid)
            return

        with self._udp_lock:
            if fid in self._udp_map:
                _log("debug", "duplicate UDP_OPEN for %s — sending UDP_CLOSE", fid)
                _emit_ctrl(_make_frame("UDP_CLOSE", fid))
                return

        result = _parse_host_port(parts[2], fid, "UDP_OPEN")
        if result is None:
            _emit_ctrl(_make_frame("UDP_CLOSE", fid))
            return
        host, port = result

        def on_close(flow_id: str = fid) -> None:
            with self._udp_lock:
                self._udp_map.pop(flow_id, None)

        flow = UdpFlowWorker(fid, host, port, on_close=on_close)
        with self._udp_lock:
            self._udp_map[fid] = flow
        flow.start()

    def _on_udp_data(self, parts: list[str]) -> None:
        if len(parts) < 3:
            return
        fid = parts[1]
        with self._udp_lock:
            flow = self._udp_map.get(fid)
        if flow is None:
            return
        try:
            data = _b64decode(parts[2])
        except ValueError:
            _log("debug", "invalid base64url UDP_DATA for flow %s", fid)
            _emit_ctrl(_make_frame("UDP_CLOSE", fid))
            return
        flow.feed(data)

    def _on_udp_close(self, parts: list[str]) -> None:
        if len(parts) < 2:
            return
        fid = parts[1]
        with self._udp_lock:
            flow = self._udp_map.get(fid)
        if flow is not None:
            flow.close()

    def run(self) -> None:
        """Read stdin line-by-line and dispatch each frame until EOF."""
        for raw_line in sys.stdin:
            self.dispatch(raw_line.rstrip("\n\r"))
        _log("info", "stdin EOF — dispatcher exiting")


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    """Parse arguments, initialise the writer, and run the dispatcher."""
    _install_sigpipe_handler()

    if len(sys.argv) == 1:
        fixed_host = None
        fixed_port = None
    elif len(sys.argv) == 3:
        fixed_host = sys.argv[1]
        try:
            fixed_port = int(sys.argv[2])
        except ValueError:
            sys.stderr.write(f"invalid port: {sys.argv[2]!r}\n")
            sys.exit(1)
        if fixed_port <= 0 or fixed_port > 65_535:
            sys.stderr.write(f"port out of range: {fixed_port}\n")
            sys.exit(1)
    else:
        sys.stderr.write("usage: agent.py [<host> <port>]\n")
        sys.exit(1)

    for path in ("/tmp/exectunnel_agent.py", "/tmp/exectunnel_agent.b64"):
        with contextlib.suppress(OSError):
            os.unlink(path)

    global _writer
    _disable_echo()
    _writer = _FrameWriter()
    _emit_ctrl(_make_frame("AGENT_READY"))

    try:
        _Dispatcher(fixed_host, fixed_port).run()
    finally:
        if _writer is not None:
            _writer.stop()


if __name__ == "__main__":
    main()
