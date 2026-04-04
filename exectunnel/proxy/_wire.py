"""Private SOCKS5 wire-format helpers: address parsing, reply building, domain validation.

All functions in this module operate on raw bytes and SOCKS5 enums.  They have
no knowledge of asyncio transports, WebSocket framing, or session state.

The only async function (:func:`read_socks5_addr`) depends on
:func:`~exectunnel._stream.read_exact` for stream I/O.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import struct

from exectunnel._stream import read_exact
from exectunnel.exceptions import ConfigurationError, ProtocolError
from exectunnel.protocol.enums import AddrType, Reply

__all__ = [
    "build_socks5_reply",
    "parse_udp_header",
    "read_socks5_addr",
    "validate_socks5_domain",
]

# ── Domain-name validation ────────────────────────────────────────────────────

# RFC 1123 relaxed: labels of 1–63 chars, total ≤ 253, no leading/trailing dot.
# re.ASCII ensures [A-Za-z0-9] never matches Unicode digits on exotic builds.
# Underscores are intentionally excluded — not valid per RFC 1123 and would
# corrupt tunnel frames alongside frame-unsafe chars.
_DOMAIN_LABEL_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?$",
    re.ASCII,
)
_DOMAIN_UNSAFE_RE = re.compile(r"[\x00:<>]", re.ASCII)


# ── Domain validation ────────────────────────────────────────────────────────


def validate_socks5_domain(domain: str) -> None:
    """Raise :class:`ProtocolError` if *domain* is not a safe, well-formed hostname.

    Checks performed:

    * Total length ≤ 253 characters.
    * No frame-unsafe characters (``\\x00``, ``:``, ``<``, ``>``).
    * Each dot-separated label matches RFC 1123 rules (1–63 chars,
      alphanumeric start/end, hyphens allowed in the middle).

    Note:
        A trailing dot (FQDN notation) is stripped before label splitting so
        that ``"example.com."`` is treated identically to ``"example.com"``.
        A bare ``"."`` (DNS root) becomes an empty string after stripping and
        triggers the empty-label error — this is intentional.

    Args:
        domain: The decoded domain string to validate.

    Raises:
        ProtocolError: If any check fails.
    """
    if len(domain) > 253:
        raise ProtocolError(
            f"SOCKS5 domain name is too long: {len(domain)} chars (max 253).",
            error_code="protocol.socks5_domain_too_long",
            details={"length": len(domain), "max_length": 253},
            hint="The SOCKS5 client sent a domain name exceeding the 253-character DNS limit.",
        )

    if _DOMAIN_UNSAFE_RE.search(domain):
        raise ProtocolError(
            f"SOCKS5 domain name {domain!r} contains unsafe characters.",
            error_code="protocol.socks5_domain_unsafe_chars",
            details={"domain": domain},
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
                error_code="protocol.socks5_domain_empty_label",
                details={"domain": domain},
                hint=(
                    "Consecutive dots or a leading dot produce empty labels, "
                    "which are invalid per RFC 1123."
                ),
            )
        if not _DOMAIN_LABEL_RE.match(label):
            raise ProtocolError(
                f"SOCKS5 domain label {label!r} in {domain!r} is not RFC 1123 compliant.",
                error_code="protocol.socks5_domain_bad_label",
                details={"domain": domain, "label": label},
                hint=(
                    "Each DNS label must start and end with an alphanumeric character "
                    "and contain only letters, digits, and hyphens."
                ),
            )


# ── Address reading ───────────────────────────────────────────────────────────


async def read_socks5_addr(reader: asyncio.StreamReader) -> tuple[str, int]:
    """Read ``ATYP + address + port`` from *reader* and return ``(host, port)``.

    Uses :mod:`ipaddress` for IP parsing — portable across all platforms,
    including Windows builds that lack ``socket.inet_ntop``.

    Args:
        reader: The asyncio stream positioned immediately before the ATYP byte.

    Returns:
        A ``(host, port)`` tuple.  *host* is a normalised string:
        compressed IPv6 notation for IPv6 addresses, dotted-decimal for IPv4,
        and the raw decoded string for domain names.

    Raises:
        ProtocolError:
            * Unsupported address type.
            * Zero-length domain.
            * Domain fails RFC 1123 / safety validation.
            * Domain bytes are not valid UTF-8.
            * Port is zero (not a valid destination port).
        ProtocolError (via :func:`read_exact`):
            Stream truncated at any point during the read.
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
                error_code="protocol.socks5_domain_zero_length",
                details={"atyp": hex(atyp)},
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
                error_code="protocol.socks5_domain_bad_encoding",
                details={
                    "raw_bytes": raw_host.hex(),
                    "declared_length": length,
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
            error_code="protocol.socks5_unsupported_atyp",
            details={"atyp": hex(atyp)},
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
            error_code="protocol.socks5_zero_port",
            details={"host": host, "atyp": hex(atyp)},
            hint="Port 0 is not a valid destination port in a SOCKS5 request.",
        )

    return host, port


