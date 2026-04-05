"""Private async I/O helpers for the SOCKS5 proxy layer.

Separated from :mod:`exectunnel.proxy._wire` so that the wire module remains
purely synchronous and side-effect free.  Only this module performs stream I/O.

:func:`read_exact` is defined here and is the single authorised implementation
for exact-length stream reads within the proxy layer.  It is re-exported so
that :mod:`exectunnel.proxy.server` can import it from one place.

Public surface (package-internal only — not re-exported from ``__init__.py``)
------------------------------------------------------------------------------
* :func:`read_exact`             — read exactly *n* bytes from an asyncio stream.
* :func:`read_socks5_addr`       — read ATYP+addr+port from an asyncio stream.
* :func:`close_writer`           — close a StreamWriter, suppressing OS errors.
* :func:`write_and_drain_silent` — best-effort write+drain for error replies.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import struct

from exectunnel.exceptions import ProtocolError
from exectunnel.protocol import AddrType
from exectunnel.proxy._wire import validate_socks5_domain

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
        ProtocolError: If the stream is closed before *n* bytes are available
                       (i.e. ``readexactly`` raises ``asyncio.IncompleteReadError``).
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


async def read_socks5_addr(reader: asyncio.StreamReader) -> tuple[str, int]:
    """Read ``ATYP + address + port`` from *reader* and return ``(host, port)``.

    Uses :mod:`ipaddress` for IP parsing — portable across all platforms,
    including Windows builds that lack ``socket.inet_ntop``.

    The stream must be positioned immediately before the ATYP byte on entry.

    Args:
        reader: The asyncio stream reader.

    Returns:
        A ``(host, port)`` tuple.  *host* is a normalised string:
        compressed IPv6 notation for IPv6, dotted-decimal for IPv4, and the
        raw decoded string for domain names.

    Raises:
        ProtocolError:
            * Unsupported address type.
            * Zero-length domain.
            * Domain fails RFC 1123 / safety validation.
            * Domain bytes are not valid UTF-8.
            * Port is zero.
            * Stream truncated at any point during the read (via
              :func:`read_exact`).
    """
    atyp_byte = await read_exact(reader, 1)
    atyp = atyp_byte[0]

    if atyp == AddrType.IPV4:
        raw = await read_exact(reader, 4)
        host = str(ipaddress.IPv4Address(raw))

    elif atyp == AddrType.IPV6:
        raw = await read_exact(reader, 16)
        host = str(ipaddress.IPv6Address(raw).compressed)

    elif atyp == AddrType.DOMAIN:
        length_byte = await read_exact(reader, 1)
        length = length_byte[0]
        if length == 0:
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
        raw_host = await read_exact(reader, length)
        try:
            host = raw_host.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(
                "SOCKS5 DOMAIN address bytes are not valid UTF-8.",
                details={
                    "socks5_field": "DST.ADDR",
                    "raw_bytes": raw_host.hex()[:128],
                    "codec": "utf-8",
                },
                hint=(
                    "The SOCKS5 client sent a domain name that cannot be decoded "
                    "as UTF-8.  Only ASCII/UTF-8 hostnames are supported."
                ),
            ) from exc
        validate_socks5_domain(host)

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

    port_raw = await read_exact(reader, 2)
    port = struct.unpack("!H", port_raw)[0]

    if port == 0:
        raise ProtocolError(
            f"SOCKS5 request destination port is 0 for host {host!r}.",
            details={
                "socks5_field": "DST.PORT",
                "expected": "destination port in [1, 65535]",
            },
            hint="Port 0 is not a valid destination port in a SOCKS5 request.",
        )

    return host, port


async def close_writer(writer: asyncio.StreamWriter) -> None:
    """Close *writer*, suppressing ``OSError``.

    Centralised so the identical close pattern is not repeated in every
    ``except`` branch of the server's connection handler.

    ``RuntimeError`` is intentionally **not** suppressed — it indicates a
    closed event loop or other abnormal condition that must propagate.

    Args:
        writer: The asyncio stream writer to close.
    """
    with contextlib.suppress(OSError):
        writer.close()
        await writer.wait_closed()


async def write_and_drain_silent(
    writer: asyncio.StreamWriter,
    data: bytes,
) -> None:
    """Write *data* and drain, suppressing ``OSError``.

    Used for best-effort error-reply writes inside the SOCKS5 negotiation
    where the connection may already be half-closed.

    ``RuntimeError`` is intentionally **not** suppressed — it indicates a
    closed event loop or other abnormal condition that must propagate.

    Args:
        writer: The asyncio stream writer to write to.
        data:   Raw bytes to write.
    """
    with contextlib.suppress(OSError):
        writer.write(data)
        await writer.drain()
