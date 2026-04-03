"""Private low-level SOCKS5 wire I/O helpers."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import struct

from exectunnel.exceptions import ConfigurationError, ProtocolError
from exectunnel.protocol.enums import AddrType, Reply

# ── Domain-name validation ────────────────────────────────────────────────────

# RFC 1123 relaxed: labels of 1–63 chars, total ≤ 253, no leading/trailing dot.
# Underscores are intentionally excluded: they are not valid per RFC 1123 and
# would corrupt tunnel frames if they appeared alongside frame-unsafe chars.
# Real-world SRV / DMARC labels (_dmarc, _sip) use underscores; if you need
# them, add a permissive flag to _validate_domain and document the trade-off.
#
# We also reject null bytes and the frame-unsafe chars validated by the
# protocol layer (: < >) so a hostile domain can never corrupt a tunnel frame.
_DOMAIN_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?$")
_DOMAIN_UNSAFE_RE = re.compile(r"[\x00:<>]")

# Maximum number of bytes read_exact() will accept in a single call.
# Protects against a caller accidentally passing a huge length field.
_MAX_READ_BYTES: int = 65_535

__all__ = ["build_reply", "read_addr", "read_exact"]


# ── Helpers ───────────────────────────────────────────────────────────────────


def _validate_domain(domain: str) -> None:
    """Raise :class:`ProtocolError` if *domain* is not a safe, well-formed hostname.

    Checks performed:

    * Total length ≤ 253 characters.
    * No frame-unsafe characters (``\\x00``, ``:``, ``<``, ``>``).
    * Each dot-separated label matches RFC 1123 rules (1–63 chars,
      alphanumeric start/end, hyphens allowed in the middle).

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
                "(':', '<', '>'). This may indicate a protocol injection attempt."
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


# ── Public API ────────────────────────────────────────────────────────────────


async def read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    """Read exactly *n* bytes from *reader*.

    Args:
        reader: The asyncio stream to read from.
        n:      Number of bytes to read.  Must be in ``[1, 65535]``.

    Returns:
        Exactly *n* bytes.

    Raises:
        ValueError:    If *n* is outside ``[1, 65535]`` — indicates a caller
                       logic bug; check the SOCKS5 length field before calling.
        ProtocolError: If the stream ends before *n* bytes are available.
    """
    if not (1 <= n <= _MAX_READ_BYTES):
        raise ValueError(
            f"read_exact() called with n={n}; must be in [1, {_MAX_READ_BYTES}]. "
            "This is a caller bug — check the SOCKS5 length field before calling."
        )
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
        # IPv4Address(bytes[4]) never raises ValueError — no try/except needed.
        raw = await read_exact(reader, 4)
        host = str(ipaddress.IPv4Address(raw))

    elif atyp == AddrType.IPV6:
        # IPv6Address(bytes[16]) never raises ValueError — no try/except needed.
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
                    "as UTF-8. Only ASCII/UTF-8 hostnames are supported."
                ),
            ) from exc
        # Validate domain structure and safety before it touches any frame.
        _validate_domain(host)

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


def build_reply(
    reply: Reply | int,
    bind_host: str = "0.0.0.0",
    bind_port: int = 0,
) -> bytes:
    """Serialise a SOCKS5 reply packet.

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
        reply = Reply(reply)
    except ValueError as exc:
        raise ConfigurationError(
            f"reply {reply!r} is not a valid SOCKS5 Reply code.",
            error_code="config.socks5_invalid_reply_code",
            details={
                "reply": reply,
                "valid_codes": [r.value for r in Reply],
            },
            hint=(
                "Pass a member of exectunnel.protocol.enums.Reply. "
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
                "Ensure the bind_port passed to build_reply() is a valid "
                "TCP/UDP port number. This is a caller bug, not a client error."
            ),
        )

    try:
        addr = ipaddress.ip_address(bind_host)
    except ValueError as exc:
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
