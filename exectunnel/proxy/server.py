"""SOCKS5 server — asyncio, no auth."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from exectunnel.config.defaults import HANDSHAKE_TIMEOUT_SECS
from exectunnel.exceptions import (
    ExecTunnelError,
    FrameDecodingError,
    ProtocolError,
    TransportError,
)
from exectunnel.observability import metrics_inc, metrics_observe, span
from exectunnel.protocol.enums import AuthMethod, Cmd, Reply
from exectunnel.proxy._codec import _build_reply, _read_addr, _read_exact
from exectunnel.proxy.relay import UdpRelay
from exectunnel.proxy.request import Socks5Request

logger = logging.getLogger("exectunnel.proxy")


class Socks5Server:
    """
    Async SOCKS5 server.  Yields :class:`Socks5Request` objects via
    ``async for``.

    The server owns the accept loop; each accepted connection is negotiated
    asynchronously.  If negotiation fails (bad version, unsupported auth, …)
    the connection is silently closed and logged at DEBUG level.

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
        self._host = host
        self._port = port
        self._handshake_timeout = handshake_timeout
        self._queue: asyncio.Queue[Socks5Request | None] = asyncio.Queue()
        self._server: asyncio.Server | None = None
        self._stopped = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind the SOCKS5 listen socket and begin accepting connections.

        Raises
        ------
        TransportError
            If the OS refuses to bind the listen socket (e.g. port already in
            use, insufficient permissions).
        """
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
        """Close the listen socket, drain unconsumed requests, and signal the iterator."""
        self._stopped = True
        metrics_inc("socks5.server.stopped")
        # Close the server socket first so no new connections are accepted
        # before we enqueue the sentinel — prevents late _handle_client
        # completions from enqueuing requests after the sentinel.
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        # Unblock any task waiting on _queue.get() so it can observe _stopped.
        await self._queue.put(None)
        # Drain any requests that were queued but never consumed.
        while not self._queue.empty():
            try:
                req = self._queue.get_nowait()
                if not isinstance(req, Socks5Request):
                    continue
                if req.udp_relay is not None:
                    req.udp_relay.close()
                with contextlib.suppress(OSError):
                    req.writer.close()
                with contextlib.suppress(OSError):
                    await req.writer.wait_closed()
            except asyncio.QueueEmpty:
                break

    # ── Async iteration ───────────────────────────────────────────────────────

    def __aiter__(self) -> AsyncIterator[Socks5Request]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Socks5Request]:
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

        All failures are caught here and converted to metrics + log entries so
        that a single bad client never tears down the server.  The exception
        hierarchy drives distinct handling per failure domain:

        * :class:`ProtocolError`     – client sent a malformed or unsupported
          SOCKS5 message; log at DEBUG (very common, not actionable).
        * :class:`FrameDecodingError`– stream truncated or address bytes
          corrupt; log at DEBUG (subset of protocol errors).
        * :class:`TransportError`    – UDP relay bind failed during
          ``UDP_ASSOCIATE``; log at WARNING (operator-actionable).
        * :class:`ExecTunnelError`   – any other library error; log at WARNING.
        * ``asyncio.TimeoutError``   – handshake exceeded the configured limit.
        * ``OSError``                – socket I/O error during handshake.
        * ``Exception``              – truly unexpected; log at WARNING with traceback.
        """
        with span("socks5.handshake"):
            start = asyncio.get_running_loop().time()
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
                with contextlib.suppress(OSError):
                    writer.close()
                    await writer.wait_closed()

            except FrameDecodingError as exc:
                # Stream truncated or address bytes corrupt — very common when
                # clients disconnect mid-handshake; log at DEBUG.
                metrics_inc(
                    "socks5.handshake.error",
                    error=exc.error_code.replace(".", "_"),
                )
                logger.debug(
                    "socks5 handshake frame error [%s]: %s (error_id=%s)",
                    exc.error_code,
                    exc.message,
                    exc.error_id,
                )
                with contextlib.suppress(OSError):
                    writer.close()
                    await writer.wait_closed()

            except ProtocolError as exc:
                # Client sent a malformed or unsupported SOCKS5 message.
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
                with contextlib.suppress(OSError):
                    writer.close()
                    await writer.wait_closed()

            except TransportError as exc:
                # UDP relay bind failed — operator-actionable; log at WARNING.
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
                with contextlib.suppress(OSError):
                    writer.close()
                    await writer.wait_closed()

            except ExecTunnelError as exc:
                # Catch-all for any other library error.
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
                with contextlib.suppress(OSError):
                    writer.close()
                    await writer.wait_closed()

            except OSError as exc:
                metrics_inc("socks5.handshake.error", error="OSError")
                logger.debug("socks5 handshake I/O error: %s", exc)
                with contextlib.suppress(OSError):
                    writer.close()
                    await writer.wait_closed()

            except Exception as exc:
                metrics_inc("socks5.handshake.error", error=exc.__class__.__name__)
                logger.warning("socks5 handshake unexpected failure: %s", exc)
                logger.debug("socks5 handshake traceback", exc_info=True)
                with contextlib.suppress(OSError):
                    writer.close()
                    await writer.wait_closed()

            finally:
                metrics_observe(
                    "socks5.handshake.duration_sec",
                    asyncio.get_running_loop().time() - start,
                )

    # ── SOCKS5 negotiation ────────────────────────────────────────────────────

    @staticmethod
    async def _negotiate(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> Socks5Request | None:
        """Perform the full SOCKS5 handshake and return a :class:`Socks5Request`.

        Returns ``None`` when the command is valid but not supported (e.g.
        ``BIND``) — the appropriate SOCKS5 error reply has already been sent.

        Raises
        ------
        ProtocolError
            If the client sends a non-SOCKS5 greeting, a bad request version,
            an unsupported ``CMD``, or a zero port for ``CONNECT``.
        FrameDecodingError
            If the stream is truncated or the address bytes are corrupt
            (propagated from :func:`_read_exact` and :func:`_read_addr`).
        TransportError
            If the UDP relay socket cannot be bound during ``UDP_ASSOCIATE``
            (propagated from :meth:`UdpRelay.start`).
        """
        # ── Greeting: VER(1) + NMETHODS(1) + METHODS(N) ──────────────────────
        header = await _read_exact(reader, 2)
        if header[0] != 0x05:
            raise ProtocolError(
                f"Not a SOCKS5 client: version byte is {header[0]:#x}, expected 0x05.",
                error_code="protocol.socks5_bad_version",
                details={"received_version": hex(header[0]), "expected_version": "0x05"},
                hint=(
                    "Ensure the connecting client is configured to use SOCKS5. "
                    "SOCKS4 and HTTP CONNECT proxies are not supported."
                ),
            )

        nmethods = header[1]
        methods = await _read_exact(reader, nmethods)

        if AuthMethod.NO_AUTH not in methods:
            writer.write(bytes([0x05, AuthMethod.NO_ACCEPT]))
            with contextlib.suppress(OSError):
                await writer.drain()
            with contextlib.suppress(OSError):
                writer.close()
                await writer.wait_closed()
            raise ProtocolError(
                "SOCKS5 client does not support NO_AUTH (method 0x00).",
                error_code="protocol.socks5_no_auth_not_offered",
                details={
                    "offered_methods": list(methods),
                    "required_method": AuthMethod.NO_AUTH,
                },
                hint=(
                    "Configure the SOCKS5 client to use no-authentication mode. "
                    "Username/password and GSSAPI authentication are not supported."
                ),
            )

        writer.write(bytes([0x05, AuthMethod.NO_AUTH]))
        await writer.drain()

        # ── Request: VER(1) + CMD(1) + RSV(1) + ATYP+addr+port ──────────────
        req_header = await _read_exact(reader, 3)
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
            # RSV byte must be 0x00 per RFC 1928 §4.
            await _write_and_drain_suppress(writer, _build_reply(Reply.GENERAL_FAILURE))
            raise ProtocolError(
                f"SOCKS5 request RSV byte is {req_header[2]:#x}, expected 0x00.",
                error_code="protocol.socks5_bad_rsv",
                details={"received_rsv": hex(req_header[2])},
                hint="The SOCKS5 client sent a non-zero RSV byte, violating RFC 1928 §4.",
            )

        try:
            cmd = Cmd(req_header[1])
        except ValueError:
            await _write_and_drain_suppress(writer, _build_reply(Reply.CMD_NOT_SUPPORTED))
            raise ProtocolError(
                f"Unsupported SOCKS5 command: {req_header[1]:#x}.",
                error_code="protocol.socks5_unsupported_cmd",
                details={"received_cmd": hex(req_header[1])},
                hint=(
                    "Only CONNECT (0x01) and UDP_ASSOCIATE (0x03) are supported. "
                    "BIND (0x02) is not implemented."
                ),
            )

        # _read_addr raises ProtocolError / FrameDecodingError on bad input.
        host, port = await _read_addr(reader)

        if cmd == Cmd.CONNECT and port == 0:
            await _write_and_drain_suppress(writer, _build_reply(Reply.GENERAL_FAILURE))
            raise ProtocolError(
                f"SOCKS5 CONNECT request for {host!r} has port 0.",
                error_code="protocol.socks5_connect_zero_port",
                details={"host": host, "port": port},
                hint="Port 0 is not a valid destination for a CONNECT request.",
            )

        if cmd == Cmd.CONNECT:
            return Socks5Request(
                cmd=cmd, host=host, port=port, reader=reader, writer=writer
            )

        if cmd == Cmd.UDP_ASSOCIATE:
            # TransportError propagates to _handle_client if relay bind fails.
            relay = UdpRelay()
            await relay.start()
            return Socks5Request(
                cmd=cmd,
                host=host,
                port=port,
                reader=reader,
                writer=writer,
                udp_relay=relay,
            )

        # BIND and any future commands — send CMD_NOT_SUPPORTED and return None
        # so _handle_client does not enqueue a request it cannot route.
        await _write_and_drain_suppress(writer, _build_reply(Reply.CMD_NOT_SUPPORTED))
        return None


# ── Module-level helpers ──────────────────────────────────────────────────────


def _write_and_suppress(writer: asyncio.StreamWriter, data: bytes) -> None:
    """Write *data* to *writer*, suppressing any ``OSError``.

    Used for best-effort error-reply writes inside ``_negotiate`` where the
    connection may already be half-closed.  ``drain()`` is called to flush the
    kernel write buffer so the SOCKS5 error reply reaches the client before the
    socket is closed by the caller.  Both the write and drain are suppressed on
    ``OSError`` because the connection may already be half-closed.
    """
    with contextlib.suppress(OSError):
        writer.write(data)


async def _write_and_drain_suppress(writer: asyncio.StreamWriter, data: bytes) -> None:
    """Write *data* and drain, suppressing ``OSError`` on either operation.

    Async variant of :func:`_write_and_suppress` that also flushes the kernel
    write buffer so the SOCKS5 error reply actually reaches the client before
    the caller closes the socket.
    """
    with contextlib.suppress(OSError):
        writer.write(data)
    with contextlib.suppress(OSError):
        await writer.drain()
