"""UDP relay for SOCKS5 ``UDP_ASSOCIATE`` sessions.

:class:`UdpRelay` wraps a local UDP socket that the SOCKS5 client sends
datagrams to.  It strips and adds SOCKS5 UDP headers (RFC 1928 §7) and
exposes a simple :meth:`~UdpRelay.recv` / :meth:`~UdpRelay.send_to_client`
interface to the session layer.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging

from exectunnel.exceptions import ProtocolError, TransportError
from exectunnel.observability import (
    metrics_gauge_dec,
    metrics_gauge_inc,
    metrics_inc,
    metrics_observe,
)
from exectunnel.protocol import AddrType
from exectunnel.proxy._constants import (
    DEFAULT_QUEUE_CAPACITY,
    DROP_WARN_INTERVAL,
    MAX_UDP_PAYLOAD_BYTES,
)
from exectunnel.proxy._wire import build_udp_header, parse_udp_header

__all__: list[str] = ["UdpRelay"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# asyncio datagram protocol
# ---------------------------------------------------------------------------


class _RelayDatagramProtocol(asyncio.DatagramProtocol):
    """asyncio datagram protocol forwarding datagrams to a :class:`UdpRelay`."""

    __slots__ = ("_relay",)

    def __init__(self, relay: UdpRelay) -> None:
        self._relay = relay

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._relay._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        logger.debug(
            "udp relay socket error [%s]: %s",
            type(exc).__name__,
            exc,
        )

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None:
            logger.debug("udp relay connection lost: %s", exc)


# ---------------------------------------------------------------------------
# UdpRelay
# ---------------------------------------------------------------------------


class UdpRelay:
    """Local UDP socket relay for a single SOCKS5 ``UDP_ASSOCIATE`` session.

    Lifecycle::

        relay = UdpRelay()
        port = await relay.start()
        item = await relay.recv()  # (payload, host, port) | None
        relay.send_to_client(data, h, p)
        relay.close()

    Args:
        queue_capacity:      Max inbound datagrams to buffer before dropping.
        drop_warn_interval:  Log a warning every *N* queue-full drops.
    """

    def __init__(
        self,
        *,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
        drop_warn_interval: int = DROP_WARN_INTERVAL,
    ) -> None:
        self._queue_capacity = queue_capacity
        self._drop_warn_interval = drop_warn_interval

        self._transport: asyncio.DatagramTransport | None = None

        # Created lazily in start() so UdpRelay can be constructed before the loop.
        self._queue: asyncio.Queue[tuple[bytes, str, int]] | None = None
        self._close_event: asyncio.Event | None = None

        self._local_port: int = 0
        self._closed: bool = False
        self._started: bool = False

        self._client_addr: tuple[str, int] | None = None
        self._expected_client_addr: tuple[str, int] | None = None

        # Reused across recv() calls to avoid per-call task allocation.
        self._recv_close_task: asyncio.Task[None] | None = None

        # Internal counters kept for property access by callers.
        self._drop_count: int = 0
        self._foreign_client_count: int = 0
        self._accepted_count: int = 0

    # ── Async context manager ────────────────────────────────────────────────

    async def __aenter__(self) -> UdpRelay:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        self.close()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(
        self,
        expected_client_addr: tuple[str, int] | None = None,
    ) -> int:
        """Bind the UDP socket and return the local port.

        Args:
            expected_client_addr: Optional ``(host, port)`` hint from the
                ``UDP_ASSOCIATE`` request.

        Returns:
            The ephemeral port the relay is bound to on ``127.0.0.1``.

        Raises:
            RuntimeError:   If already started.
            TransportError: If bind fails.
        """
        if self._started:
            raise RuntimeError(
                "UdpRelay.start() has already been called. "
                "Create a new UdpRelay instance for each UDP_ASSOCIATE session."
            )
        self._started = True

        self._queue = asyncio.Queue(self._queue_capacity)
        self._close_event = asyncio.Event()

        if expected_client_addr is not None:
            hint_host, hint_port = expected_client_addr
            try:
                hint_addr = ipaddress.ip_address(hint_host)
                is_unspecified = hint_addr.is_unspecified
            except ValueError:
                logger.debug(
                    "udp relay: ignoring malformed expected_client_addr host %r",
                    hint_host,
                )
                is_unspecified = True
            if not is_unspecified and hint_port != 0:
                self._expected_client_addr = expected_client_addr

        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _RelayDatagramProtocol(self),
                local_addr=("127.0.0.1", 0),
            )
        except OSError as exc:
            raise TransportError(
                "UDP relay failed to bind a local datagram endpoint.",
                details={
                    "host": "127.0.0.1",
                    "port": 0,
                    "url": "udp://127.0.0.1:0",
                },
                hint=(
                    "Check that the ephemeral port range is not exhausted and "
                    "that the process has permission to bind UDP sockets on "
                    "127.0.0.1."
                ),
            ) from exc

        if not isinstance(transport, asyncio.DatagramTransport):
            transport.close()
            raise TransportError(
                "UDP relay received an unexpected transport type from asyncio.",
                details={
                    "host": "127.0.0.1",
                    "port": 0,
                    "url": "udp://127.0.0.1:0",
                },
                hint="This is an asyncio internal error. Please report it.",
            )

        self._transport = transport
        self._local_port = transport.get_extra_info("sockname")[1]
        logger.debug("udp relay bound on 127.0.0.1:%d", self._local_port)
        metrics_gauge_inc("socks5.udp.relays_active")
        return self._local_port

    def close(self) -> None:
        """Close the relay and unblock any coroutine awaiting :meth:`recv`.

        Idempotent.
        """
        if self._closed:
            return
        self._closed = True
        logger.debug("udp relay closing (port=%d)", self._local_port)
        if self._started:
            metrics_gauge_dec("socks5.udp.relays_active")

        if self._transport is not None:
            self._transport.close()

        if self._close_event is not None:
            self._close_event.set()

        # Cancel the reused close-wait task to avoid a dangling reference.
        if self._recv_close_task is not None and not self._recv_close_task.done():
            self._recv_close_task.cancel()
            self._recv_close_task = None

    # ── Inbound path (client → relay) ────────────────────────────────────────

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Parse the SOCKS5 UDP header and enqueue ``(payload, dst_host, dst_port)``.

        Called from :class:`_RelayDatagramProtocol`; must never raise.
        """
        if self._closed or self._queue is None:
            return

        # Size guard.
        if len(data) > MAX_UDP_PAYLOAD_BYTES:
            logger.debug(
                "udp relay dropping oversized datagram from %s:%d (%d bytes > %d)",
                addr[0],
                addr[1],
                len(data),
                MAX_UDP_PAYLOAD_BYTES,
            )
            return

        # Client address binding / filtering.
        if self._client_addr is None:
            if (
                self._expected_client_addr is not None
                and addr != self._expected_client_addr
            ):
                self._foreign_client_count += 1
                metrics_inc("socks5.udp.foreign_client_drops")
                logger.debug(
                    "udp relay dropping datagram from %s:%d before client bound "
                    "(expected hint %s:%d)",
                    addr[0],
                    addr[1],
                    self._expected_client_addr[0],
                    self._expected_client_addr[1],
                )
                return
            self._client_addr = addr
            logger.debug(
                "udp relay client bound to %s:%d (port=%d)",
                addr[0],
                addr[1],
                self._local_port,
            )
        elif addr != self._client_addr:
            self._foreign_client_count += 1
            metrics_inc("socks5.udp.foreign_client_drops")
            if (
                self._foreign_client_count == 1
                or self._foreign_client_count % self._drop_warn_interval == 0
            ):
                logger.warning(
                    "udp relay dropping datagram from unexpected client %s:%d "
                    "(expected %s:%d, total_foreign_drops=%d)",
                    addr[0],
                    addr[1],
                    self._client_addr[0],
                    self._client_addr[1],
                    self._foreign_client_count,
                )
            return

        # Parse SOCKS5 UDP header (RFC 1928 §7).
        try:
            payload, host, port = parse_udp_header(data)
        except ProtocolError as exc:
            logger.debug(
                "udp relay dropping malformed datagram from %s:%d [%s]: %s",
                addr[0],
                addr[1],
                exc.error_code,
                exc.message,
            )
            return

        # Enqueue.
        try:
            self._queue.put_nowait((payload, host, port))
            self._accepted_count += 1
            metrics_inc("socks5.udp.datagrams_accepted")
            metrics_observe("socks5.udp.datagram_size", len(data))
        except asyncio.QueueFull:
            self._drop_count += 1
            metrics_inc("socks5.udp.datagrams_dropped")
            if (
                self._drop_count == 1
                or self._drop_count % self._drop_warn_interval == 0
            ):
                logger.warning(
                    "udp relay inbound queue full, dropping datagram "
                    "(total_queue_drops=%d, port=%d)",
                    self._drop_count,
                    self._local_port,
                )

    # ── Outbound path (relay → session layer) ────────────────────────────────

    async def recv(self) -> tuple[bytes, str, int] | None:
        """Return the next ``(payload, host, port)`` or ``None`` on close.

        Raises:
            RuntimeError: If called before :meth:`start`.
        """
        if self._queue is None or self._close_event is None:
            raise RuntimeError(
                "UdpRelay.recv() called before start(). "
                "Await UdpRelay.start() before calling recv()."
            )

        # Fast path: drain immediately available item.
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

        if self._close_event.is_set():
            return None

        # Race queue against close event.
        queue_task: asyncio.Task[tuple[bytes, str, int]] = asyncio.create_task(
            self._queue.get(),
        )
        if self._recv_close_task is None or self._recv_close_task.done():
            self._recv_close_task = asyncio.create_task(
                self._close_event.wait(),
            )
        close_task = self._recv_close_task

        try:
            done, _pending = await asyncio.wait(
                [queue_task, close_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            queue_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await queue_task
            raise

        if queue_task not in done:
            queue_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await queue_task

        if queue_task in done and not queue_task.cancelled():
            return queue_task.result()

        # Close won — drain one final time.
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    # ── Outbound path (session layer → client) ───────────────────────────────

    def send_to_client(self, payload: bytes, src_host: str, src_port: int) -> None:
        """Wrap *payload* in a SOCKS5 UDP header and send to the client.

        Non-IP ``src_host`` values are silently dropped (RFC 1928 §7).

        Args:
            payload:  Raw datagram bytes.
            src_host: Source IP address string.
            src_port: Source port in ``[0, 65535]``.

        Raises:
            TransportError: *payload* not bytes, or *src_port* out of range.
        """
        if not isinstance(payload, bytes):
            raise TransportError(
                f"send_to_client() requires a bytes payload, "
                f"got {type(payload).__name__!r}.",
                details={"socks5_field": "DATA", "expected": "bytes payload"},
                hint="This is a caller bug in the session layer.",
            )

        if not (0 <= src_port <= 65_535):
            raise TransportError(
                f"send_to_client() src_port {src_port!r} is out of range [0, 65535].",
                details={
                    "socks5_field": "SRC.PORT",
                    "expected": "src_port in [0, 65535]",
                },
                hint="This is a caller bug in the session layer.",
            )

        if self._transport is None or self._closed:
            return

        if self._client_addr is None:
            logger.debug(
                "udp relay: dropping reply for %s:%d — client address not yet bound "
                "(port=%d)",
                src_host,
                src_port,
                self._local_port,
            )
            return

        try:
            addr = ipaddress.ip_address(src_host)
        except ValueError:
            logger.debug(
                "udp relay: dropping reply with non-IP src_host %r (RFC 1928 §7)",
                src_host,
            )
            return

        atyp = AddrType.IPV4 if addr.version == 4 else AddrType.IPV6
        header = build_udp_header(atyp, addr.packed, src_port)
        with contextlib.suppress(OSError):
            self._transport.sendto(header + payload, self._client_addr)

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def local_port(self) -> int:
        """The ephemeral port the relay is bound to."""
        return self._local_port

    @property
    def is_running(self) -> bool:
        """``True`` once started and before closed."""
        return (
            self._started
            and not self._closed
            and self._transport is not None
            and not self._transport.is_closing()
        )

    @property
    def accepted_count(self) -> int:
        """Total datagrams successfully enqueued."""
        return self._accepted_count

    @property
    def drop_count(self) -> int:
        """Total datagrams dropped due to queue saturation."""
        return self._drop_count

    @property
    def foreign_client_count(self) -> int:
        """Total datagrams dropped from unexpected client addresses."""
        return self._foreign_client_count
