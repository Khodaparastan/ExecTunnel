"""Private SOCKS5 wire-format helpers.

All functions are **pure and synchronous** — no I/O, no asyncio, no network
calls.  They operate exclusively on raw :class:`bytes` and
:mod:`exectunnel.protocol` enums.

Public surface (package-internal only)
--------------------------------------
* :func:`validate_socks5_domain` — RFC 1123 domain safety check.
* :func:`parse_socks5_addr_buf`  — Sync ATYP+addr+port parser from a buffer.
* :func:`parse_udp_header`       — SOCKS5 UDP datagram header parser (RFC 1928 §7).
* :func:`build_socks5_reply`     — SOCKS5 reply packet serialiser (RFC 1928 §6).
* :func:`build_udp_header`       — SOCKS5 UDP reply header builder.
"""

from __future__ import annotations

import ipaddress
import re
import struct

from exectunnel.exceptions import ConfigurationError, ProtocolError
from exectunnel.protocol import AddrType, Reply

__all__: list[str] = [
    "build_socks5_reply",
    "build_udp_header",
    "parse_socks5_addr_buf",
    "parse_udp_header",
    "validate_socks5_domain",
]

# ---------------------------------------------------------------------------
# Domain-name validation
# ---------------------------------------------------------------------------

# RFC 1123 relaxed + underscore (RFC 952 forbids it, but real-world DNS uses
# underscores extensively: _dmarc, _srv, _acme-challenge, etc.).
_DOMAIN_LABEL_RE: re.Pattern[str] = re.compile(
    r"^[A-Za-z0-9_]([A-Za-z0-9\-_]{0,61}[A-Za-z0-9_])?$",
    re.ASCII,
)

# Characters that are NUL (stream terminator) or frame-unsafe.
_DOMAIN_UNSAFE_RE: re.Pattern[str] = re.compile(r"[\x00:\r\n<>]", re.ASCII)

# Maximum total length of a DNS name per RFC 1035 §2.3.4.
_DOMAIN_MAX_LEN: int = 253


def validate_socks5_domain(domain: str) -> None:
    """Raise :class:`~exectunnel.exceptions.ProtocolError` if *domain* is unsafe.

    Checks performed (in order):

    1. Total length ≤ 253 characters (RFC 1035 §2.3.4).
    2. No frame-unsafe characters (``\\x00``, ``:``, ``<``, ``>``).
    3. Each dot-separated label matches relaxed RFC 1123 rules (underscores
       permitted for SRV/DMARC compatibility).

    A trailing dot (FQDN notation) is stripped before label splitting.

    Args:
        domain: The decoded domain string to validate.

    Raises:
        ProtocolError: If any check fails.
    """
    if len(domain) > _DOMAIN_MAX_LEN:
        raise ProtocolError(
            f"SOCKS5 domain name is too long: {len(domain)} chars (max {_DOMAIN_MAX_LEN}).",
            details={
                "socks5_field": "DST.ADDR",
                "expected": f"length ≤ {_DOMAIN_MAX_LEN} (RFC 1035 §2.3.4)",
            },
            hint="The SOCKS5 client sent a domain name exceeding the 253-character DNS limit.",
        )

    if _DOMAIN_UNSAFE_RE.search(domain):
        raise ProtocolError(
            f"SOCKS5 domain name {domain!r} contains frame-unsafe characters.",
            details={
                "socks5_field": "DST.ADDR",
                "expected": "no NUL, colon, or angle-bracket characters",
            },
            hint=(
                "The domain name contains null bytes or frame-unsafe characters "
                "(':', '<', '>').  This may indicate a protocol injection attempt."
            ),
        )

    labels = domain.rstrip(".").split(".")
    for label in labels:
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
                f"SOCKS5 domain label {label!r} in {domain!r} is not RFC 1123 compliant.",
                details={
                    "socks5_field": "DST.ADDR",
                    "expected": (
                        "label starting and ending with alphanumeric (or underscore), "
                        "interior chars alphanumeric, hyphen, or underscore, length 1–63"
                    ),
                },
                hint=(
                    "Each DNS label must start and end with an alphanumeric character "
                    "(or underscore) and contain only letters, digits, hyphens, and "
                    "underscores."
                ),
            )


