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
    <<<EXECTUNNEL:DATA:cN:base64>>>
    <<<EXECTUNNEL:CONN_CLOSE:cN>>>
    <<<EXECTUNNEL:UDP_OPEN:uN:host:port>>>
    <<<EXECTUNNEL:UDP_DATA:uN:base64>>>
    <<<EXECTUNNEL:UDP_CLOSE:uN>>>

  agent → local:
    <<<EXECTUNNEL:AGENT_READY>>>
    <<<EXECTUNNEL:CONN_ACK:cN>>>
    <<<EXECTUNNEL:DATA:cN:base64>>>
    <<<EXECTUNNEL:CONN_CLOSED_ACK:cN>>>
    <<<EXECTUNNEL:ERROR:cN:base64_reason>>>
    <<<EXECTUNNEL:UDP_DATA:uN:base64>>>
    <<<EXECTUNNEL:UDP_CLOSED:uN>>>

Design notes
------------
* This script is intentionally self-contained (no third-party deps) so it
  can run in a bare Python 3 environment inside any pod.
* TCP and UDP workers use threads with a self-pipe to avoid blocking the
  stdin-reader thread.
* ``sock.setblocking(True)`` is **never** used while we own the select loop.
  All sends use a per-chunk write loop with non-blocking sockets so a slow
  remote cannot stall the entire thread.
* Stdout is written by a single dedicated ``_FrameWriter`` daemon thread.
  Control frames (CONN_ACK, ERROR, …) go into an unbounded
  ``queue.SimpleQueue`` that is always drained first; DATA/UDP_DATA frames
  go into a bounded ``queue.Queue`` (cap ``_STDOUT_DATA_QUEUE_CAP``).
  This prevents a DATA-frame backlog from blocking CONN_ACK delivery when
  the WS pipe buffer fills under high-throughput TCP sessions.
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

_FRAME_PREFIX = "<<<EXECTUNNEL:"
_FRAME_SUFFIX = ">>>"

_LOG_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
_LOG_LEVEL = _LOG_LEVELS.get(
    os.getenv("EXECTUNNEL_AGENT_LOG_LEVEL", "warning").lower(), 30
)

_MAX_TCP_INBOUND_CHUNKS = 1024
_MAX_UDP_INBOUND_DGRAMS = 2048

# Capacity of the bounded data-frame stdout queue.  When full, emit_data()
# blocks the calling worker thread — propagating backpressure through the
# kernel TCP receive buffer to the remote sender naturally.
_STDOUT_DATA_QUEUE_CAP = 2048


class _FrameWriter:
    """
    Single daemon thread that serialises all stdout writes.

    Control frames (CONN_ACK, ERROR, AGENT_READY, …) are enqueued into an
    unbounded SimpleQueue and always flushed before any pending data frame.
    DATA / UDP_DATA frames go into a bounded Queue; when it is full the calling
    worker thread blocks — propagating backpressure through the kernel TCP
    receive buffer to the remote sender naturally.
    """

    # Sentinel that tells the writer thread to exit.
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
        self._thread.join()

    def _run(self) -> None:
        out = sys.stdout
        ctrl = self._ctrl
        data = self._data
        stop_sentinel = self._STOP
        while True:
            # Try to get a control frame first; fall through to data after a
            # short timeout so data frames are not starved when no control
            # frames are in flight (the common case during steady TCP streaming).
            try:
                item = ctrl.get(timeout=0.005)
            except queue.Empty:
                item = None

            if item is stop_sentinel:
                # Drain remaining data frames before exiting.
                while True:
                    try:
                        d = data.get_nowait()
                        if d is not stop_sentinel:
                            out.write(d + "\n")  # type: ignore[operator]
                    except queue.Empty:
                        break
                out.flush()
                return

            if item is not None:
                # Write the control frame.
                out.write(item + "\n")  # type: ignore[operator]
                # Drain any additional pending control frames first.
                while True:
                    try:
                        nxt = ctrl.get_nowait()
                        if nxt is stop_sentinel:
                            out.flush()
                            return
                        out.write(nxt + "\n")  # type: ignore[operator]
                    except queue.Empty:
                        break

            # Drain pending data frames (up to a batch to stay fair).
            batch = 0
            while batch < 64:
                try:
                    d = data.get_nowait()
                    out.write(d + "\n")  # type: ignore[operator]
                    batch += 1
                except queue.Empty:
                    break

            if item is not None or batch > 0:
                out.flush()


