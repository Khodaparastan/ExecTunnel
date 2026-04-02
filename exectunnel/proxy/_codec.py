"""Private low-level SOCKS5 I/O helpers."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import struct

from exectunnel.exceptions import ConfigurationError, ProtocolError
from exectunnel.protocol.enums import AddrType, Reply


async def read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    """Read exactly *n* bytes from *reader*.

    Raises
    ------
    ProtocolError
        If the stream ends before *n* bytes are available (client disconnected
        mid-handshake or sent a truncated SOCKS5 message).
    """
    try:
        return await reader.readexactly(n)
    except asyncio.IncompleteReadError as exc:
        received = len(exc.partial) if exc.partial else 0
        raise ProtocolError(
            f"Stream ended after {received} byte(s); expected {n} byte(s).",
            error_code="protocol.socks5_truncated_read",
            details={
                "expected_bytes": n,
                "received_bytes": received,
            },
            hint=(
                "The SOCKS5 client disconnected or sent a truncated message. "
                "This is usually benign — the client closed the connection early."
            ),
        ) from exc


async def read_addr(reader: asyncio.StreamReader) -> tuple[str, int]:
    """Read ``ATYP + address + port`` from *reader* and return ``(host, port)``.

    Raises
    ------
    ProtocolError
        If the address type is unsupported, the domain length is zero, the
        domain bytes are not valid UTF-8, or the stream is truncated at any
        point during the read.
    """
    atyp_byte = await read_exact(reader, 1)
    atyp = atyp_byte[0]

    if atyp == AddrType.IPV4:
        raw = await read_exact(reader, 4)
        try:
            host = socket.inet_ntop(socket.AF_INET, raw)
        except OSError as exc:
            raise ProtocolError(
                "Failed to parse IPv4 address bytes from SOCKS5 request.",
                error_code="protocol.socks5_bad_ipv4",
                details={"raw_bytes": raw.hex()},
                hint="The client sent a malformed 4-byte IPv4 address field.",
            ) from exc

    elif atyp == AddrType.IPV6:
        raw = await read_exact(reader, 16)
        try:
            host = socket.inet_ntop(socket.AF_INET6, raw)
        except OSError as exc:
            raise ProtocolError(
                "Failed to parse IPv6 address bytes from SOCKS5 request.",
                error_code="protocol.socks5_bad_ipv6",
                details={"raw_bytes": raw.hex()},
                hint="The client sent a malformed 16-byte IPv6 address field.",
            ) from exc

    elif atyp == AddrType.DOMAIN:
        length_byte = await read_exact(reader, 1)
        length = length_byte[0]
        if length == 0:
            raise ProtocolError(
                "SOCKS5 DOMAIN address length must be greater than zero.",
                error_code="protocol.socks5_domain_zero_length",
                details={"atyp": hex(atyp)},
                hint=(
                    "The SOCKS5 client sent a zero-length domain name, which "
                    "violates RFC 1928 §5."
                ),
            )
        raw_host = await read_exact(reader, length)
        try:
            host = raw_host.decode()
        except UnicodeDecodeError as exc:
            raise ProtocolError(
                "SOCKS5 DOMAIN address bytes are not valid UTF-8.",
                error_code="protocol.socks5_domain_bad_encoding",
                details={
                    "raw_bytes": raw_host.hex(),
                    "declared_length": length,
                },
                hint=(
                    "The SOCKS5 client sent a domain name that cannot be decoded "
                    "as UTF-8. Only ASCII/UTF-8 hostnames are supported."
                ),
            ) from exc

    else:
        raise ProtocolError(
            f"Unsupported SOCKS5 address type: {atyp:#x}.",
            error_code="protocol.socks5_unsupported_atyp",
            details={"atyp": hex(atyp)},
            hint=(
                "Only ATYP 0x01 (IPv4), 0x03 (DOMAIN), and 0x04 (IPv6) are "
                "supported per RFC 1928 §5."
            ),
        )

    port_raw = await read_exact(reader, 2)
    port = struct.unpack("!H", port_raw)[0]
    return host, port


def build_reply(
    reply: Reply,
    bind_host: str = "0.0.0.0",
    bind_port: int = 0,
) -> bytes:
    """Serialise a SOCKS5 reply packet.

    Parameters
    ----------
    reply:
        The SOCKS5 reply code to include in the response.
    bind_host:
        The ``BND.ADDR`` field value.  Must be a valid IPv4 or IPv6 address
        string — RFC 1928 §6 prohibits domain names in replies.
    bind_port:
        The ``BND.PORT`` field value.  Must be in the range ``[0, 65535]``.

    Raises
    ------
    ConfigurationError
        If *bind_port* is outside ``[0, 65535]`` or *bind_host* is not a
        valid IP address string.  Both indicate a programming error in the
        caller rather than a client-side protocol violation.
    """
    if not (0 <= bind_port <= 65535):
        raise ConfigurationError(
            f"bind_port {bind_port!r} is out of the valid range [0, 65535].",
            error_code="config.socks5_invalid_bind_port",
            details={
                "bind_port": bind_port,
                "valid_range": "0–65535",
            },
            hint=(
                "Ensure the bind_port passed to _build_reply() is a valid "
                "TCP/UDP port number. This is a caller bug, not a client error."
            ),
        )

    try:
        addr = ipaddress.ip_address(bind_host)
    except ValueError as exc:
        # RFC 1928 §6: BND.ADDR in a reply MUST be an IP address.
        # DOMAIN address type is only valid in requests, not replies.
        raise ConfigurationError(
            f"bind_host {bind_host!r} is not a valid IP address; "
            "SOCKS5 replies must use an IP address for BND.ADDR (RFC 1928 §6).",
            error_code="config.socks5_invalid_bind_host",
            details={
                "bind_host": bind_host,
                "constraint": "RFC 1928 §6 — BND.ADDR must be an IP address in replies",
            },
            hint=(
                "Pass a valid IPv4 or IPv6 address string as bind_host. "
                "Domain names are not permitted in SOCKS5 reply packets."
            ),
        ) from exc

    atyp = AddrType.IPV4 if addr.version == 4 else AddrType.IPV6

    return (
        bytes([0x05, int(reply), 0x00, int(atyp)])
        + addr.packed
        + struct.pack("!H", bind_port)
    )


__all__ = ["build_reply", "read_addr", "read_exact"]
