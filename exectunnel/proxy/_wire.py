"""Private SOCKS5 wire-format helpers.

All functions are pure and synchronous — no I/O, no asyncio, no network
calls. They operate exclusively on raw bytes and
:mod:`exectunnel.protocol` enums.

Public surface (package-internal only):
    * :func:`validate_socks5_domain` — RFC 1123 domain safety check.
    * :func:`parse_socks5_addr_buf`  — synchronous ATYP+addr+port parser.
    * :func:`parse_udp_header`       — SOCKS5 UDP datagram header parser.
    * :func:`build_socks5_reply`     — SOCKS5 reply packet serialiser.
    * :func:`build_udp_header`       — SOCKS5 UDP reply header builder.
"""

from __future__ import annotations

import ipaddress
import re
import struct
from typing import Final

from exectunnel.exceptions import ConfigurationError, ProtocolError
from exectunnel.protocol import AddrType, Reply

from ._constants import MAX_TCP_UDP_PORT, SOCKS5_VERSION

__all__ = [
    "build_socks5_reply",
    "build_udp_header",
    "parse_socks5_addr_buf",
    "build_udp_header_for_host",
    "parse_udp_header",
    "validate_socks5_domain",
]

_DOMAIN_LABEL_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9_]([A-Za-z0-9\-_]{0,61}[A-Za-z0-9_])?$",
    re.ASCII,
)
_DOMAIN_UNSAFE_RE: Final[re.Pattern[str]] = re.compile(r"[\x00:\r\n<>]", re.ASCII)
_DOMAIN_MAX_LEN: Final[int] = 253
_IPV4_LEN: Final[int] = 4
_IPV6_LEN: Final[int] = 16
_PORT_LEN: Final[int] = 2
_RAW_HEX_PREVIEW_LEN: Final[int] = 128


def validate_socks5_domain(domain: str) -> None:
    """Raise :exc:`ProtocolError` if *domain* is unsafe for a SOCKS5 frame.

    Checks performed in order:

    1. Total length ≤ 253 characters (RFC 1035 §2.3.4).
    2. No frame-unsafe characters (NUL, ``:``, ``<``, ``>``, CR, LF).
    3. Each dot-separated label matches relaxed RFC 1123 rules
       (underscores permitted for SRV / DMARC / ACME compatibility).

    A trailing dot (FQDN notation) is stripped before label splitting.

    Args:
        domain: The decoded domain string to validate.

    Raises:
        ProtocolError: If any check fails.
    """
    if len(domain) > _DOMAIN_MAX_LEN:
        raise ProtocolError(
            f"SOCKS5 domain name is too long: {len(domain)} chars "
            f"(max {_DOMAIN_MAX_LEN}).",
            details={
                "socks5_field": "DST.ADDR",
                "expected": f"length ≤ {_DOMAIN_MAX_LEN} (RFC 1035 §2.3.4)",
            },
            hint=(
                "The SOCKS5 client sent a domain name exceeding the "
                "253-character DNS limit."
            ),
        )

    if _DOMAIN_UNSAFE_RE.search(domain):
        raise ProtocolError(
            f"SOCKS5 domain name {domain!r} contains frame-unsafe characters.",
            details={
                "socks5_field": "DST.ADDR",
                "expected": "no NUL, colon, or angle-bracket characters",
            },
            hint=(
                "The domain name contains null bytes or frame-unsafe "
                "characters (':', '<', '>'). This may indicate a protocol "
                "injection attempt."
            ),
        )

    for label in domain.rstrip(".").split("."):
        if not label:
            raise ProtocolError(
                f"SOCKS5 domain name {domain!r} contains an empty label.",
                details={
                    "socks5_field": "DST.ADDR",
                    "expected": "non-empty RFC 1123 labels separated by single dots",
                },
                hint=(
                    "Consecutive dots or a leading dot produce empty labels, "
                    "which are invalid per RFC 1123."
                ),
            )
        if not _DOMAIN_LABEL_RE.match(label):
            raise ProtocolError(
                f"SOCKS5 domain label {label!r} in {domain!r} is not RFC 1123 "
                "compliant.",
                details={
                    "socks5_field": "DST.ADDR",
                    "expected": (
                        "label starting and ending with alphanumeric (or "
                        "underscore), interior chars alphanumeric, hyphen, or "
                        "underscore, length 1–63"
                    ),
                },
                hint=(
                    "Each DNS label must start and end with an alphanumeric "
                    "character (or underscore) and contain only letters, "
                    "digits, hyphens, and underscores."
                ),
            )


