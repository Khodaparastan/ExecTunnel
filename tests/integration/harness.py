"""In-process fake-agent harness for ExecTunnel integration tests.

Architecture
------------
``FakeAgent`` is an async WebSocket server handler that speaks the
ExecTunnel agent protocol:

1. **Bootstrap phase** — absorbs all shell commands sent by
   ``TunnelSession._bootstrap()`` (stty, rm, printf, base64, python3 …)
   and replies with ``AGENT_READY`` so the tunnel proceeds to ``_serve()``.

2. **Protocol phase** — reads frames from the tunnel and dispatches them:

   * ``CONN_OPEN``       → calls ``on_conn_open`` hook (default: ACK + echo server)
   * ``CONN_CLOSE``      → calls ``on_conn_close`` hook (default: send CONN_CLOSED_ACK)
   * ``DATA``            → calls ``on_data`` hook (default: echo back)
   * ``UDP_OPEN``        → calls ``on_udp_open`` hook (default: ACK)
   * ``UDP_DATA``        → calls ``on_udp_data`` hook (default: echo back)
   * ``UDP_CLOSE``       → calls ``on_udp_close`` hook (default: send UDP_CLOSED)
   * ``KEEPALIVE``       → silently ignored
   * ``CONN_CLOSE``      → sends ``CONN_CLOSED_ACK``

All hooks are async callables that receive ``(agent, frame_fields)`` and
can be overridden per-test for custom behaviour (slow responses, errors,
saturation, etc.).

``TunnelHarness`` is the top-level fixture helper that:
- starts a ``websockets.serve()`` server on a random loopback port,
- builds a ``TunnelSession`` pointed at that server,
- runs the session in a background task,
- exposes ``socks_addr`` for SOCKS5 client connections,
- provides ``stop()`` to tear everything down cleanly.

Usage example::

    async with TunnelHarness() as h:
        reader, writer = await asyncio.open_connection(*h.socks_addr)
        # ... send SOCKS5 CONNECT handshake ...
        writer.write(b"hello")
        data = await reader.read(1024)
        assert data == b"hello"   # echo
"""

from __future__ import annotations

import asyncio
import base64
import logging
import socket
import struct
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

from exectunnel.core.config import AppConfig, BridgeConfig, get_tunnel_config
from exectunnel.core.consts import FRAME_PREFIX, FRAME_SUFFIX, READY_FRAME
from exectunnel.helpers import parse_frame
from exectunnel.tunnel import TunnelSession

logger = logging.getLogger(__name__)

# ── Frame helpers ─────────────────────────────────────────────────────────────

_P = FRAME_PREFIX
_S = FRAME_SUFFIX


def _frame(msg_type: str, conn_id: str, payload: str = "") -> str:
    if payload:
        return f"{_P}{msg_type}:{conn_id}:{payload}{_S}\n"
    return f"{_P}{msg_type}:{conn_id}{_S}\n"


def _conn_ack(conn_id: str) -> str:
    return _frame("CONN_ACK", conn_id)


def _conn_closed_ack(conn_id: str) -> str:
    return _frame("CONN_CLOSED_ACK", conn_id)


def _data_frame(conn_id: str, data: bytes) -> str:
    return _frame("DATA", conn_id, base64.b64encode(data).decode())


def _udp_data_frame(flow_id: str, host: str, port: int, data: bytes) -> str:
    addr = base64.b64encode(f"{host}:{port}".encode()).decode()
    payload_b64 = base64.b64encode(data).decode()
    return _frame("UDP_DATA", flow_id, f"{addr}:{payload_b64}")


def _udp_closed_frame(flow_id: str) -> str:
    return _frame("UDP_CLOSED", flow_id)


def _error_frame(conn_id: str, reason: str) -> str:
    return _frame("ERROR", conn_id, base64.b64encode(reason.encode()).decode())


# ── Hook type aliases ─────────────────────────────────────────────────────────

HookFn = Callable[["FakeAgent", list[str]], Coroutine[Any, Any, None]]


# ── FakeAgent ─────────────────────────────────────────────────────────────────


