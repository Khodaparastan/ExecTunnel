"""Private low-level SOCKS5 I/O helpers."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import struct

from exectunnel.protocol.enums import AddrType, Reply


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