def parse_socks5_addr_buf(
    data: bytes,
    offset: int,
    *,
    context: str = "SOCKS5",
    allow_port_zero: bool = False,
) -> tuple[str, int, int]:
    """Parse ``ATYP + address + port`` from *data* starting at *offset*.

    Args:
        data: Raw bytes buffer.
        offset: Byte position of the ATYP field.
        context: Human-readable context string for error messages.
        allow_port_zero: If ``True``, port 0 is permitted.

    Returns:
        A ``(host, port, new_offset)`` tuple where ``new_offset`` is the
        byte position immediately after the parsed port field.

    Raises:
        ProtocolError: On any parse failure.
    """
    if len(data) < offset + 1:
        raise ProtocolError(
            f"{context} address truncated before ATYP byte.",
            details={"socks5_field": "ATYP", "expected": "at least 1 byte for ATYP"},
            hint=f"The {context} address field is truncated.",
        )

    raw_atyp = data[offset]
    offset += 1

    try:
        atyp = AddrType(raw_atyp)
    except ValueError:
        raise ProtocolError(
            f"Unsupported {context} address type: {raw_atyp:#x}.",
            details={
                "socks5_field": "ATYP",
                "expected": "ATYP 0x01 (IPv4), 0x03 (DOMAIN), or 0x04 (IPv6)",
            },
            hint=(
                "Only ATYP 0x01 (IPv4), 0x03 (DOMAIN), and 0x04 (IPv6) are supported."
            ),
        ) from None

    if atyp is AddrType.IPV4:
        host, offset = _parse_ipv4(data, offset, context)
    elif atyp is AddrType.IPV6:
        host, offset = _parse_ipv6(data, offset, context)
    else:
        # AddrType is a strict IntEnum; DOMAIN is the only remaining member.
        host, offset = _parse_domain(data, offset, context)

    if len(data) < offset + _PORT_LEN:
        raise ProtocolError(
            f"{context} address truncated before port field.",
            details={
                "socks5_field": "DST.PORT",
                "expected": f"at least {offset + _PORT_LEN} bytes for port field",
            },
            hint=f"The {context} address has no port field.",
        )

    (port,) = struct.unpack("!H", data[offset : offset + _PORT_LEN])
    offset += _PORT_LEN

    if port == 0 and not allow_port_zero:
        raise ProtocolError(
            f"{context} destination port is 0.",
            details={
                "socks5_field": "DST.PORT",
                "expected": "destination port in [1, 65535]",
            },
            hint="Port 0 is not a valid destination port.",
        )

    return host, port, offset


def _parse_ipv4(data: bytes, offset: int, context: str) -> tuple[str, int]:
    """Parse a 4-byte IPv4 address starting at *offset*.

    Args:
        data: Raw bytes buffer.
        offset: Byte position of the first address byte.
        context: Human-readable context string for error messages.

    Returns:
        A ``(host, new_offset)`` tuple.

    Raises:
        ProtocolError: On buffer truncation.
    """
    if len(data) < offset + _IPV4_LEN:
        raise ProtocolError(
            f"{context} IPv4 address truncated.",
            details={
                "socks5_field": "DST.ADDR",
                "expected": f"at least {offset + _IPV4_LEN} bytes for IPv4 address",
            },
            hint=f"The {context} IPv4 address field is incomplete.",
        )
    host = str(ipaddress.IPv4Address(data[offset : offset + _IPV4_LEN]))
    return host, offset + _IPV4_LEN


