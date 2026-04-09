"""Private SOCKS5 wire-format helpers.

All functions are **pure and synchronous** — no I/O, no asyncio, no network
calls.  They operate exclusively on raw :class:`bytes` and
:mod:`exectunnel.protocol` enums.

Async address reading lives in :mod:`exectunnel.proxy._io` to keep this
module side-effect free and trivially unit-testable.

Public surface (package-internal only — not re-exported from ``__init__.py``)
------------------------------------------------------------------------------
* :func:`validate_socks5_domain` — RFC 1123 domain safety check.
* :func:`parse_socks5_addr_buf`  — Sync ATYP+addr+port parser from a buffer.
* :func:`parse_udp_header`       — SOCKS5 UDP datagram header parser (RFC 1928 §7).
* :func:`build_socks5_reply`     — SOCKS5 reply packet serialiser (RFC 1928 §6).
* :func:`build_udp_header`       — SOCKS5 UDP reply header builder.

Exceptions raised
-----------------
* :class:`~exectunnel.exceptions.ProtocolError`      — malformed wire data from
  the remote SOCKS5 client.
* :class:`~exectunnel.exceptions.ConfigurationError` — bad arguments supplied by
  the *caller* (e.g. invalid ``bind_host`` / ``bind_port`` to
  :func:`build_socks5_reply`).
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

# RFC 1123 relaxed: labels of 1–63 chars, total ≤ 253, no leading/trailing dot.
_DOMAIN_LABEL_RE: re.Pattern[str] = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?$",
    re.ASCII,
)

# Characters that are NUL (stream terminator) or frame-unsafe per the
# ExecTunnel wire format.
_DOMAIN_UNSAFE_RE: re.Pattern[str] = re.compile(r"[\x00:<>]", re.ASCII)

# Maximum total length of a DNS name per RFC 1035 §2.3.4.
_DOMAIN_MAX_LEN: int = 253


def validate_socks5_domain(domain: str) -> None:
    """Raise :class:`~exectunnel.exceptions.ProtocolError` if *domain* is unsafe.

    Checks performed (in order):

    1. Total length ≤ 253 characters (RFC 1035 §2.3.4).
    2. No frame-unsafe characters (``\\x00``, ``:``, ``<``, ``>``).
    3. Each dot-separated label matches RFC 1123 rules:

       * 1–63 characters.
       * Starts and ends with an alphanumeric character.
       * Interior characters are alphanumeric or ``-``.

    A trailing dot (FQDN notation) is stripped before label splitting so
    ``"example.com."`` is treated identically to ``"example.com"``.

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
                        "label starting and ending with alphanumeric, "
                        "interior chars alphanumeric or hyphen, length 1–63"
                    ),
                },
                hint=(
                    "Each DNS label must start and end with an alphanumeric character "
                    "and contain only letters, digits, and hyphens."
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

    Single source of truth for SOCKS5 address parsing from a contiguous
    buffer.  Used by :func:`parse_udp_header` directly and by
    :func:`~exectunnel.proxy._io.read_socks5_addr` after assembling bytes
    from the stream.

    Args:
        data:            Raw bytes buffer.
        offset:          Byte position of the ATYP field.
        context:         Human-readable context for error messages
                         (e.g. ``"SOCKS5 UDP"``).
        allow_port_zero: If ``False`` (default), port 0 raises
                         :class:`ProtocolError`.  Set to ``True`` for
                         ``UDP_ASSOCIATE`` requests where the client may
                         declare ``DST.PORT=0`` per RFC 1928 §4.

    Returns:
        A ``(host, port, new_offset)`` tuple where *new_offset* points to
        the first byte after the port field.

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
            host = raw_domain.decode("utf-8")
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

    port = struct.unpack("!H", data[offset : offset + 2])[0]
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

    Pure function — no I/O, no state.

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

    # ATYP starts at offset 3; parse_socks5_addr_buf reads ATYP+addr+port.
    host, port, end_offset = parse_socks5_addr_buf(
        data, 3, context="SOCKS5 UDP", allow_port_zero=False
    )
    payload = data[end_offset:]
    return payload, host, port


# ---------------------------------------------------------------------------
# UDP reply header builder
# ---------------------------------------------------------------------------


def build_udp_header(atyp: AddrType, addr_packed: bytes, port: int) -> bytes:
    """Build the SOCKS5 UDP reply header (RSV + FRAG + ATYP + ADDR + PORT).

    Pre-computes the header bytes to avoid per-datagram allocation overhead
    in :meth:`~exectunnel.proxy.udp_relay.UdpRelay.send_to_client`.

    Args:
        atyp:        :class:`~exectunnel.protocol.AddrType` enum member.
        addr_packed: Packed address bytes (4 for IPv4, 16 for IPv6).
        port:        Port number in ``[0, 65535]``.

    Returns:
        Header bytes ready for concatenation with the payload.
    """
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

    The default ``bind_host="0.0.0.0"`` and ``bind_port=0`` represent the
    "unspecified" address per RFC 1928 §6 convention and are appropriate for
    error replies.

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
