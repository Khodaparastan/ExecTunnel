"""SOCKS5 server — asyncio, no auth."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator

from exectunnel.config.defaults import HANDSHAKE_TIMEOUT_SECS
from exectunnel.exceptions import (
    ExecTunnelError,
    ProtocolError,
    TransportError,
)
from exectunnel.observability import metrics_inc, metrics_observe, span
from exectunnel.protocol.enums import AuthMethod, Cmd, Reply
from exectunnel.proxy._codec import build_reply, read_addr, read_exact
from exectunnel.proxy.relay import UdpRelay
from exectunnel.proxy.request import Socks5Request

logger = logging.getLogger("exectunnel.proxy")


class Socks5Server:
    """Async SOCKS5 server.  Yields :class:`Socks5Request` objects via ``async for``.

    The server owns the accept loop; each accepted connection is negotiated
    asynchronously.  If negotiation fails (bad version, unsupported auth, …)
    the connection is silently closed and logged at DEBUG level.

    Only one consumer of the async iterator is supported.  Calling
    ``async for`` twice on the same server instance produces undefined
    behaviour.

    Example::

        server = Socks5Server("127.0.0.1", 1080)
        await server.start()
        async for req in server:
            asyncio.create_task(handle(req))
        await server.stop()
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 1080,
        handshake_timeout: float = HANDSHAKE_TIMEOUT_SECS,
    ) -> None:
        self._host: str = host
        self._port: int = port
        self._handshake_timeout: float = handshake_timeout
        self._queue: asyncio.Queue[Socks5Request | None] = asyncio.Queue()
        self._server: asyncio.Server | None = None
        self._stopped: bool = False
        self._started: bool = False
        # Track in-flight handshake tasks so stop() can cancel them.
        self._handshake_tasks: set[asyncio.Task[None]] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind the SOCKS5 listen socket and begin accepting connections.

        Raises:
            RuntimeError:   If :meth:`start` has already been called.
            TransportError: If the OS refuses to bind the listen socket
                            (e.g. port already in use, insufficient permissions).
        """
        if self._started:
            raise RuntimeError(
                "Socks5Server.start() has already been called. "
                "Create a new Socks5Server instance to rebind."
            )
        self._started = True

        try:
            self._server = await asyncio.start_server(
                self._handle_client, self._host, self._port
            )
        except OSError as exc:
            raise TransportError(
                f"SOCKS5 server failed to bind on {self._host}:{self._port}.",
                error_code="transport.socks5_bind_failed",
                details={
                    "host": self._host,
                    "port": self._port,
                    "os_error": str(exc),
                },
                hint=(
                    f"Ensure port {self._port} is not already in use and that "
                    "the process has permission to bind on the requested address. "
                    "Set EXECTUNNEL_SOCKS_PORT to an available port if needed."
                ),
            ) from exc

        logger.info("SOCKS5 listening on %s:%d", self._host, self._port)
        metrics_inc("socks5.server.started")

        if self._host not in ("127.0.0.1", "::1", "localhost"):
            logger.warning(
                "SOCKS5 server bound to non-loopback address %s:%d — "
                "any network-reachable host can use this as an open proxy. "
                "Set EXECTUNNEL_SOCKS_HOST=127.0.0.1 unless you intend public access.",
                self._host,
                self._port,
            )

    async def stop(self) -> None:
        """Close the listen socket, cancel in-flight handshakes, drain the queue.

        Safe to call before :meth:`start` — a no-op in that case.
        """
        if self._stopped:
            return
        self._stopped = True
        metrics_inc("socks5.server.stopped")

        # Stop accepting new connections first so no new _handle_client
        # coroutines are spawned after we cancel existing ones.
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

        # Cancel all in-flight handshake tasks and wait for them to finish
        # so their writers are properly closed before we drain the queue.
        if self._handshake_tasks:
            for task in list(self._handshake_tasks):
                task.cancel()
            await asyncio.gather(*self._handshake_tasks, return_exceptions=True)
            self._handshake_tasks.clear()

        # Signal the async iterator to stop.
        await self._queue.put(None)

        # Drain any requests that were queued but never consumed.
        while not self._queue.empty():
            try:
                req = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(req, Socks5Request):
                await req.close()

    # ── Async context manager ─────────────────────────────────────────────────

    async def __aenter__(self) -> Socks5Server:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    # ── Async iteration ───────────────────────────────────────────────────────

    def __aiter__(self) -> AsyncGenerator[Socks5Request]:
        return self._iter()

    async def _iter(self) -> AsyncGenerator[Socks5Request]:
        """Yield :class:`Socks5Request` objects until :meth:`stop` is called."""
        while True:
            req = await self._queue.get()
            if req is None or self._stopped:
                return
            yield req

    # ── Connection handler ────────────────────────────────────────────────────

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Accept one TCP connection and run the SOCKS5 handshake.

        Registers itself as a tracked task so :meth:`stop` can cancel it.
        All failures are caught here so a single bad client never tears down
        the server.
        """
        task = asyncio.current_task()
        if task is not None:
            self._handshake_tasks.add(task)

        # Capture start time before span setup to exclude tracing overhead.
        start = asyncio.get_running_loop().time()

        with span("socks5.handshake"):
            metrics_inc("socks5.handshake.started")
            try:
                req = await asyncio.wait_for(
                    self._negotiate(reader, writer),
                    timeout=self._handshake_timeout,
                )
                if req is not None:
                    await self._queue.put(req)
                    metrics_inc("socks5.handshake.ok", cmd=req.cmd.name)

            except TimeoutError:
                metrics_inc("socks5.handshake.timeout")
                logger.warning(
                    "socks5 handshake timed out after %.1fs",
                    self._handshake_timeout,
                )
                await _close_writer(writer)

            except ProtocolError as exc:
                metrics_inc(
                    "socks5.handshake.error",
                    error=exc.error_code.replace(".", "_"),
                )
                logger.debug(
                    "socks5 handshake rejected [%s]: %s (error_id=%s)",
                    exc.error_code,
                    exc.message,
                    exc.error_id,
                )
                await _close_writer(writer)

            except TransportError as exc:
                metrics_inc(
                    "socks5.handshake.error",
                    error=exc.error_code.replace(".", "_"),
                )
                logger.warning(
                    "socks5 handshake transport error [%s]: %s (error_id=%s)",
                    exc.error_code,
                    exc.message,
                    exc.error_id,
                )
                await _close_writer(writer)

            except ExecTunnelError as exc:
                metrics_inc(
                    "socks5.handshake.error",
                    error=exc.error_code.replace(".", "_"),
                )
                logger.warning(
                    "socks5 handshake library error [%s]: %s (error_id=%s)",
                    exc.error_code,
                    exc.message,
                    exc.error_id,
                )
                await _close_writer(writer)

            except OSError as exc:
                metrics_inc("socks5.handshake.error", error="os_error")
                logger.debug("socks5 handshake I/O error: %s", exc)
                await _close_writer(writer)

            except asyncio.CancelledError:
                # stop() cancelled this task — close the writer and re-raise
                # so the task exits cleanly.
                await _close_writer(writer)
                raise

            except Exception as exc:
                metrics_inc(
                    "socks5.handshake.error",
                    error=type(exc).__name__,
                )
                logger.warning("socks5 handshake unexpected failure: %s", exc)
                logger.debug("socks5 handshake traceback", exc_info=True)
                await _close_writer(writer)

            finally:
                metrics_observe(
                    "socks5.handshake.duration_sec",
                    asyncio.get_running_loop().time() - start,
                )
                if task is not None:
                    self._handshake_tasks.discard(task)

    # ── SOCKS5 negotiation ────────────────────────────────────────────────────

    @staticmethod
    async def _negotiate(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> Socks5Request | None:
        """Perform the full SOCKS5 handshake and return a :class:`Socks5Request`.

        Returns ``None`` when the command is valid but not supported (e.g.
        ``BIND``) — the appropriate SOCKS5 error reply has already been sent.

        Raises:
            ProtocolError:
                * Non-SOCKS5 greeting (bad version byte).
                * Client does not offer ``NO_AUTH``.
                * Bad request version byte.
                * Non-zero RSV byte.
                * Unsupported ``CMD`` byte.
                * Zero destination port for ``CONNECT``.
                * Address parse errors (propagated from :func:`read_addr`).
            TransportError:
                UDP relay socket cannot be bound during ``UDP_ASSOCIATE``
                (propagated from :meth:`UdpRelay.start`).
        """
        # ── Greeting: VER(1) + NMETHODS(1) + METHODS(N) ──────────────────────
        header = await read_exact(reader, 2)
        if header[0] != 0x05:
            raise ProtocolError(
                f"Not a SOCKS5 client: version byte is {header[0]:#x}, expected 0x05.",
                error_code="protocol.socks5_bad_version",
                details={
                    "received_version": hex(header[0]),
                    "expected_version": "0x05",
                },
                hint=(
                    "Ensure the connecting client is configured to use SOCKS5. "
                    "SOCKS4 and HTTP CONNECT proxies are not supported."
                ),
            )

        nmethods = header[1]
        # Guard against nmethods=0 before calling read_exact.
        if nmethods == 0:
            await _write_and_drain_suppress(writer, bytes([0x05, AuthMethod.NO_ACCEPT]))
            raise ProtocolError(
                "SOCKS5 greeting lists zero authentication methods.",
                error_code="protocol.socks5_no_methods",
                details={"nmethods": nmethods},
                hint="The SOCKS5 client sent a greeting with no authentication methods.",
            )

        methods = await read_exact(reader, nmethods)

        # Check for NO_AUTH using integer membership on bytes (IntEnum-safe).
        if int(AuthMethod.NO_AUTH) not in methods:
            await _write_and_drain_suppress(
                writer, bytes([0x05, int(AuthMethod.NO_ACCEPT)])
            )
            raise ProtocolError(
                "SOCKS5 client does not support NO_AUTH (method 0x00).",
                error_code="protocol.socks5_no_auth_not_offered",
                details={
                    "offered_methods": list(methods),
                    "required_method": int(AuthMethod.NO_AUTH),
                },
                hint=(
                    "Configure the SOCKS5 client to use no-authentication mode. "
                    "Username/password and GSSAPI authentication are not supported."
                ),
            )

        await _write_and_drain_suppress(writer, bytes([0x05, int(AuthMethod.NO_AUTH)]))

        # ── Request: VER(1) + CMD(1) + RSV(1) + ATYP+addr+port ──────────────
        req_header = await read_exact(reader, 3)
        if req_header[0] != 0x05:
            raise ProtocolError(
                f"Bad SOCKS5 request version: {req_header[0]:#x}, expected 0x05.",
                error_code="protocol.socks5_bad_request_version",
                details={
                    "received_version": hex(req_header[0]),
                    "expected_version": "0x05",
                },
                hint="The SOCKS5 client sent a malformed request header.",
            )

        if req_header[2] != 0x00:
            await _write_and_drain_suppress(writer, build_reply(Reply.GENERAL_FAILURE))
            raise ProtocolError(
                f"SOCKS5 request RSV byte is {req_header[2]:#x}, expected 0x00.",
                error_code="protocol.socks5_bad_rsv",
                details={"received_rsv": hex(req_header[2])},
                hint="The SOCKS5 client sent a non-zero RSV byte, violating RFC 1928 §4.",
            )

        try:
            cmd = Cmd(req_header[1])
        except ValueError:
            await _write_and_drain_suppress(
                writer, build_reply(Reply.CMD_NOT_SUPPORTED)
            )
            raise ProtocolError(
                f"Unsupported SOCKS5 command: {req_header[1]:#x}.",
                error_code="protocol.socks5_unsupported_cmd",
                details={"received_cmd": hex(req_header[1])},
                hint=(
                    "Only CONNECT (0x01) and UDP_ASSOCIATE (0x03) are supported. "
                    "BIND (0x02) is not implemented."
                ),
            )

        # read_addr validates port != 0 internally; raises ProtocolError on
        # any malformed address field.
        host, port = await read_addr(reader)

        if cmd == Cmd.CONNECT:
            return Socks5Request(
                cmd=cmd,
                host=host,
                port=port,
                reader=reader,
                writer=writer,
            )

        if cmd == Cmd.UDP_ASSOCIATE:
            # Pass the client's declared address as a hint for source filtering.
            # RFC 1928 §7: the host/port may be 0.0.0.0:0 if the client does
            # not know its sending address; UdpRelay.start() handles that case.
            relay = UdpRelay()
            relay_port = await relay.start(
                expected_client_addr=(host, port) if port != 0 else None
            )
            return Socks5Request(
                cmd=cmd,
                host=host,
                port=port,
                reader=reader,
                writer=writer,
                udp_relay=relay,
            )

        # BIND and any future unknown commands.
        await _write_and_drain_suppress(writer, build_reply(Reply.CMD_NOT_SUPPORTED))
        return None


# ── Module-level helpers ──────────────────────────────────────────────────────


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    """Close *writer*, suppressing ``OSError`` and ``RuntimeError``.

    Centralised so the identical close pattern is not repeated in every
    ``except`` branch of :meth:`Socks5Server._handle_client`.

    Args:
        writer: The asyncio stream writer to close.
    """
    with contextlib.suppress(OSError, RuntimeError):
        writer.close()
        await writer.wait_closed()


async def _write_and_drain_suppress(
    writer: asyncio.StreamWriter,
    data: bytes,
) -> None:
    """Write *data* and drain, suppressing ``OSError`` and ``RuntimeError``.

    Used for best-effort error-reply writes inside ``_negotiate`` where the
    connection may already be half-closed.

    Args:
        writer: The asyncio stream writer to write to.
        data:   Raw bytes to write.
    """
    with contextlib.suppress(OSError, RuntimeError):
        writer.write(data)
    with contextlib.suppress(OSError, RuntimeError):
        await writer.drain()
