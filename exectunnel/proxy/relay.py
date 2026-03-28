"""UDP relay helper for SOCKS5 UDP ASSOCIATE."""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
import struct

from exectunnel.config.defaults import UDP_SEND_QUEUE_CAP, UDP_WARN_EVERY
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
        self._queue: asyncio.Queue[tuple[bytes, str, int]] = asyncio.Queue(
            UDP_SEND_QUEUE_CAP
        )
        self._local_port: int = 0
        self._closed = False
        self._client_addr: tuple[str, int] | None = None
        self._drop_count = 0
        self._foreign_client_count = 0

    async def start(self) -> int:
        """Bind the UDP socket and return the local port."""
        loop = asyncio.get_running_loop()

        class _Proto(asyncio.DatagramProtocol):
            def __init__(self, relay: UdpRelay) -> None:
                self._r = relay

            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                self._r._on_datagram(data, addr)

            def error_received(self, exc: Exception) -> None:
                logger.debug("udp relay error: %s", exc)

        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Proto(self),
            local_addr=("127.0.0.1", 0),
        )
        assert isinstance(transport, asyncio.DatagramTransport)
        self._transport = transport
        self._local_port = transport.get_extra_info("sockname")[1]
        metrics_inc("socks5.udp_relay.started")
        return self._local_port

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Parse the SOCKS5 UDP header and enqueue ``(payload, dst_host, dst_port)``."""
        if self._closed:
            return
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
                    "udp relay dropping datagram from unexpected client %s:%d (expected %s:%d)",
                    addr[0],
                    addr[1],
                    self._client_addr[0],
                    self._client_addr[1],
                )
            return

        # RFC 1928 §7: RSV(2) + FRAG(1) + ATYP(1) + addr + port(2) + data
        if len(data) < 4 or data[2] != 0:  # FRAG != 0 means reassembly required; drop.
            return
        atyp = data[3]
        offset = 4
        if atyp == AddrType.IPV4:
            if len(data) < offset + 4 + 2:
                return
            host = socket.inet_ntop(socket.AF_INET, data[offset : offset + 4])
            offset += 4
        elif atyp == AddrType.IPV6:
            if len(data) < offset + 16 + 2:
                return
            host = socket.inet_ntop(socket.AF_INET6, data[offset : offset + 16])
            offset += 16
        elif atyp == AddrType.DOMAIN:
            if len(data) < offset + 1:
                return
            dlen = data[offset]
            offset += 1
            if dlen == 0:
                return
            if len(data) < offset + dlen + 2:
                return
            try:
                host = data[offset : offset + dlen].decode()
            except UnicodeDecodeError:
                return
            offset += dlen
        else:
            return
        if len(data) < offset + 2:
            return
        port = struct.unpack("!H", data[offset : offset + 2])[0]
        payload = data[offset + 2 :]
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

    async def recv(self) -> tuple[bytes, str, int] | None:
        """Return the next ``(payload, host, port)`` from the SOCKS5 client.

        Returns ``None`` when the relay has been closed.
        """
        item = await self._queue.get()
        return item  # None sentinel is returned as-is to signal closure

    def send_to_client(self, payload: bytes, src_host: str, src_port: int) -> None:
        """Wrap *payload* in a SOCKS5 UDP header and send it back to the client."""
        if self._transport is None or self._closed or self._client_addr is None:
            return
        try:
            addr = ipaddress.ip_address(src_host)
            if addr.version == 4:
                atyp = AddrType.IPV4
                addr_bytes = addr.packed
            else:
                atyp = AddrType.IPV6
                addr_bytes = addr.packed
        except ValueError:
            atyp = AddrType.DOMAIN
            enc = src_host.encode()
            addr_bytes = bytes([len(enc)]) + enc

        header = (
            b"\x00\x00\x00"  # RSV + FRAG
            + bytes([int(atyp)])
            + addr_bytes
            + struct.pack("!H", src_port)
        )
        with contextlib.suppress(OSError):
            self._transport.sendto(header + payload, self._client_addr)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        metrics_inc("socks5.udp_relay.closed")
        if self._transport:
            self._transport.close()
        # Unblock any coroutine awaiting recv().
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)  # type: ignore[arg-type]

    @property
    def local_port(self) -> int:
        return self._local_port
