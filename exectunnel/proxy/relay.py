"""UDP relay helper for SOCKS5 UDP ASSOCIATE."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
import struct
from typing import override

from exectunnel.config.defaults import UDP_SEND_QUEUE_CAP, UDP_WARN_EVERY
from exectunnel.exceptions import (
    FrameDecodingError,
    ProtocolError,
    TransportError,
)
from exectunnel.observability import metrics_inc
from exectunnel.protocol.enums import AddrType

logger = logging.getLogger("exectunnel.proxy")


class UdpRelay:
    """
    Wraps a local UDP socket that the SOCKS5 client sends datagrams to.

    Strips/adds SOCKS5 UDP headers (RFC 1928 §7).  The socket is bound to an
    ephemeral port on ``127.0.0.1``; the actual bound port is returned by
    :meth:`start`.
    """

    def __init__(self) -> None:
        self._transport: asyncio.DatagramTransport | None = None
        self._queue: asyncio.Queue[tuple[bytes, str, int] | None] = asyncio.Queue(
            UDP_SEND_QUEUE_CAP
        )
        self._local_port: int = 0
        self._closed: bool = False
        self._client_addr: tuple[str, int] | None = None
        self._drop_count: int = 0
        self._foreign_client_count: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> int:
        """Bind the UDP socket and return the local port.

        Raises
        ------
        TransportError
            If the OS refuses to create or bind the datagram endpoint (e.g.
            ephemeral port range exhausted, insufficient permissions).
        """
        loop = asyncio.get_running_loop()

        class _Proto(asyncio.DatagramProtocol):
            def __init__(self, relay: UdpRelay) -> None:
                self._r = relay

            @override
            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                self._r._on_datagram(data, addr)

            @override
            def error_received(self, exc: Exception) -> None:
                # OS-level socket errors are not actionable per-datagram;
                # count them and log at DEBUG.
                metrics_inc("socks5.udp_relay.socket_error")
                logger.debug("udp relay socket error: %s (%s)", exc, type(exc).__name__)

        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _Proto(self),
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

        assert isinstance(transport, asyncio.DatagramTransport)
        self._transport = transport
        self._local_port = transport.get_extra_info("sockname")[1]
        metrics_inc("socks5.udp_relay.started")
        return self._local_port

    def close(self) -> None:
        """Close the relay and unblock any coroutine awaiting :meth:`recv`."""
        if self._closed:
            return
        self._closed = True
        metrics_inc("socks5.udp_relay.closed")
        if self._transport:
            self._transport.close()
        # Unblock any coroutine awaiting recv() with the None sentinel.
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)

    # ── Inbound (client → relay) ──────────────────────────────────────────────

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Parse the SOCKS5 UDP header and enqueue ``(payload, dst_host, dst_port)``.

        Malformed datagrams are silently dropped with a metric — they arrive
        from an untrusted UDP client and must never raise into the event loop.
        Structured exceptions are constructed locally for logging/metrics but
        are always suppressed here; the caller (asyncio) cannot handle them.
        """
        if self._closed:
            return

        # ── Client binding ────────────────────────────────────────────────────
        if self._client_addr is None:
            self._client_addr = addr
            metrics_inc("socks5.udp_relay.client_bound")
        elif addr != self._client_addr:
            self._foreign_client_count += 1
            metrics_inc("socks5.udp_relay.foreign_client_drop")
            if (
                self._foreign_client_count == 1
                or self._foreign_client_count % UDP_WARN_EVERY == 0
            ):
                logger.warning(
                    "udp relay dropping datagram from unexpected client %s:%d "
                    "(expected %s:%d, count=%d)",
                    addr[0],
                    addr[1],
                    self._client_addr[0],
                    self._client_addr[1],
                    self._foreign_client_count,
                )
            return

        # ── Parse SOCKS5 UDP header (RFC 1928 §7) ─────────────────────────────
        # RSV(2) + FRAG(1) + ATYP(1) + addr + port(2) + data
        try:
            payload, host, port = self._parse_udp_header(data)
        except FrameDecodingError as exc:
            # Malformed header from an untrusted client — drop silently with
            # a metric.  The error_code is used as the metric label so
            # operators can distinguish fragmented vs. truncated vs. bad-ATYP
            # datagrams in their dashboards without reading log lines.
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
            metrics_inc("socks5.udp_relay.datagram.accepted")
        except asyncio.QueueFull:
            self._drop_count += 1
            metrics_inc("socks5.udp_relay.queue_drop")
            if self._drop_count == 1 or self._drop_count % UDP_WARN_EVERY == 0:
                logger.warning(
                    "udp relay inbound queue full, dropping datagram (drops=%d)",
                    self._drop_count,
                )

    def _parse_udp_header(self, data: bytes) -> tuple[bytes, str, int]:
        """Parse a SOCKS5 UDP datagram header and return ``(payload, host, port)``.

        Parameters
        ----------
        data:
            Raw bytes received from the SOCKS5 client, including the
            RFC 1928 §7 header.

        Raises
        ------
        FrameDecodingError
            If the datagram is too short, uses an unsupported ATYP, carries a
            non-zero FRAG field, has a zero-length domain, or contains a domain
            name that is not valid UTF-8.
        """
        # Minimum: RSV(2) + FRAG(1) + ATYP(1) = 4 bytes.
        if len(data) < 4:
            raise FrameDecodingError(
                f"SOCKS5 UDP datagram too short: {len(data)} byte(s), minimum is 4.",
                error_code="protocol.socks5_udp_too_short",
                details={"received_bytes": len(data), "minimum_bytes": 4},
                hint="The SOCKS5 client sent a datagram shorter than the minimum header size.",
            )

        # FRAG != 0 means reassembly is required; we do not support fragmentation.
        if data[2] != 0:
            raise FrameDecodingError(
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
                raise FrameDecodingError(
                    "SOCKS5 UDP IPv4 datagram truncated before address+port.",
                    error_code="protocol.socks5_udp_ipv4_truncated",
                    details={
                        "received_bytes": len(data),
                        "required_bytes": offset + 4 + 2,
                    },
                    hint="The SOCKS5 client sent an incomplete IPv4 address field.",
                )
            try:
                host = socket.inet_ntop(socket.AF_INET, data[offset : offset + 4])
            except OSError as exc:
                raise FrameDecodingError(
                    "Failed to parse IPv4 address bytes in SOCKS5 UDP header.",
                    error_code="protocol.socks5_udp_bad_ipv4",
                    details={"raw_bytes": data[offset : offset + 4].hex()},
                    hint="The SOCKS5 client sent a malformed 4-byte IPv4 address.",
                ) from exc
            offset += 4

        elif atyp == AddrType.IPV6:
            if len(data) < offset + 16 + 2:
                raise FrameDecodingError(
                    "SOCKS5 UDP IPv6 datagram truncated before address+port.",
                    error_code="protocol.socks5_udp_ipv6_truncated",
                    details={
                        "received_bytes": len(data),
                        "required_bytes": offset + 16 + 2,
                    },
                    hint="The SOCKS5 client sent an incomplete IPv6 address field.",
                )
            try:
                host = socket.inet_ntop(socket.AF_INET6, data[offset : offset + 16])
            except OSError as exc:
                raise FrameDecodingError(
                    "Failed to parse IPv6 address bytes in SOCKS5 UDP header.",
                    error_code="protocol.socks5_udp_bad_ipv6",
                    details={"raw_bytes": data[offset : offset + 16].hex()},
                    hint="The SOCKS5 client sent a malformed 16-byte IPv6 address.",
                ) from exc
            offset += 16

        elif atyp == AddrType.DOMAIN:
            if len(data) < offset + 1:
                raise FrameDecodingError(
                    "SOCKS5 UDP DOMAIN datagram truncated before length byte.",
                    error_code="protocol.socks5_udp_domain_no_length",
                    details={"received_bytes": len(data)},
                    hint="The SOCKS5 client sent a DOMAIN header with no length byte.",
                )
            dlen = data[offset]
            offset += 1
            if dlen == 0:
                raise FrameDecodingError(
                    "SOCKS5 UDP DOMAIN address length must be greater than zero.",
                    error_code="protocol.socks5_udp_domain_zero_length",
                    details={"atyp": hex(atyp)},
                    hint=(
                        "The SOCKS5 client sent a zero-length domain name, which "
                        "violates RFC 1928 §7."
                    ),
                )
            if len(data) < offset + dlen + 2:
                raise FrameDecodingError(
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
                host = data[offset : offset + dlen].decode()
            except UnicodeDecodeError as exc:
                raise FrameDecodingError(
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
            offset += dlen

        else:
            raise FrameDecodingError(
                f"Unsupported SOCKS5 UDP address type: {atyp:#x}.",
                error_code="protocol.socks5_udp_unsupported_atyp",
                details={"atyp": hex(atyp)},
                hint=(
                    "Only ATYP 0x01 (IPv4), 0x03 (DOMAIN), and 0x04 (IPv6) are "
                    "supported per RFC 1928 §7."
                ),
            )

        if len(data) < offset + 2:
            raise FrameDecodingError(
                "SOCKS5 UDP datagram truncated before port field.",
                error_code="protocol.socks5_udp_no_port",
                details={
                    "received_bytes": len(data),
                    "required_bytes": offset + 2,
                },
                hint="The SOCKS5 client sent a datagram with no port field.",
            )

        port = struct.unpack("!H", data[offset : offset + 2])[0]
        payload = data[offset + 2 :]
        return payload, host, port

    # ── Outbound (relay → client) ─────────────────────────────────────────────

    async def recv(self) -> tuple[bytes, str, int] | None:
        """Return the next ``(payload, host, port)`` from the SOCKS5 client.

        Returns ``None`` when the relay has been closed.

        Raises
        ------
        TransportError
            If an unexpected error occurs while awaiting the inbound queue
            (e.g. event-loop shutdown).
        """
        try:
            return await self._queue.get()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TransportError(
                "Unexpected error while awaiting the UDP relay inbound queue.",
                error_code="transport.udp_relay_recv_failed",
                details={"local_port": self._local_port},
                hint="This may indicate an event-loop shutdown or abnormal task cancellation.",
            ) from exc

    def send_to_client(self, payload: bytes, src_host: str, src_port: int) -> None:
        """Wrap *payload* in a SOCKS5 UDP header and send it back to the client.

        Non-IP ``src_host`` values are dropped with a debug log — RFC 1928 §7
        requires ``BND.ADDR`` in UDP replies to be an IP address.

        Raises
        ------
        ProtocolError
            If *payload* is not a ``bytes`` instance, indicating a caller bug.
        """
        if not isinstance(payload, bytes):
            raise ProtocolError(
                f"send_to_client() received a non-bytes payload: {type(payload).__name__!r}.",
                error_code="protocol.udp_relay_send_bad_type",
                details={
                    "received_type": type(payload).__name__,
                    "src_host": src_host,
                    "src_port": src_port,
                },
                hint="Ensure callers always pass raw bytes to send_to_client().",
            )

        if self._transport is None or self._closed or self._client_addr is None:
            return

        try:
            addr = ipaddress.ip_address(src_host)
        except ValueError:
            # RFC 1928 §7: BND.ADDR in UDP replies must be an IP address.
            # Domain names are not valid here — drop the reply silently.
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
    def is_running(self) -> bool:
        """``True`` once :meth:`start` has completed and before :meth:`close` is called."""
        return self._transport is not None and not self._closed
