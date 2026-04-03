#!/usr/bin/env python3
"""
exectunnel agent — runs inside the pod via exec.

Usage
-----
  python3 /tmp/exectunnel_agent.py                  # tunnel mode (SOCKS5 proxy)
  python3 /tmp/exectunnel_agent.py <host> <port>    # portforward mode (fixed target)

Frame protocol (newline-terminated)
------------------------------------
  local → agent:
    <<<EXECTUNNEL:CONN_OPEN:cN:host:port>>>     tunnel mode
    <<<EXECTUNNEL:CONN_OPEN:cN>>>               portforward mode (uses fixed target)
    <<<EXECTUNNEL:DATA:cN:base64url>>>
    <<<EXECTUNNEL:CONN_CLOSE:cN>>>
    <<<EXECTUNNEL:UDP_OPEN:uN:[host]:port>>>
    <<<EXECTUNNEL:UDP_DATA:uN:base64url>>>
    <<<EXECTUNNEL:UDP_CLOSE:uN>>>

  agent → local:
    <<<EXECTUNNEL:AGENT_READY>>>
    <<<EXECTUNNEL:CONN_ACK:cN>>>
    <<<EXECTUNNEL:DATA:cN:base64url>>>
    <<<EXECTUNNEL:CONN_CLOSE:cN>>>          (was CONN_CLOSED_ACK — renamed)
    <<<EXECTUNNEL:ERROR:cN:base64url_reason>>>
    <<<EXECTUNNEL:UDP_DATA:uN:base64url>>>
    <<<EXECTUNNEL:UDP_CLOSE:uN>>>           (was UDP_CLOSED — renamed)

Encoding
--------
All binary payloads (DATA, UDP_DATA, ERROR) use URL-safe base64 with no
padding (``urlsafe_b64encode(...).rstrip(b"=")``) — consistent with the
client-side ``encode_data_frame`` / ``decode_data_payload`` helpers.

Design notes
------------
* Self-contained — no third-party deps; runs in any bare Python 3 pod.
* TCP and UDP workers use threads with a self-pipe to avoid blocking the
  stdin-reader thread.
* All sends use a per-chunk write loop with non-blocking sockets so a slow
  remote cannot stall the entire thread.
* Stdout is written by a single dedicated ``_FrameWriter`` daemon thread.
  Control frames go into an unbounded ``SimpleQueue`` (always drained first);
  DATA/UDP_DATA frames go into a bounded ``Queue`` (cap ``_STDOUT_DATA_QUEUE_CAP``).
"""

from __future__ import annotations

import base64
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

_FRAME_PREFIX = "<<<EXECTUNNEL:"
_FRAME_SUFFIX = ">>>"

# Agent version — emitted as VERSION_MISMATCH:<ver> if the client sends an
# incompatible version header during bootstrap.
_AGENT_VERSION = "1"

# ── Logging ───────────────────────────────────────────────────────────────────

_LOG_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
_LOG_LEVEL = _LOG_LEVELS.get(
    os.getenv("EXECTUNNEL_AGENT_LOG_LEVEL", "warning").lower(), 30
)

# ── Tuning constants ──────────────────────────────────────────────────────────

_MAX_TCP_INBOUND_CHUNKS: int = 1_024
_MAX_UDP_INBOUND_DGRAMS: int = 2_048
_STDOUT_DATA_QUEUE_CAP: int = 2_048
_TCP_CONNECT_TIMEOUT_SECS: float = 28.0
_MAX_UDP_DGRAM_BYTES: int = 65_535
_UDP_DROP_WARN_EVERY: int = 100
# select() timeout — 10 ms balances interactive latency vs CPU overhead.
_SELECT_TIMEOUT_SECS: float = 0.01
# Writer control-queue poll timeout — longer than select to avoid busy-polling.
_WRITER_CTRL_POLL_SECS: float = 0.05
# Writer thread join timeout.
_WRITER_JOIN_TIMEOUT_SECS: float = 5.0
# Half-close deadline: how long to wait for the remote to close after SHUT_WR.
_HALF_CLOSE_DEADLINE_SECS: float = 30.0


# ── Base64url helpers (no padding) ────────────────────────────────────────────


