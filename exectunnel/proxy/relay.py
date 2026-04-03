"""UDP relay helper for SOCKS5 UDP ASSOCIATE."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import struct
from typing import Final

from exectunnel.config.defaults import UDP_RELAY_QUEUE_CAP, UDP_WARN_EVERY
from exectunnel.exceptions import ProtocolError, TransportError
from exectunnel.observability import metrics_inc
from exectunnel.protocol.enums import AddrType
from exectunnel.proxy._codec import validate_domain

__all__ = ["UdpRelay"]

logger = logging.getLogger(__name__)

# Maximum UDP payload accepted from the SOCKS5 client.
# 65507 = 65535 − 20 (IP header) − 8 (UDP header).
# Anything larger is physically impossible over standard IPv4.
MAX_UDP_PAYLOAD_BYTES: Final[int] = 65_507


# ── UDP header parser ─────────────────────────────────────────────────────────
# Defined before UdpRelay so _on_datagram can reference it without a forward
# reference and so it can be unit-tested independently of the relay lifecycle.


def _parse_udp_header(data: bytes) -> tuple[bytes, str, int]:
    """Parse a SOCKS5 UDP datagram header and return ``(payload, host, port)``.

    Args:
        data: Raw bytes received from the SOCKS5 client, including the
              RFC 1928 §7 header.

    Returns:
        A ``(payload, host, port)`` tuple where *host* is a normalised IP or
        domain string and *port* is an integer in ``[1, 65535]``.

    Raises:
        ProtocolError:
            If the datagram is too short, uses an unsupported ATYP, carries a
            non-zero FRAG field, has a zero-length domain, contains a domain
            that is not valid UTF-8 or fails RFC 1123 / safety validation,
            or has port 0.
    """
    # Minimum: RSV(2) + FRAG(1) + ATYP(1) = 4 bytes.
    if len(data) < 4:
        raise ProtocolError(
            f"SOCKS5 UDP datagram too short: {len(data)} byte(s), minimum is 4.",
            error_code="protocol.socks5_udp_too_short",
            details={"received_bytes": len(data), "minimum_bytes": 4},
            hint="The SOCKS5 client sent a datagram shorter than the minimum header size.",
        )

    # FRAG != 0 means reassembly is required; we do not support fragmentation.
    if data[2] != 0:
        raise ProtocolError(
            f"SOCKS5 UDP fragmentation is not supported (FRAG={data[2]:#x}).",
            error_code="protocol.socks5_udp_fragmented",
            details={"frag": data[2]},
            hint=(
                "The SOCKS5 client requested UDP fragment reassembly, which "
                "exectunnel does not support. Disable fragmentation on the client."
            ),
        )

    atyp = data[3]
    offset = 4

    if atyp == AddrType.IPV4:
        if len(data) < offset + 4 + 2:
            raise ProtocolError(
                "SOCKS5 UDP IPv4 datagram truncated before address+port.",
                error_code="protocol.socks5_udp_ipv4_truncated",
                details={
                    "received_bytes": len(data),
                    "required_bytes": offset + 4 + 2,
                },
                hint="The SOCKS5 client sent an incomplete IPv4 address field.",
            )
        # IPv4Address(bytes[4]) never raises ValueError.
        host = str(ipaddress.IPv4Address(data[offset : offset + 4]))
        offset += 4

    elif atyp == AddrType.IPV6:
        if len(data) < offset + 16 + 2:
            raise ProtocolError(
                "SOCKS5 UDP IPv6 datagram truncated before address+port.",
                error_code="protocol.socks5_udp_ipv6_truncated",
                details={
                    "received_bytes": len(data),
                    "required_bytes": offset + 16 + 2,
                },
                hint="The SOCKS5 client sent an incomplete IPv6 address field.",
            )
        # IPv6Address(bytes[16]) never raises ValueError.
        host = str(ipaddress.IPv6Address(data[offset : offset + 16]).compressed)
        offset += 16

    elif atyp == AddrType.DOMAIN:
        if len(data) < offset + 1:
            raise ProtocolError(
                "SOCKS5 UDP DOMAIN datagram truncated before length byte.",
                error_code="protocol.socks5_udp_domain_no_length",
                details={"received_bytes": len(data)},
                hint="The SOCKS5 client sent a DOMAIN header with no length byte.",
            )
        dlen = data[offset]
        offset += 1
        if dlen == 0:
            raise ProtocolError(
                "SOCKS5 UDP DOMAIN address length must be greater than zero.",
                error_code="protocol.socks5_udp_domain_zero_length",
                details={"atyp": hex(atyp)},
                hint=(
                    "The SOCKS5 client sent a zero-length domain name, which "
                    "violates RFC 1928 §7."
                ),
            )
        if len(data) < offset + dlen + 2:
            raise ProtocolError(
                "SOCKS5 UDP DOMAIN datagram truncated before domain bytes+port.",
                error_code="protocol.socks5_udp_domain_truncated",
                details={
                    "received_bytes": len(data),
                    "required_bytes": offset + dlen + 2,
                    "declared_length": dlen,
                },
                hint="The SOCKS5 client sent a domain name shorter than its declared length.",
            )
        try:
            host = data[offset : offset + dlen].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(
                "SOCKS5 UDP DOMAIN address bytes are not valid UTF-8.",
                error_code="protocol.socks5_udp_domain_bad_encoding",
                details={
                    "raw_bytes": data[offset : offset + dlen].hex(),
                    "declared_length": dlen,
                },
                hint=(
                    "The SOCKS5 client sent a domain name that cannot be decoded "
                    "as UTF-8. Only ASCII/UTF-8 hostnames are supported."
                ),
            ) from exc
        # Validate domain structure and safety — same rules as TCP path.
        validate_domain(host)
        offset += dlen

    else:
        raise ProtocolError(
            f"Unsupported SOCKS5 UDP address type: {atyp:#x}.",
            error_code="protocol.socks5_udp_unsupported_atyp",
            details={"atyp": hex(atyp)},
            hint=(
                "Only ATYP 0x01 (IPv4), 0x03 (DOMAIN), and 0x04 (IPv6) are "
                "supported per RFC 1928 §7."
            ),
        )

    if len(data) < offset + 2:
        raise ProtocolError(
            "SOCKS5 UDP datagram truncated before port field.",
            error_code="protocol.socks5_udp_no_port",
            details={
                "received_bytes": len(data),
                "required_bytes": offset + 2,
            },
            hint="The SOCKS5 client sent a datagram with no port field.",
        )

    port = struct.unpack("!H", data[offset : offset + 2])[0]
    if port == 0:
        raise ProtocolError(
            "SOCKS5 UDP datagram destination port is 0.",
            error_code="protocol.socks5_udp_zero_port",
            details={"host": host},
            hint="Port 0 is not a valid destination port in a SOCKS5 UDP datagram.",
        )

    payload = data[offset + 2 :]
    return payload, host, port


# ── Datagram protocol ─────────────────────────────────────────────────────────


class _RelayDatagramProtocol(asyncio.DatagramProtocol):
    """asyncio datagram protocol that forwards received datagrams to a :class:`UdpRelay`."""

    def __init__(self, relay: UdpRelay) -> None:
        self._relay = relay

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        # Delegate to the package-internal method — avoids SLF001 suppression.
        self._relay._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        # OS-level socket errors (e.g. ICMP port-unreachable) are not
        # actionable per-datagram; count them and log at DEBUG.
        metrics_inc(
            "socks5.udp_relay.socket_error",
            reason=type(exc).__name__,
        )
        logger.debug(
            "udp relay socket error [%s]: %s",
            type(exc).__name__,
            exc,
        )

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None:
            logger.debug("udp relay connection lost: %s", exc)


# ── UdpRelay ──────────────────────────────────────────────────────────────────


class UdpRelay:
    """Wraps a local UDP socket that the SOCKS5 client sends datagrams to.

    Strips/adds SOCKS5 UDP headers (RFC 1928 §7).  The socket is bound to an
    ephemeral port on ``127.0.0.1``; the actual bound port is returned by
    :meth:`start` and also available via :attr:`local_port`.

    Lifecycle::

        relay = UdpRelay()
        port = await relay.start()          # bind; returns ephemeral port
        item = await relay.recv()           # (payload, host, port) | None
        relay.send_to_client(data, h, p)    # wrap + send back to client
        relay.close()                       # tear down

    Async context manager usage::

        async with UdpRelay() as relay:
            port = relay.local_port
            ...
    """

    def __init__(self) -> None:
        self._transport: asyncio.DatagramTransport | None = None

        # Bounded inbound queue.  The close sentinel is delivered via a
        # dedicated asyncio.Event so close() always unblocks recv() even
        # when the inbound queue is completely full.
        self._queue: asyncio.Queue[tuple[bytes, str, int]] = asyncio.Queue(
            UDP_RELAY_QUEUE_CAP
        )
        self._close_event: asyncio.Event = asyncio.Event()

        self._local_port: int = 0
        self._closed: bool = False
        self._started: bool = False

        # Address of the first SOCKS5 client that sends a datagram.
        self._client_addr: tuple[str, int] | None = None
        # Optional hint from the UDP_ASSOCIATE request (may be 0.0.0.0:0).
        self._expected_client_addr: tuple[str, int] | None = None

        # Telemetry counters.
        self._drop_count: int = 0
        self._foreign_client_count: int = 0
        self._accepted_count: int = 0

    # ── Async context manager ─────────────────────────────────────────────────

    async def __aenter__(self) -> UdpRelay:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        self.close()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(
        self,
        expected_client_addr: tuple[str, int] | None = None,
    ) -> int:
        """Bind the UDP socket and return the local port.

        Args:
            expected_client_addr: Optional ``(host, port)`` hint from the
                ``UDP_ASSOCIATE`` request.  When the host is not ``"0.0.0.0"``
                or ``"::"`` and the port is non-zero, datagrams from other
                addresses are rejected immediately rather than waiting for the
                first datagram to bind the client address.

        Returns:
            The ephemeral port the relay is bound to on ``127.0.0.1``.

        Raises:
            RuntimeError:   If :meth:`start` has already been called.
            TransportError: If the OS refuses to create or bind the datagram
                endpoint (e.g. ephemeral port range exhausted, insufficient
                permissions).
        """
        if self._started:
            raise RuntimeError(
                "UdpRelay.start() has already been called. "
                "Create a new UdpRelay instance for each UDP_ASSOCIATE session."
            )
        self._started = True

        # Record the client hint only when it carries a meaningful address.
        if expected_client_addr is not None:
            host, port = expected_client_addr
            try:
                addr = ipaddress.ip_address(host)
                is_unspecified = addr.is_unspecified
            except ValueError:
                is_unspecified = True
            if not is_unspecified and port != 0:
                self._expected_client_addr = expected_client_addr

        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _RelayDatagramProtocol(self),
                local_addr=("127.0.0.1", 0),
            )
        except Exception as exc:
            raise TransportError(
                "UDP relay failed to bind a local datagram endpoint.",
                error_code="transport.udp_relay_bind_failed",
                details={"local_addr": "127.0.0.1:0"},
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
                error_code="transport.udp_relay_bad_transport_type",
                details={"transport_type": type(transport).__name__},
                hint="This is an asyncio internal error. Please report it.",
            )

        self._transport = transport
        self._local_port = transport.get_extra_info("sockname")[1]
        metrics_inc("socks5.udp_relay.started")
        logger.debug("udp relay bound on 127.0.0.1:%d", self._local_port)
        return self._local_port

    def close(self) -> None:
        """Close the relay and unblock any coroutine awaiting :meth:`recv`.

        Idempotent — safe to call multiple times.

        The close sentinel is delivered via a dedicated :class:`asyncio.Event`
        rather than a queue item, so it is guaranteed to unblock :meth:`recv`
        even when the inbound queue is completely full.
        """
        if self._closed:
            return
        self._closed = True
        metrics_inc("socks5.udp_relay.closed")
        logger.debug("udp relay closing (port=%d)", self._local_port)

        if self._transport is not None:
            self._transport.close()

        # Signal recv() to return None — always succeeds regardless of queue state.
        self._close_event.set()

    # ── Inbound (client → relay) ──────────────────────────────────────────────

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Parse the SOCKS5 UDP header and enqueue ``(payload, dst_host, dst_port)``.

        Malformed datagrams are silently dropped with a metric — they arrive
        from an untrusted UDP client and must never raise into the event loop.
        """
        if self._closed:
            return

        # ── Datagram size guard ───────────────────────────────────────────────
        if len(data) > MAX_UDP_PAYLOAD_BYTES:
            metrics_inc("socks5.udp_relay.oversized_drop")
            logger.debug(
                "udp relay dropping oversized datagram from %s:%d (%d bytes > %d)",
                addr[0],
                addr[1],
                len(data),
                MAX_UDP_PAYLOAD_BYTES,
            )
            return

        # ── Client address binding / filtering ────────────────────────────────
        if self._client_addr is None:
            if (
                self._expected_client_addr is not None
                and addr != self._expected_client_addr
            ):
                self._foreign_client_count += 1
                metrics_inc("socks5.udp_relay.foreign_client_drop")
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
            metrics_inc("socks5.udp_relay.client_bound")
            logger.debug(
                "udp relay client bound to %s:%d (port=%d)",
                addr[0],
                addr[1],
                self._local_port,
            )
        elif addr != self._client_addr:
            self._foreign_client_count += 1
            metrics_inc("socks5.udp_relay.foreign_client_drop")
            if (
                self._foreign_client_count == 1
                or self._foreign_client_count % UDP_WARN_EVERY == 0
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

        # ── Parse SOCKS5 UDP header (RFC 1928 §7) ─────────────────────────────
        try:
            payload, host, port = _parse_udp_header(data)
        except ProtocolError as exc:
            metrics_inc(
                "socks5.udp_relay.header_parse_error",
                reason=exc.error_code.split(".")[-1],
            )
            logger.debug(
                "udp relay dropping malformed datagram from %s:%d [%s]: %s",
                addr[0],
                addr[1],
                exc.error_code,
                exc.message,
            )
            return

        # ── Enqueue ───────────────────────────────────────────────────────────
        try:
            self._queue.put_nowait((payload, host, port))
            self._accepted_count += 1
            metrics_inc("socks5.udp_relay.datagram.accepted")
        except asyncio.QueueFull:
            self._drop_count += 1
            metrics_inc("socks5.udp_relay.queue_drop")
            if self._drop_count == 1 or self._drop_count % UDP_WARN_EVERY == 0:
                logger.warning(
                    "udp relay inbound queue full, dropping datagram "
                    "(total_queue_drops=%d, port=%d)",
                    self._drop_count,
                    self._local_port,
                )

    # ── Outbound (relay → client) ─────────────────────────────────────────────

    async def recv(self) -> tuple[bytes, str, int] | None:
        """Return the next ``(payload, host, port)`` from the SOCKS5 client.

        Blocks until a datagram is available or the relay is closed.

        The close sentinel is delivered via an :class:`asyncio.Event` that
        races against the queue, so this method always returns promptly after
        :meth:`close` is called — even when the inbound queue is full.

        After the close event fires, the queue is drained one final time so
        that no datagram is lost when :meth:`close` races with a queued item.

        Returns:
            A ``(payload, host, port)`` tuple, or ``None`` when the relay has
            been closed and the queue is fully drained.

        Raises:
            asyncio.CancelledError: Propagated as-is — never suppressed.
        """
        # Fast path: drain any immediately available item first.
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

        # If already closed and queue is empty, signal EOF.
        if self._close_event.is_set():
            return None

        # Race the queue against the close event.
        queue_task: asyncio.Task[tuple[bytes, str, int]] = asyncio.create_task(
            self._queue.get(),
            name=f"udp-relay-recv-{self._local_port}",
        )
        close_task: asyncio.Task[None] = asyncio.create_task(
            self._close_event.wait(),
            name=f"udp-relay-close-{self._local_port}",
        )

        try:
            done, pending = await asyncio.wait(
                {queue_task, close_task},
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

        # Cancel the loser.
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # queue_task won — return the item.
        if queue_task in done and not queue_task.cancelled():
            return queue_task.result()

        # close_task won — drain one final time to avoid losing the last item
        # if a datagram arrived between the close signal and task cancellation.
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

        return None

    def send_to_client(self, payload: bytes, src_host: str, src_port: int) -> None:
        """Wrap *payload* in a SOCKS5 UDP header and send it back to the client.

        Non-IP ``src_host`` values are dropped with a debug log — RFC 1928 §7
        requires ``BND.ADDR`` in UDP replies to be an IP address.

        If the client address has not yet been bound (no datagram received),
        the packet is dropped and logged at DEBUG level.

        Args:
            payload:  Raw datagram bytes to wrap and forward.
            src_host: Source IP address string (IPv4 or IPv6).
            src_port: Source port number in ``[0, 65535]``.

        Raises:
            TypeError:
                If *payload* is not :class:`bytes` — indicates a caller bug.
            ValueError:
                If *src_port* is outside ``[0, 65535]`` — indicates a caller
                bug; ``struct.pack`` would otherwise raise an opaque error.
        """
        if not isinstance(payload, bytes):
            raise TypeError(
                f"send_to_client() requires bytes payload, "
                f"got {type(payload).__name__!r}."
            )

        if not (0 <= src_port <= 65_535):
            raise ValueError(
                f"send_to_client() src_port {src_port!r} is out of range [0, 65535]."
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
            metrics_inc("socks5.udp_relay.reply_before_client_bound")
            return

        try:
            addr = ipaddress.ip_address(src_host)
        except ValueError:
            logger.debug(
                "udp relay: dropping reply with non-IP src_host %r (RFC 1928 §7)",
                src_host,
            )
            metrics_inc("socks5.udp_relay.non_ip_src_drop")
            return

        atyp = AddrType.IPV4 if addr.version == 4 else AddrType.IPV6
        header = (
            b"\x00\x00\x00"  # RSV(2) + FRAG(1)
            + bytes([int(atyp)])
            + addr.packed
            + struct.pack("!H", src_port)
        )
        try:
            self._transport.sendto(header + payload, self._client_addr)
        except OSError as exc:
            metrics_inc("socks5.udp_relay.send_error", reason=type(exc).__name__)
            logger.debug(
                "udp relay: send_to_client OSError for %s:%d: %s",
                src_host,
                src_port,
                exc,
            )

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def local_port(self) -> int:
        """The ephemeral port the relay is bound to on ``127.0.0.1``."""
        return self._local_port

    @property
    def drop_count(self) -> int:
        """Total datagrams dropped due to inbound queue saturation."""
        return self._drop_count

    @property
    def foreign_client_count(self) -> int:
        """Total datagrams dropped because they arrived from an unexpected client address."""
        return self._foreign_client_count

    @property
    def accepted_count(self) -> int:
        """Total datagrams successfully enqueued for processing."""
        return self._accepted_count

    @property
    def is_running(self) -> bool:
        """``True`` once :meth:`start` has completed and before :meth:`close` is called."""
        return (
            self._started
            and not self._closed
            and self._transport is not None
            and not self._transport.is_closing()
        )