@dataclass
class FakeAgent:
    """Stateful fake agent that handles one WebSocket connection.

    Attributes
    ----------
    ws:
        The live WebSocket server connection (set by ``__call__``).
    on_conn_open:
        Hook called when a ``CONN_OPEN`` frame arrives.
        Signature: ``async (agent, parts) -> None`` where ``parts`` is
        ``[msg_type, conn_id, "host:port"]``.
    on_conn_close:
        Hook called when a ``CONN_CLOSE`` frame arrives.
    on_data:
        Hook called when a ``DATA`` frame arrives.
    on_udp_open:
        Hook called when a ``UDP_OPEN`` frame arrives.
    on_udp_data:
        Hook called when a ``UDP_DATA`` frame arrives.
    on_udp_close:
        Hook called when a ``UDP_CLOSE`` frame arrives.
    frames_received:
        All parsed frame parts received after AGENT_READY (for assertions).
    bootstrap_commands:
        Raw shell command lines received during bootstrap phase.
    ready_event:
        Set once AGENT_READY has been sent — tests can await this.
    """

    ws: ServerConnection | None = field(default=None, init=False)
    on_conn_open: HookFn = field(default_factory=lambda: FakeAgent._default_conn_open)
    on_conn_close: HookFn = field(default_factory=lambda: FakeAgent._default_conn_close)
    on_data: HookFn = field(default_factory=lambda: FakeAgent._default_data)
    on_udp_open: HookFn = field(default_factory=lambda: FakeAgent._default_udp_open)
    on_udp_data: HookFn = field(default_factory=lambda: FakeAgent._default_udp_data)
    on_udp_close: HookFn = field(default_factory=lambda: FakeAgent._default_udp_close)
    fast_bootstrap: bool = True
    frames_received: list[list[str]] = field(default_factory=list, init=False)
    bootstrap_commands: list[str] = field(default_factory=list, init=False)
    ready_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def send(self, frame: str) -> None:
        """Send a frame string to the tunnel (thread-safe)."""
        assert self.ws is not None
        async with self._send_lock:
            await self.ws.send(frame.encode())

    async def send_conn_ack(self, conn_id: str) -> None:
        await self.send(_conn_ack(conn_id))

    async def send_conn_closed_ack(self, conn_id: str) -> None:
        await self.send(_conn_closed_ack(conn_id))

    async def send_data(self, conn_id: str, data: bytes) -> None:
        await self.send(_data_frame(conn_id, data))

    async def send_error(self, conn_id: str, reason: str) -> None:
        await self.send(_error_frame(conn_id, reason))

    async def send_udp_data(
        self, flow_id: str, host: str, port: int, data: bytes
    ) -> None:
        await self.send(_udp_data_frame(flow_id, host, port, data))

    async def send_udp_closed(self, flow_id: str) -> None:
        await self.send(_udp_closed_frame(flow_id))

    # ── Default hook implementations ──────────────────────────────────────────

    @staticmethod
    async def _default_conn_open(agent: FakeAgent, parts: list[str]) -> None:
        """ACK the connection immediately."""
        conn_id = parts[1]
        await agent.send_conn_ack(conn_id)

    @staticmethod
    async def _default_conn_close(agent: FakeAgent, parts: list[str]) -> None:
        """Acknowledge the close."""
        conn_id = parts[1]
        await agent.send_conn_closed_ack(conn_id)

    @staticmethod
    async def _default_data(agent: FakeAgent, parts: list[str]) -> None:
        """Echo data back to the tunnel."""
        conn_id = parts[1]
        payload = parts[2] if len(parts) > 2 else ""
        try:
            data = base64.b64decode(payload)
        except Exception:
            return
        await agent.send_data(conn_id, data)

    @staticmethod
    async def _default_udp_open(agent: FakeAgent, parts: list[str]) -> None:
        """No-op — UDP flows don't need an explicit ACK in the protocol."""

    @staticmethod
    async def _default_udp_data(agent: FakeAgent, parts: list[str]) -> None:
        """Echo UDP data back."""
        flow_id = parts[1]
        payload = parts[2] if len(parts) > 2 else ""
        # UDP_DATA payload: base64(host:port):base64(data)
        try:
            addr_b64, data_b64 = payload.split(":", 1)
            addr = base64.b64decode(addr_b64).decode()
            host, _, port_str = addr.rpartition(":")
            data = base64.b64decode(data_b64)
        except Exception:
            return
        await agent.send_udp_data(flow_id, host, int(port_str), data)

    @staticmethod
    async def _default_udp_close(agent: FakeAgent, parts: list[str]) -> None:
        """Send UDP_CLOSED back."""
        flow_id = parts[1]
        await agent.send_udp_closed(flow_id)

    # ── Bootstrap phase ───────────────────────────────────────────────────────

    async def _run_bootstrap(self) -> None:
        """Absorb shell commands until we decide to send AGENT_READY.

        The tunnel sends: stty, rm, printf×N, base64, python3 -c (syntax),
        exec python3.  We absorb all of them and send AGENT_READY after a
        brief delay to simulate the agent starting up.

        When ``fast_bootstrap=True`` (default), AGENT_READY is sent after the
        very first WS message — skipping the full agent.py upload and cutting
        per-test startup from ~5 s to ~50 ms.  Set ``fast_bootstrap=False``
        only when testing bootstrap correctness (scenario 10).
        """
        assert self.ws is not None
        async for msg in self.ws:
            text = msg.decode() if isinstance(msg, bytes) else msg
            for line in text.splitlines():
                line = line.strip()
                if line:
                    self.bootstrap_commands.append(line)
                    logger.debug("fake-agent bootstrap cmd: %s", line[:80])
                    if self.fast_bootstrap or line.startswith("exec python3"):
                        await self.ws.send((READY_FRAME + "\n").encode())
                        self.ready_event.set()
                        return

    # ── Protocol phase ────────────────────────────────────────────────────────

    async def _run_protocol(self) -> None:
        """Dispatch incoming frames to hooks."""
        assert self.ws is not None
        buf = ""
        async for msg in self.ws:
            chunk = msg.decode() if isinstance(msg, bytes) else msg
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                parts = parse_frame(line)
                if parts is None:
                    continue
                self.frames_received.append(parts)
                msg_type = parts[0]
                try:
                    if msg_type == "CONN_OPEN":
                        await self.on_conn_open(self, parts)
                    elif msg_type == "CONN_CLOSE":
                        await self.on_conn_close(self, parts)
                    elif msg_type == "DATA":
                        await self.on_data(self, parts)
                    elif msg_type == "UDP_OPEN":
                        await self.on_udp_open(self, parts)
                    elif msg_type == "UDP_DATA":
                        await self.on_udp_data(self, parts)
                    elif msg_type == "UDP_CLOSE":
                        await self.on_udp_close(self, parts)
                    elif msg_type == "KEEPALIVE":
                        pass  # silently ignored
                    else:
                        logger.debug("fake-agent: unknown frame type %s", msg_type)
                except Exception as exc:
                    # Suppress noisy "connection closed" errors that occur when
                    # the tunnel forces a reconnect mid-test (e.g. B8 pressure).
                    msg = str(exc)
                    if "going away" in msg or "ConnectionClosed" in type(exc).__name__:
                        logger.debug("fake-agent WS closed during %s hook", msg_type)
                    else:
                        logger.warning("fake-agent hook error for %s: %s", msg_type, exc)

    # ── Entry point ───────────────────────────────────────────────────────────

    async def __call__(self, ws: ServerConnection) -> None:
        """WebSocket handler — called by ``websockets.serve()``."""
        self.ws = ws
        try:
            await self._run_bootstrap()
            await self._run_protocol()
        except Exception as exc:
            logger.debug("fake-agent connection closed: %s", exc)


