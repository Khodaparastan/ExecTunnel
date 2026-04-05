"""UDP relay for SOCKS5 ``UDP_ASSOCIATE`` sessions.

:class:`UdpRelay` wraps a local UDP socket that the SOCKS5 client sends
datagrams to.  It strips and adds SOCKS5 UDP headers (RFC 1928 §7) and
exposes a simple :meth:`~UdpRelay.recv` / :meth:`~UdpRelay.send_to_client`
interface to the session layer.

Design notes
------------
* The socket is bound to an ephemeral port on ``127.0.0.1``.
* The close sentinel is delivered via a dedicated :class:`asyncio.Event` so
  :meth:`UdpRelay.close` always unblocks :meth:`UdpRelay.recv` even when the
  inbound queue is completely full.
* :func:`~exectunnel.proxy._wire.parse_udp_header` is the single source of
  truth for UDP header parsing — no duplication here.
* Observability hooks (metrics, spans) are intentionally absent from this
  layer.  The session layer is responsible for instrumentation.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import struct

from exectunnel.exceptions import ProtocolError, TransportError
from exectunnel.protocol import AddrType
from exectunnel.proxy._constants import (
    DEFAULT_QUEUE_CAPACITY,
    DROP_WARN_INTERVAL,
    MAX_UDP_PAYLOAD_BYTES,
)
from exectunnel.proxy._wire import parse_udp_header

__all__: list[str] = ["UdpRelay"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# asyncio datagram protocol
# ---------------------------------------------------------------------------


class _RelayDatagramProtocol(asyncio.DatagramProtocol):
    """asyncio datagram protocol that forwards received datagrams to a
    :class:`UdpRelay`."""

    __slots__ = ("_relay",)

    def __init__(self, relay: UdpRelay) -> None:
        self._relay = relay

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._relay._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        # OS-level socket errors (e.g. ICMP port-unreachable) are not
        # actionable per-datagram; log at DEBUG only.
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

    Strips/adds SOCKS5 UDP headers (RFC 1928 §7).  The socket is bound to an
    ephemeral port on ``127.0.0.1``; the actual bound port is available via
    :attr:`local_port` after :meth:`start` completes.

    Lifecycle::

        relay = UdpRelay()
        port = await relay.start()          # bind; returns ephemeral port
        item = await relay.recv()           # (payload, host, port) | None
        relay.send_to_client(data, h, p)   # wrap + send back to client
        relay.close()                       # tear down

    Async context manager::

        async with UdpRelay() as relay:
            port = relay.local_port
            # … relay data …

    Args:
        queue_capacity:      Maximum number of inbound datagrams to buffer
                             before dropping.  Defaults to
                             :data:`~exectunnel.proxy._constants.DEFAULT_QUEUE_CAPACITY`.
        drop_warn_interval:  Log a warning every *N* queue-full drops to avoid
                             log flooding.  Defaults to
                             :data:`~exectunnel.proxy._constants.DROP_WARN_INTERVAL`.
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

        # Inbound queue and close sentinel — created lazily in start() so that
        # UdpRelay can be constructed before the event loop is running.
        self._queue: asyncio.Queue[tuple[bytes, str, int]] | None = None
        self._close_event: asyncio.Event | None = None

        self._local_port: int = 0
        self._closed: bool = False
        self._started: bool = False

        # Address of the first SOCKS5 client that sends a datagram.
        self._client_addr: tuple[str, int] | None = None
        # Optional hint from the UDP_ASSOCIATE request (may be 0.0.0.0:0).
        self._expected_client_addr: tuple[str, int] | None = None

        # Telemetry counters (read via properties — no external dependency).
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

        Creates the inbound queue and close event here (not in ``__init__``)
        so that :class:`UdpRelay` can be constructed before the asyncio event
        loop is running.

        Args:
            expected_client_addr: Optional ``(host, port)`` hint from the
                ``UDP_ASSOCIATE`` request.  When the host is not an unspecified
                address (``0.0.0.0`` / ``::``) and the port is non-zero,
                datagrams from other addresses are rejected immediately rather
                than waiting for the first datagram to bind the client address.
                A malformed hint host is silently ignored (logged at DEBUG).

        Returns:
            The ephemeral port the relay is bound to on ``127.0.0.1``.

        Raises:
            RuntimeError:   If :meth:`start` has already been called.
            TransportError: If the OS refuses to create or bind the datagram
                            endpoint (e.g. ephemeral port range exhausted,
                            insufficient permissions).
        """
        if self._started:
            raise RuntimeError(
                "UdpRelay.start() has already been called. "
                "Create a new UdpRelay instance for each UDP_ASSOCIATE session."
            )
        self._started = True

        # Lazily create asyncio primitives now that the loop is guaranteed running.
        self._queue = asyncio.Queue(self._queue_capacity)
        self._close_event = asyncio.Event()

        # Record the client hint only when it carries a meaningful address.
        if expected_client_addr is not None:
            hint_host, hint_port = expected_client_addr
            try:
                hint_addr = ipaddress.ip_address(hint_host)
                is_unspecified = hint_addr.is_unspecified
            except ValueError:
                # Malformed hint (e.g. domain name) — ignore silently.
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
        return self._local_port

    def close(self) -> None:
        """Close the relay and unblock any coroutine awaiting :meth:`recv`.

        Idempotent — safe to call multiple times.

        The close sentinel is delivered via a dedicated :class:`asyncio.Event`
        that races against the queue, so this method always unblocks
        :meth:`recv` even when the inbound queue is completely full.
        """
        if self._closed:
            return
        self._closed = True
        logger.debug("udp relay closing (port=%d)", self._local_port)

        if self._transport is not None:
            self._transport.close()

        # Signal recv() to return None — always succeeds regardless of queue state.
        if self._close_event is not None:
            self._close_event.set()

    # ── Inbound path (client → relay) ────────────────────────────────────────

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Parse the SOCKS5 UDP header and enqueue ``(payload, dst_host, dst_port)``.

        Malformed datagrams are silently dropped with a debug log — they arrive
        from an untrusted UDP client and must never raise into the event loop.

        This method is called from :class:`_RelayDatagramProtocol` and must
        never raise.
        """
        if self._closed or self._queue is None:
            return

        # ── Size guard ───────────────────────────────────────────────────────
        if len(data) > MAX_UDP_PAYLOAD_BYTES:
            logger.debug(
                "udp relay dropping oversized datagram from %s:%d (%d bytes > %d)",
                addr[0],
                addr[1],
                len(data),
                MAX_UDP_PAYLOAD_BYTES,
            )
            return

        # ── Client address binding / filtering ───────────────────────────────
        if self._client_addr is None:
            if (
                self._expected_client_addr is not None
                and addr != self._expected_client_addr
            ):
                self._foreign_client_count += 1
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

        # ── Parse SOCKS5 UDP header (RFC 1928 §7) ────────────────────────────
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

        # ── Enqueue ──────────────────────────────────────────────────────────
        try:
            self._queue.put_nowait((payload, host, port))
            self._accepted_count += 1
        except asyncio.QueueFull:
            self._drop_count += 1
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
        """Return the next ``(payload, host, port)`` from the SOCKS5 client.

        Blocks until a datagram is available or the relay is closed.

        The close sentinel races against the queue via :class:`asyncio.Event`
        so this method always returns promptly after :meth:`close` is called —
        even when the inbound queue is full.

        After the close event fires, the queue is drained one final time so
        that no datagram is lost when :meth:`close` races with a queued item.

        Returns:
            A ``(payload, host, port)`` tuple, or ``None`` when the relay has
            been closed and the queue is fully drained.

        Raises:
            RuntimeError:           If called before :meth:`start`.
            asyncio.CancelledError: Propagated as-is — never suppressed.
        """
        if self._queue is None or self._close_event is None:
            raise RuntimeError(
                "UdpRelay.recv() called before start(). "
                "Await UdpRelay.start() before calling recv()."
            )

        # Fast path: drain any immediately available item first.
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

        # If already closed and queue is empty, signal EOF.
        if self._close_event.is_set():
            return None

        # Race the queue against the close event using asyncio.create_task()
        # (asyncio.ensure_future is deprecated since Python 3.10).
        queue_task: asyncio.Task[tuple[bytes, str, int]] = asyncio.create_task(
            self._queue.get(),
        )
        close_task: asyncio.Task[None] = asyncio.create_task(
            self._close_event.wait(),
        )

        try:
            done, pending = await asyncio.wait(
                [queue_task, close_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            queue_task.cancel()
            close_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await queue_task
            with contextlib.suppress(asyncio.CancelledError):
                await close_task
            raise

        # Cancel the loser — but harvest its result first if it already finished
        # to avoid discarding a datagram that arrived in the same scheduler tick.
        for task in pending:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.InvalidStateError):
                await task

        # queue_task won — return the item.
        if queue_task in done and not queue_task.cancelled():
            return queue_task.result()

        # close_task won — drain one final time to avoid losing the last item
        # if a datagram arrived between the close signal and task cancellation.
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    # ── Outbound path (session layer → client) ───────────────────────────────

    def send_to_client(self, payload: bytes, src_host: str, src_port: int) -> None:
        """Wrap *payload* in a SOCKS5 UDP header and send it back to the client.

        Non-IP ``src_host`` values are dropped with a debug log — RFC 1928 §7
        requires ``BND.ADDR`` in UDP replies to be an IP address.

        If the client address has not yet been bound (no datagram received yet),
        the packet is dropped and logged at DEBUG level.

        Args:
            payload:  Raw datagram bytes to wrap and forward.
            src_host: Source IP address string (IPv4 or IPv6).
            src_port: Source port number in ``[0, 65535]``.

        Raises:
            ProtocolError:
                * *payload* is not :class:`bytes`.
                * *src_port* is outside ``[0, 65535]``.
                These are caller bugs in the session layer.
        """
        if not isinstance(payload, bytes):
            raise ProtocolError(
                f"send_to_client() requires a bytes payload, "
                f"got {type(payload).__name__!r}.",
                details={
                    "socks5_field": "DATA",
                    "expected": "bytes payload",
                },
                hint="This is a caller bug in the session layer.",
            )

        if not (0 <= src_port <= 65_535):
            raise ProtocolError(
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
        header = (
            b"\x00\x00\x00"  # RSV(2) + FRAG(1)
            + bytes([int(atyp)])
            + addr.packed
            + struct.pack("!H", src_port)
        )
        with contextlib.suppress(OSError):
            self._transport.sendto(header + payload, self._client_addr)

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def local_port(self) -> int:
        """The ephemeral port the relay is bound to on ``127.0.0.1``."""
        return self._local_port

    @property
    def is_running(self) -> bool:
        """``True`` once :meth:`start` has completed and before :meth:`close`
        is called."""
        return (
            self._started
            and not self._closed
            and self._transport is not None
            and not self._transport.is_closing()
        )

    @property
    def accepted_count(self) -> int:
        """Total datagrams successfully enqueued for processing."""
        return self._accepted_count

    @property
    def drop_count(self) -> int:
        """Total datagrams dropped due to inbound queue saturation."""
        return self._drop_count

    @property
    def foreign_client_count(self) -> int:
        """Total datagrams dropped because they arrived from an unexpected
        client address."""
        return self._foreign_client_count
