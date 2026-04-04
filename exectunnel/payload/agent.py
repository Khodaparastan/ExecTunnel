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
padding (``urlsafe_b64encode(...).rstrip(b"=")``), consistent with the
client-side ``encode_data_frame`` / ``decode_data_payload`` helpers.

Design notes
------------
* Self-contained — no third-party deps; runs on any bare Python 3.13+ pod.
* TCP and UDP workers use threads with a self-pipe to avoid blocking the
  stdin-reader thread.
* All TCP sends use a per-chunk write loop with ``select``-based blocking so
  a slow remote cannot stall the entire thread.
* Stdout is serialised by a single ``_FrameWriter`` daemon thread.
  Control frames go into an unbounded ``SimpleQueue`` (always drained first);
  DATA / UDP_DATA frames go into a bounded ``Queue`` (cap
  ``_STDOUT_DATA_QUEUE_CAP``).  Both queues receive a stop sentinel on
  shutdown so no worker thread blocks indefinitely in ``emit_data``.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import errno
import os
import queue
import select
import socket
import sys
import termios
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

# ── Protocol constants ────────────────────────────────────────────────────────

_FRAME_PREFIX: str = "<<<EXECTUNNEL:"
_FRAME_SUFFIX: str = ">>>"

# Agent version — must match the client's expected version.
_AGENT_VERSION: str = "1"

# ── Logging ───────────────────────────────────────────────────────────────────

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

# Maximum queued inbound datagrams before a UDP flow starts dropping.
_MAX_UDP_INBOUND_DGRAMS: int = 2_048

# Bounded stdout data-frame queue capacity.
_STDOUT_DATA_QUEUE_CAP: int = 2_048

# Maximum data frames drained per writer-loop iteration (fairness cap).
_WRITER_DATA_BATCH_SIZE: int = 64

# TCP connect timeout in seconds.
_TCP_CONNECT_TIMEOUT_SECS: float = 28.0

# Maximum UDP datagram size (theoretical IPv4/IPv6 maximum).
_MAX_UDP_DGRAM_BYTES: int = 65_535

# Log a UDP drop warning every N drops.
_UDP_DROP_WARN_EVERY: int = 100

# select() timeout — 10 ms balances interactive latency vs CPU overhead.
_SELECT_TIMEOUT_SECS: float = 0.01

# Writer control-queue poll timeout — longer than select to reduce busy-polling.
_WRITER_CTRL_POLL_SECS: float = 0.05

# Writer thread join timeout on shutdown.
_WRITER_JOIN_TIMEOUT_SECS: float = 5.0

# How long to wait for the remote to close after SHUT_WR (half-close).
_HALF_CLOSE_DEADLINE_SECS: float = 30.0


# ── Base64url helpers (no padding) ────────────────────────────────────────────


def _b64encode(data: bytes) -> str:
    """URL-safe base64 encode with no padding.

    Matches the client-side ``encode_data_frame`` / ``urlsafe_b64encode``
    convention so frames are decodable without modification.
    """
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
    """Remove RFC 2732 brackets from an IPv6 address literal.

    ``encode_host_port`` on the client side bracket-quotes IPv6 addresses
    (e.g. ``[::1]``).  ``socket.create_connection`` and ``getaddrinfo``
    accept bracketed literals on most platforms, but stripping is safer and
    more portable.
    """
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


# ── Frame parsing helpers ─────────────────────────────────────────────────────