# ── TunnelHarness ─────────────────────────────────────────────────────────────


@dataclass
class TunnelHarness:
    """Full end-to-end harness: fake-agent WS server + TunnelSession.

    Parameters
    ----------
    agent:
        The ``FakeAgent`` instance to use.  Defaults to a fresh one with
        all default hooks (ACK + echo).
    conn_ack_timeout:
        Passed to ``TunnelConfig``.  Keep short for tests (default 2 s).
    socks_port:
        Local SOCKS5 port.  0 = pick a free port (default).
    ping_interval:
        Keepalive interval in seconds (default 30 — effectively disabled
        for short tests).

    Attributes
    ----------
    socks_addr:
        ``(host, port)`` tuple for SOCKS5 client connections.
    session:
        The live ``TunnelSession``.
    agent:
        The ``FakeAgent`` instance.
    """

    agent: FakeAgent = field(default_factory=FakeAgent)
    conn_ack_timeout: float = 2.0
    socks_port: int = 0
    ping_interval: float = 30.0

    # Set after start()
    socks_addr: tuple[str, int] = field(default=("127.0.0.1", 0), init=False)
    session: TunnelSession = field(init=False)
    _ws_port: int = field(default=0, init=False)
    _server_task: asyncio.Task[None] | None = field(default=None, init=False)
    _session_task: asyncio.Task[None] | None = field(default=None, init=False)
    _ws_server: Any = field(default=None, init=False)

    async def start(self) -> None:
        """Start the fake-agent WS server and the TunnelSession."""
        # Pick a free port for the WS server.
        self._ws_port = _free_port()
        socks_p = self.socks_port if self.socks_port != 0 else _free_port()

        app = AppConfig(
            wss_url=f"ws://127.0.0.1:{self._ws_port}/ws",
            insecure=False,
            bridge=BridgeConfig(
                send_queue_cap=256,
                ping_interval=self.ping_interval,
                reconnect_max_retries=0,
            ),
        )
        tun = get_tunnel_config(
            app,
            socks_host="127.0.0.1",
            socks_port=socks_p,
            conn_ack_timeout=self.conn_ack_timeout,
        )
        self.socks_addr = ("127.0.0.1", socks_p)
        self.session = TunnelSession(app, tun)

        # Start the fake-agent WebSocket server.
        self._ws_server = await serve(
            self.agent,
            "127.0.0.1",
            self._ws_port,
        )

        # Run the tunnel session in a background task.
        self._session_task = asyncio.create_task(
            self.session.run(), name="harness-tunnel-session"
        )

        # Wait until the SOCKS5 server is accepting connections.
        await self._wait_socks_ready(socks_p)

    async def _wait_socks_ready(self, port: int, wait_secs: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + wait_secs
        while asyncio.get_running_loop().time() < deadline:
            try:
                _, w = await asyncio.open_connection("127.0.0.1", port)
                w.close()
                await w.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.05)
        raise RuntimeError(f"SOCKS5 server on port {port} did not start within {wait_secs}s")

    async def stop(self) -> None:
        """Tear down the session and the WS server."""
        if self._session_task and not self._session_task.done():
            self._session_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._session_task, timeout=5.0)
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()

    async def __aenter__(self) -> TunnelHarness:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()


