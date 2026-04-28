"""Pure, stateless codecs used by both the encoder and parser layers.

This module contains:

* :func:`encode_host_port` / :func:`parse_host_port` — the ``[host]:port``
  payload codec for ``CONN_OPEN`` and ``UDP_OPEN`` frames.
* :func:`encode_binary_payload` / :func:`decode_binary_payload` —
  base64url codec (no padding) for ``DATA`` / ``UDP_DATA`` / ``ERROR``
  payloads.
* :func:`decode_error_payload` — convenience wrapper that additionally
  decodes UTF-8.

The codecs are deliberately free of logging, I/O, and sibling-module
imports other than :mod:`exectunnel.protocol.constants` and the package
exception module.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
from typing import Final

from exectunnel.exceptions import FrameDecodingError, ProtocolError

from .constants import (
    MAX_TCP_UDP_PORT,
    MIN_TCP_UDP_PORT,
    PAYLOAD_PREVIEW_LEN,
)

__all__ = [
    "decode_binary_payload",
    "decode_error_payload",
    "encode_binary_payload",
    "encode_host_port",
    "parse_host_port",
]

# ── Validation patterns ───────────────────────────────────────────────────────

_FRAME_UNSAFE_RE: Final[re.Pattern[str]] = re.compile(r"[:<>]", re.ASCII)

# Intentionally loose — full RFC 1123 per-label validation is the resolver's
# job. Underscores permitted for Kubernetes / SRV / DMARC compatibility.
# Trailing dots (FQDN form) are not supported; callers must strip before use.
_DOMAIN_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9_]([A-Za-z0-9\-_.]*[A-Za-z0-9_])?$",
    re.ASCII,
)

# Valid base64url alphabet: A-Z a-z 0-9 - _  (RFC 4648 §5).
# urlsafe_b64decode silently discards non-alphabet chars, so we must
# validate the alphabet explicitly before calling it.
_BASE64URL_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]*$", re.ASCII)

_HEX_PREVIEW_CHARS: Final[int] = 128


# ── Internal helpers ──────────────────────────────────────────────────────────


def _hex_preview(text: str) -> str:
    """Return a truncated ASCII-safe hex preview of *text* for telemetry.

    Args:
        text: The potentially unsafe string to encode.

    Returns:
        At most :data:`_HEX_PREVIEW_CHARS` hex characters of the ASCII
        encoding of *text*, with unencodable characters replaced.
    """
    return text.encode("ascii", errors="replace").hex()[:_HEX_PREVIEW_CHARS]


def _truncate_for_error(payload: str) -> str:
    """Return a short preview of *payload* suitable for inclusion in messages.

    Args:
        payload: The payload string to preview.

    Returns:
        The first :data:`PAYLOAD_PREVIEW_LEN` characters followed by
        ``"..."`` if the payload was truncated, otherwise the payload
        unchanged.
    """
    if len(payload) <= PAYLOAD_PREVIEW_LEN:
        return payload
    return payload[:PAYLOAD_PREVIEW_LEN] + "..."


# ── Host / port codec ─────────────────────────────────────────────────────────


def encode_host_port(host: str, port: int) -> str:
    """Encode a host and port into the canonical OPEN-frame payload.

    * IPv6 addresses are bracket-quoted: ``[2001:db8::1]:8080``.
    * IPv4 addresses and domain names are left bare: ``example.com:8080``.
    * IPv6 and IPv4 addresses are normalised to compressed form.
    * Domain names may contain underscores (Kubernetes/SRV compatibility).

    Args:
        host: Destination hostname or IP address string.
        port: Destination TCP/UDP port number in ``[1, 65535]``.

    Returns:
        A string safe to embed as the payload of a ``CONN_OPEN`` or
        ``UDP_OPEN`` frame.

    Raises:
        ProtocolError: If *port* is out of range, *host* is empty, contains
            frame-unsafe characters, contains consecutive dots, or is not a
            valid hostname or IP address.
    """
    if not host:
        raise ProtocolError(
            "host must not be empty.",
            details={"frame_type": "OPEN", "expected": "non-empty hostname or IP"},
        )

    if not (MIN_TCP_UDP_PORT <= port <= MAX_TCP_UDP_PORT):
        raise ProtocolError(
            f"Port {port} is out of range [{MIN_TCP_UDP_PORT}, {MAX_TCP_UDP_PORT}].",
            details={
                "frame_type": "OPEN",
                "expected": f"integer in [{MIN_TCP_UDP_PORT}, {MAX_TCP_UDP_PORT}]",
                "got": port,
            },
        )

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None

    if addr is not None:
        if isinstance(addr, ipaddress.IPv6Address):
            return f"[{addr.compressed}]:{port}"
        return f"{addr.compressed}:{port}"

    if _FRAME_UNSAFE_RE.search(host):
        raise ProtocolError(
            f"Host {host!r} contains frame-unsafe characters (':', '<', '>'). "
            "Possible injection attempt.",
            details={
                "frame_type": "OPEN",
                "expected": "hostname free of ':', '<', '>'",
                "got": host,
            },
        )

    if ".." in host:
        raise ProtocolError(
            f"Host {host!r} contains consecutive dots, which is not a valid hostname.",
            details={
                "frame_type": "OPEN",
                "expected": "hostname without consecutive dots",
                "got": host,
            },
        )

    if not _DOMAIN_RE.match(host):
        raise ProtocolError(
            f"Host {host!r} is not a valid hostname or IP address.",
            details={
                "frame_type": "OPEN",
                "expected": "valid RFC 1123 hostname (underscores permitted)",
                "got": host,
            },
        )

    return f"{host}:{port}"


def parse_host_port(payload: str) -> tuple[str, int]:
    """Parse a ``[host]:port`` or ``host:port`` payload string.

    This is the canonical inverse of :func:`encode_host_port`.

    Args:
        payload: The raw payload string from a ``CONN_OPEN`` or
            ``UDP_OPEN`` frame.

    Returns:
        A ``(host, port)`` tuple where *host* is a plain string (no
        brackets) and *port* is an integer in ``[1, 65535]``.

    Raises:
        FrameDecodingError: If the payload is malformed.
    """
    raw_hex = _hex_preview(payload)
    bracketed = payload.startswith("[")

    if bracketed:
        bracket_end = payload.find("]")
        if bracket_end == -1 or payload[bracket_end + 1 : bracket_end + 2] != ":":
            raise FrameDecodingError(
                f"Malformed bracketed host in OPEN frame payload: {payload!r}",
                details={"raw_bytes": raw_hex, "codec": "host:port"},
            )
        host = payload[1:bracket_end]
        port_str = payload[bracket_end + 2 :]
    else:
        host, sep, port_str = payload.rpartition(":")
        if not sep:
            raise FrameDecodingError(
                f"Missing port separator in OPEN frame payload: {payload!r}",
                details={"raw_bytes": raw_hex, "codec": "host:port"},
            )

    if not host:
        raise FrameDecodingError(
            f"Empty host in OPEN frame payload: {payload!r}",
            details={"raw_bytes": raw_hex, "codec": "host:port"},
        )

    try:
        port = int(port_str)
    except ValueError as exc:
        raise FrameDecodingError(
            f"Non-numeric port {port_str!r} in OPEN frame payload: {payload!r}",
            details={"raw_bytes": raw_hex, "codec": "host:port"},
        ) from exc

    if not (MIN_TCP_UDP_PORT <= port <= MAX_TCP_UDP_PORT):
        raise FrameDecodingError(
            f"Port {port} out of range "
            f"[{MIN_TCP_UDP_PORT}, {MAX_TCP_UDP_PORT}] "
            f"in OPEN frame payload: {payload!r}",
            details={"raw_bytes": raw_hex, "codec": "host:port"},
        )

    if bracketed:
        try:
            addr = ipaddress.ip_address(host)
        except ValueError as exc:
            raise FrameDecodingError(
                f"Bracketed host {host!r} in OPEN frame payload is not an IP address.",
                details={"raw_bytes": raw_hex, "codec": "host:port"},
            ) from exc
        if not isinstance(addr, ipaddress.IPv6Address):
            raise FrameDecodingError(
                f"Bracketed host {host!r} in OPEN frame payload is not IPv6.",
                details={"raw_bytes": raw_hex, "codec": "host:port"},
            )
    elif ":" in host:
        raise FrameDecodingError(
            f"Unbracketed host {host!r} contains ':' in OPEN frame payload. "
            "IPv6 addresses must use [addr]:port form.",
            details={"raw_bytes": raw_hex, "codec": "host:port"},
        )

    try:
        # Reuse the encoder-side validator so decode accepts only the canonical
        # host/port domain that this protocol can generate.
        encode_host_port(host, port)
    except ProtocolError as exc:
        raise FrameDecodingError(
            f"Invalid host {host!r} in OPEN frame payload: {payload!r}",
            details={"raw_bytes": raw_hex, "codec": "host:port"},
        ) from exc

    return host, port


# ── Binary (base64url) payload codec ──────────────────────────────────────────


def encode_binary_payload(data: bytes) -> str:
    """Encode raw bytes to base64url without trailing ``=`` padding.

    Args:
        data: Raw bytes to encode.

    Returns:
        The base64url-encoded string.
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode_binary_payload(payload: str) -> bytes:
    """Decode a base64url payload (no padding) back into raw bytes.

    Args:
        payload: The raw base64url string from a ``DATA``, ``UDP_DATA``,
            or ``ERROR`` frame.

    Returns:
        Decoded raw bytes.

    Raises:
        FrameDecodingError: If *payload* contains non-base64url characters
            or is structurally invalid.
    """
    if not _BASE64URL_RE.match(payload):
        raise FrameDecodingError(
            f"Invalid base64url payload: {_truncate_for_error(payload)!r}",
            details={"raw_bytes": _hex_preview(payload), "codec": "base64url"},
        )
    padding = (4 - len(payload) % 4) % 4
    try:
        return base64.urlsafe_b64decode(payload + "=" * padding)
    except (binascii.Error, ValueError) as exc:
        raise FrameDecodingError(
            f"Invalid base64url payload: {_truncate_for_error(payload)!r}",
            details={"raw_bytes": _hex_preview(payload), "codec": "base64url"},
        ) from exc


def decode_error_payload(payload: str) -> str:
    """Decode an ``ERROR`` frame payload into a UTF-8 string.

    Args:
        payload: The raw base64url string from an ``ERROR`` frame.

    Returns:
        The decoded UTF-8 error message.

    Raises:
        FrameDecodingError: If *payload* is not valid base64url or the
            decoded bytes are not valid UTF-8.
    """
    raw = decode_binary_payload(payload)
    try:
        return raw.decode()
    except UnicodeDecodeError as exc:
        raise FrameDecodingError(
            "ERROR frame payload is not valid UTF-8 after base64url decoding.",
            details={"raw_bytes": raw.hex()[:_HEX_PREVIEW_CHARS], "codec": "utf-8"},
        ) from exc