# ---------------------------------------------------------------------------
# Shared address parsing from a buffer (sync)
# ---------------------------------------------------------------------------


def parse_socks5_addr_buf(
    data: bytes,
    offset: int,
    *,
    context: str = "SOCKS5",
    allow_port_zero: bool = False,
) -> tuple[str, int, int]:
    """Parse ``ATYP + address + port`` from *data* starting at *offset*.

    Args:
        data:            Raw bytes buffer.
        offset:          Byte position of the ATYP field.
        context:         Human-readable context for error messages.
        allow_port_zero: If ``True``, port 0 is permitted.

    Returns:
        A ``(host, port, new_offset)`` tuple.

    Raises:
        ProtocolError: On any parse failure.
    """
    if len(data) < offset + 1:
        raise ProtocolError(
            f"{context} address truncated before ATYP byte.",
            details={"socks5_field": "ATYP", "expected": "at least 1 byte for ATYP"},
            hint=f"The {context} address field is truncated.",
        )

    atyp = data[offset]
    offset += 1

    if atyp == AddrType.IPV4:
        if len(data) < offset + 4:
            raise ProtocolError(
                f"{context} IPv4 address truncated.",
                details={
                    "socks5_field": "DST.ADDR",
                    "expected": f"at least {offset + 4} bytes for IPv4 address",
                },
                hint=f"The {context} IPv4 address field is incomplete.",
            )
        host = str(ipaddress.IPv4Address(data[offset : offset + 4]))
        offset += 4

    elif atyp == AddrType.IPV6:
        if len(data) < offset + 16:
            raise ProtocolError(
                f"{context} IPv6 address truncated.",
                details={
                    "socks5_field": "DST.ADDR",
                    "expected": f"at least {offset + 16} bytes for IPv6 address",
                },
                hint=f"The {context} IPv6 address field is incomplete.",
            )
        host = str(ipaddress.IPv6Address(data[offset : offset + 16]).compressed)
        offset += 16

    elif atyp == AddrType.DOMAIN:
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
                details={
                    "socks5_field": "DST.ADDR.LEN",
                    "expected": "domain length ≥ 1",
                },
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
            host = raw_domain.decode()
        except UnicodeDecodeError as exc:
            raise ProtocolError(
                f"{context} DOMAIN address bytes are not valid UTF-8.",
                details={
                    "socks5_field": "DST.ADDR",
                    "raw_bytes": raw_domain.hex()[:128],
                    "codec": "utf-8",
                },
                hint=(
                    f"The {context} client sent a domain name that cannot be decoded "
                    "as UTF-8.  Only ASCII/UTF-8 hostnames are supported."
                ),
            ) from exc

        validate_socks5_domain(host)
        offset += dlen

    else:
        raise ProtocolError(
            f"Unsupported {context} address type: {atyp:#x}.",
            details={
                "socks5_field": "ATYP",
                "expected": "ATYP 0x01 (IPv4), 0x03 (DOMAIN), or 0x04 (IPv6)",
            },
            hint=(
                "Only ATYP 0x01 (IPv4), 0x03 (DOMAIN), and 0x04 (IPv6) are supported."
            ),
        )

    # Port
    if len(data) < offset + 2:
        raise ProtocolError(
            f"{context} address truncated before port field.",
            details={
                "socks5_field": "DST.PORT",
                "expected": f"at least {offset + 2} bytes for port field",
            },
            hint=f"The {context} address has no port field.",
        )

    (port,) = struct.unpack("!H", data[offset : offset + 2])
    offset += 2

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