def _b64encode(data: bytes) -> str:
    """URL-safe base64 encode with no padding — matches client ``encode_data_frame``."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
    """URL-safe base64 decode — re-adds padding stripped by ``_b64encode``."""
    padding = (4 - len(s) % 4) % 4
    try:
        return base64.urlsafe_b64decode(s + "=" * padding)
    except Exception as exc:
        raise ValueError(f"invalid base64url: {s!r}") from exc


# ── IPv6 bracket helper ───────────────────────────────────────────────────────


def _strip_brackets(host: str) -> str:
    """Remove RFC 2732 brackets from an IPv6 address literal.

    ``encode_host_port`` on the client side bracket-quotes IPv6 addresses
    (e.g. ``[::1]``).  ``socket.create_connection`` and ``getaddrinfo``
    accept bracketed literals on most platforms but stripping is safer and
    more portable.
    """
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


# ── Logging ───────────────────────────────────────────────────────────────────


def _log(level: str, msg: str, *args: object) -> None:
    """Write agent diagnostics to stderr without polluting frame stdout."""
    lvl = _LOG_LEVELS.get(level.lower(), 30)
    if lvl < _LOG_LEVEL:
        return
    text = msg % args if args else msg
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write(f"{ts} {level.upper():7s} agent: {text}\n")
    sys.stderr.flush()


# ── Frame writer ──────────────────────────────────────────────────────────────


class _FrameWriter:
    """Single daemon thread that serialises all stdout writes.

    Control frames (CONN_ACK, ERROR, AGENT_READY, …) are enqueued into an
    unbounded ``SimpleQueue`` and always flushed before any pending data frame.
    DATA / UDP_DATA frames go into a bounded ``Queue``; when it is full the
    calling worker thread blocks — propagating backpressure through the kernel
    TCP receive buffer to the remote sender naturally.
    """

    _STOP = object()

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
        """Enqueue a data frame — blocks the caller when the queue is full."""
        self._data.put(line)

    def stop(self) -> None:
        """Ask the writer thread to flush remaining frames and exit."""
        self._ctrl.put(self._STOP)
        self._thread.join(timeout=_WRITER_JOIN_TIMEOUT_SECS)

    def _run(self) -> None:
        out = sys.stdout
        ctrl = self._ctrl
        data = self._data
        stop_sentinel = self._STOP

        while True:
            # Poll control queue; longer timeout than the original 5 ms to
            # avoid busy-polling at 200 Hz during steady data streaming.
            try:
                item = ctrl.get(timeout=_WRITER_CTRL_POLL_SECS)
            except queue.Empty:
                item = None

            if item is stop_sentinel:
                # Drain remaining data frames before exiting.
                while True:
                    try:
                        d = data.get_nowait()
                        if d is not stop_sentinel:
                            assert isinstance(d, str)
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
                assert isinstance(item, str)
                try:
                    out.write(item + "\n")
                    # Drain any additional pending control frames first.
                    while True:
                        try:
                            nxt = ctrl.get_nowait()
                            if nxt is stop_sentinel:
                                out.flush()
                                return
                            assert isinstance(nxt, str)
                            out.write(nxt + "\n")
                        except queue.Empty:
                            break
                except OSError:
                    return

            # Drain pending data frames (up to a batch to stay fair).
            batch = 0
            while batch < 64:
                try:
                    d = data.get_nowait()
                    assert isinstance(d, str)
                    try:
                        out.write(d + "\n")
                    except OSError:
                        return
                    batch += 1
                except queue.Empty:
                    break

            if item is not None or batch > 0:
                try:
                    out.flush()
                except OSError:
                    return


# Module-level writer — initialised in main() before any threads start.
_writer: _FrameWriter | None = None


def _write_ctrl_frame(line: str) -> None:
    """Emit a control frame — always delivered before any pending data frame."""
    if _writer is not None:
        _writer.emit_ctrl(line)
    else:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


# Public alias matching architecture docs.
_emit_ctrl = _write_ctrl_frame


def _write_data_frame(line: str) -> None:
    """Emit a data frame — blocks the calling thread when the stdout queue is full,
    propagating backpressure through the kernel TCP receive buffer to the sender.
    """
    if _writer is not None:
        _writer.emit_data(line)
    else:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


# ── Terminal helpers ──────────────────────────────────────────────────────────


def _disable_echo() -> None:
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

    Returns ``True`` on success, ``False`` if the socket closed mid-send.
    Uses a tight write loop with ``select`` so the calling thread is never
    indefinitely blocked on a single slow send.
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
    """Manages one TCP connection to (host, port) on behalf of a conn_id.

    The worker thread runs a select() loop:
    * readable socket  → emit DATA frame to stdout (base64url, no padding)
    * notify pipe      → drain inbound queue → send to socket
    * _closed flag     → SHUT_WR then wait for remote FIN
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
        self._closed = False
        self._saturated = False
        self._drop_count = 0
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"conn-{conn_id}"
        )

    def start(self) -> None:
        self._thread.start()

    def feed(self, data: bytes) -> None:
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
        # Emit ERROR *outside* the lock — avoids holding the lock across
        # a potential GIL-release in queue.SimpleQueue.put().
        if saturated:
            reason = _b64encode(b"agent tcp inbound saturated")
            _write_ctrl_frame(
                f"{_FRAME_PREFIX}ERROR:{self.conn_id}:{reason}{_FRAME_SUFFIX}"
            )
            _log(
                "warning",
                "conn %s inbound saturated; closing connection",
                self.conn_id,
            )
            return
        with contextlib.suppress(OSError):
            os.write(self._notify_w, b"\x00")

    def signal_inbound_eof(self) -> None:
        """Signal the IO loop to stop after draining queued writes."""
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
            # create_connection returns a blocking socket; make it non-blocking
            # before entering the select loop.
            sock.setblocking(False)
        except OSError as exc:
            reason = _b64encode(str(exc).encode())
            _write_ctrl_frame(f"{_FRAME_PREFIX}ERROR:{cid}:{reason}{_FRAME_SUFFIX}")
            _log("warning", "conn %s open failed: %s", cid, exc)
            os.close(self._notify_r)
            os.close(self._notify_w)
            if self._on_close is not None:
                self._on_close()
            return

        self._sock = sock
        _write_ctrl_frame(f"{_FRAME_PREFIX}CONN_ACK:{cid}{_FRAME_SUFFIX}")

        try:
            self._io_loop(sock, cid)
        finally:
            sock.close()
            os.close(self._notify_r)
            os.close(self._notify_w)
            # CONN_CLOSE (renamed from CONN_CLOSED_ACK) — matches client
            # dispatcher which listens for "CONN_CLOSE" as the agent-close signal.
            _write_ctrl_frame(f"{_FRAME_PREFIX}CONN_CLOSE:{cid}{_FRAME_SUFFIX}")
            # on_close is called after pipe FDs are closed; callers must not
            # write to _notify_w inside on_close.
            if self._on_close is not None:
                self._on_close()

    def _io_loop(self, sock: socket.socket, cid: str) -> None:
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
                    _write_data_frame(
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
                # Swap pattern avoids copying the list on every iteration.
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
                    local_shut_deadline = time.monotonic() + _HALF_CLOSE_DEADLINE_SECS
                    with contextlib.suppress(OSError):
                        sock.shutdown(socket.SHUT_WR)

            # ── Half-close deadline ───────────────────────────────────────────
            if (
                local_shut
                and local_shut_deadline is not None
                and time.monotonic() >= local_shut_deadline
            ):
                readable_now, _, _ = select.select([sock], [], [], 0)
                if sock in readable_now:
                    try:
                        chunk = sock.recv(4_096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    _write_data_frame(
                        f"{_FRAME_PREFIX}DATA:{cid}:{_b64encode(chunk)}{_FRAME_SUFFIX}"
                    )
                    # Remote is still alive — extend the deadline.
                    local_shut_deadline = time.monotonic() + _HALF_CLOSE_DEADLINE_SECS
                else:
                    break


# ── UDP flow worker ───────────────────────────────────────────────────────────


class UdpFlowWorker:
    """UDP flow: datagrams sent to (host, port); responses forwarded back.

    Uses ``socket.connect()`` so ``recv`` only returns datagrams from the
    target and ``send`` auto-fills the destination.
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
        self._drop_count = 0
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"udp-{flow_id}"
        )

    def start(self) -> None:
        self._thread.start()

    def feed(self, data: bytes) -> None:
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
        self._closed = True
        with contextlib.suppress(OSError):
            os.write(self._notify_w, b"\x00")

    def _run(self) -> None:
        fid = self._id
        sock: socket.socket | None = None

        # Try each resolved address in order — handles dual-stack hosts where
        # infos[0] may be IPv6 on an IPv4-only network.
        try:
            infos = socket.getaddrinfo(self._host, self._port, type=socket.SOCK_DGRAM)
            if not infos:
                raise OSError(f"could not resolve {self._host!r}")
        except OSError as exc:
            _write_ctrl_frame(f"{_FRAME_PREFIX}UDP_CLOSE:{fid}{_FRAME_SUFFIX}")
            _log("warning", "udp flow %s resolve failed: %s", fid, exc)
            os.close(self._notify_r)
            os.close(self._notify_w)
            if self._on_close is not None:
                self._on_close()
            return

        last_exc: OSError | None = None
        for family, _, _, _, addr in infos:
            try:
                candidate = socket.socket(family, socket.SOCK_DGRAM)
                candidate.setblocking(False)
                candidate.connect(addr)
                sock = candidate
                break
            except OSError as exc:
                last_exc = exc
                with contextlib.suppress(OSError):
                    candidate.close()

        if sock is None:
            _write_ctrl_frame(f"{_FRAME_PREFIX}UDP_CLOSE:{fid}{_FRAME_SUFFIX}")
            _log(
                "warning",
                "udp flow %s open failed: %s",
                fid,
                last_exc,
            )
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
            # UDP_CLOSE (renamed from UDP_CLOSED) — matches client dispatcher.
            _write_ctrl_frame(f"{_FRAME_PREFIX}UDP_CLOSE:{fid}{_FRAME_SUFFIX}")
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
                    _write_data_frame(
                        f"{_FRAME_PREFIX}UDP_DATA:{fid}:{_b64encode(chunk)}"
                        f"{_FRAME_SUFFIX}"
                    )

            if self._notify_r in readable:
                with contextlib.suppress(OSError):
                    os.read(self._notify_r, 4_096)

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


# ── Stdin dispatcher ──────────────────────────────────────────────────────────


def _dispatch_loop(fixed_host: str | None, fixed_port: int | None) -> None:
    # Typed without | None — workers are always non-None when inserted.
    conn_map: dict[str, TcpConnectionWorker] = {}
    udp_map: dict[str, UdpFlowWorker] = {}
    conn_lock = threading.Lock()
    udp_lock = threading.Lock()

    for raw_line in sys.stdin:
        line = raw_line.rstrip("\n\r")

        if not (line.startswith(_FRAME_PREFIX) and line.endswith(_FRAME_SUFFIX)):
            continue

        inner = line[len(_FRAME_PREFIX) : -len(_FRAME_SUFFIX)]
        # Split into at most 3 parts: type, id, rest.
        # "rest" may contain ":" (IPv6 addresses, base64 padding, etc.)
        parts = inner.split(":", 2)
        if not parts:
            continue
        msg_type = parts[0]

        # ── TCP ───────────────────────────────────────────────────────────────

        if msg_type == "CONN_OPEN":
            if len(parts) < 2:
                continue
            cid = parts[1]
            with conn_lock:
                if cid in conn_map:
                    _log("debug", "duplicate CONN_OPEN ignored for %s", cid)
                    continue

            if fixed_host is not None:
                assert fixed_port is not None
                host, port = fixed_host, fixed_port
            elif len(parts) >= 3:
                # Tunnel mode: target is everything after the conn_id.
                # rpartition splits on the last ":" so IPv6 bracket-quoted
                # addresses (e.g. [::1]:8080) parse correctly.
                host_raw, _, port_str = parts[2].rpartition(":")
                if not host_raw or not port_str:
                    _log("debug", "invalid CONN_OPEN payload for %s", cid)
                    continue
                # Strip RFC 2732 brackets from IPv6 literals.
                host = _strip_brackets(host_raw)
                try:
                    port = int(port_str)
                except ValueError:
                    _log(
                        "debug",
                        "invalid CONN_OPEN port for %s: %r",
                        cid,
                        port_str,
                    )
                    continue
            else:
                continue

            if port <= 0 or port > 65_535:
                _log(
                    "debug",
                    "out-of-range CONN_OPEN port for %s: %d",
                    cid,
                    port,
                )
                continue

            # Default-argument capture is the correct Python closure pattern
            # for variables that change each loop iteration.
            def on_conn_close(conn_id: str = cid) -> None:
                with conn_lock:
                    conn_map.pop(conn_id, None)

            conn = TcpConnectionWorker(cid, host, port, on_close=on_conn_close)
            with conn_lock:
                conn_map[cid] = conn
            conn.start()

        elif msg_type == "DATA":
            if len(parts) < 3:
                continue
            cid = parts[1]
            with conn_lock:
                data_conn = conn_map.get(cid)
            if data_conn is None:
                continue
            try:
                data = _b64decode(parts[2])
            except ValueError:
                _log("debug", "invalid base64url DATA for conn %s", cid)
                continue
            data_conn.feed(data)

        elif msg_type == "CONN_CLOSE":
            if len(parts) < 2:
                continue
            cid = parts[1]
            with conn_lock:
                closed_conn = conn_map.pop(cid, None)
            if closed_conn is not None:
                closed_conn.signal_inbound_eof()

        # ── UDP ───────────────────────────────────────────────────────────────

        elif msg_type == "UDP_OPEN":
            if len(parts) < 3:
                continue
            fid = parts[1]
            with udp_lock:
                if fid in udp_map:
                    _log("debug", "duplicate UDP_OPEN ignored for %s", fid)
                    continue
            # rpartition + bracket-strip — same as CONN_OPEN.
            host_raw, _, port_str = parts[2].rpartition(":")
            if not host_raw or not port_str:
                _log("debug", "invalid UDP_OPEN payload for %s", fid)
                continue
            host = _strip_brackets(host_raw)
            try:
                port = int(port_str)
            except ValueError:
                _log(
                    "debug",
                    "invalid UDP_OPEN port for %s: %r",
                    fid,
                    port_str,
                )
                continue
            if port <= 0 or port > 65_535:
                _log(
                    "debug",
                    "out-of-range UDP_OPEN port for %s: %d",
                    fid,
                    port,
                )
                continue

            def on_udp_close(flow_id: str = fid) -> None:
                with udp_lock:
                    udp_map.pop(flow_id, None)

            flow = UdpFlowWorker(fid, host, port, on_close=on_udp_close)
            with udp_lock:
                udp_map[fid] = flow
            flow.start()

        elif msg_type == "UDP_DATA":
            if len(parts) < 3:
                continue
            fid = parts[1]
            with udp_lock:
                data_flow = udp_map.get(fid)
            if data_flow is None:
                continue
            try:
                data = _b64decode(parts[2])
            except ValueError:
                _log("debug", "invalid base64url UDP_DATA for flow %s", fid)
                continue
            data_flow.feed(data)

        elif msg_type == "UDP_CLOSE":
            if len(parts) < 2:
                continue
            fid = parts[1]
            with udp_lock:
                closed_flow = udp_map.pop(fid, None)
            if closed_flow is not None:
                closed_flow.close()

        else:
            _log("debug", "unknown frame type ignored: %s", msg_type)


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
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
    else:
        sys.stderr.write("usage: agent.py [<host> <port>]\n")
        sys.exit(1)

    # Remove own files immediately so agent source is not left on disk.
    for _agent_file in (
        "/tmp/exectunnel_agent.py",
        "/tmp/exectunnel_agent.b64",
    ):
        with contextlib.suppress(OSError):
            os.unlink(_agent_file)

    global _writer
    _disable_echo()
    _writer = _FrameWriter()
    _write_ctrl_frame(f"{_FRAME_PREFIX}AGENT_READY{_FRAME_SUFFIX}")
    try:
        _dispatch_loop(fixed_host, fixed_port)
    finally:
        _writer.stop()


if __name__ == "__main__":
    main()