def _parse_ipv6(data: bytes, offset: int, context: str) -> tuple[str, int]:
    """Parse a 16-byte IPv6 address starting at *offset*.

    Args:
        data: Raw bytes buffer.
        offset: Byte position of the first address byte.
        context: Human-readable context string for error messages.

    Returns:
        A ``(host, new_offset)`` tuple — host is in RFC 5952 compressed form.

    Raises:
        ProtocolError: On buffer truncation.
    """
    if len(data) < offset + _IPV6_LEN:
        raise ProtocolError(
            f"{context} IPv6 address truncated.",
            details={
                "socks5_field": "DST.ADDR",
                "expected": f"at least {offset + _IPV6_LEN} bytes for IPv6 address",
            },
            hint=f"The {context} IPv6 address field is incomplete.",
        )
    host = ipaddress.IPv6Address(data[offset : offset + _IPV6_LEN]).compressed
    return host, offset + _IPV6_LEN


def _parse_domain(data: bytes, offset: int, context: str) -> tuple[str, int]:
    """Parse a length-prefixed domain name starting at *offset*.

    Args:
        data: Raw bytes buffer.
        offset: Byte position of the length byte.
        context: Human-readable context string for error messages.

    Returns:
        A ``(host, new_offset)`` tuple.

    Raises:
        ProtocolError: On any parse failure.
    """
    if len(data) < offset + 1:
        raise ProtocolError(
            f"{context} DOMAIN address truncated before length byte.",
            details={
                "socks5_field": "DST.ADDR.LEN",
                "expected": "at least 1 byte for domain length field",
            },
            hint=f"The {context} DOMAIN header has no length byte.",
        )
    dlen = data[offset]
    offset += 1

    if dlen == 0:
        raise ProtocolError(
            f"{context} DOMAIN address length must be greater than zero.",
            details={"socks5_field": "DST.ADDR.LEN", "expected": "domain length ≥ 1"},
            hint=f"The {context} client sent a zero-length domain name.",
        )

    if len(data) < offset + dlen:
        raise ProtocolError(
            f"{context} DOMAIN address truncated before domain bytes.",
            details={
                "socks5_field": "DST.ADDR",
                "expected": f"at least {offset + dlen} bytes for domain",
            },
            hint=f"The {context} domain name is shorter than its declared length.",
        )

    raw_domain = data[offset : offset + dlen]
    try:
        host = raw_domain.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtocolError(
            f"{context} DOMAIN address bytes are not valid ASCII/IDNA.",
            details={
                "socks5_field": "DST.ADDR",
                "raw_bytes": raw_domain.hex()[:_RAW_HEX_PREVIEW_LEN],
                "codec": "ascii",
            },
            hint=(
                f"The {context} client sent a domain name that cannot be "
                "decoded as ASCII. Use IDNA/punycode for non-ASCII names."
            ),
        ) from exc

    # RFC-style FQDN notation is accepted from SOCKS clients, but the tunnel
    # protocol intentionally does not carry trailing-dot hostnames. Normalize
    # here so SOCKS validation and tunnel encoding agree.
    host = host.rstrip(".")
    if not host:
        raise ProtocolError(
            f"{context} DOMAIN address is empty after FQDN normalization.",
            details={
                "socks5_field": "DST.ADDR",
                "expected": "non-empty domain name",
            },
            hint="Do not send '.' as a SOCKS5 destination domain.",
        )

    validate_socks5_domain(host)
    return host, offset + dlen