# Module-level writer instance — initialised in main() before any threads start.
_writer: _FrameWriter | None = None


def _log(level: str, msg: str, *args: object) -> None:
    """Write agent diagnostics to stderr without polluting frame stdout."""
    lvl = _LOG_LEVELS.get(level.lower(), 30)
    if lvl < _LOG_LEVEL:
        return
    text = msg % args if args else msg
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write(f"{ts} {level.upper():7s} agent: {text}\n")
    sys.stderr.flush()


def _write_ctrl_frame(line: str) -> None:
    """Emit a control frame — always delivered before any pending data frame."""
    # Architecture-doc alias used by tests and documentation.
    # See docs/architecture.md which refers to this function as ``_emit_ctrl``.
    if _writer is not None:
        _writer.emit_ctrl(line)
    else:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


_emit_ctrl = _write_ctrl_frame  # public alias matching architecture docs


def _write_data_frame(line: str) -> None:
    """Emit a data frame — blocks the calling thread when the stdout queue is full,
    propagating backpressure through the kernel TCP receive buffer to the sender."""
    if _writer is not None:
        _writer.emit_data(line)
    else:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _disable_echo() -> None:
    try:
        fd = sys.stdin.fileno()
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~(termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        _log("debug", "stdin is not a tty; skipping echo disable")


def _send_all_nonblocking(sock: socket.socket, data: bytes) -> bool:
    """
    Send all of *data* through a non-blocking *sock*.

    Returns True on success, False if the socket closed mid-send.
    Uses a tight write loop with ``select`` so that the calling thread is
    never indefinitely blocked on a single slow send.
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
            # Socket buffer full — wait briefly for it to drain.
            _, writable, _ = select.select([], [sock], [], 5.0)
            if not writable:
                # Timed out — remote is not consuming data; give up.
                return False
        except OSError:
            return False
    return True


# ── TCP connection worker ─────────────────────────────────────────────────────


class TcpConnectionWorker:
    """
    Manages one TCP connection to (host, port) on behalf of a conn_id.

    The worker thread runs a select() loop:
    * readable socket  → emit DATA frame to stdout
    * notify pipe      → drain inbound queue → send to socket
    * _closed flag     → break after draining pending data
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
        # Emit the ERROR control frame *outside* the lock so that
        # _inbound_lock is not held while calling into _FrameWriter —
        # this avoids holding the lock across a potential GIL-release
        # in queue.SimpleQueue.put() and keeps the critical section tight.
        if saturated:
            reason = base64.b64encode(b"agent tcp inbound saturated").decode()
            _write_ctrl_frame(f"{_FRAME_PREFIX}ERROR:{self.conn_id}:{reason}{_FRAME_SUFFIX}")
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
            sock = socket.create_connection((self._host, self._port), timeout=28)
            sock.setblocking(False)
        except OSError as exc:
            reason = base64.b64encode(str(exc).encode()).decode()
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
            _write_ctrl_frame(f"{_FRAME_PREFIX}CONN_CLOSED_ACK:{cid}{_FRAME_SUFFIX}")
            if self._on_close is not None:
                self._on_close()

    def _io_loop(self, sock: socket.socket, cid: str) -> None:
        # True once we have sent FIN to the remote (SHUT_WR).
        local_shut = False
        # Deadline after which we stop waiting for the remote to close its side.
        local_shut_deadline: float | None = None
        while True:
            readable, _, _ = select.select([sock, self._notify_r], [], [], 0.05)

            # ── Receive from remote ───────────────────────────────────────────
            remote_closed = False
            if sock in readable:
                try:
                    chunk = sock.recv(4096)
                except OSError:
                    break
                if not chunk:
                    # Remote sent FIN — drain any remaining queued writes first,
                    # then exit.
                    remote_closed = True
                else:
                    _write_data_frame(
                        f"{_FRAME_PREFIX}DATA:{cid}:{base64.b64encode(chunk).decode()}{_FRAME_SUFFIX}"
                    )

            # ── Drain notify pipe ─────────────────────────────────────────────
            if self._notify_r in readable:
                with contextlib.suppress(OSError):
                    os.read(self._notify_r, 4096)

            # ── Send pending inbound data ─────────────────────────────────────
            # Skip if we already sent FIN — any data queued after SHUT_WR
            # cannot be sent and would cause EPIPE; discard it instead.
            if not local_shut:
                with self._inbound_lock:
                    pending = self._inbound[:]
                    self._inbound.clear()

                for chunk in pending:
                    if not _send_all_nonblocking(sock, chunk):
                        return
            else:
                pending = []

            # ── Remote closed its write side ──────────────────────────────────
            # Drain any remaining queued writes before exiting so we don't RST.
            if remote_closed:
                if not local_shut:
                    with self._inbound_lock:
                        remaining = self._inbound[:]
                        self._inbound.clear()
                    for chunk in remaining:
                        _send_all_nonblocking(sock, chunk)
                break

            # ── Half-close: local side done sending ───────────────────────────
            # Once _closed is set and the inbound queue is fully drained, send
            # FIN to the remote (SHUT_WR) instead of closing the socket
            # abruptly.  Re-check self._inbound under the lock to avoid a race
            # where feed() enqueued data after we drained pending above.
            if self._closed and not local_shut:
                with self._inbound_lock:
                    still_pending = bool(self._inbound)
                if not still_pending and not pending:
                    local_shut = True
                    local_shut_deadline = time.monotonic() + 30.0
                    with contextlib.suppress(OSError):
                        sock.shutdown(socket.SHUT_WR)

            # If we already sent FIN but the remote hasn't closed within the
            # deadline, only break if the remote is also no longer sending data.
            # Breaking while the remote is still actively sending would silently
            # discard in-flight data and RST a live connection (e.g. an SSH
            # session that is still streaming output after the client sent EOF).
            if local_shut and local_shut_deadline is not None and time.monotonic() >= local_shut_deadline:
                # Check whether the remote socket still has data to read.
                readable_now, _, _ = select.select([sock], [], [], 0)
                if sock in readable_now:
                    # Remote is still sending — drain this chunk and reset the
                    # deadline so we keep waiting rather than hard-killing the
                    # connection while data is in flight.
                    try:
                        chunk = sock.recv(4096)
                    except OSError:
                        break
                    if not chunk:
                        # Remote sent FIN — we are done.
                        break
                    _write_data_frame(
                        f"{_FRAME_PREFIX}DATA:{cid}:{base64.b64encode(chunk).decode()}{_FRAME_SUFFIX}"
                    )
                    # Extend the deadline: remote is still alive and sending.
                    local_shut_deadline = time.monotonic() + 30.0
                else:
                    # Remote is not sending anything — it is safe to close.
                    break


# ── UDP flow worker ───────────────────────────────────────────────────────────


class UdpFlowWorker:
    """
    UDP flow: datagrams are sent to (host, port) and responses forwarded back.

    Uses ``socket.connect()`` so that ``recv`` only returns datagrams from the
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
                if self._drop_count == 1 or self._drop_count % 100 == 0:
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
        try:
            # Resolve address family before creating the socket.
            infos = socket.getaddrinfo(self._host, self._port, type=socket.SOCK_DGRAM)
            if not infos:
                raise OSError(f"could not resolve {self._host!r}")
            family, _, _, _, addr = infos[0]
            sock = socket.socket(family, socket.SOCK_DGRAM)
            sock.setblocking(False)
            # connect() restricts recv to packets from this address.
            sock.connect(addr)
        except OSError as exc:
            _write_ctrl_frame(f"{_FRAME_PREFIX}UDP_CLOSED:{fid}{_FRAME_SUFFIX}")
            _log("warning", "udp flow %s open failed: %s", fid, exc)
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
            _write_ctrl_frame(f"{_FRAME_PREFIX}UDP_CLOSED:{fid}{_FRAME_SUFFIX}")
            if self._on_close is not None:
                self._on_close()

    def _io_loop(self, sock: socket.socket, fid: str) -> None:
        while not self._closed:
            readable, _, _ = select.select([sock, self._notify_r], [], [], 0.05)

            if sock in readable:
                try:
                    chunk = sock.recv(65535)
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                        break
                    chunk = b""
                if chunk:
                    _write_data_frame(
                        f"{_FRAME_PREFIX}UDP_DATA:{fid}:{base64.b64encode(chunk).decode()}"
                        f"{_FRAME_SUFFIX}"
                    )

            if self._notify_r in readable:
                with contextlib.suppress(OSError):
                    os.read(self._notify_r, 4096)

            with self._inbound_lock:
                pending = self._inbound[:]
                self._inbound.clear()

            for dgram in pending:
                try:
                    sock.send(dgram)
                except BlockingIOError:
                    # UDP send buffer full — drop the datagram (UDP semantics).
                    pass
                except OSError:
                    return


# ── Stdin dispatcher ──────────────────────────────────────────────────────────


def _dispatch_loop(fixed_host: str | None, fixed_port: int | None) -> None:
    conn_map: dict[str, TcpConnectionWorker | None] = {}
    udp_map: dict[str, UdpFlowWorker | None] = {}
    conn_lock = threading.Lock()
    udp_lock = threading.Lock()

    for raw_line in sys.stdin:
        line = raw_line.rstrip("\n\r")

        if not (line.startswith(_FRAME_PREFIX) and line.endswith(_FRAME_SUFFIX)):
            continue

        inner = line[len(_FRAME_PREFIX) : -len(_FRAME_SUFFIX)]
        # Split into at most 3 parts: type, id, rest.
        # "rest" may itself contain ":" (IPv6 addresses, base64 padding, etc.)
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
                # Use rpartition so IPv6 addresses (with colons) parse correctly.
                host, _, port_str = parts[2].rpartition(":")
                if not host or not port_str:
                    _log("debug", "invalid CONN_OPEN payload for %s", cid)
                    continue
                try:
                    port = int(port_str)
                except ValueError:
                    _log("debug", "invalid CONN_OPEN port for %s: %r", cid, port_str)
                    continue
            else:
                continue

            if port <= 0 or port > 65535:
                _log("debug", "out-of-range CONN_OPEN port for %s: %d", cid, port)
                continue

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
                data = base64.b64decode(parts[2], validate=True)
            except ValueError:
                _log("debug", "invalid base64 DATA for conn %s", cid)
                continue
            data_conn.feed(data)
        elif msg_type == "CONN_CLOSE":
            if len(parts) < 2:
                continue
            cid = parts[1]
            with conn_lock:
                closed_conn = conn_map.pop(cid, None)
            if closed_conn:
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
            host, _, port_str = parts[2].rpartition(":")
            if not host or not port_str:
                _log("debug", "invalid UDP_OPEN payload for %s", fid)
                continue
            try:
                port = int(port_str)
            except ValueError:
                _log("debug", "invalid UDP_OPEN port for %s: %r", fid, port_str)
                continue
            if port <= 0 or port > 65535:
                _log("debug", "out-of-range UDP_OPEN port for %s: %d", fid, port)
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
                data = base64.b64decode(parts[2], validate=True)
            except ValueError:
                _log("debug", "invalid base64 UDP_DATA for flow %s", fid)
                continue
            data_flow.feed(data)
        elif msg_type == "UDP_CLOSE":
            if len(parts) < 2:
                continue
            fid = parts[1]
            with udp_lock:
                closed_flow = udp_map.pop(fid, None)
            if closed_flow:
                closed_flow.close()
        else:
            _log("debug", "unknown frame type ignored: %s", msg_type)


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