# ---------------------------------------------------------------------------
# UDP datagram header parser
# ---------------------------------------------------------------------------


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
            hint="The SOCKS5 client sent a datagram shorter than the minimum header size.",
        )

    if data[0:2] != b"\x00\x00":
        raise ProtocolError(
            f"SOCKS5 UDP RSV field must be 0x0000 (got {data[0:2].hex()}).",
            details={
                "socks5_field": "RSV",
                "expected": "RSV=0x0000 (RFC 1928 §7)",
            },
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
                "exectunnel does not support.  Disable fragmentation on the client."
            ),
        )

    host, port, end_offset = parse_socks5_addr_buf(data, 3, context="SOCKS5 UDP")
    payload = data[end_offset:]
    return payload, host, port


# ---------------------------------------------------------------------------
# UDP reply header builder
# ---------------------------------------------------------------------------

_ATYP_PACKED_LENGTHS: dict[AddrType, int] = {
    AddrType.IPV4: 4,
    AddrType.IPV6: 16,
}


def build_udp_header(atyp: AddrType, addr_packed: bytes, port: int) -> bytes:
    """Build the SOCKS5 UDP reply header (RSV + FRAG + ATYP + ADDR + PORT).

    Args:
        atyp:        Only ``IPV4`` and ``IPV6`` are valid (RFC 1928 §7).
        addr_packed: Packed address bytes (4 for IPv4, 16 for IPv6).
        port:        Port number in ``[0, 65535]``.

    Returns:
        Header bytes ready for concatenation with the payload.

    Raises:
        ConfigurationError: Invalid arguments.
    """
    expected_len = _ATYP_PACKED_LENGTHS.get(atyp)
    if expected_len is None:
        raise ConfigurationError(
            f"build_udp_header: atyp={atyp!r} is not valid for UDP reply headers; "
            "only IPV4 and IPV6 are permitted (RFC 1928 §7).",
            details={
                "field": "atyp",
                "value": repr(atyp),
                "expected": "AddrType.IPV4 or AddrType.IPV6",
            },
            hint="This is a caller bug — DOMAIN addresses cannot appear in UDP reply headers.",
        )

    if len(addr_packed) != expected_len:
        raise ConfigurationError(
            f"build_udp_header: addr_packed length {len(addr_packed)} does not match "
            f"atyp={atyp!r} (expected {expected_len} bytes).",
            details={
                "field": "addr_packed",
                "value": len(addr_packed),
                "expected": f"{expected_len} bytes for {atyp!r}",
            },
            hint="Ensure addr_packed is the correct length for the given address type.",
        )

    if not (0 <= port <= 65_535):
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


# ---------------------------------------------------------------------------
# Reply builder
# ---------------------------------------------------------------------------


def build_socks5_reply(
    reply: Reply,
    bind_host: str = "0.0.0.0",
    bind_port: int = 0,
) -> bytes:
    """Serialise a SOCKS5 reply packet (RFC 1928 §6).

    Args:
        reply:     The SOCKS5 reply code.
        bind_host: The ``BND.ADDR`` field.  Must be a valid IPv4 or IPv6
                   address string.  Defaults to ``"0.0.0.0"``.
        bind_port: The ``BND.PORT`` field.  Must be in ``[0, 65535]``.

    Returns:
        A serialised SOCKS5 reply packet.

    Raises:
        ConfigurationError: Invalid *bind_port* or *bind_host*.
    """
    if not (0 <= bind_port <= 65_535):
        raise ConfigurationError(
            f"bind_port {bind_port!r} is out of the valid range [0, 65535].",
            details={
                "field": "bind_port",
                "value": bind_port,
                "expected": "integer in [0, 65535]",
            },
            hint=(
                "Ensure the bind_port passed to build_socks5_reply() is a valid "
                "TCP/UDP port number.  This is a caller bug, not a client error."
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
                "Pass a valid IPv4 or IPv6 address string as bind_host.  "
                "Domain names are not permitted in SOCKS5 reply packets."
            ),
        ) from exc

    atyp = AddrType.IPV4 if addr.version == 4 else AddrType.IPV6
    return (
        bytes([0x05, int(reply), 0x00, int(atyp)])
        + addr.packed
        + struct.pack("!H", bind_port)
    )
