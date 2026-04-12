"""Private async I/O helpers for the SOCKS5 proxy layer.

Separated from :mod:`exectunnel.proxy._wire` so that the wire module remains
purely synchronous and side-effect free.  Only this module performs stream I/O.

Public surface (package-internal only)
--------------------------------------
* :func:`read_exact`             — read exactly *n* bytes from an asyncio stream.
* :func:`read_socks5_addr`       — read ATYP+addr+port from an asyncio stream.
* :func:`close_writer`           — close a StreamWriter, suppressing OS errors.
* :func:`write_and_drain_silent` — best-effort write+drain for error replies.
"""

from __future__ import annotations

import asyncio
import contextlib

from exectunnel.exceptions import ProtocolError
from exectunnel.protocol import AddrType

from ._wire import parse_socks5_addr_buf

__all__: list[str] = [
    "close_writer",
    "read_exact",
    "read_socks5_addr",
    "write_and_drain_silent",
]


async def read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    """Read exactly *n* bytes from *reader*.

    Args:
        reader: The asyncio stream reader.
        n:      Number of bytes to read.  Must be ≥ 1.

    Returns:
        Exactly *n* bytes.

    Raises:
        ProtocolError: If the stream is closed before *n* bytes are available.
    """
    try:
        return await reader.readexactly(n)
    except asyncio.IncompleteReadError as exc:
        raise ProtocolError(
            f"SOCKS5 stream closed after {len(exc.partial)} byte(s); "
            f"expected {n} byte(s).",
            details={
                "socks5_field": "stream",
                "expected": f"{n} bytes",
            },
            hint=(
                "The SOCKS5 client closed the connection mid-handshake. "
                "This is usually benign — the client disconnected early."
            ),
        ) from exc


async def read_socks5_addr(
    reader: asyncio.StreamReader,
    *,
    allow_port_zero: bool = False,
) -> tuple[str, int]:
    """Read ``ATYP + address + port`` from *reader* and return ``(host, port)``.

    Args:
        reader:          The asyncio stream reader.
        allow_port_zero: If ``True``, port 0 is permitted (needed for
                         ``UDP_ASSOCIATE`` per RFC 1928 §4).

    Returns:
        A ``(host, port)`` tuple.

    Raises:
        ProtocolError: On any parse failure or stream truncation.
    """
    atyp_byte = await read_exact(reader, 1)
    atyp = atyp_byte[0]

    if atyp == AddrType.IPV4:
        rest = await read_exact(reader, 6)
    elif atyp == AddrType.IPV6:
        rest = await read_exact(reader, 18)
    elif atyp == AddrType.DOMAIN:
        len_byte = await read_exact(reader, 1)
        dlen = len_byte[0]
        if dlen == 0:
            raise ProtocolError(
                "SOCKS5 DOMAIN address length must be greater than zero.",
                details={
                    "socks5_field": "DST.ADDR.LEN",
                    "expected": "domain length ≥ 1 (RFC 1928 §5)",
                },
                hint=(
                    "The SOCKS5 client sent a zero-length domain name, which "
                    "violates RFC 1928 §5."
                ),
            )
        rest = len_byte + await read_exact(reader, dlen + 2)
    else:
        raise ProtocolError(
            f"Unsupported SOCKS5 address type: {atyp:#x}.",
            details={
                "socks5_field": "ATYP",
                "expected": "ATYP 0x01 (IPv4), 0x03 (DOMAIN), or 0x04 (IPv6)",
            },
            hint=(
                "Only ATYP 0x01 (IPv4), 0x03 (DOMAIN), and 0x04 (IPv6) are "
                "supported per RFC 1928 §5."
            ),
        )

    buf = atyp_byte + rest
    host, port, _ = parse_socks5_addr_buf(
        buf, 0, context="SOCKS5", allow_port_zero=allow_port_zero
    )
    return host, port


async def close_writer(writer: asyncio.StreamWriter) -> None:
    """Close *writer*, suppressing ``OSError``, ``RuntimeError``

    Args:
        writer: The asyncio stream writer to close.
    """
    with contextlib.suppress(OSError, RuntimeError):
        writer.close()
        await writer.wait_closed()


async def write_and_drain_silent(
    writer: asyncio.StreamWriter,
    data: bytes,
) -> None:
    """Write *data* and drain, suppressing ``OSError``.

    Args:
        writer: The asyncio stream writer to write to.
        data:   Raw bytes to write.
    """
    with contextlib.suppress(OSError):
        writer.write(data)
        await writer.drain()