# ── SOCKS5 client helpers ─────────────────────────────────────────────────────


async def socks5_connect(
    socks_addr: tuple[str, int],
    target_host: str,
    target_port: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Perform a SOCKS5 CONNECT handshake and return the open stream."""
    reader, writer = await asyncio.open_connection(*socks_addr)

    # Greeting: VER=5, NMETHODS=1, METHOD=0 (no auth)
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    resp = await reader.readexactly(2)
    assert resp == b"\x05\x00", f"unexpected greeting response: {resp!r}"

    # CONNECT request
    host_bytes = target_host.encode()
    writer.write(
        bytes([0x05, 0x01, 0x00, 0x03, len(host_bytes)])
        + host_bytes
        + struct.pack("!H", target_port)
    )
    await writer.drain()

    # Reply: VER, REP, RSV, ATYP, BND.ADDR, BND.PORT
    header = await reader.readexactly(4)
    assert header[1] == 0x00, f"SOCKS5 CONNECT failed with code {header[1]}"
    atyp = header[3]
    if atyp == 0x01:
        await reader.readexactly(4 + 2)
    elif atyp == 0x04:
        await reader.readexactly(16 + 2)
    elif atyp == 0x03:
        n = (await reader.readexactly(1))[0]
        await reader.readexactly(n + 2)

    return reader, writer


# ── Utility ───────────────────────────────────────────────────────────────────


def _free_port() -> int:
    """Return a free TCP port on loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def echo_tcp_server(
    host: str = "127.0.0.1",
) -> AsyncIterator[tuple[str, int]]:
    """Async context manager that runs a simple TCP echo server.

    Yields ``(host, port)`` for use as the CONNECT target.
    The server echoes every received byte back to the sender.
    """

    async def _handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(_handle, host, 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with server:
            yield host, port
    finally:
        pass