def _parse_host_port(
    raw: str, frame_id: str, frame_type: str
) -> tuple[str, int] | None:
    """Parse a ``[host]:port`` or ``host:port`` string into ``(host, port)``.

    Uses ``rpartition(":")`` so IPv6 bracket-quoted addresses parse correctly.

    Args:
        raw:        The raw ``host:port`` string from the frame payload.
        frame_id:   Connection or flow ID for log context.
        frame_type: Frame type name for log context (``"CONN_OPEN"`` etc.).

    Returns:
        ``(host, port)`` on success, ``None`` on any parse failure.
    """
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

    Control frames (CONN_ACK, ERROR, AGENT_READY, …) are enqueued into an
    unbounded ``SimpleQueue`` and always flushed before any pending data frame.
    DATA / UDP_DATA frames go into a bounded ``Queue``; when it is full the
    calling worker thread blocks — propagating backpressure through the kernel
    TCP receive buffer to the remote sender naturally.

    Shutdown
    --------
    :meth:`stop` inserts the stop sentinel into **both** queues so that any
    worker thread blocked in :meth:`emit_data` unblocks immediately rather
    than waiting for the bounded queue to drain.  The writer thread drains
    remaining data frames before exiting so no in-flight frames are lost.
    """

    _STOP: object = object()

    def __init__(self) -> None:
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

        Unblocks immediately if :meth:`stop` is called concurrently, because
        :meth:`stop` inserts the stop sentinel into the data queue.  Callers
        must discard the frame if :meth:`stop` has been called.
        """
        self._data.put(line)

    def stop(self) -> None:
        """Signal the writer thread to flush remaining frames and exit.

        Inserts the stop sentinel into both queues so that any thread blocked
        in :meth:`emit_data` unblocks immediately.
        """
        self._ctrl.put(self._STOP)
        # Unblock any thread blocked in emit_data() — put_nowait is safe
        # here because the data queue is bounded but the sentinel is small
        # and we only insert one.  If the queue is full, force-insert by
        # temporarily bypassing the cap via put() with no timeout — the
        # writer thread will drain it promptly.
        try:
            self._data.put_nowait(self._STOP)
        except queue.Full:
            # Queue is full — writer thread will drain it and see the ctrl
            # sentinel first, then exit.  Worker threads will unblock when
            # the writer drains enough space.  This is acceptable: the
            # sentinel in ctrl guarantees the writer exits regardless.
            pass
        self._thread.join(timeout=_WRITER_JOIN_TIMEOUT_SECS)

    def _run(self) -> None:
        out = sys.stdout
        ctrl = self._ctrl
        data = self._data
        stop = self._STOP
        batch_size = _WRITER_DATA_BATCH_SIZE

        while True:
            # ── Poll control queue ────────────────────────────────────────────
            try:
                item = ctrl.get(timeout=_WRITER_CTRL_POLL_SECS)
            except queue.Empty:
                item = None

            if item is stop:
                # Drain remaining data frames before exiting so no
                # in-flight frames are silently discarded.
                while True:
                    try:
                        d = data.get_nowait()
                        if d is stop:
                            break
                        if isinstance(d, str):
                            try:
                                out.write(d + "\n")
                            except OSError:
                                return
                    except queue.Empty:
                        break
                with contextlib.suppress(OSError):
                    out.flush()
                return

            if item is not None:
                if not isinstance(item, str):
                    continue
                try:
                    out.write(item + "\n")
                    # Drain any additional pending control frames first
                    # to ensure control frames are never delayed by data.
                    while True:
                        try:
                            nxt = ctrl.get_nowait()
                            if nxt is stop:
                                with contextlib.suppress(OSError):
                                    out.flush()
                                return
                            if isinstance(nxt, str):
                                out.write(nxt + "\n")
                        except queue.Empty:
                            break
                except OSError:
                    return

            # ── Drain pending data frames (bounded batch for fairness) ────────
            batch = 0
            while batch < batch_size:
                try:
                    d = data.get_nowait()
                    if d is stop:
                        # Stop sentinel in data queue — exit promptly.
                        with contextlib.suppress(OSError):
                            out.flush()
                        return
                    if isinstance(d, str):
                        try:
                            out.write(d + "\n")
                        except OSError:
                            return
                    batch += 1
                except queue.Empty:
                    break

            if item is not None or batch > 0:
                with contextlib.suppress(OSError):
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
    """Emit a data frame — blocks the calling thread when the stdout queue is full.

    Backpressure propagates naturally through the kernel TCP receive buffer
    to the remote sender.
    """
    if _writer is not None:
        _writer.emit_data(line)
    else:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


# ── Terminal helpers ──────────────────────────────────────────────────────────


