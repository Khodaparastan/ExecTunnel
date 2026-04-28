"""SOCKS5 server — async accept loop (RFC 1928, no-auth only).

:class:`Socks5Server` binds a TCP listen socket, negotiates the SOCKS5
handshake for each accepted connection via
:mod:`exectunnel.proxy._handshake`, and yields completed
:class:`~exectunnel.proxy.tcp_relay.TCPRelay` objects via ``async for``.

Supported commands:
    * ``CONNECT`` (0x01).
    * ``UDP_ASSOCIATE`` (0x03).

Unsupported:
    * ``BIND`` (0x02) — replies ``CMD_NOT_SUPPORTED``.

Authentication:
    * ``NO_AUTH`` (0x00) only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable
from typing import Final

from exectunnel.exceptions import TransportError
from exectunnel.observability import (
    aspan,
    metrics_gauge_dec,
    metrics_gauge_inc,
    metrics_inc,
    metrics_observe,
    start_trace,
)
from exectunnel.protocol import Reply

from ._errors import classify_handshake_error
from ._handshake import negotiate
from ._io import close_writer
from .config import Socks5ServerConfig
from .tcp_relay import TCPRelay

__all__ = ["Socks5Server"]

_log: Final[logging.Logger] = logging.getLogger(__name__)


class Socks5Server:
    """Async SOCKS5 server that yields :class:`TCPRelay` objects via ``async for``.

    Example::

        async with Socks5Server(Socks5ServerConfig()) as server:
            async for req in server:
                asyncio.create_task(handle(req))

    Args:
        config: Server configuration. Defaults to
            :class:`Socks5ServerConfig` with all defaults.
    """

    def __init__(self, config: Socks5ServerConfig | None = None) -> None:
        self._config = config if config is not None else Socks5ServerConfig()
        self._queue: asyncio.Queue[TCPRelay | None] = asyncio.Queue(
            self._config.request_queue_capacity
        )
        self._server: asyncio.Server | None = None
        self._started: bool = False
        self._stopped: bool = False
        self._handshake_tasks: set[asyncio.Task[None]] = set()
        self._active_writers: set[asyncio.StreamWriter] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind the SOCKS5 listen socket and begin accepting connections.

        Raises:
            RuntimeError: If :meth:`start` has already been called.
            TransportError: If the socket cannot be bound.
        """
        if self._started:
            raise RuntimeError(
                "Socks5Server.start() has already been called. "
                "Create a new Socks5Server instance to rebind."
            )

        cfg = self._config
        try:
            self._server = await asyncio.start_server(
                self._handle_client,
                cfg.host,
                cfg.port,
                backlog=cfg.listen_backlog,
            )
        except OSError as exc:
            raise TransportError(
                f"SOCKS5 server failed to bind on {cfg.host}:{cfg.port}.",
                details={"host": cfg.host, "port": cfg.port, "url": cfg.url},
                hint=(
                    f"Ensure port {cfg.port} is not already in use and that "
                    "the process has permission to bind on the requested "
                    "address."
                ),
            ) from exc

        self._started = True
        _log.info("SOCKS5 listening on %s:%d", cfg.host, cfg.port)

        if not cfg.is_loopback:
            _log.warning(
                "SOCKS5 server bound to non-loopback address %s:%d — any "
                "network-reachable host can use this as an open proxy. Bind "
                "to 127.0.0.1 unless public access is intentional.",
                cfg.host,
                cfg.port,
            )

    async def stop(self) -> None:
        """Close the listen socket, cancel in-flight handshakes, drain the queue.

        Idempotent. Safe to call before :meth:`start`.
        """
        if self._stopped:
            return
        self._stopped = True

        if self._server is not None:
            self._server.close()

        await self._close_active_writers()
        await self._cancel_handshake_tasks()
        await self._drain_pending_requests()

        if self._server is not None:
            await self._server.wait_closed()

        await self._enqueue_stop_sentinel()

    async def _enqueue_stop_sentinel(self) -> None:
        """Enqueue the async-iterator stop sentinel after draining capacity."""
        while True:
            try:
                self._queue.put_nowait(None)
                return
            except asyncio.QueueFull:
                await self._drain_pending_requests()
                await asyncio.sleep(0)

    async def _close_active_writers(self) -> None:
        """Force-close every writer currently mid-handshake and drain close."""
        if not self._active_writers:
            return

        pending = tuple(self._active_writers)
        await asyncio.gather(
            *(close_writer(writer) for writer in pending),
            return_exceptions=True,
        )

    async def _cancel_handshake_tasks(self) -> None:
        """Cancel and await every in-flight handshake task."""
        if not self._handshake_tasks:
            return
        pending = tuple(self._handshake_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._handshake_tasks.clear()

    async def _drain_pending_requests(self) -> None:
        """Close any fully-negotiated requests still queued at stop time."""
        close_tasks: list[Awaitable[None]] = []
        while True:
            try:
                req = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(req, TCPRelay):
                if req.udp_relay is not None:
                    req.udp_relay.close()
                close_tasks.append(close_writer(req.writer))

        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)

    async def __aenter__(self) -> Socks5Server:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    # ── Async iteration ───────────────────────────────────────────────────────

    def __aiter__(self) -> AsyncIterator[TCPRelay]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[TCPRelay]:
        """Yield requests until :meth:`stop` enqueues the ``None`` sentinel."""
        while True:
            req = await self._queue.get()
            if req is None:
                return
            yield req

    # ── Per-connection handling ───────────────────────────────────────────────

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Accept one TCP connection and run the SOCKS5 handshake.

        All failures are contained so a single bad client never tears
        down the server.

        Args:
            reader: asyncio stream reader for the accepted connection.
            writer: asyncio stream writer for the accepted connection.
        """
        start_trace()
        metrics_inc("socks5.connections.accepted")

        if self._stopped:
            metrics_inc("socks5.connections.rejected")
            metrics_inc("socks5.handshakes.error", reason="server_stopped")
            await close_writer(writer)
            return

        if len(self._handshake_tasks) >= self._config.max_concurrent_handshakes:
            metrics_inc("socks5.connections.rejected")
            metrics_inc("socks5.handshakes.error", reason="handshake_cap")
            _log.warning(
                "socks5 handshake cap reached (%d) — rejecting new client",
                self._config.max_concurrent_handshakes,
            )
            await close_writer(writer)
            return

        metrics_gauge_inc("socks5.connections.active")

        task = asyncio.current_task()
        if task is not None:
            self._handshake_tasks.add(task)
        self._active_writers.add(writer)

        try:
            await self._do_handshake(reader, writer)
        except asyncio.CancelledError:
            await close_writer(writer)
            raise
        except Exception:
            await close_writer(writer)
        finally:
            metrics_gauge_dec("socks5.connections.active")
            if task is not None:
                self._handshake_tasks.discard(task)
            self._active_writers.discard(writer)

    async def _do_handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Run the full SOCKS5 handshake and enqueue the completed request.

        Args:
            reader: asyncio stream reader for the accepted connection.
            writer: asyncio stream writer for the accepted connection.
        """
        handshake_start = time.monotonic()

        async with aspan("socks5.handshake"):
            try:
                async with asyncio.timeout(self._config.handshake_timeout):
                    req = await negotiate(
                        reader,
                        writer,
                        udp_associate_enabled=self._config.udp_associate_enabled,
                        udp_relay_queue_capacity=self._config.udp_relay_queue_capacity,
                        udp_max_payload_bytes=self._config.udp_max_payload_bytes,
                        udp_drop_warn_interval=self._config.udp_drop_warn_interval,
                        udp_bind_host=self._config.udp_bind_host,
                        udp_advertise_host=self._config.effective_udp_advertise_host,
                    )
            except (TimeoutError, Exception) as exc:  # noqa: BLE001
                await self._handle_handshake_failure(exc, writer, handshake_start)
                return

            if req is None:
                metrics_inc("socks5.connections.rejected")
                metrics_observe(
                    "socks5.handshake_duration_sec",
                    time.monotonic() - handshake_start,
                )
                await close_writer(writer)
                return

            await self._enqueue_or_reject(req, handshake_start)

    async def _handle_handshake_failure(
        self,
        exc: BaseException,
        writer: asyncio.StreamWriter,
        handshake_start: float,
    ) -> None:
        """Classify *exc*, emit metrics, and close *writer*.

        Args:
            exc: The exception raised during the handshake.
            writer: Writer associated with the failed handshake.
            handshake_start: ``time.monotonic()`` stamp from the start
                of the handshake.
        """
        reason, _level = classify_handshake_error(exc)
        metrics_inc("socks5.handshakes.error", reason=reason)
        metrics_inc("socks5.connections.rejected")
        metrics_observe(
            "socks5.handshake_duration_sec",
            time.monotonic() - handshake_start,
        )
        if reason == "timeout":
            _log.warning(
                "socks5 handshake timed out after %.1fs",
                self._config.handshake_timeout,
            )
        await close_writer(writer)

    async def _enqueue_or_reject(
        self,
        req: TCPRelay,
        handshake_start: float,
    ) -> None:
        """Enqueue *req* for the session layer, or reject it on backpressure.

        Args:
            req: The fully-negotiated request.
            handshake_start: ``time.monotonic()`` stamp from the start
                of the handshake.
        """
        if self._stopped:
            metrics_inc("socks5.handshakes.error", reason="server_stopped")
            metrics_inc("socks5.connections.rejected")
            with contextlib.suppress(Exception):
                await req.send_reply_error(Reply.GENERAL_FAILURE)
            return

        try:
            async with asyncio.timeout(self._config.queue_put_timeout):
                await self._queue.put(req)
        except TimeoutError:
            metrics_inc("socks5.handshakes.error", reason="queue_full")
            metrics_inc("socks5.connections.rejected")
            metrics_observe(
                "socks5.handshake_duration_sec",
                time.monotonic() - handshake_start,
            )
            _log.warning(
                "socks5 queue full — dropping handshake for %s:%d "
                "(queue_put_timeout=%.1fs)",
                req.host,
                req.port,
                self._config.queue_put_timeout,
            )
            with contextlib.suppress(Exception):
                await req.send_reply_error(Reply.GENERAL_FAILURE)
            return

        metrics_inc("socks5.handshakes.ok", cmd=req.cmd.name)
        metrics_observe(
            "socks5.handshake_duration_sec",
            time.monotonic() - handshake_start,
        )
        _log.debug(
            "socks5 handshake ok: cmd=%s host=%s port=%d",
            req.cmd.name,
            req.host,
            req.port,
        )