def parse_udp_header(data: bytes) -> tuple[bytes, str, int]:
    """Parse a SOCKS5 UDP datagram header and return ``(payload, host, port)``.

    Wire layout (RFC 1928 §7)::

        +----+------+------+----------+----------+----------+
        |RSV | FRAG | ATYP | DST.ADDR | DST.PORT |   DATA   |
        +----+------+------+----------+----------+----------+
        | 2  |  1   |  1   | Variable |    2     | Variable |
        +----+------+------+----------+----------+----------+

    Args:
        data: Raw bytes received from the SOCKS5 client.

    Returns:
        A ``(payload, host, port)`` tuple.

    Raises:
        ProtocolError: On any parse failure.
    """
    if len(data) < 4:
        raise ProtocolError(
            f"SOCKS5 UDP datagram too short: {len(data)} byte(s), minimum is 4.",
            details={
                "socks5_field": "RSV+FRAG+ATYP",
                "expected": "at least 4 bytes (RSV + FRAG + ATYP)",
            },
            hint=(
                "The SOCKS5 client sent a datagram shorter than the minimum "
                "header size."
            ),
        )

    if data[0:2] != b"\x00\x00":
        raise ProtocolError(
            f"SOCKS5 UDP RSV field must be 0x0000 (got {data[0:2].hex()}).",
            details={"socks5_field": "RSV", "expected": "RSV=0x0000 (RFC 1928 §7)"},
            hint="The SOCKS5 client sent a non-zero RSV field in the UDP header.",
        )

    if data[2] != 0:
        raise ProtocolError(
            f"SOCKS5 UDP fragmentation is not supported (FRAG={data[2]:#x}).",
            details={
                "socks5_field": "FRAG",
                "expected": "FRAG=0x00 (no fragmentation, RFC 1928 §7)",
            },
            hint=(
                "The SOCKS5 client requested UDP fragment reassembly, which "
                "exectunnel does not support. Disable fragmentation on the "
                "client."
            ),
        )

    host, port, end_offset = parse_socks5_addr_buf(data, 3, context="SOCKS5 UDP")
    return data[end_offset:], host, port


def build_udp_header(atyp: AddrType, addr_packed: bytes, port: int) -> bytes:
    """Build the SOCKS5 UDP reply header.

    Layout: ``RSV + FRAG + ATYP + ADDR + PORT``.

    Args:
        atyp: Only :attr:`AddrType.IPV4` and :attr:`AddrType.IPV6` are
            valid (RFC 1928 §7).
        addr_packed: Packed address bytes — 4 for IPv4, 16 for IPv6.
        port: Port number in ``[0, 65535]``.

    Returns:
        Header bytes ready for concatenation with the datagram payload.

    Raises:
        ConfigurationError: If *atyp* is :attr:`AddrType.DOMAIN`,
            *addr_packed* has the wrong length for *atyp*, or *port* is
            out of range. Always a caller bug.
    """
    if atyp is AddrType.IPV4:
        expected_len = _IPV4_LEN
    elif atyp is AddrType.IPV6:
        expected_len = _IPV6_LEN
    else:
        raise ConfigurationError(
            f"build_udp_header: atyp={atyp!r} is not valid for UDP reply "
            "headers; only IPV4 and IPV6 are permitted (RFC 1928 §7).",
            details={
                "field": "atyp",
                "value": repr(atyp),
                "expected": "AddrType.IPV4 or AddrType.IPV6",
            },
            hint=(
                "This is a caller bug — DOMAIN addresses cannot appear in "
                "UDP reply headers."
            ),
        )

    if len(addr_packed) != expected_len:
        raise ConfigurationError(
            f"build_udp_header: addr_packed length {len(addr_packed)} does not "
            f"match atyp={atyp!r} (expected {expected_len} bytes).",
            details={
                "field": "addr_packed",
                "value": len(addr_packed),
                "expected": f"{expected_len} bytes for {atyp!r}",
            },
            hint="Ensure addr_packed is the correct length for the given address type.",
        )

    if not (0 <= port <= MAX_TCP_UDP_PORT):
        raise ConfigurationError(
            f"build_udp_header: port {port!r} is out of range [0, 65535].",
            details={
                "field": "port",
                "value": port,
                "expected": "integer in [0, 65535]",
            },
            hint="Ensure the port passed to build_udp_header() is a valid port number.",
        )

    return b"\x00\x00\x00" + bytes([int(atyp)]) + addr_packed + struct.pack("!H", port)


