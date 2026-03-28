"""SOCKS5 server — asyncio, no auth."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from exectunnel.config.defaults import HANDSHAKE_TIMEOUT_SECS
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

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._port
        )
        logger.info("SOCKS5 listening on %s:%d", self._host, self._port)
        metrics_inc("socks5.server.started")

    async def stop(self) -> None:
        self._stopped = True
        metrics_inc("socks5.server.stopped")
        # Unblock any task waiting on _queue.get() so it can observe _stopped.
        await self._queue.put(None)
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        # Drain any requests that were queued but never consumed.
        while not self._queue.empty():
            try:
                req = self._queue.get_nowait()
                if req is None:
                    continue
                writer = getattr(req, "writer", None)
                if writer is None:
                    continue
                with contextlib.suppress(OSError, AttributeError):
                    writer.close()
                with contextlib.suppress(OSError, AttributeError):
                    await writer.wait_closed()
            except asyncio.QueueEmpty:
                break

    def __aiter__(self) -> AsyncIterator[Socks5Request]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Socks5Request]:
        while True:
            req = await self._queue.get()
            if req is None or self._stopped:
                return
            yield req

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
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
                logger.warning("socks5 handshake timed out")
                with contextlib.suppress(OSError):
                    writer.close()
                    await writer.wait_closed()
            except asyncio.IncompleteReadError as exc:
                metrics_inc("socks5.handshake.error", error="IncompleteReadError")
                # Very common in practice: client drops during handshake.
                logger.debug(
                    "socks5 handshake closed early: got %d/%d bytes",
                    len(exc.partial),
                    exc.expected,
                )
                with contextlib.suppress(OSError):
                    writer.close()
                    await writer.wait_closed()
            except ValueError as exc:
                metrics_inc("socks5.handshake.error", error="ValueError")
                logger.debug("socks5 handshake rejected: %s", exc)
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

    @staticmethod
    async def _negotiate(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> Socks5Request | None:
        # ── Greeting: VER(1) + NMETHODS(1) + METHODS(N) ──────────────────────
        header = await _read_exact(reader, 2)
        if header[0] != 0x05:
            raise ValueError(f"not SOCKS5 (ver={header[0]:#x})")
        nmethods = header[1]
        methods = await _read_exact(reader, nmethods)

        if AuthMethod.NO_AUTH not in methods:
            writer.write(bytes([0x05, AuthMethod.NO_ACCEPT]))
            with contextlib.suppress(OSError):
                await writer.drain()
            writer.close()
            raise ValueError("client does not support NO_AUTH")

        writer.write(bytes([0x05, AuthMethod.NO_AUTH]))
        await writer.drain()

        # ── Request: VER(1) + CMD(1) + RSV(1) + ATYP+addr+port ──────────────
        req_header = await _read_exact(reader, 3)
        if req_header[0] != 0x05:
            raise ValueError("bad request version")
        if req_header[2] != 0x00:
            writer.write(_build_reply(Reply.GENERAL_FAILURE))
            with contextlib.suppress(OSError):
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            return None

        try:
            cmd = Cmd(req_header[1])
        except ValueError:
            reply = _build_reply(Reply.CMD_NOT_SUPPORTED)
            writer.write(reply)
            with contextlib.suppress(OSError):
                await writer.drain()
            writer.close()
            return None

        host, port = await _read_addr(reader)
        if cmd == Cmd.CONNECT and port == 0:
            writer.write(_build_reply(Reply.GENERAL_FAILURE))
            with contextlib.suppress(OSError):
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            return None

        if cmd == Cmd.CONNECT:
            return Socks5Request(
                cmd=cmd, host=host, port=port, reader=reader, writer=writer
            )

        if cmd == Cmd.UDP_ASSOCIATE:
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

        # BIND and future commands
        writer.write(_build_reply(Reply.CMD_NOT_SUPPORTED))
        with contextlib.suppress(OSError):
            await writer.drain()
        writer.close()
        return None
