"""SOCKS5 server — async accept loop (RFC 1928, no-auth only).

:class:`Socks5Server` binds a TCP listen socket, negotiates the SOCKS5
handshake for each accepted connection, and yields completed
:class:`~exectunnel.proxy.request.Socks5Request` objects via ``async for``.

Supported commands
------------------
* ``CONNECT``       (0x01) — TCP tunnel.
* ``UDP_ASSOCIATE`` (0x03) — UDP relay via :class:`~exectunnel.proxy.udp_relay.UdpRelay`.

Unsupported commands
--------------------
* ``BIND`` (0x02) — responds ``CMD_NOT_SUPPORTED`` and closes the connection.

Authentication
--------------
Only ``NO_AUTH`` (0x00) is supported.  Clients that do not offer ``NO_AUTH``
receive ``NO_ACCEPT`` (0xFF) and are disconnected.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from exectunnel.exceptions import (
    ExecTunnelError,
    ProtocolError,
    TransportError,
)
from exectunnel.protocol import AuthMethod, Cmd, Reply
from exectunnel.proxy._io import (
    close_writer,
    read_exact,
    read_socks5_addr,
    write_and_drain_silent,
)
from exectunnel.proxy._wire import build_socks5_reply
from exectunnel.proxy.config import Socks5ServerConfig
from exectunnel.proxy.request import Socks5Request
from exectunnel.proxy.udp_relay import UdpRelay

__all__: list[str] = ["Socks5Server"]

logger = logging.getLogger(__name__)


class Socks5Server:
    """Async SOCKS5 server.  Yields :class:`~exectunnel.proxy.request.Socks5Request`
    objects via ``async for``.

    The server owns the accept loop; each accepted connection is negotiated
    asynchronously.  If negotiation fails (bad version, unsupported auth, …)
    the connection is closed and logged at DEBUG level — a single bad client
    never tears down the server.

    Only one consumer of the async iterator is supported at a time.

    Example — fire-and-forget handler::

        cfg = Socks5ServerConfig(host="127.0.0.1", port=1080)
        server = Socks5Server(cfg)
        await server.start()
        async for req in server:
            asyncio.create_task(handle(req))
        await server.stop()

    Example — async context manager::

        async with Socks5Server(Socks5ServerConfig()) as server:
            async for req in server:
                asyncio.create_task(handle(req))

    Args:
        config: :class:`~exectunnel.proxy.config.Socks5ServerConfig` instance.
                All bind parameters and timeouts are taken from here.
                Defaults to ``Socks5ServerConfig()`` (127.0.0.1:1080).
    """

    def __init__(self, config: Socks5ServerConfig | None = None) -> None:
        self._config = config if config is not None else Socks5ServerConfig()

        self._queue: asyncio.Queue[Socks5Request | None] = asyncio.Queue(
            self._config.queue_capacity
        )
        self._server: asyncio.Server | None = None
        self._started: bool = False
        self._stopped: bool = False

        # In-flight handshake tasks — tracked so stop() can cancel them.
        self._handshake_tasks: set[asyncio.Task[None]] = set()

    # ── Lifecycle ────────────────────────────────────────────────────────────

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

        Safe to call before :meth:`start` — a no-op in that case.
        Idempotent — subsequent calls are no-ops.

        Shutdown order:
        1. Stop accepting new connections.
        2. Cancel and await all in-flight handshake tasks (so their writers
           are closed before the queue is drained).
        3. Enqueue the ``None`` sentinel to stop the async iterator.
        4. Drain and close any requests that were queued but never consumed.

        Note on the ``_stopped`` / sentinel race: ``_stopped`` is set at the
        top of this method.  The async iterator checks ``_stopped`` after
        dequeuing each item, so a real request dequeued just as ``_stopped``
        becomes ``True`` will be returned to the consumer but the iterator
        will exit on the *next* iteration.  Requests that were never dequeued
        are cleaned up by the drain loop below.
        """
        if self._stopped:
            return
        self._stopped = True

        # 1. Stop accepting new connections before cancelling existing handshakes.
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

        # 2. Cancel all in-flight handshake tasks and wait for them to finish so
        #    their writers are properly closed before we drain the queue.
        if self._handshake_tasks:
            for task in list(self._handshake_tasks):
                task.cancel()
            await asyncio.gather(*self._handshake_tasks, return_exceptions=True)
            self._handshake_tasks.clear()

        # 3. Signal the async iterator to stop.
        await self._queue.put(None)

        # 4. Drain any requests that were queued but never consumed.
        while True:
            try:
                req = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(req, Socks5Request):
                await req.close()

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
        """Yield :class:`~exectunnel.proxy.request.Socks5Request` objects until
        :meth:`stop` is called."""
        while True:
            req = await self._queue.get()
            if req is None or self._stopped:
                return
            yield req

    # ── Connection handler ───────────────────────────────────────────────────

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

        try:
            async with asyncio.timeout(self._config.handshake_timeout):
                req = await self._negotiate(reader, writer)

            if req is not None:
                await self._queue.put(req)
                logger.debug(
                    "socks5 handshake ok: cmd=%s host=%s port=%d",
                    req.cmd.name,
                    req.host,
                    req.port,
                )

        except TimeoutError:
            logger.warning(
                "socks5 handshake timed out after %.1fs",
                self._config.handshake_timeout,
            )
            await close_writer(writer)

        except ProtocolError as exc:
            logger.debug(
                "socks5 handshake rejected [%s]: %s (error_id=%s)",
                exc.error_code,
                exc.message,
                exc.error_id,
            )
            await close_writer(writer)

        except TransportError as exc:
            logger.warning(
                "socks5 handshake transport error [%s]: %s (error_id=%s)",
                exc.error_code,
                exc.message,
                exc.error_id,
            )
            await close_writer(writer)

        except ExecTunnelError as exc:
            logger.warning(
                "socks5 handshake library error [%s]: %s (error_id=%s)",
                exc.error_code,
                exc.message,
                exc.error_id,
            )
            await close_writer(writer)

        except OSError as exc:
            logger.debug("socks5 handshake I/O error: %s", exc)
            await close_writer(writer)

        except asyncio.CancelledError:
            # stop() cancelled this task — close the writer and re-raise so
            # the task exits cleanly.
            await close_writer(writer)
            raise

        except Exception:
            logger.exception("socks5 handshake unexpected failure")
            await close_writer(writer)

        finally:
            if task is not None:
                self._handshake_tasks.discard(task)

    # ── SOCKS5 negotiation ───────────────────────────────────────────────────

    async def _negotiate(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> Socks5Request | None:
        """Perform the full SOCKS5 handshake and return a
        :class:`~exectunnel.proxy.request.Socks5Request`.

        Returns ``None`` when the command is ``BIND`` — the appropriate
        SOCKS5 error reply has already been sent to the client.

        For ``UDP_ASSOCIATE``, the relay port is stored on
        ``Socks5Request.udp_relay.local_port``.  Callers **must** pass it as
        ``bind_port`` to
        :meth:`~exectunnel.proxy.request.Socks5Request.send_reply_success`
        so the SOCKS5 client knows which port to send datagrams to
        (RFC 1928 §7).

        If ``UdpRelay.start()`` raises, the relay is closed before the
        exception propagates so no socket is leaked.

        Raises:
            ProtocolError:
                * Non-SOCKS5 greeting (bad version byte).
                * Client offers zero authentication methods.
                * Client does not offer ``NO_AUTH``.
                * Bad request version byte.
                * Non-zero RSV byte.
                * Unknown ``CMD`` byte.
                * Address parse errors (propagated from
                  :func:`~exectunnel.proxy._io.read_socks5_addr`).
            TransportError:
                UDP relay socket cannot be bound during ``UDP_ASSOCIATE``
                (propagated from
                :meth:`~exectunnel.proxy.udp_relay.UdpRelay.start`).
        """
        # ── Greeting: VER(1) + NMETHODS(1) + METHODS(N) ─────────────────────
        header = await read_exact(reader, 2)
        if header[0] != 0x05:
            raise ProtocolError(
                f"Not a SOCKS5 client: version byte is {header[0]:#x}, expected 0x05.",
                details={
                    "socks5_field": "VER",
                    "expected": "version byte 0x05",
                },
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
                details={
                    "socks5_field": "VER",
                    "expected": "version byte 0x05",
                },
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
                hint=(
                    "The SOCKS5 client sent a non-zero RSV byte, "
                    "violating RFC 1928 §4."
                ),
            )

        try:
            cmd = Cmd(req_header[1])
        except ValueError:
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
            )

        host, port = await read_socks5_addr(reader)

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
                # RFC 1928 §7: the host/port in the request is the client's
                # declared sending address.  It may be 0.0.0.0:0 if the client
                # does not know its sending address; UdpRelay.start() handles
                # the unspecified-address case internally.
                relay = UdpRelay(
                    queue_capacity=self._config.queue_capacity,
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
                # Known but explicitly unsupported.
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
                # Exhaustive guard — unreachable given current Cmd enum coverage,
                # but protects against future enum additions that miss a branch.
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