# ── Reply building ────────────────────────────────────────────────────────────


def build_socks5_reply(
    reply: Reply | int,
    bind_host: str = "0.0.0.0",
    bind_port: int = 0,
) -> bytes:
    """Serialise a SOCKS5 reply packet (RFC 1928 §6).

    Args:
        reply:     The SOCKS5 reply code.  Accepts a :class:`Reply` member or
                   a raw ``int`` — the value is coerced to :class:`Reply` so
                   unknown codes are rejected early.
        bind_host: The ``BND.ADDR`` field.  Must be a valid IPv4 or IPv6
                   address string — RFC 1928 §6 prohibits domain names in
                   replies.  Defaults to ``"0.0.0.0"``.
        bind_port: The ``BND.PORT`` field.  Must be in ``[0, 65535]``.
                   Defaults to ``0``.

    Returns:
        A serialised SOCKS5 reply packet as :class:`bytes`.

    Raises:
        ConfigurationError:
            * *reply* is not a valid :class:`Reply` code.
            * *bind_port* is outside ``[0, 65535]``.
            * *bind_host* is not a valid IP address string.
    """
    try:
        reply_code = Reply(reply)
    except ValueError as exc:
        raise ConfigurationError(
            f"reply {reply!r} is not a valid SOCKS5 Reply code.",
            error_code="config.socks5_invalid_reply_code",
            details={
                "reply": reply,
                "valid_codes": [r.value for r in Reply],
            },
            hint=(
                "Pass a member of exectunnel.protocol.enums.Reply.  "
                "This is a caller bug, not a client error."
            ),
        ) from exc

    if not (0 <= bind_port <= 65_535):
        raise ConfigurationError(
            f"bind_port {bind_port!r} is out of the valid range [0, 65535].",
            error_code="config.socks5_invalid_bind_port",
            details={
                "bind_port": bind_port,
                "valid_range": "0–65535",
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
            "must use an IP address for BND.ADDR (RFC 1928 §6).",
            error_code="config.socks5_invalid_bind_host",
            details={
                "bind_host": bind_host,
                "constraint": "RFC 1928 §6 — BND.ADDR must be an IP address in replies",
            },
            hint=(
                "Pass a valid IPv4 or IPv6 address string as bind_host.  "
                "Domain names are not permitted in SOCKS5 reply packets."
            ),
        ) from exc

    atyp = AddrType.IPV4 if addr.version == 4 else AddrType.IPV6

    return (
        bytes([0x05, int(reply_code), 0x00, int(atyp)])
        + addr.packed
        + struct.pack("!H", bind_port)
    )


# ── UDP header parsing ────────────────────────────────────────────────────────


def parse_udp_header(data: bytes) -> tuple[bytes, str, int]:
    """Parse a SOCKS5 UDP datagram header and return ``(payload, host, port)``.

    Pure function — no I/O, no state.  Suitable for unit testing in isolation.

    Args:
        data: Raw bytes received from the SOCKS5 client, including the
              RFC 1928 §7 header.

    Returns:
        A ``(payload, host, port)`` tuple where *host* is a normalised IP or
        domain string and *port* is an integer in ``[1, 65535]``.

    Raises:
        ProtocolError:
            If the datagram is too short, uses an unsupported ATYP, carries a
            non-zero FRAG field, has a zero-length domain, contains a domain
            that is not valid UTF-8 or fails RFC 1123 / safety validation,
            or has port 0.
    """
    # Minimum: RSV(2) + FRAG(1) + ATYP(1) = 4 bytes.
    if len(data) < 4:
        raise ProtocolError(
            f"SOCKS5 UDP datagram too short: {len(data)} byte(s), minimum is 4.",
            error_code="protocol.socks5_udp_too_short",
            details={"received_bytes": len(data), "minimum_bytes": 4},
            hint="The SOCKS5 client sent a datagram shorter than the minimum header size.",
        )

    # FRAG != 0 means reassembly is required; we do not support fragmentation.
    if data[2] != 0:
        raise ProtocolError(
            f"SOCKS5 UDP fragmentation is not supported (FRAG={data[2]:#x}).",
            error_code="protocol.socks5_udp_fragmented",
            details={"frag": data[2]},
            hint=(
                "The SOCKS5 client requested UDP fragment reassembly, which "
                "exectunnel does not support.  Disable fragmentation on the client."
            ),
        )

    atyp = data[3]
    offset = 4

    if atyp == AddrType.IPV4:
        if len(data) < offset + 4 + 2:
            raise ProtocolError(
                "SOCKS5 UDP IPv4 datagram truncated before address+port.",
                error_code="protocol.socks5_udp_ipv4_truncated",
                details={
                    "received_bytes": len(data),
                    "required_bytes": offset + 4 + 2,
                },
                hint="The SOCKS5 client sent an incomplete IPv4 address field.",
            )
        host = str(ipaddress.IPv4Address(data[offset : offset + 4]))
        offset += 4

    elif atyp == AddrType.IPV6:
        if len(data) < offset + 16 + 2:
            raise ProtocolError(
                "SOCKS5 UDP IPv6 datagram truncated before address+port.",
                error_code="protocol.socks5_udp_ipv6_truncated",
                details={
                    "received_bytes": len(data),
                    "required_bytes": offset + 16 + 2,
                },
                hint="The SOCKS5 client sent an incomplete IPv6 address field.",
            )
        host = str(ipaddress.IPv6Address(data[offset : offset + 16]).compressed)
        offset += 16

    elif atyp == AddrType.DOMAIN:
        if len(data) < offset + 1:
            raise ProtocolError(
                "SOCKS5 UDP DOMAIN datagram truncated before length byte.",
                error_code="protocol.socks5_udp_domain_no_length",
                details={"received_bytes": len(data)},
                hint="The SOCKS5 client sent a DOMAIN header with no length byte.",
            )
        dlen = data[offset]
        offset += 1
        if dlen == 0:
            raise ProtocolError(
                "SOCKS5 UDP DOMAIN address length must be greater than zero.",
                error_code="protocol.socks5_udp_domain_zero_length",
                details={"atyp": hex(atyp)},
                hint=(
                    "The SOCKS5 client sent a zero-length domain name, which "
                    "violates RFC 1928 §7."
                ),
            )
        if len(data) < offset + dlen + 2:
            raise ProtocolError(
                "SOCKS5 UDP DOMAIN datagram truncated before domain bytes+port.",
                error_code="protocol.socks5_udp_domain_truncated",
                details={
                    "received_bytes": len(data),
                    "required_bytes": offset + dlen + 2,
                    "declared_length": dlen,
                },
                hint="The SOCKS5 client sent a domain name shorter than its declared length.",
            )
        try:
            host = data[offset : offset + dlen].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(
                "SOCKS5 UDP DOMAIN address bytes are not valid UTF-8.",
                error_code="protocol.socks5_udp_domain_bad_encoding",
                details={
                    "raw_bytes": data[offset : offset + dlen].hex(),
                    "declared_length": dlen,
                },
                hint=(
                    "The SOCKS5 client sent a domain name that cannot be decoded "
                    "as UTF-8.  Only ASCII/UTF-8 hostnames are supported."
                ),
            ) from exc
        validate_socks5_domain(host)
        offset += dlen

    else:
        raise ProtocolError(
            f"Unsupported SOCKS5 UDP address type: {atyp:#x}.",
            error_code="protocol.socks5_udp_unsupported_atyp",
            details={"atyp": hex(atyp)},
            hint=(
                "Only ATYP 0x01 (IPv4), 0x03 (DOMAIN), and 0x04 (IPv6) are "
                "supported per RFC 1928 §7."
            ),
        )

    if len(data) < offset + 2:
        raise ProtocolError(
            "SOCKS5 UDP datagram truncated before port field.",
            error_code="protocol.socks5_udp_no_port",
            details={
                "received_bytes": len(data),
                "required_bytes": offset + 2,
            },
            hint="The SOCKS5 client sent a datagram with no port field.",
        )

    port = struct.unpack("!H", data[offset : offset + 2])[0]
    if port == 0:
        raise ProtocolError(
            "SOCKS5 UDP datagram destination port is 0.",
            error_code="protocol.socks5_udp_zero_port",
            details={"host": host},
            hint="Port 0 is not a valid destination port in a SOCKS5 UDP datagram.",
        )

    payload = data[offset + 2 :]
    return payload, host, port