def _disable_echo() -> None:
    """Disable terminal echo on stdin so shell output does not pollute the channel.

    No-op when stdin is not a tty (e.g. piped input in tests).
    """
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

    Uses a tight write loop with ``select`` so the calling thread is never
    indefinitely blocked on a single slow send.

    Args:
        sock: A non-blocking socket.
        data: The bytes to send in full.

    Returns:
        ``True`` on success, ``False`` if the socket closed or errored
        mid-send.
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
            _, writable, _ = select.select([], [sock], [], 5.0)
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
    frame is emitted.  The IO loop exits on its next iteration.

    Resource cleanup
    ----------------
    Pipe FDs and the socket are always closed in ``_run``'s ``finally`` block,
    regardless of how ``_io_loop`` exits (normal return, early return on send
    failure, or exception).
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

        # Self-pipe: any thread writes a byte to wake the IO loop immediately.
        self._notify_r, self._notify_w = os.pipe()
        os.set_blocking(self._notify_r, False)
        os.set_blocking(self._notify_w, False)

        # _closed: set by signal_eof() (client EOF) or feed() (saturation).
        self._closed = False
        # _saturated: set only by feed() when the inbound queue overflows.
        # Prevents further feeds and triggers ERROR emission.
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
            # Emit ERROR outside the lock — avoids holding the lock across
            # a potential GIL-release in SimpleQueue.put().
            reason = _b64encode(b"agent tcp inbound saturated")
            _emit_ctrl(f"{_FRAME_PREFIX}ERROR:{self.conn_id}:{reason}{_FRAME_SUFFIX}")
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

        Sets ``_closed`` and wakes the IO loop so it can drain remaining
        inbound chunks, issue ``SHUT_WR``, and wait for the remote FIN.

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
            # create_connection returns a blocking socket; switch to
            # non-blocking before entering the select loop.
            sock.setblocking(False)
        except OSError as exc:
            reason = _b64encode(str(exc).encode())
            _emit_ctrl(f"{_FRAME_PREFIX}ERROR:{cid}:{reason}{_FRAME_SUFFIX}")
            _log("warning", "conn %s connect failed: %s", cid, exc)
            os.close(self._notify_r)
            os.close(self._notify_w)
            if self._on_close is not None:
                self._on_close()
            return

        self._sock = sock
        _emit_ctrl(f"{_FRAME_PREFIX}CONN_ACK:{cid}{_FRAME_SUFFIX}")

        try:
            self._io_loop(sock, cid)
        finally:
            # Always close resources regardless of how _io_loop exits.
            sock.close()
            os.close(self._notify_r)
            os.close(self._notify_w)
            _emit_ctrl(f"{_FRAME_PREFIX}CONN_CLOSE:{cid}{_FRAME_SUFFIX}")
            if self._on_close is not None:
                self._on_close()

    def _io_loop(self, sock: socket.socket, cid: str) -> None:
        """Main select loop — runs until the connection is fully closed.

        Early returns from this method are safe: all resource cleanup is
        performed in ``_run``'s ``finally`` block, not here.
        """
        local_shut = False
        local_shut_deadline: float | None = None

        while True:
            readable, _, errored = select.select(
                [sock, self._notify_r], [], [sock], _SELECT_TIMEOUT_SECS
            )

            # ── Socket error ──────────────────────────────────────────────────
            if errored:
                break

            # ── Receive from remote ───────────────────────────────────────────
            remote_closed = False
            if sock in readable:
                try:
                    chunk = sock.recv(4_096)
                except OSError:
                    break
                if not chunk:
                    remote_closed = True
                else:
                    _emit_data(
                        f"{_FRAME_PREFIX}DATA:{cid}:{_b64encode(chunk)}{_FRAME_SUFFIX}"
                    )

            # ── Drain notify pipe ─────────────────────────────────────────────
            if self._notify_r in readable:
                with contextlib.suppress(OSError):
                    os.read(self._notify_r, 4_096)

            # ── Send pending inbound data ─────────────────────────────────────
            # Skip if we already sent FIN — data queued after SHUT_WR would
            # cause EPIPE; discard it instead.
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
                    # Drain any remaining inbound before tearing down.
                    with self._inbound_lock:
                        remaining, self._inbound = self._inbound, []
                    for chunk in remaining:
                        # Best-effort — ignore send failures on teardown path.
                        _send_all_nonblocking(sock, chunk)
                break

            # ── Half-close: local side done sending ───────────────────────────
            if self._closed and not local_shut:
                with self._inbound_lock:
                    still_pending = bool(self._inbound)
                if not still_pending and not pending:
                    local_shut = True
                    local_shut_deadline = time.monotonic() + _HALF_CLOSE_DEADLINE_SECS
                    with contextlib.suppress(OSError):
                        sock.shutdown(socket.SHUT_WR)

            # ── Half-close deadline ───────────────────────────────────────────
            # If the remote has not closed within the deadline, tear down.
            if (
                local_shut
                and local_shut_deadline is not None
                and time.monotonic() >= local_shut_deadline
            ):
                break


