"""
Minimal SOCKS5 server (RFC 1928) — asyncio, no auth.

Supported commands
------------------
* ``CMD 0x01  CONNECT``       – yields a connected-stream request
* ``CMD 0x03  UDP ASSOCIATE`` – yields a UDP relay request

Usage::

    server = Socks5Server("127.0.0.1", 1080)
    await server.start()
    async for request in server:
        asyncio.create_task(handle(request))
    await server.stop()
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from exectunnel.core.consts import (
    HANDSHAKE_TIMEOUT_SECS,
    UDP_QUEUE_CAP,
    UDP_WARN_EVERY,
    AddrType,
    AuthMethod,
    Cmd,
    Reply,
)
from exectunnel.observability import metrics_inc, metrics_observe, span

logger = logging.getLogger("exectunnel.socks5")


# ── Low-level I/O helpers ─────────────────────────────────────────────────────


async def _read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    return await reader.readexactly(n)


async def _read_addr(reader: asyncio.StreamReader) -> tuple[str, int]:
    """Read ATYP + address + port, return ``(host, port)``."""
    atyp = (await _read_exact(reader, 1))[0]
    if atyp == AddrType.IPV4:
        raw = await _read_exact(reader, 4)
        host = socket.inet_ntop(socket.AF_INET, raw)
    elif atyp == AddrType.IPV6:
        raw = await _read_exact(reader, 16)
        host = socket.inet_ntop(socket.AF_INET6, raw)
    elif atyp == AddrType.DOMAIN:
        length = (await _read_exact(reader, 1))[0]
        if length == 0:
            raise ValueError("DOMAIN length must be > 0")
        raw_host = await _read_exact(reader, length)
        try:
            host = raw_host.decode()
        except UnicodeDecodeError as exc:
            raise ValueError("invalid DOMAIN bytes in SOCKS5 request") from exc
    else:
        raise ValueError(f"unsupported ATYP {atyp:#x}")
    port_raw = await _read_exact(reader, 2)
    port = struct.unpack("!H", port_raw)[0]
    return host, port


def _build_reply(reply: Reply, bind_host: str = "0.0.0.0", bind_port: int = 0) -> bytes:
    """Serialise a SOCKS5 reply packet."""
    if not (0 <= bind_port <= 65535):
        raise ValueError(f"invalid bind_port {bind_port}")
    try:
        addr = ipaddress.ip_address(bind_host)
        if addr.version == 4:
            atyp = AddrType.IPV4
            addr_bytes = addr.packed
        else:
            atyp = AddrType.IPV6
            addr_bytes = addr.packed
    except ValueError:
        atyp = AddrType.DOMAIN
        enc = bind_host.encode()
        if len(enc) > 255:
            raise ValueError("domain reply host exceeds 255 bytes")
        addr_bytes = bytes([len(enc)]) + enc

    return (
        bytes([0x05, int(reply), 0x00, int(atyp)])
        + addr_bytes
        + struct.pack("!H", bind_port)
    )


# ── UDP relay helper ──────────────────────────────────────────────────────────


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
            UDP_QUEUE_CAP
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

    async def recv(self) -> tuple[bytes, str, int]:
        """Return the next ``(payload, host, port)`` from the SOCKS5 client."""
        return await self._queue.get()

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
        self._closed = True
        metrics_inc("socks5.udp_relay.closed")
        if self._transport:
            self._transport.close()

    @property
    def local_port(self) -> int:
        return self._local_port


# ── Request object ────────────────────────────────────────────────────────────


@dataclass
class Socks5Request:
    """
    Represents one completed SOCKS5 handshake.

    The handler must call :meth:`reply_success` **or** :meth:`reply_error`
    exactly once, then ``await writer.drain()`` before transferring data.
    Both reply helpers write to the transport buffer only — the caller is
    responsible for draining.
    """

    cmd: Cmd
    host: str
    port: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    udp_relay: UdpRelay | None = field(default=None)

    @property
    def is_connect(self) -> bool:
        return self.cmd == Cmd.CONNECT

    @property
    def is_udp(self) -> bool:
        return self.cmd == Cmd.UDP_ASSOCIATE

    def reply_success(self, bind_host: str = "127.0.0.1", bind_port: int = 0) -> None:
        """Queue a SUCCESS reply into the writer buffer (caller must drain)."""
        self.writer.write(_build_reply(Reply.SUCCESS, bind_host, bind_port))

    def reply_error(self, reply: Reply = Reply.GENERAL_FAILURE) -> None:
        """Queue an error reply into the writer buffer (caller must drain)."""
        self.writer.write(_build_reply(reply))

    async def send_reply_success(
        self, bind_host: str = "127.0.0.1", bind_port: int = 0
    ) -> None:
        """Write and flush a SUCCESS reply."""
        self.reply_success(bind_host, bind_port)
        await self.writer.drain()

    async def send_reply_error(self, reply: Reply = Reply.GENERAL_FAILURE) -> None:
        """Write and flush an error reply, then close the writer."""
        self.reply_error(reply)
        with contextlib.suppress(OSError):
            await self.writer.drain()
        with contextlib.suppress(OSError):
            self.writer.close()
            await self.writer.wait_closed()


# ── Server ────────────────────────────────────────────────────────────────────


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
