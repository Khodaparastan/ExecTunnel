"""Frame encode/decode and protocol constants.

Wire format (newline-terminated)
─────────────────────────────────
    <<<EXECTUNNEL:{msg_type}:{conn_id}[:{payload}]>>>\\n

Rules
─────
* ``msg_type`` and ``conn_id`` MUST NOT contain ``:``, ``<``, or ``>``.
* ``payload`` is always base64url (no padding) for DATA / UDP_DATA / ERROR
  frames, and ``{host}:{port}`` for OPEN frames (IPv6 hosts are
  bracket-quoted).
* ``parse_frame`` splits on ``:`` at most twice so base64url payloads that
  happen to contain ``:`` are never truncated.
* Host strings in OPEN frames are validated and normalised by
  ``encode_host_port`` before embedding; ``parse_host_port`` is the
  canonical inverse used by both sides.

Frame catalogue
───────────────
    AGENT_READY   — agent bootstrap complete   (no conn_id / payload)
    CONN_OPEN     — open a TCP connection       payload: [host]:port | host:port
    CONN_CLOSE    — close a TCP connection      (no payload)
    DATA          — TCP data chunk              payload: base64url
    UDP_OPEN      — open a UDP flow             payload: [host]:port | host:port
    UDP_DATA      — UDP datagram                payload: base64url
    UDP_CLOSE     — close a UDP flow            (no payload)
    ERROR         — agent error report          payload: base64url-encoded UTF-8

Exception contract
──────────────────
* ``FrameDecodingError``  — raised when a structurally valid tunnel frame
  carries a payload that cannot be decoded (bad base64url, bad host/port).
* ``ProtocolError``       — raised when an encoder receives arguments that
  would produce an invalid or unsafe frame (bad ID, unsafe host, port out
  of range, frame too long).
* ``UnexpectedFrameError``— NOT raised here; callers (session/proxy layers)
  raise it when a valid frame arrives in the wrong protocol state.
* Plain ``None``          — returned by ``parse_frame`` only for lines that
  are not tunnel frames at all (shell noise, blank lines, etc.).
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import logging
import re
from dataclasses import dataclass
from typing import Final

from exectunnel.exceptions import FrameDecodingError, ProtocolError

from .ids import ID_RE, _TCP_PREFIX, _TOKEN_BYTES

__all__ = [
    # ── Constants ──────────────────────────────────────────────────────────
    "FRAME_PREFIX",
    "FRAME_SUFFIX",
    "MAX_FRAME_LEN",
    "READY_FRAME",
    "SESSION_CONN_ID",
    # ── Frame result type ──────────────────────────────────────────────────
    "ParsedFrame",
    # ── Frame encoders ─────────────────────────────────────────────────────
    "encode_conn_close_frame",
    "encode_conn_open_frame",
    "encode_data_frame",
    "encode_error_frame",
    "encode_udp_close_frame",
    "encode_udp_data_frame",
    "encode_udp_open_frame",
    # ── Frame decoder ──────────────────────────────────────────────────────
    "is_ready_frame",
    "parse_frame",
    # ── Payload helpers ────────────────────────────────────────────────────
    "decode_binary_payload",
    "decode_error_payload",
    "encode_host_port",
    "parse_host_port",
]

log: Final[logging.Logger] = logging.getLogger(__name__)

# ── Frame protocol constants ──────────────────────────────────────────────────

FRAME_PREFIX: Final[str] = "<<<EXECTUNNEL:"
FRAME_SUFFIX: Final[str] = ">>>"

# Sentinel emitted by the agent once it is ready to accept tunnel frames.
READY_FRAME: Final[str] = "<<<EXECTUNNEL:AGENT_READY>>>"

# Session-level error sentinel: a valid conn_id-shaped value that is
# deliberately outside the random space (all-zero token) so callers can
# distinguish session-level errors from per-connection errors.
# Derived from ids constants so it stays consistent if the format ever changes.
SESSION_CONN_ID: Final[str] = _TCP_PREFIX + "0" * (_TOKEN_BYTES * 2)

# Maximum accepted frame length (characters, excluding the trailing newline).
# Frames longer than this are rejected by parse_frame to guard against memory
# exhaustion from malformed input.
MAX_FRAME_LEN: Final[int] = 8_192

# ── Allowed message types ─────────────────────────────────────────────────────

_VALID_MSG_TYPES: Final[frozenset[str]] = frozenset({
    "AGENT_READY",
    "CONN_OPEN",
    "CONN_CLOSE",
    "DATA",
    "UDP_OPEN",
    "UDP_DATA",
    "UDP_CLOSE",
    "ERROR",
})

# ── Validation helpers ────────────────────────────────────────────────────────

# Characters that are structurally significant in the frame wire format.
_FRAME_UNSAFE_RE: Final[re.Pattern[str]] = re.compile(r"[:<>]", re.ASCII)

# Minimal domain-name sanity check (intentionally loose — full RFC 1123
# validation is the resolver's job).  Accepts single-label names (e.g. "redis")
# which are common inside Kubernetes clusters.
_DOMAIN_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9\-.]*[A-Za-z0-9])?$", re.ASCII
)


def _validate_id(value: str, name: str = "id") -> None:
    """Raise :exc:`ProtocolError` if *value* is not a well-formed tunnel ID.

    Args:
        value: The ID string to validate.
        name:  Human-readable label used in the error message.

    Raises:
        ProtocolError: If *value* does not match ``ID_RE``.
    """
    if not ID_RE.match(value):
        raise ProtocolError(
            f"Invalid tunnel {name} {value!r}: must match [cu][0-9a-f]{{24}}",
            details={"frame_type": name, "expected": "[cu][0-9a-f]{24}"},
        )


def _validate_msg_type(msg_type: str) -> None:
    """Raise :exc:`ProtocolError` if *msg_type* is not in the allowed catalogue.

    Args:
        msg_type: The message type string to validate.

    Raises:
        ProtocolError: If *msg_type* is not a known frame type.
    """
    if msg_type not in _VALID_MSG_TYPES:
        raise ProtocolError(
            f"Unknown msg_type {msg_type!r}.",
            details={
                "frame_type": msg_type,
                "expected": sorted(_VALID_MSG_TYPES),
            },
        )


# ── Host / port codec ─────────────────────────────────────────────────────────


def encode_host_port(host: str, port: int) -> str:
    """Encode a host + port into the canonical wire payload for OPEN frames.

    * IPv6 addresses are bracket-quoted: ``[2001:db8::1]:8080``
    * IPv4 addresses and domain names are left bare: ``example.com:8080``
    * The port is validated to be in ``[1, 65535]``.
    * Domain names are checked for frame-unsafe characters and basic
      structural validity.

    Args:
        host: Destination hostname or IP address string.
        port: Destination TCP/UDP port number.

    Returns:
        A string safe to embed as the payload of a ``CONN_OPEN`` or
        ``UDP_OPEN`` frame.

    Raises:
        ProtocolError: If *port* is out of range, *host* is empty, or *host*
            contains characters that would corrupt the frame wire format.
    """
    if not host:
        raise ProtocolError(
            "host must not be empty",
            details={"frame_type": "OPEN", "expected": "non-empty hostname or IP"},
        )

    if not (1 <= port <= 65_535):
        raise ProtocolError(
            f"Port {port} is out of range [1, 65535]",
            details={
                "frame_type": "OPEN",
                "expected": "integer in [1, 65535]",
            },
        )

    # Normalise and bracket-quote IPv6 addresses.
    try:
        addr = ipaddress.ip_address(host)
        if isinstance(addr, ipaddress.IPv6Address):
            return f"[{addr.compressed}]:{port}"
        # IPv4 — bare is unambiguous.
        return f"{addr.compressed}:{port}"
    except ValueError:
        pass  # Not an IP literal — treat as domain name.

    # Domain name: reject frame-unsafe characters first (fast path).
    if _FRAME_UNSAFE_RE.search(host):
        raise ProtocolError(
            f"Host {host!r} contains frame-unsafe characters (':', '<', '>'). "
            "Possible injection attempt.",
            details={
                "frame_type": "OPEN",
                "expected": "hostname free of ':', '<', '>'",
            },
        )

    # Basic structural check: must look like a plausible hostname.
    if not _DOMAIN_RE.match(host):
        raise ProtocolError(
            f"Host {host!r} is not a valid hostname or IP address.",
            details={"frame_type": "OPEN", "expected": "valid RFC 1123 hostname"},
        )

    return f"{host}:{port}"


def parse_host_port(payload: str) -> tuple[str, int]:
    """Parse a ``[host]:port`` or ``host:port`` payload string.

    This is the canonical inverse of :func:`encode_host_port` and MUST be
    used by both the client and the agent to ensure consistent IPv6 handling.

    * Bracket-quoted IPv6: ``[2001:db8::1]:8080`` → ``("2001:db8::1", 8080)``
    * Bare IPv4 / domain:  ``example.com:8080``   → ``("example.com", 8080)``

    Args:
        payload: The raw payload string from a ``CONN_OPEN`` or ``UDP_OPEN``
            frame.

    Returns:
        A ``(host, port)`` tuple where *host* is a plain string (no brackets)
        and *port* is an integer.

    Raises:
        FrameDecodingError: If the payload is malformed, the host is empty,
            or the port is non-numeric / out of range.
    """
    if payload.startswith("["):
        # Bracket-quoted IPv6: [addr]:port
        bracket_end = payload.find("]")
        if bracket_end == -1 or payload[bracket_end + 1 : bracket_end + 2] != ":":
            raise FrameDecodingError(
                f"Malformed bracketed host in OPEN frame payload: {payload!r}",
                details={
                    "raw_bytes": payload.encode().hex()[:128],
                    "codec": "host:port",
                },
            )
        host = payload[1:bracket_end]
        port_str = payload[bracket_end + 2 :]
    else:
        # IPv4 or domain — rpartition splits on the *last* colon so that
        # bare (non-bracketed) IPv6 literals still work, though callers
        # should always bracket-quote IPv6 via encode_host_port.
        host, sep, port_str = payload.rpartition(":")
        if not sep:
            raise FrameDecodingError(
                f"Missing port separator in OPEN frame payload: {payload!r}",
                details={
                    "raw_bytes": payload.encode().hex()[:128],
                    "codec": "host:port",
                },
            )

    if not host:
        raise FrameDecodingError(
            f"Empty host in OPEN frame payload: {payload!r}",
            details={
                "raw_bytes": payload.encode().hex()[:128],
                "codec": "host:port",
            },
        )

    try:
        port = int(port_str)
    except ValueError as exc:
        raise FrameDecodingError(
            f"Non-numeric port {port_str!r} in OPEN frame payload: {payload!r}",
            details={
                "raw_bytes": payload.encode().hex()[:128],
                "codec": "host:port",
            },
        ) from exc

    if not (1 <= port <= 65_535):
        raise FrameDecodingError(
            f"Port {port} out of range in OPEN frame payload: {payload!r}",
            details={
                "raw_bytes": payload.encode().hex()[:128],
                "codec": "host:port",
            },
        )

    return host, port


# ── Parsed frame result type ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ParsedFrame:
    """The structured result of :func:`parse_frame`.

    Attributes:
        msg_type: One of the frame type strings in ``_VALID_MSG_TYPES``.
        conn_id:  Tunnel connection / flow ID, or ``""`` for ``AGENT_READY``.
        payload:  Frame payload string, or ``""`` when absent.
    """

    msg_type: str
    conn_id: str
    payload: str


# ── Frame codec ───────────────────────────────────────────────────────────────


def _encode_frame(msg_type: str, conn_id: str, payload: str = "") -> str:
    """Low-level frame encoder — validates inputs and returns a
    newline-terminated frame string.

    This function is intentionally private.  All callers outside this module
    should use the typed high-level helpers (``encode_conn_open_frame``, etc.).

    Args:
        msg_type: Must be a member of ``_VALID_MSG_TYPES``.
        conn_id:  Must match ``ID_RE``, or be ``""`` for ``AGENT_READY``.
        payload:  Optional payload string.  Must not contain ``FRAME_SUFFIX``
                  or ``FRAME_PREFIX``.

    Returns:
        A newline-terminated frame string.

    Raises:
        ProtocolError: On any validation failure or if the encoded frame
            content would exceed ``MAX_FRAME_LEN`` characters.
    """
    _validate_msg_type(msg_type)

    # AGENT_READY is the only frame that legitimately has no conn_id.
    if conn_id:
        _validate_id(conn_id, name=msg_type)

    # Guard against payload containing the frame suffix or prefix, which
    # would allow a crafted payload to terminate or inject frames.
    if FRAME_SUFFIX in payload:
        raise ProtocolError(
            f"Payload contains the frame suffix {FRAME_SUFFIX!r}, "
            "which would corrupt the wire format.",
            details={"frame_type": msg_type, "expected": f"payload free of {FRAME_SUFFIX!r}"},
        )
    if FRAME_PREFIX in payload:
        raise ProtocolError(
            f"Payload contains the frame prefix {FRAME_PREFIX!r}, "
            "which would corrupt the wire format.",
            details={"frame_type": msg_type, "expected": f"payload free of {FRAME_PREFIX!r}"},
        )

    if conn_id and payload:
        content = f"{FRAME_PREFIX}{msg_type}:{conn_id}:{payload}{FRAME_SUFFIX}"
    elif conn_id:
        content = f"{FRAME_PREFIX}{msg_type}:{conn_id}{FRAME_SUFFIX}"
    else:
        # No conn_id (AGENT_READY).
        content = f"{FRAME_PREFIX}{msg_type}{FRAME_SUFFIX}"

    if len(content) > MAX_FRAME_LEN:
        raise ProtocolError(
            f"Encoded frame length {len(content)} exceeds MAX_FRAME_LEN={MAX_FRAME_LEN}.",
            details={
                "frame_type": msg_type,
                "expected": f"frame content ≤ {MAX_FRAME_LEN} chars",
            },
        )

    return content + "\n"


def encode_conn_open_frame(conn_id: str, host: str, port: int) -> str:
    """Encode a ``CONN_OPEN`` frame.

    The host/port payload is normalised by :func:`encode_host_port`, which
    bracket-quotes IPv6 addresses and rejects frame-unsafe characters.

    Args:
        conn_id: TCP connection ID produced by :func:`~exectunnel.protocol.ids.new_conn_id`.
        host:    Destination hostname or IP address.
        port:    Destination TCP port.

    Returns:
        A newline-terminated ``CONN_OPEN`` frame string.

    Raises:
        ProtocolError: If *conn_id*, *host*, or *port* are invalid.
    """
    return _encode_frame("CONN_OPEN", conn_id, encode_host_port(host, port))


def encode_conn_close_frame(conn_id: str) -> str:
    """Encode a ``CONN_CLOSE`` frame.

    Signals explicit TCP connection teardown.  Both sides MUST emit this frame
    when closing a connection so the peer can release associated resources
    without relying on timeout heuristics.

    Args:
        conn_id: TCP connection ID of the connection being closed.

    Returns:
        A newline-terminated ``CONN_CLOSE`` frame string.

    Raises:
        ProtocolError: If *conn_id* is invalid.
    """
    return _encode_frame("CONN_CLOSE", conn_id)


def encode_data_frame(conn_id: str, data: bytes) -> str:
    """Encode a ``DATA`` frame.

    The raw bytes are base64url-encoded (no padding) before embedding so that
    the payload is guaranteed to be free of frame-unsafe characters.

    Args:
        conn_id: TCP connection ID.
        data:    Raw bytes to transmit.

    Returns:
        A newline-terminated ``DATA`` frame string.

    Raises:
        ProtocolError: If *conn_id* is invalid or the encoded frame exceeds
            ``MAX_FRAME_LEN``.
    """
    payload_b64 = base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
    return _encode_frame("DATA", conn_id, payload_b64)


def encode_udp_open_frame(flow_id: str, host: str, port: int) -> str:
    """Encode a ``UDP_OPEN`` frame.

    Same host/port normalisation rules as :func:`encode_conn_open_frame`.

    Args:
        flow_id: UDP flow ID produced by :func:`~exectunnel.protocol.ids.new_flow_id`.
        host:    Destination hostname or IP address.
        port:    Destination UDP port.

    Returns:
        A newline-terminated ``UDP_OPEN`` frame string.

    Raises:
        ProtocolError: If *flow_id*, *host*, or *port* are invalid.
    """
    return _encode_frame("UDP_OPEN", flow_id, encode_host_port(host, port))


def encode_udp_data_frame(flow_id: str, data: bytes) -> str:
    """Encode a ``UDP_DATA`` frame.

    Args:
        flow_id: UDP flow ID.
        data:    Raw datagram bytes to transmit.

    Returns:
        A newline-terminated ``UDP_DATA`` frame string.

    Raises:
        ProtocolError: If *flow_id* is invalid or the encoded frame exceeds
            ``MAX_FRAME_LEN``.
    """
    payload_b64 = base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
    return _encode_frame("UDP_DATA", flow_id, payload_b64)


def encode_udp_close_frame(flow_id: str) -> str:
    """Encode a ``UDP_CLOSE`` frame.

    Args:
        flow_id: UDP flow ID of the flow being closed.

    Returns:
        A newline-terminated ``UDP_CLOSE`` frame string.

    Raises:
        ProtocolError: If *flow_id* is invalid.
    """
    return _encode_frame("UDP_CLOSE", flow_id)


def encode_error_frame(conn_id: str, message: str) -> str:
    """Encode an ``ERROR`` frame.

    The error message is UTF-8 encoded then base64url-encoded so that
    arbitrary diagnostic text (including newlines and non-ASCII characters)
    can be transmitted without corrupting the frame stream.

    Args:
        conn_id: The connection / flow ID associated with the error, or
            ``SESSION_CONN_ID`` for session-level errors.
        message: Human-readable error description.

    Returns:
        A newline-terminated ``ERROR`` frame string.

    Raises:
        ProtocolError: If *conn_id* is invalid or the encoded frame exceeds
            ``MAX_FRAME_LEN``.
    """
    payload_b64 = (
        base64.urlsafe_b64encode(message.encode("utf-8")).rstrip(b"=").decode("ascii")
    )
    return _encode_frame("ERROR", conn_id, payload_b64)


def decode_binary_payload(payload: str) -> bytes:
    """Decode a base64url payload (no padding) from a ``DATA`` or ``UDP_DATA``
    frame back into raw bytes.

    Padding is re-added before decoding to satisfy the standard library.

    Args:
        payload: The raw payload string from a parsed frame.

    Returns:
        Decoded bytes.

    Raises:
        FrameDecodingError: If *payload* is not valid base64url, with
            ``details["raw_bytes"]`` containing a hex-encoded truncated
            excerpt and ``details["codec"]`` set to ``"base64url"``.
    """
    padding = (4 - len(payload) % 4) % 4
    try:
        return base64.urlsafe_b64decode(payload + "=" * padding)
    except binascii.Error as exc:
        raise FrameDecodingError(
            f"Invalid base64url payload: {payload[:64]!r}{'...' if len(payload) > 64 else ''}",
            details={
                "raw_bytes": payload.encode("ascii", errors="replace").hex()[:128],
                "codec": "base64url",
            },
        ) from exc


def decode_error_payload(payload: str) -> str:
    """Decode the base64url payload of an ``ERROR`` frame into a UTF-8 string.

    This is the typed inverse of :func:`encode_error_frame`.

    Args:
        payload: The raw payload string from a parsed ``ERROR`` frame.

    Returns:
        The decoded error message string.

    Raises:
        FrameDecodingError: If *payload* is not valid base64url or the
            decoded bytes are not valid UTF-8.
    """
    raw = decode_binary_payload(payload)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FrameDecodingError(
            "ERROR frame payload is not valid UTF-8 after base64url decoding.",
            details={
                "raw_bytes": raw.hex()[:128],
                "codec": "utf-8",
            },
        ) from exc

def parse_frame(line: str) -> ParsedFrame | None:
    """Parse one line into a :class:`ParsedFrame` or return ``None``.

    Return semantics
    ────────────────
    * ``None``               — the line is not a tunnel frame (shell noise,
                               blank lines, bootstrap output, etc.).  This is
                               the normal case during agent startup and is NOT
                               an error.
    * ``ParsedFrame``        — a structurally valid, recognised tunnel frame.
    * ``FrameDecodingError`` — raised when the line carries the tunnel
                               prefix/suffix but its internal structure is
                               corrupt.  Callers MUST propagate this upward;
                               it indicates a protocol violation, not noise.

    Splitting strategy
    ──────────────────
    ``inner.split(":", 2)`` limits the split to at most two colons, so a
    base64url payload that happens to contain ``:`` is never truncated.

    Args:
        line: A single line of text from the tunnel channel (may or may not
            be newline-terminated).

    Returns:
        A :class:`ParsedFrame` on success, ``None`` if the line is not a
        tunnel frame.

    Raises:
        FrameDecodingError: If the line has the tunnel frame prefix/suffix but
            contains a malformed ``conn_id`` or an unrecognised ``msg_type``.
            ``details["raw_bytes"]`` holds a hex-encoded truncated excerpt;
            ``details["codec"]`` is ``"frame"``.
    """
    line = line.strip()

    # Fast-path: oversized lines are never tunnel frames — drop silently.
    if len(line) > MAX_FRAME_LEN:
        log.debug("parse_frame: dropping oversized line (%d chars)", len(line))
        return None

    # Fast-path: not a tunnel frame at all (shell noise, blank lines, etc.).
    if not (line.startswith(FRAME_PREFIX) and line.endswith(FRAME_SUFFIX)):
        return None

    # ── From here on the line IS a tunnel frame — errors are protocol faults,
    #    not noise, and must be raised rather than silently dropped. ──────────

    inner = line[len(FRAME_PREFIX) : -len(FRAME_SUFFIX)]
    parts = inner.split(":", 2)

    msg_type = parts[0]

    if msg_type not in _VALID_MSG_TYPES:
        raise FrameDecodingError(
            f"Tunnel frame carries unrecognised msg_type {msg_type!r}.",
            details={
                "raw_bytes": line.encode("ascii", errors="replace").hex()[:128],
                "codec": "frame",
            },
        )

    conn_id = parts[1] if len(parts) > 1 else ""
    payload = parts[2] if len(parts) > 2 else ""

    # Validate conn_id format for frames that carry one.
    if conn_id and not ID_RE.match(conn_id):
        raise FrameDecodingError(
            f"Tunnel frame has malformed conn_id {conn_id!r} "
            f"(msg_type={msg_type!r}).",
            details={
                "raw_bytes": line.encode("ascii", errors="replace").hex()[:128],
                "codec": "frame",
            },
        )

    return ParsedFrame(msg_type=msg_type, conn_id=conn_id, payload=payload)


def is_ready_frame(line: str) -> bool:
    """Return ``True`` if *line* is the agent-ready sentinel frame.

    Delegates to :func:`parse_frame` so that whitespace stripping and all
    structural validation are applied consistently.

    ``FrameDecodingError`` from :func:`parse_frame` is intentionally **not**
    caught here — a corrupt frame that looks like ``AGENT_READY`` is a
    protocol fault and must propagate to the caller (bootstrap layer).

    Args:
        line: A single line of text from the tunnel channel.

    Returns:
        ``True`` if the line is the ``AGENT_READY`` sentinel.

    Raises:
        FrameDecodingError: Propagated from :func:`parse_frame` if the line
            has the tunnel prefix/suffix but is structurally corrupt.
    """
    parsed = parse_frame(line)
    return parsed is not None and parsed.msg_type == "AGENT_READY"