# ── UDP flow worker ───────────────────────────────────────────────────────────


class UdpFlowWorker:
    """UDP flow: datagrams sent to ``(host, port)``; responses forwarded back.

    Uses ``socket.connect()`` so ``recv`` only returns datagrams from the
    target and ``send`` auto-fills the destination address.

    Address resolution
    ------------------
    ``getaddrinfo`` is used to resolve *host* and tries each returned address
    in order — handles dual-stack hosts where the first result may be IPv6
    on an IPv4-only network.

    Inbound saturation
    ------------------
    When the inbound queue reaches :data:`_MAX_UDP_INBOUND_DGRAMS`, datagrams
    are silently dropped (UDP semantics).  A warning is logged every
    :data:`_UDP_DROP_WARN_EVERY` drops.

    Resource cleanup
    ----------------
    Pipe FDs and the socket are always closed in ``_run``'s ``finally`` block.
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

        Silently drops and increments the drop counter when the inbound queue
        is full — this is intentional UDP semantics.

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
                        "udp flow %s inbound saturated; dropping datagram (drops=%d)",
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
            infos = socket.getaddrinfo(self._host, self._port, type=socket.SOCK_DGRAM)
            if not infos:
                raise OSError(f"could not resolve {self._host!r}")
        except OSError as exc:
            _emit_ctrl(f"{_FRAME_PREFIX}UDP_CLOSE:{fid}{_FRAME_SUFFIX}")
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
            _emit_ctrl(f"{_FRAME_PREFIX}UDP_CLOSE:{fid}{_FRAME_SUFFIX}")
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
            _emit_ctrl(f"{_FRAME_PREFIX}UDP_CLOSE:{fid}{_FRAME_SUFFIX}")
            if self._on_close is not None:
                self._on_close()

    def _io_loop(self, sock: socket.socket, fid: str) -> None:
        """Main select loop — runs until ``_closed`` is set and pending sends drain."""
        while not self._closed:
            readable, _, errored = select.select(
                [sock, self._notify_r], [], [sock], _SELECT_TIMEOUT_SECS
            )

            if errored:
                break

            # ── Receive response from remote ──────────────────────────────────
            if sock in readable:
                try:
                    chunk = sock.recv(_MAX_UDP_DGRAM_BYTES)
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                        break
                    chunk = b""
                if chunk:
                    _emit_data(
                        f"{_FRAME_PREFIX}UDP_DATA:{fid}:{_b64encode(chunk)}"
                        f"{_FRAME_SUFFIX}"
                    )

            # ── Drain notify pipe ─────────────────────────────────────────────
            if self._notify_r in readable:
                with contextlib.suppress(OSError):
                    os.read(self._notify_r, 4_096)

            # ── Send pending inbound datagrams ────────────────────────────────
            # Swap pattern — avoids list copy on every iteration.
            with self._inbound_lock:
                pending, self._inbound = self._inbound, []

            for dgram in pending:
                try:
                    sock.send(dgram)
                except BlockingIOError:
                    # UDP send buffer full — drop (UDP semantics).
                    pass
                except OSError:
                    return


# ── Dispatcher ────────────────────────────────────────────────────────────────


class _Dispatcher:
    """Owns the TCP and UDP worker registries and dispatches incoming frames.

    All public methods are called from the single stdin-reader thread, so
    ``_conn_map`` and ``_udp_map`` are only mutated from one thread.  The
    locks exist solely for the ``on_close`` callbacks which are called from
    worker threads.

    Args:
        fixed_host: If not ``None``, all ``CONN_OPEN`` frames use this host
                    regardless of the frame payload (portforward mode).
        fixed_port: Required when *fixed_host* is set.
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

    # ── Frame dispatch ────────────────────────────────────────────────────────

    def dispatch(self, line: str) -> None:
        """Parse and dispatch one raw frame line.

        Silently ignores:
        * Lines that are not valid frames (no prefix/suffix).
        * ``KEEPALIVE`` frames (client heartbeat — no response needed).
        * Unknown frame types (forward-compatible).

        Args:
            line: A single newline-stripped line from stdin.
        """
        if not (line.startswith(_FRAME_PREFIX) and line.endswith(_FRAME_SUFFIX)):
            return

        inner = line[len(_FRAME_PREFIX) : -len(_FRAME_SUFFIX)]
        # Split into at most 3 parts: type, id, rest.
        # "rest" may contain ":" (IPv6 addresses, base64 payload, etc.)
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
                pass  # Client heartbeat — intentionally ignored.
            case _:
                _log("debug", "unknown frame type ignored: %s", parts[0])

    # ── TCP frame handlers ────────────────────────────────────────────────────

    def _on_conn_open(self, parts: list[str]) -> None:
        if len(parts) < 2:
            return
        cid = parts[1]

        with self._conn_lock:
            if cid in self._conn_map:
                _log("debug", "duplicate CONN_OPEN ignored for %s", cid)
                return

        if self._fixed_host is not None:
            assert self._fixed_port is not None  # guaranteed by main()
            host, port = self._fixed_host, self._fixed_port
        elif len(parts) >= 3:
            result = _parse_host_port(parts[2], cid, "CONN_OPEN")
            if result is None:
                return
            host, port = result
        else:
            _log("debug", "CONN_OPEN missing host:port for %s", cid)
            return

        def on_close(conn_id: str = cid) -> None:
            with self._conn_lock:
                self._conn_map.pop(conn_id, None)

        worker = TcpConnectionWorker(cid, host, port, on_close=on_close)
        with self._conn_lock:
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
            return
        worker.feed(data)

    def _on_conn_close(self, parts: list[str]) -> None:
        if len(parts) < 2:
            return
        cid = parts[1]
        with self._conn_lock:
            worker = self._conn_map.pop(cid, None)
        if worker is not None:
            worker.signal_eof()

    # ── UDP frame handlers ────────────────────────────────────────────────────

    def _on_udp_open(self, parts: list[str]) -> None:
        if len(parts) < 3:
            return
        fid = parts[1]

        with self._udp_lock:
            if fid in self._udp_map:
                _log("debug", "duplicate UDP_OPEN ignored for %s", fid)
                return

        result = _parse_host_port(parts[2], fid, "UDP_OPEN")
        if result is None:
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
            return
        flow.feed(data)

    def _on_udp_close(self, parts: list[str]) -> None:
        if len(parts) < 2:
            return
        fid = parts[1]
        with self._udp_lock:
            flow = self._udp_map.pop(fid, None)
        if flow is not None:
            flow.close()

    # ── Stdin loop ────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Read stdin line-by-line and dispatch each frame until EOF.

        EOF is the normal exit condition — it occurs when the client closes
        the WebSocket / exec channel.
        """
        for raw_line in sys.stdin:
            self.dispatch(raw_line.rstrip("\n\r"))
        _log("info", "stdin EOF — dispatcher exiting")


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    """Parse arguments, initialise the writer, and run the dispatcher."""
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

    # Remove own files immediately so agent source is not left on disk.
    for path in ("/tmp/exectunnel_agent.py", "/tmp/exectunnel_agent.b64"):
        with contextlib.suppress(OSError):
            os.unlink(path)

    global _writer
    _disable_echo()
    _writer = _FrameWriter()
    _emit_ctrl(f"{_FRAME_PREFIX}AGENT_READY{_FRAME_SUFFIX}")

    try:
        _Dispatcher(fixed_host, fixed_port).run()
    finally:
        _writer.stop()


if __name__ == "__main__":
    main()
