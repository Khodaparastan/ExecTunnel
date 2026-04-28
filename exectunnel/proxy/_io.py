"""Private async I/O helpers for the SOCKS5 proxy layer.

Separated from :mod:`exectunnel.proxy._wire` so that the wire module
remains purely synchronous and side-effect free. Only this module
performs stream I/O.

Public surface (package-internal only):
    * :func:`read_exact`             — read exactly *n* bytes.
    * :func:`read_socks5_addr`       — read ATYP+addr+port.
    * :func:`close_writer`           — close a writer, suppressing OS errors.
    * :func:`write_and_drain_silent` — best-effort write+drain for error replies.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Final

from exectunnel.exceptions import ProtocolError
from exectunnel.protocol import AddrType

from ._constants import DEFAULT_WRITER_CLOSE_TIMEOUT
from ._wire import parse_socks5_addr_buf

__all__ = [
    "close_writer",
    "read_exact",
    "read_socks5_addr",
    "write_and_drain_silent",
]

_IPV4_TAIL: Final[int] = 6  # 4 address bytes + 2 port bytes
_IPV6_TAIL: Final[int] = 18  # 16 address bytes + 2 port bytes
_PORT_LEN: Final[int] = 2


async def read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    """Read exactly *n* bytes from *reader*.

    Args:
        reader: The asyncio stream reader.
        n: Number of bytes to read. Must be ≥ 1.

    Returns:
        Exactly *n* bytes.

    Raises:
        ProtocolError: If the stream closes before *n* bytes are
            available.
    """
    try:
        return await reader.readexactly(n)
    except asyncio.IncompleteReadError as exc:
        raise ProtocolError(
            f"SOCKS5 stream closed after {len(exc.partial)} byte(s); "
            f"expected {n} byte(s).",
            details={"socks5_field": "stream", "expected": f"{n} bytes"},
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

    Reads the minimum number of bytes needed for each address type, then
    delegates all structural validation to :func:`parse_socks5_addr_buf`.

    Args:
        reader: The asyncio stream reader.
        allow_port_zero: If ``True``, port 0 is permitted (required for
            ``UDP_ASSOCIATE`` per RFC 1928 §4).

    Returns:
        A ``(host, port)`` tuple.

    Raises:
        ProtocolError: On any parse failure or stream truncation.
    """
    atyp_byte = await read_exact(reader, 1)

    try:
        addr_type = AddrType(atyp_byte[0])
    except ValueError:
        raise ProtocolError(
            f"Unsupported SOCKS5 address type: {atyp_byte[0]:#x}.",
            details={
                "socks5_field": "ATYP",
                "expected": "ATYP 0x01 (IPv4), 0x03 (DOMAIN), or 0x04 (IPv6)",
            },
            hint=(
                "Only ATYP 0x01 (IPv4), 0x03 (DOMAIN), and 0x04 (IPv6) are "
                "supported per RFC 1928 §5."
            ),
        ) from None

    if addr_type is AddrType.IPV4:
        rest = await read_exact(reader, _IPV4_TAIL)
    elif addr_type is AddrType.IPV6:
        rest = await read_exact(reader, _IPV6_TAIL)
    else:
        # AddrType is a strict IntEnum; DOMAIN is the only remaining member.
        len_byte = await read_exact(reader, 1)
        dlen = len_byte[0]
        rest = len_byte + await read_exact(reader, dlen + _PORT_LEN)

    host, port, _ = parse_socks5_addr_buf(
        atyp_byte + rest,
        0,
        context="SOCKS5",
        allow_port_zero=allow_port_zero,
    )
    return host, port


async def close_writer(
    writer: asyncio.StreamWriter,
    timeout: float = DEFAULT_WRITER_CLOSE_TIMEOUT,
) -> None:
    """Close *writer*, suppressing :exc:`OSError` and :exc:`RuntimeError`.

    Args:
        writer: The asyncio stream writer to close.
        timeout: Maximum seconds to wait for ``wait_closed``.
    """
    with contextlib.suppress(OSError, RuntimeError, TimeoutError):
        writer.close()
        async with asyncio.timeout(timeout):
            await writer.wait_closed()


async def write_and_drain_silent(writer: asyncio.StreamWriter, data: bytes) -> None:
    """Write *data* and drain, suppressing :exc:`OSError`.

    Args:
        writer: The asyncio stream writer to write to.
        data: Raw bytes to write.
    """
    with contextlib.suppress(OSError, RuntimeError, TimeoutError):
        writer.write(data)
        async with asyncio.timeout(DEFAULT_WRITER_CLOSE_TIMEOUT):
            await writer.drain()
