"""Private SOCKS5 wire-format helpers.

All functions are **pure and synchronous** — no I/O, no asyncio, no network
calls.  They operate exclusively on raw :class:`bytes` and
:mod:`exectunnel.protocol` enums.

Async address reading lives in :mod:`exectunnel.proxy._io` to keep this
module side-effect free and trivially unit-testable.

Public surface (package-internal only — not re-exported from ``__init__.py``)
------------------------------------------------------------------------------
* :func:`validate_socks5_domain` — RFC 1123 domain safety check.
* :func:`parse_udp_header`       — SOCKS5 UDP datagram header parser (RFC 1928 §7).
* :func:`build_socks5_reply`     — SOCKS5 reply packet serialiser (RFC 1928 §6).

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
from exectunnel.proxy._constants import MAX_UDP_PAYLOAD_BYTES

__all__: list[str] = [
    "build_socks5_reply",
    "parse_udp_header",
    "validate_socks5_domain",
]

# ---------------------------------------------------------------------------
# Domain-name validation
# ---------------------------------------------------------------------------

# RFC 1123 relaxed: labels of 1–63 chars, total ≤ 253, no leading/trailing dot.
# re.ASCII ensures [A-Za-z0-9] never matches Unicode digits on exotic builds.
# Underscores are intentionally excluded — not valid per RFC 1123 and would
# corrupt tunnel frames alongside frame-unsafe characters.
_DOMAIN_LABEL_RE: re.Pattern[str] = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?$",
    re.ASCII,
)

# Characters that are either NUL (stream terminator) or frame-unsafe per the
# ExecTunnel wire format (protocol.md — FRAME_PREFIX / FRAME_SUFFIX chars).
# Blocked set: \x00 (NUL), : (field separator), < and > (frame delimiters).
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
    ``"example.com."`` is treated identically to ``"example.com"``.  A bare
    ``"."`` becomes an empty string after stripping and triggers the
    empty-label error — this is intentional.

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
# UDP datagram header parser
# ---------------------------------------------------------------------------


def parse_udp_header(data: bytes) -> tuple[bytes, str, int]:
    """Parse a SOCKS5 UDP datagram header and return ``(payload, host, port)``.

    Pure function — no I/O, no state.  Safe to call from any execution context
    including synchronous unit tests and the in-pod agent.

    Wire layout (RFC 1928 §7)::

        +----+------+------+----------+----------+----------+
        |RSV | FRAG | ATYP | DST.ADDR | DST.PORT |   DATA   |
        +----+------+------+----------+----------+----------+
        | 2  |  1   |  1   | Variable |    2     | Variable |
        +----+------+------+----------+----------+----------+

    Args:
        data: Raw bytes received from the SOCKS5 client, including the
              RFC 1928 §7 header.

    Returns:
        A ``(payload, host, port)`` tuple where *host* is a normalised IP or
        domain string and *port* is an integer in ``[1, 65535]``.

    Raises:
        ProtocolError:
            * Datagram shorter than 4 bytes (minimum header).
            * Non-zero FRAG field (fragmentation not supported).
            * Unsupported ATYP value.
            * Truncated address or port field.
            * Zero-length domain name.
            * Domain bytes not valid UTF-8.
            * Domain fails RFC 1123 / safety validation.
            * Destination port is 0.
    """
    # Minimum: RSV(2) + FRAG(1) + ATYP(1) = 4 bytes.
    if len(data) < 4:
        raise ProtocolError(
            f"SOCKS5 UDP datagram too short: {len(data)} byte(s), minimum is 4.",
            details={
                "socks5_field": "RSV+FRAG+ATYP",
                "expected": "at least 4 bytes (RSV + FRAG + ATYP)",
            },
            hint="The SOCKS5 client sent a datagram shorter than the minimum header size.",
        )

    # FRAG != 0 means reassembly is required; fragmentation is not supported.
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

    atyp = data[3]
    offset = 4

    if atyp == AddrType.IPV4:
        if len(data) < offset + 4 + 2:
            raise ProtocolError(
                "SOCKS5 UDP IPv4 datagram truncated before address+port.",
                details={
                    "socks5_field": "DST.ADDR",
                    "expected": f"at least {offset + 4 + 2} bytes for IPv4 address+port",
                },
                hint="The SOCKS5 client sent an incomplete IPv4 address field.",
            )
        host = str(ipaddress.IPv4Address(data[offset : offset + 4]))
        offset += 4

    elif atyp == AddrType.IPV6:
        if len(data) < offset + 16 + 2:
            raise ProtocolError(
                "SOCKS5 UDP IPv6 datagram truncated before address+port.",
                details={
                    "socks5_field": "DST.ADDR",
                    "expected": f"at least {offset + 16 + 2} bytes for IPv6 address+port",
                },
                hint="The SOCKS5 client sent an incomplete IPv6 address field.",
            )
        host = str(ipaddress.IPv6Address(data[offset : offset + 16]).compressed)
        offset += 16

    elif atyp == AddrType.DOMAIN:
        if len(data) < offset + 1:
            raise ProtocolError(
                "SOCKS5 UDP DOMAIN datagram truncated before length byte.",
                details={
                    "socks5_field": "DST.ADDR.LEN",
                    "expected": "at least 1 byte for domain length field",
                },
                hint="The SOCKS5 client sent a DOMAIN header with no length byte.",
            )
        dlen = data[offset]
        offset += 1

        if dlen == 0:
            raise ProtocolError(
                "SOCKS5 UDP DOMAIN address length must be greater than zero.",
                details={
                    "socks5_field": "DST.ADDR.LEN",
                    "expected": "domain length ≥ 1 (RFC 1928 §7)",
                },
                hint=(
                    "The SOCKS5 client sent a zero-length domain name, which "
                    "violates RFC 1928 §7."
                ),
            )

        if len(data) < offset + dlen + 2:
            raise ProtocolError(
                "SOCKS5 UDP DOMAIN datagram truncated before domain bytes+port.",
                details={
                    "socks5_field": "DST.ADDR",
                    "expected": f"at least {offset + dlen + 2} bytes for domain+port",
                },
                hint="The SOCKS5 client sent a domain name shorter than its declared length.",
            )

        raw_domain = data[offset : offset + dlen]
        try:
            host = raw_domain.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(
                "SOCKS5 UDP DOMAIN address bytes are not valid UTF-8.",
                details={
                    "socks5_field": "DST.ADDR",
                    "raw_bytes": raw_domain.hex()[:128],
                    "codec": "utf-8",
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
            details={
                "socks5_field": "ATYP",
                "expected": "ATYP 0x01 (IPv4), 0x03 (DOMAIN), or 0x04 (IPv6)",
            },
            hint=(
                "Only ATYP 0x01 (IPv4), 0x03 (DOMAIN), and 0x04 (IPv6) are "
                "supported per RFC 1928 §7."
            ),
        )

    if len(data) < offset + 2:
        raise ProtocolError(
            "SOCKS5 UDP datagram truncated before port field.",
            details={
                "socks5_field": "DST.PORT",
                "expected": f"at least {offset + 2} bytes for port field",
            },
            hint="The SOCKS5 client sent a datagram with no port field.",
        )

    port = struct.unpack("!H", data[offset : offset + 2])[0]
    if port == 0:
        raise ProtocolError(
            "SOCKS5 UDP datagram destination port is 0.",
            details={
                "socks5_field": "DST.PORT",
                "expected": "destination port in [1, 65535]",
            },
            hint="Port 0 is not a valid destination port in a SOCKS5 UDP datagram.",
        )

    payload = data[offset + 2 :]
    return payload, host, port


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
    error replies.  For success replies, callers should pass the actual bound
    address — see :meth:`~exectunnel.proxy.request.Socks5Request.send_reply_success`
    which defaults to ``"127.0.0.1"`` for the loopback tunnel use-case.

    Args:
        reply:     The SOCKS5 reply code as a
                   :class:`~exectunnel.protocol.Reply` member.
        bind_host: The ``BND.ADDR`` field.  Must be a valid IPv4 or IPv6
                   address string — RFC 1928 §6 prohibits domain names in
                   replies.  Defaults to ``"0.0.0.0"``.
        bind_port: The ``BND.PORT`` field.  Must be in ``[0, 65535]``.
                   Defaults to ``0``.

    Returns:
        A serialised SOCKS5 reply packet as :class:`bytes`.

    Raises:
        ConfigurationError:
            * *bind_port* is outside ``[0, 65535]``.
            * *bind_host* is not a valid IP address string.
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