def build_udp_header_for_host(host: str, port: int) -> bytes:
    """Build a SOCKS5 UDP header for an IPv4, IPv6, or DOMAIN host.

    Args:
        host: IPv4, IPv6, or validated SOCKS5 domain name.
        port: Port number in ``[0, 65535]``.

    Returns:
        SOCKS5 UDP header bytes.

    Raises:
        ConfigurationError: If *port* is out of range.
        ProtocolError: If *host* is not a valid SOCKS5 domain and not an IP.
    """
    if not (0 <= port <= MAX_TCP_UDP_PORT):
        raise ConfigurationError(
            f"build_udp_header_for_host: port {port!r} is out of range [0, 65535].",
            details={
                "field": "port",
                "value": port,
                "expected": "integer in [0, 65535]",
            },
            hint="Ensure the UDP source port is a valid port number.",
        )

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        validate_socks5_domain(host)
        raw = host.encode("ascii")
        if not (1 <= len(raw) <= 255):
            raise ProtocolError(
                f"SOCKS5 UDP domain wire length is invalid: {len(raw)}.",
                details={
                    "socks5_field": "DST.ADDR",
                    "expected": "domain wire length in [1, 255]",
                },
                hint="Use a valid SOCKS5 domain name.",
            )
        return (
            b"\x00\x00\x00"
            + bytes([int(AddrType.DOMAIN), len(raw)])
            + raw
            + struct.pack("!H", port)
        )

    atyp = AddrType.IPV4 if addr.version == 4 else AddrType.IPV6
    return build_udp_header(atyp, addr.packed, port)


def build_socks5_reply(
    reply: Reply,
    bind_host: str = "0.0.0.0",
    bind_port: int = 0,
) -> bytes:
    """Serialise a SOCKS5 reply packet (RFC 1928 §6).

    Args:
        reply: The SOCKS5 reply code.
        bind_host: The ``BND.ADDR`` field. Must be a valid IPv4 or IPv6
            address string. Defaults to ``"0.0.0.0"``.
        bind_port: The ``BND.PORT`` field. Must be in ``[0, 65535]``.

    Returns:
        A serialised SOCKS5 reply packet ready to write to the client.

    Raises:
        ConfigurationError: If *bind_port* is out of range or
            *bind_host* is not a valid IP address string.
    """
    if not (0 <= bind_port <= MAX_TCP_UDP_PORT):
        raise ConfigurationError(
            f"bind_port {bind_port!r} is out of the valid range [0, 65535].",
            details={
                "field": "bind_port",
                "value": bind_port,
                "expected": "integer in [0, 65535]",
            },
            hint=(
                "Ensure the bind_port passed to build_socks5_reply() is a "
                "valid TCP/UDP port number. This is a caller bug, not a "
                "client error."
            ),
        )

    try:
        addr = ipaddress.ip_address(bind_host)
    except ValueError as exc:
        raise ConfigurationError(
            f"bind_host {bind_host!r} is not a valid IP address; "
            "RFC 1928 §6 requires BND.ADDR to be an IP address in replies.",
            details={
                "field": "bind_host",
                "value": bind_host,
                "expected": "valid IPv4 or IPv6 address string",
            },
            hint=(
                "Pass a valid IPv4 or IPv6 address string as bind_host. "
                "Domain names are not permitted in SOCKS5 reply packets."
            ),
        ) from exc

    atyp = AddrType.IPV4 if addr.version == 4 else AddrType.IPV6
    return (
        bytes([SOCKS5_VERSION, int(reply), 0x00, int(atyp)])
        + addr.packed
        + struct.pack("!H", bind_port)
    )
