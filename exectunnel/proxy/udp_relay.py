"""UDP relay for SOCKS5 ``UDP_ASSOCIATE`` sessions.

:class:`UDPRelay` wraps a local UDP socket that the SOCKS5 client sends
datagrams to. It strips and adds SOCKS5 UDP headers (RFC 1928 §7) and
exposes a simple :meth:`UDPRelay.recv` / :meth:`UDPRelay.send_to_client`
interface to the session layer.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
from typing import Final

from exectunnel.exceptions import ConfigurationError, ProtocolError, TransportError
from exectunnel.observability import (
    metrics_gauge_dec,
    metrics_gauge_inc,
    metrics_inc,
    metrics_observe,
)
from exectunnel.protocol import AddrType

from ._constants import (
    DEFAULT_DROP_WARN_INTERVAL,
    DEFAULT_UDP_QUEUE_CAPACITY,
    MAX_TCP_UDP_PORT,
    MAX_TUNNEL_UDP_PAYLOAD_BYTES,
    MAX_UDP_PAYLOAD_BYTES,
)
from ._udp_protocol import RelayDatagramProtocol
from ._wire import build_udp_header_for_host, parse_udp_header

__all__ = ["UDPRelay"]

_log: Final[logging.Logger] = logging.getLogger(__name__)


class UDPRelay:
    """Local UDP socket relay for a single SOCKS5 ``UDP_ASSOCIATE`` session.

    Lifecycle::

        relay = UDPRelay()
        port = await relay.start()
        item = await relay.recv()  # (payload, host, port) | None
        relay.send_to_client(data, host, port)
        relay.close()

    Args:
        queue_capacity: Max inbound datagrams to buffer before dropping.
        drop_warn_interval: Log a warning every *N* queue-full drops.
    """

    __slots__ = (
        "_queue_capacity",
        "_max_payload_bytes",
        "_drop_warn_interval",
        "_bind_host",
        "_advertise_host",
        "_transport",
        "_queue",
        "_close_event",
        "_local_port",
        "_closed",
        "_started",
        "_client_addr",
        "_expected_client_addr",
        "_expected_client_host",
        "_close_wait_task",
        "_active_metric_emitted",
        "_drop_count",
        "_send_drop_count",
        "_foreign_client_count",
        "_accepted_count",
    )

    def __init__(
        self,
        *,
        queue_capacity: int = DEFAULT_UDP_QUEUE_CAPACITY,
        max_payload_bytes: int = MAX_TUNNEL_UDP_PAYLOAD_BYTES,
        drop_warn_interval: int = DEFAULT_DROP_WARN_INTERVAL,
        bind_host: str = "127.0.0.1",
        advertise_host: str | None = None,
    ) -> None:

        if not isinstance(queue_capacity, int) or queue_capacity < 1:
            raise ConfigurationError(
                f"UDPRelay.queue_capacity must be an integer >= 1, got {queue_capacity!r}.",
                details={"field": "queue_capacity", "value": queue_capacity},
                hint="Set udp_relay_queue_capacity to at least 1.",
            )
        if not isinstance(max_payload_bytes, int) or not (
            1 <= max_payload_bytes <= MAX_TUNNEL_UDP_PAYLOAD_BYTES
        ):
            raise ConfigurationError(
                f"UDPRelay.max_payload_bytes must be in [1, {MAX_TUNNEL_UDP_PAYLOAD_BYTES}], "
                f"got {max_payload_bytes!r}.",
                details={"field": "max_payload_bytes", "value": max_payload_bytes},
                hint="Use a payload cap compatible with the tunnel frame budget.",
            )
        if not isinstance(drop_warn_interval, int) or drop_warn_interval < 1:
            raise ConfigurationError(
                "UDPRelay.drop_warn_interval must be an integer >= 1.",
                details={"field": "drop_warn_interval", "value": drop_warn_interval},
                hint="Set udp_drop_warn_interval to at least 1.",
            )
        if not bind_host:
            raise ConfigurationError(
                "UDPRelay.bind_host must not be empty.",
                details={"field": "bind_host", "value": bind_host},
                hint="Use a bind host such as 127.0.0.1.",
            )

        self._queue_capacity = queue_capacity
        self._max_payload_bytes = max_payload_bytes
        self._drop_warn_interval = drop_warn_interval
        self._bind_host = bind_host
        self._advertise_host = advertise_host or bind_host

        self._transport: asyncio.DatagramTransport | None = None
        self._queue: asyncio.Queue[tuple[bytes, str, int]] | None = None
        self._close_event: asyncio.Event | None = None

        self._local_port: int = 0
        self._closed: bool = False
        self._started: bool = False

        self._client_addr: tuple[str, int] | None = None
        self._expected_client_addr: tuple[str, int] | None = None
        self._expected_client_host: str | None = None

        self._close_wait_task: asyncio.Task[None] | None = None

        self._active_metric_emitted: bool = False
        self._drop_count: int = 0
        self._send_drop_count: int = 0
        self._foreign_client_count: int = 0
        self._accepted_count: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def __aenter__(self) -> UDPRelay:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        self.close()

    async def start(
        self,
        expected_client_addr: tuple[str, int] | None = None,
        expected_client_host: str | None = None,
    ) -> int:
        """Bind the UDP socket and return the local port.

        Args:
            expected_client_addr: Optional ``(host, port)`` hint from
                the ``UDP_ASSOCIATE`` request. Ignored when the host is
                unspecified (``0.0.0.0`` / ``::``) or the port is ``0``.
            expected_client_host: TCP peer host observed on the control
                connection. Used as a weaker admission hint when the SOCKS5
                request used an unspecified UDP port.

        Returns:
            The ephemeral port the relay is bound to on ``127.0.0.1``.

        Raises:
            RuntimeError: If :meth:`start` has already been called.
            TransportError: If the UDP socket cannot be bound.
        """
        if self._started:
            raise RuntimeError(
                "UDPRelay.start() has already been called. "
                "Create a new UDPRelay instance for each UDP_ASSOCIATE session."
            )
        self._started = True
        self._queue = asyncio.Queue(self._queue_capacity)
        self._close_event = asyncio.Event()

        self._validate_advertise_host()
        self._bind_expected_client(expected_client_addr, expected_client_host)
        await self._bind_datagram_endpoint()

        metrics_gauge_inc("socks5.udp.relays_active")
        self._active_metric_emitted = True
        return self._local_port

    def _validate_advertise_host(self) -> None:
        """Validate the UDP_ASSOCIATE advertised address."""
        try:
            addr = ipaddress.ip_address(self._advertise_host)
        except ValueError as exc:
            raise TransportError(
                f"UDP relay advertise_host {self._advertise_host!r} is not an IP address.",
                error_code="proxy.udp_relay.invalid_advertise_host",
                details={
                    "advertise_host": self._advertise_host,
                    "expected": "IPv4 or IPv6 address string",
                },
                hint=(
                    "Set udp_advertise_host to an IP address reachable by the "
                    "SOCKS5 client."
                ),
            ) from exc

        if addr.is_unspecified:
            raise TransportError(
                f"UDP relay advertise_host {self._advertise_host!r} is unspecified.",
                error_code="proxy.udp_relay.unspecified_advertise_host",
                details={"advertise_host": self._advertise_host},
                hint="Advertise a concrete IP address, not 0.0.0.0 or ::.",
            )

    @staticmethod
    def _normalise_host_for_compare(host: str) -> str:
        try:
            return ipaddress.ip_address(host).compressed
        except ValueError:
            return host

    @classmethod
    def _addr_key(cls, addr: tuple[str, int]) -> tuple[str, int]:
        return cls._normalise_host_for_compare(addr[0]), addr[1]

    def _bind_expected_client(
        self,
        expected_client_addr: tuple[str, int] | None,
        expected_client_host: str | None,
    ) -> None:
        """Record *expected_client_addr* when specific and routable.

        Args:
            expected_client_addr: Optional client-address hint from the
                ``UDP_ASSOCIATE`` request.
            expected_client_host: TCP peer host observed on the control
                connection. Used as a weaker admission hint when the SOCKS5
                request used an unspecified UDP port.
        """
        if expected_client_host:
            try:
                peer_ip = ipaddress.ip_address(expected_client_host)
            except ValueError:
                _log.debug(
                    "udp relay: ignoring malformed TCP peer host %r",
                    expected_client_host,
                )
            else:
                if not peer_ip.is_unspecified:
                    self._expected_client_host = peer_ip.compressed

        if expected_client_addr is None:
            return

        hint_host, hint_port = expected_client_addr
        try:
            hint_ip = ipaddress.ip_address(hint_host)
            is_unspecified = hint_ip.is_unspecified
        except ValueError:
            _log.debug(
                "udp relay: ignoring malformed expected_client_addr host %r",
                hint_host,
            )
            return

        if not is_unspecified and hint_port != 0:
            self._expected_client_addr = (hint_ip.compressed, hint_port)
            self._expected_client_host = hint_ip.compressed
        elif not is_unspecified:
            self._expected_client_host = hint_ip.compressed

    async def _bind_datagram_endpoint(self) -> None:
        """Create the local asyncio datagram endpoint on ``127.0.0.1:0``.

        Raises:
            TransportError: On bind failure or unexpected transport
                type.
        """
        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: RelayDatagramProtocol(self),
                local_addr=(self._bind_host, 0),
            )
        except OSError as exc:
            raise TransportError(
                "UDP relay failed to bind a local datagram endpoint.",
                details={
                    "host": self._bind_host,
                    "port": 0,
                    "url": f"udp://{self._bind_host}:0",
                },
                hint=(
                    "Check that the ephemeral port range is not exhausted and "
                    "that the process has permission to bind UDP sockets on "
                    f"{self._bind_host}."
                ),
            ) from exc

        if not isinstance(transport, asyncio.DatagramTransport):
            transport.close()
            raise TransportError(
                "UDP relay received an unexpected transport type from asyncio.",
                details={
                    "host": self._bind_host,
                    "port": 0,
                    "url": f"udp://{self._bind_host}:0",
                },
                hint="This is an asyncio internal error. Please report it.",
            )

        self._transport = transport
        self._local_port = transport.get_extra_info("sockname")[1]
        _log.debug("udp relay bound on %s:%d", self._bind_host, self._local_port)

    def close(self) -> None:
        """Close the relay and unblock any coroutine awaiting :meth:`recv`.

        Idempotent.
        """
        if self._closed:
            return
        self._closed = True
        _log.debug("udp relay closing (port=%d)", self._local_port)

        if self._active_metric_emitted:
            metrics_gauge_dec("socks5.udp.relays_active")
            self._active_metric_emitted = False

        if self._transport is not None:
            self._transport.close()

        if self._close_event is not None:
            self._close_event.set()

        if self._close_wait_task is not None and not self._close_wait_task.done():
            self._close_wait_task.cancel()
            self._close_wait_task = None

    # ── Inbound path (client → session) ───────────────────────────────────────

    def on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Parse the SOCKS5 UDP header and enqueue the payload.

        Called by :class:`RelayDatagramProtocol`; must never raise.

        Args:
            data: Raw datagram bytes received from the client.
            addr: Source ``(host, port)`` of the datagram.
        """
        if self._closed or self._queue is None:
            return

        if not self._check_and_bind_client(addr):
            return

        if len(data) > MAX_UDP_PAYLOAD_BYTES:
            metrics_inc("socks5.udp.datagrams_dropped")
            metrics_inc("socks5.udp.datagrams_oversized")
            _log.debug(
                "udp relay dropping oversized datagram from %s:%d (%d bytes > %d)",
                addr[0],
                addr[1],
                len(data),
                MAX_UDP_PAYLOAD_BYTES,
            )
            return

        try:
            payload, host, port = parse_udp_header(data)
        except ProtocolError as exc:
            _log.debug(
                "udp relay dropping malformed datagram from %s:%d [%s]: %s",
                addr[0],
                addr[1],
                exc.error_code,
                exc.message,
            )
            return
        if not payload:
            metrics_inc("socks5.udp.datagrams_dropped")
            metrics_inc("socks5.udp.datagrams_empty")
            _log.debug(
                "udp relay dropping zero-length payload from %s:%d",
                addr[0],
                addr[1],
            )
            return
        if len(payload) > self._max_payload_bytes:
            metrics_inc("socks5.udp.datagrams_dropped")
            metrics_inc("socks5.udp.datagrams_payload_cap")
            _log.debug(
                "udp relay dropping payload from %s:%d (%d bytes > cap %d)",
                addr[0],
                addr[1],
                len(payload),
                self._max_payload_bytes,
            )
            return

        try:
            self._queue.put_nowait((payload, host, port))
        except asyncio.QueueFull:
            self._record_queue_drop()
            return

        self._accepted_count += 1
        metrics_inc("socks5.udp.datagrams_accepted")
        metrics_observe("socks5.udp.datagram_size", len(data))

    def _check_and_bind_client(self, addr: tuple[str, int]) -> bool:
        """Admit *addr* as the client or reject it as foreign.

        Args:
            addr: Source address of the incoming datagram.

        Returns:
            ``True`` if the datagram should be processed further;
            ``False`` if it has been dropped as foreign.
        """
        addr_key = self._addr_key(addr)

        if self._client_addr is None:
            if (
                self._expected_client_addr is not None
                and addr_key != self._expected_client_addr
            ):
                self._record_foreign_drop(addr, pre_bind=True)
                return False

            if (
                self._expected_client_host is not None
                and addr_key[0] != self._expected_client_host
            ):
                self._record_foreign_drop(addr, pre_bind=True)
                return False

            self._client_addr = addr
            _log.debug(
                "udp relay client bound to %s:%d (port=%d)",
                addr[0],
                addr[1],
                self._local_port,
            )
            return True

        if addr_key != self._addr_key(self._client_addr):
            self._record_foreign_drop(addr, pre_bind=False)
            return False
        return True

    def _record_foreign_drop(self, addr: tuple[str, int], *, pre_bind: bool) -> None:
        """Increment foreign-client metrics and emit a throttled warning.

        Args:
            addr: Source address of the dropped datagram.
            pre_bind: ``True`` if the drop occurred before any client
                was bound (unexpected hint mismatch); ``False`` if a
                different address arrived after binding.
        """
        self._foreign_client_count += 1
        metrics_inc("socks5.udp.foreign_client_drops")

        if pre_bind and self._expected_client_addr is not None:
            _log.debug(
                "udp relay dropping datagram from %s:%d before client bound "
                "(expected hint %s:%d)",
                addr[0],
                addr[1],
                self._expected_client_addr[0],
                self._expected_client_addr[1],
            )
            return

        if self._client_addr is None:
            return

        if (
            self._foreign_client_count == 1
            or self._foreign_client_count % self._drop_warn_interval == 0
        ):
            _log.warning(
                "udp relay dropping datagram from unexpected client %s:%d "
                "(expected %s:%d, total_foreign_drops=%d)",
                addr[0],
                addr[1],
                self._client_addr[0],
                self._client_addr[1],
                self._foreign_client_count,
            )

    def _record_queue_drop(self) -> None:
        """Increment queue-drop metrics and emit a throttled warning."""
        self._drop_count += 1
        metrics_inc("socks5.udp.datagrams_dropped")
        if self._drop_count == 1 or self._drop_count % self._drop_warn_interval == 0:
            _log.warning(
                "udp relay inbound queue full, dropping datagram "
                "(total_queue_drops=%d, port=%d)",
                self._drop_count,
                self._local_port,
            )

    async def recv(self) -> tuple[bytes, str, int] | None:
        """Return the next ``(payload, host, port)`` tuple, or ``None`` on close.

        Blocks until a datagram is available or the relay closes. A
        final drain of any queued items is attempted after the close
        signal fires.

        Returns:
            A ``(payload, host, port)`` tuple, or ``None`` when the
            relay has closed and the inbound queue is empty.

        Raises:
            RuntimeError: If called before :meth:`start`.
        """
        if self._queue is None or self._close_event is None:
            raise RuntimeError(
                "UDPRelay.recv() called before start(). "
                "Await UDPRelay.start() before calling recv()."
            )

        with contextlib.suppress(asyncio.QueueEmpty):
            return self._queue.get_nowait()

        if self._close_event.is_set():
            with contextlib.suppress(asyncio.QueueEmpty):
                return self._queue.get_nowait()
            return None

        return await self._race_get_vs_close()

    async def _race_get_vs_close(self) -> tuple[bytes, str, int] | None:
        """Await the next queue item or the close signal, whichever fires first.

        ``_close_wait_task`` is reused across iterations to avoid
        per-call task-allocation churn on idle relays.

        Returns:
            A queued datagram, or ``None`` if close raced ahead.
        """
        assert self._queue is not None  # noqa: S101 — invariant checked by caller
        assert self._close_event is not None  # noqa: S101 — invariant checked by caller

        queue_task: asyncio.Task[tuple[bytes, str, int]] = asyncio.create_task(
            self._queue.get()
        )
        if self._close_wait_task is None or self._close_wait_task.done():
            # _close_wait_task is Task[None] | None, but Event.wait() returns
            # Task[Literal[True]]. This is a harmless type mismatch.
            self._close_wait_task = asyncio.create_task(self._close_event.wait())  # type: ignore[assignment]
        close_task = self._close_wait_task

        try:
            done, _ = await asyncio.wait(
                [queue_task, close_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            queue_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await queue_task
            raise

        if queue_task in done and not queue_task.cancelled():
            return queue_task.result()

        queue_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await queue_task

        with contextlib.suppress(asyncio.QueueEmpty):
            return self._queue.get_nowait()
        return None

    # ── Outbound path (session → client) ──────────────────────────────────────

    def send_to_client(self, payload: bytes, src_host: str, src_port: int) -> None:
        """Wrap *payload* in a SOCKS5 UDP header and send it to the client.

        ``src_host`` may be IPv4, IPv6, or a SOCKS5 DOMAIN address. RFC 1928
        UDP headers carry the same ATYP forms as TCP request addresses.

        Args:
            payload: Raw datagram bytes to deliver to the client.
            src_host: Source IPv4, IPv6, or domain string.
            src_port: Source port in ``[0, 65535]``.

        Raises:
            TransportError: If *payload* is not bytes.
        """
        if not isinstance(payload, bytes):
            raise TransportError(
                f"send_to_client() requires a bytes payload, "
                f"got {type(payload).__name__!r}.",
                error_code="proxy.udp_send.invalid_payload_type",
                details={"field": "payload", "expected": "bytes"},
                hint="This is a caller bug in the session layer.",
            )

        try:
            header = build_udp_header_for_host(src_host, src_port)
        except (ConfigurationError, ProtocolError, UnicodeEncodeError) as exc:
            metrics_inc("socks5.udp.reply_dropped", reason="bad_source_host")
            _log.debug(
                "udp relay: dropping reply with invalid src_host=%r src_port=%d: %s",
                src_host,
                src_port,
                exc,
            )
            return

        datagram = header + payload
        if len(datagram) > MAX_UDP_PAYLOAD_BYTES:
            self._send_drop_count += 1
            metrics_inc("socks5.udp.reply_dropped", reason="oversized")
            if (
                self._send_drop_count == 1
                or self._send_drop_count % self._drop_warn_interval == 0
            ):
                _log.warning(
                    "udp relay dropping oversized reply for %s:%d "
                    "(datagram=%d bytes > %d, drops=%d, port=%d)",
                    src_host,
                    src_port,
                    len(datagram),
                    MAX_UDP_PAYLOAD_BYTES,
                    self._send_drop_count,
                    self._local_port,
                )
            return

        if len(payload) > self._max_payload_bytes:
            self._send_drop_count += 1
            metrics_inc("socks5.udp.reply_dropped", reason="payload_cap")
            if (
                self._send_drop_count == 1
                or self._send_drop_count % self._drop_warn_interval == 0
            ):
                _log.warning(
                    "udp relay dropping reply for %s:%d (%d bytes > cap %d, drops=%d)",
                    src_host,
                    src_port,
                    len(payload),
                    self._max_payload_bytes,
                    self._send_drop_count,
                )
            return

        if self._transport is None or self._closed:
            return

        if self._client_addr is None:
            _log.debug(
                "udp relay: dropping reply for %s:%d — client address not yet "
                "bound (port=%d)",
                src_host,
                src_port,
                self._local_port,
            )
            return

        with contextlib.suppress(OSError):
            self._transport.sendto(datagram, self._client_addr)

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def local_port(self) -> int:
        """The ephemeral port the relay is bound to."""
        return self._local_port

    @property
    def bind_host(self) -> str:
        """The local UDP bind host."""
        return self._bind_host

    @property
    def advertise_host(self) -> str:
        """The UDP address to advertise in the SOCKS5 UDP_ASSOCIATE reply."""
        return self._advertise_host

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
