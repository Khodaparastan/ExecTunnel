"""SOCKS5 server — async accept loop (RFC 1928, no-auth only).

:class:`Socks5Server` binds a TCP listen socket, negotiates the SOCKS5
handshake for each accepted connection, and yields completed
:class:`~exectunnel.proxy.request.Socks5Request` objects via ``async for``.

Supported commands: ``CONNECT`` (0x01), ``UDP_ASSOCIATE`` (0x03).
Unsupported: ``BIND`` (0x02) — responds ``CMD_NOT_SUPPORTED``.
Authentication: ``NO_AUTH`` (0x00) only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from exectunnel.exceptions import (
    ExecTunnelError,
    ProtocolError,
    TransportError,
)
from exectunnel.protocol import AuthMethod, Cmd, Reply

from ._io import (
    close_writer,
    read_exact,
    read_socks5_addr,
    write_and_drain_silent,
)
from ._wire import build_socks5_reply
from .config import Socks5ServerConfig
from .request import Socks5Request
from .udp_relay import UdpRelay

__all__: list[str] = ["Socks5Server"]

logger = logging.getLogger(__name__)


class Socks5Server:
    """Async SOCKS5 server yielding :class:`Socks5Request` via ``async for``.

    Example::

        async with Socks5Server(Socks5ServerConfig()) as server:
            async for req in server:
                asyncio.create_task(handle(req))

    Args:
        config: Server configuration.  Defaults to ``Socks5ServerConfig()``.
    """

    def __init__(self, config: Socks5ServerConfig | None = None) -> None:
        self._config = config if config is not None else Socks5ServerConfig()

        self._queue: asyncio.Queue[Socks5Request | None] = asyncio.Queue(
            self._config.request_queue_capacity
        )
        self._server: asyncio.Server | None = None
        self._started: bool = False
        self._stopped: bool = False

        self._handshake_tasks: set[asyncio.Task[None]] = set()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind the SOCKS5 listen socket and begin accepting connections.

        Raises:
            RuntimeError:   If already started.
            TransportError: If bind fails.
        """
        if self._started:
            raise RuntimeError(
                "Socks5Server.start() has already been called. "
                "Create a new Socks5Server instance to rebind."
            )
        self._started = True

        cfg = self._config
        try:
            self._server = await asyncio.start_server(
                self._handle_client,
                cfg.host,
                cfg.port,
            )
        except OSError as exc:
            raise TransportError(
                f"SOCKS5 server failed to bind on {cfg.host}:{cfg.port}.",
                details={
                    "host": cfg.host,
                    "port": cfg.port,
                    "url": cfg.url,
                },
                hint=(
                    f"Ensure port {cfg.port} is not already in use and that "
                    "the process has permission to bind on the requested address."
                ),
            ) from exc

        logger.info("SOCKS5 listening on %s:%d", cfg.host, cfg.port)

        if not cfg.is_loopback:
            logger.warning(
                "SOCKS5 server bound to non-loopback address %s:%d — "
                "any network-reachable host can use this as an open proxy. "
                "Bind to 127.0.0.1 unless public access is intentional.",
                cfg.host,
                cfg.port,
            )

    async def stop(self) -> None:
        """Close the listen socket, cancel in-flight handshakes, drain the queue.

        Idempotent.  Safe to call before :meth:`start`.
        """
        if self._stopped:
            return
        self._stopped = True

        # 1. Stop accepting new connections.
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

        # 2. Cancel all in-flight handshake tasks.
        if self._handshake_tasks:
            for task in list(self._handshake_tasks):
                task.cancel()
            await asyncio.gather(*self._handshake_tasks, return_exceptions=True)
            self._handshake_tasks.clear()

        # 3. Drain queued-but-unconsumed requests BEFORE the sentinel so the
        #    sentinel is never accidentally consumed by the drain loop.
        while True:
            try:
                req = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(req, Socks5Request):
                with contextlib.suppress(Exception):
                    await req.close()

        # 4. Signal the async iterator to stop.
        await self._queue.put(None)

    # ── Async context manager ────────────────────────────────────────────────

    async def __aenter__(self) -> Socks5Server:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    # ── Async iteration ──────────────────────────────────────────────────────

    def __aiter__(self) -> AsyncIterator[Socks5Request]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Socks5Request]:
        """Yield requests until :meth:`stop` enqueues the ``None`` sentinel."""
        while True:
            req = await self._queue.get()
            if req is None:
                return
            yield req

    # ── Connection handler ───────────────────────────────────────────────────

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Accept one TCP connection and run the SOCKS5 handshake.

        All failures are caught so a single bad client never tears down
        the server.
        """
        task = asyncio.current_task()
        if task is not None:
            self._handshake_tasks.add(task)

        try:
            await self._do_handshake(reader, writer)
        except asyncio.CancelledError:
            await close_writer(writer)
            raise
        except Exception:
            # Already logged inside _do_handshake; just ensure cleanup.
            await close_writer(writer)
        finally:
            if task is not None:
                self._handshake_tasks.discard(task)

    async def _do_handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Inner handshake logic, separated for cleaner exception handling."""
        try:
            async with asyncio.timeout(self._config.handshake_timeout):
                req = await self._negotiate(reader, writer)
        except TimeoutError:
            logger.warning(
                "socks5 handshake timed out after %.1fs",
                self._config.handshake_timeout,
            )
            await close_writer(writer)
            return
        except ProtocolError as exc:
            logger.debug(
                "socks5 handshake rejected [%s]: %s (error_id=%s)",
                exc.error_code,
                exc.message,
                exc.error_id,
            )
            await close_writer(writer)
            return
        except TransportError as exc:
            logger.warning(
                "socks5 handshake transport error [%s]: %s (error_id=%s)",
                exc.error_code,
                exc.message,
                exc.error_id,
            )
            await close_writer(writer)
            return
        except ExecTunnelError as exc:
            logger.warning(
                "socks5 handshake library error [%s]: %s (error_id=%s)",
                exc.error_code,
                exc.message,
                exc.error_id,
            )
            await close_writer(writer)
            return
        except OSError as exc:
            logger.debug("socks5 handshake I/O error: %s", exc)
            await close_writer(writer)
            return

        if req is None:
            await close_writer(writer)
            return

        try:
            await asyncio.wait_for(
                self._queue.put(req),
                timeout=self._config.queue_put_timeout,
            )
        except TimeoutError:
            logger.warning(
                "socks5 queue full — dropping handshake for %s:%d "
                "(queue_put_timeout=%.1fs)",
                req.host,
                req.port,
                self._config.queue_put_timeout,
            )
            with contextlib.suppress(Exception):
                await req.send_reply_error(Reply.GENERAL_FAILURE)
            return

        logger.debug(
            "socks5 handshake ok: cmd=%s host=%s port=%d",
            req.cmd.name,
            req.host,
            req.port,
        )

    # ── SOCKS5 negotiation ───────────────────────────────────────────────────

    async def _negotiate(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> Socks5Request | None:
        """Perform the full SOCKS5 handshake.

        Returns ``None`` for ``BIND`` (error reply already sent).

        Raises:
            ProtocolError:  Bad greeting/request.
            TransportError: UDP relay bind failure.
        """
        # ── Greeting: VER(1) + NMETHODS(1) + METHODS(N) ─────────────────────
        header = await read_exact(reader, 2)
        if header[0] != 0x05:
            raise ProtocolError(
                f"Not a SOCKS5 client: version byte is {header[0]:#x}, expected 0x05.",
                details={"socks5_field": "VER", "expected": "version byte 0x05"},
                hint=(
                    "Ensure the connecting client is configured to use SOCKS5. "
                    "SOCKS4 and HTTP CONNECT proxies are not supported."
                ),
            )

        nmethods = header[1]
        if nmethods == 0:
            await write_and_drain_silent(
                writer, bytes([0x05, int(AuthMethod.NO_ACCEPT)])
            )
            raise ProtocolError(
                "SOCKS5 greeting lists zero authentication methods.",
                details={
                    "socks5_field": "NMETHODS",
                    "expected": "at least one authentication method",
                },
                hint="The SOCKS5 client sent a greeting with no authentication methods.",
            )

        methods = await read_exact(reader, nmethods)

        if int(AuthMethod.NO_AUTH) not in methods:
            await write_and_drain_silent(
                writer, bytes([0x05, int(AuthMethod.NO_ACCEPT)])
            )
            raise ProtocolError(
                "SOCKS5 client does not offer NO_AUTH (method 0x00).",
                details={
                    "socks5_field": "METHODS",
                    "expected": (
                        f"method 0x{int(AuthMethod.NO_AUTH):02x} (NO_AUTH) "
                        "in offered set"
                    ),
                },
                hint=(
                    "Configure the SOCKS5 client to use no-authentication mode. "
                    "Username/password and GSSAPI authentication are not supported."
                ),
            )

        await write_and_drain_silent(writer, bytes([0x05, int(AuthMethod.NO_AUTH)]))

        # ── Request: VER(1) + CMD(1) + RSV(1) + ATYP+addr+port ─────────────
        req_header = await read_exact(reader, 3)

        if req_header[0] != 0x05:
            raise ProtocolError(
                f"Bad SOCKS5 request version: {req_header[0]:#x}, expected 0x05.",
                details={"socks5_field": "VER", "expected": "version byte 0x05"},
                hint="The SOCKS5 client sent a malformed request header.",
            )

        if req_header[2] != 0x00:
            await write_and_drain_silent(
                writer, build_socks5_reply(Reply.GENERAL_FAILURE)
            )
            raise ProtocolError(
                f"SOCKS5 request RSV byte is {req_header[2]:#x}, expected 0x00.",
                details={
                    "socks5_field": "RSV",
                    "expected": "RSV byte 0x00 (RFC 1928 §4)",
                },
                hint="The SOCKS5 client sent a non-zero RSV byte, violating RFC 1928 §4.",
            )

        try:
            cmd = Cmd(req_header[1])
        except ValueError as exc:
            await write_and_drain_silent(
                writer, build_socks5_reply(Reply.CMD_NOT_SUPPORTED)
            )
            raise ProtocolError(
                f"Unsupported SOCKS5 command: {req_header[1]:#x}.",
                details={
                    "socks5_field": "CMD",
                    "expected": "CMD 0x01 (CONNECT) or 0x03 (UDP_ASSOCIATE)",
                },
                hint=(
                    "Only CONNECT (0x01) and UDP_ASSOCIATE (0x03) are supported. "
                    "BIND (0x02) is not implemented."
                ),
            ) from exc

        allow_port_zero = cmd == Cmd.UDP_ASSOCIATE
        host, port = await read_socks5_addr(reader, allow_port_zero=allow_port_zero)

        match cmd:
            case Cmd.CONNECT:
                return Socks5Request(
                    cmd=cmd,
                    host=host,
                    port=port,
                    reader=reader,
                    writer=writer,
                )

            case Cmd.UDP_ASSOCIATE:
                relay = UdpRelay(
                    queue_capacity=self._config.udp_relay_queue_capacity,
                    drop_warn_interval=self._config.udp_drop_warn_interval,
                )
                try:
                    await relay.start(
                        expected_client_addr=(host, port) if port != 0 else None
                    )
                except Exception:
                    relay.close()
                    raise
                return Socks5Request(
                    cmd=cmd,
                    host=host,
                    port=port,
                    reader=reader,
                    writer=writer,
                    udp_relay=relay,
                )

            case Cmd.BIND:
                await write_and_drain_silent(
                    writer, build_socks5_reply(Reply.CMD_NOT_SUPPORTED)
                )
                logger.debug(
                    "socks5 BIND command rejected (not implemented) for %s:%d",
                    host,
                    port,
                )
                return None

            case _:
                await write_and_drain_silent(
                    writer, build_socks5_reply(Reply.CMD_NOT_SUPPORTED)
                )
                raise ProtocolError(
                    f"Unhandled SOCKS5 command: {cmd!r}.",
                    details={
                        "socks5_field": "CMD",
                        "expected": "handled Cmd enum member",
                    },
                    hint=(
                        "This is an exectunnel bug — a new Cmd enum value was added "
                        "without a corresponding handler in _negotiate()."
                    ),
                )
