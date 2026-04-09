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
* ``parse_frame`` splits on ``:`` at most twice so that OPEN frame payloads
  containing bracket-quoted IPv6 addresses (e.g. ``[2001:db8::1]:443``) are
  not fragmented at the colons inside the address.  base64url payloads never
  contain ``:`` so DATA / UDP_DATA / ERROR frames are unaffected.
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
    ERROR         — error report                payload: base64url-encoded UTF-8

Exception contract
──────────────────
* ``FrameDecodingError``   — raised when a structurally valid tunnel frame
  carries a payload that cannot be decoded (bad base64url, bad host/port,
  unknown msg_type, malformed conn_id).
* ``ProtocolError``        — raised when an encoder receives arguments that
  would produce an invalid or unsafe frame (bad ID, unsafe host, port out
  of range, frame too long, missing required conn_id).
* ``UnexpectedFrameError`` — NOT raised here; callers (session/proxy layers)
  raise it when a valid frame arrives in the wrong protocol state.
* Plain ``None``           — returned by ``parse_frame`` only for lines that
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

from .ids import ID_RE
from .ids import SESSION_CONN_ID as _SESSION_CONN_ID

__all__ = [
    # ── Constants ──────────────────────────────────────────────────────────
    "FRAME_PREFIX",
    "FRAME_SUFFIX",
    "MAX_FRAME_LEN",
    "PORT_UNSPECIFIED",
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

# Sentinel for the RFC 1928 §6 "unspecified" bind address in SOCKS5 error
# replies.  This value is intentionally NOT accepted by encode_host_port or
# parse_host_port (both reject port 0) because it is only meaningful in
# build_socks5_reply, which has its own separate path.  Defining it as a named
# constant prevents accidental use of the bare literal 0 in reply-building code.
PORT_UNSPECIFIED: Final[int] = 0

# Sentinel emitted by the agent once it is ready to accept tunnel frames.
READY_FRAME: Final[str] = "<<<EXECTUNNEL:AGENT_READY>>>"

# Session-level error sentinel: a valid conn_id-shaped value that is
# deliberately outside the random space (all-zero token) so callers can
# distinguish session-level errors from per-connection errors.
# Defined in ids.py and re-exported here so the public surface is unchanged.
SESSION_CONN_ID: Final[str] = _SESSION_CONN_ID

# Maximum accepted frame length (characters, excluding the trailing newline).
# Frames longer than this are rejected by parse_frame to guard against memory
# exhaustion from malformed or adversarial input.
#
# Maximum safe DATA payload derivation:
#   available = 8192 - len(FRAME_PREFIX) - len("DATA") - 2*len(":") - 25 - len(FRAME_SUFFIX)
#             = 8192 - 14 - 4 - 2 - 25 - 3
#             = 8144 base64url chars
#   max raw bytes = floor(8144 * 3 / 4) = 6108 bytes
#
# PIPE_READ_CHUNK_BYTES (4096) in the transport layer is set well below this
# limit to ensure individual read chunks always fit in a single frame.
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
    # NOTE: KEEPALIVE is intentionally absent from _VALID_MSG_TYPES.
    # KEEPALIVE is a client→agent-only heartbeat frame; the agent silently
    # discards it and never echoes it back.  parse_frame on the inbound
    # (agent→client) path would therefore never encounter a KEEPALIVE frame.
    # If that invariant ever changes, add KEEPALIVE here and add a
    # corresponding entry in _NO_CONN_ID_TYPES.
})

# Frame types that carry no conn_id on the wire.
# Currently only AGENT_READY; kept as a set for forward extensibility.
_NO_CONN_ID_TYPES: Final[frozenset[str]] = frozenset({"AGENT_READY"})

# ── Validation helpers ────────────────────────────────────────────────────────

# Characters that are structurally significant in the frame wire format.
_FRAME_UNSAFE_RE: Final[re.Pattern[str]] = re.compile(r"[:<>]", re.ASCII)

# Minimal domain-name sanity check (intentionally loose — full RFC 1123
# validation is the resolver's job).  Accepts single-label names (e.g. "redis")
# which are common inside Kubernetes clusters.
#
# Rejects:
#   - empty string (caught before regex)
#   - labels starting or ending with "-"
#   - consecutive dots ".." (caught before regex)
#   - any of ":", "<", ">" (caught before regex by _FRAME_UNSAFE_RE)
_DOMAIN_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9\-.]*[A-Za-z0-9])?$", re.ASCII
)


def _validate_id(value: str, name: str = "id") -> None:
    """Raise :exc:`ProtocolError` if *value* is not a well-formed tunnel ID.

    Args:
        value: The ID string to validate.
        name:  Human-readable label used in the error message (typically the
               frame type, e.g. ``"CONN_OPEN"``).

    Raises:
        ProtocolError: If *value* does not match ``ID_RE``.
    """
    if not ID_RE.match(value):
        raise ProtocolError(
            f"Invalid tunnel {name} ID {value!r}: must match [cu][0-9a-f]{{24}}",
            details={"frame_type": name, "expected": "[cu][0-9a-f]{24}"},
        )


# ── Host / port codec ─────────────────────────────────────────────────────────


def encode_host_port(host: str, port: int) -> str:
    """Encode a host + port into the canonical wire payload for OPEN frames.

    * IPv6 addresses are bracket-quoted: ``[2001:db8::1]:8080``
    * IPv4 addresses and domain names are left bare: ``example.com:8080``
    * IPv6 addresses are normalised to compressed form via
      ``ipaddress.IPv6Address.compressed`` to prevent ambiguity between
      representations such as ``::1`` and ``0:0:0:0:0:0:0:1``.
    * The port is validated to be in ``[1, 65535]``.  Port ``0`` is excluded
      because it is not a valid destination port for OPEN frames.
      ``build_socks5_reply`` in the proxy layer accepts port ``0`` as the
      RFC 1928 §6 "unspecified" sentinel for error replies — that is a
      separate, asymmetric use-case that does not go through this function.
    * Domain names are checked for frame-unsafe characters (``:``, ``<``,
      ``>``), consecutive dots, and basic structural validity before
      embedding.

    Args:
        host: Destination hostname or IP address string.
        port: Destination TCP/UDP port number (``[1, 65535]``).

    Returns:
        A string safe to embed as the payload of a ``CONN_OPEN`` or
        ``UDP_OPEN`` frame.

    Raises:
        ProtocolError: If *port* is out of range, *host* is empty, *host*
            contains characters that would corrupt the frame wire format,
            or *host* contains consecutive dots.
    """
    if not host:
        raise ProtocolError(
            "host must not be empty",
            details={"frame_type": "OPEN", "expected": "non-empty hostname or IP"},
        )

    if not (1 <= port <= 65_535):
        raise ProtocolError(
            f"Port {port} is out of range [1, 65535]",
            details={"frame_type": "OPEN", "expected": "integer in [1, 65535]"},
        )

    # Normalise and bracket-quote IPv6 addresses; normalise IPv4 to dotted-
    # decimal.  ipaddress rejects anything that is not a valid IP literal, so
    # this path is safe against injection via malformed IP strings.
    try:
        addr = ipaddress.ip_address(host)
        if isinstance(addr, ipaddress.IPv6Address):
            return f"[{addr.compressed}]:{port}"
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

    # Reject consecutive dots — these produce malformed DNS labels and cannot
    # be a valid hostname.
    if ".." in host:
        raise ProtocolError(
            f"Host {host!r} contains consecutive dots, which is not a valid hostname.",
            details={
                "frame_type": "OPEN",
                "expected": "hostname without consecutive dots",
            },
        )

    # Basic structural check: must look like a plausible RFC 1123 hostname.
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

    Port ``0`` is rejected for the same reason as in :func:`encode_host_port`:
    it is not a valid destination port for OPEN frames.

    Args:
        payload: The raw payload string from a ``CONN_OPEN`` or ``UDP_OPEN``
            frame.

    Returns:
        A ``(host, port)`` tuple where *host* is a plain string (no brackets)
        and *port* is an integer in ``[1, 65535]``.

    Raises:
        FrameDecodingError: If the payload is malformed, the host is empty,
            or the port is non-numeric or out of range ``[1, 65535]``.
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
        # bare (non-bracketed) IPv6 literals still parse, though encode_host_port
        # always bracket-quotes IPv6 before sending.
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
            f"Port {port} out of range [1, 65535] in OPEN frame payload: {payload!r}",
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
        conn_id:  Tunnel connection / flow ID, or ``None`` for ``AGENT_READY``.
                  Callers must not treat ``None`` as a valid empty-string ID.
        payload:  Frame payload string, or ``""`` when absent.  The payload
                  is the raw undecoded string; callers must pass it through
                  :func:`decode_binary_payload`, :func:`decode_error_payload`,
                  or :func:`parse_host_port` as appropriate for the
                  ``msg_type``.
    """

    msg_type: str
    conn_id: str | None
    payload: str


# ── Frame codec ───────────────────────────────────────────────────────────────


def _encode_frame(msg_type: str, conn_id: str | None, payload: str = "") -> str:
    """Low-level frame encoder — validates inputs and returns a
    newline-terminated frame string.

    This function is intentionally private.  All external callers must use
    the typed high-level helpers (``encode_conn_open_frame``, etc.).

    conn_id rules
    ─────────────
    * ``AGENT_READY`` is the only frame type that legitimately carries no
      conn_id.  Passing a non-empty conn_id for ``AGENT_READY`` is a
      programming error and raises :exc:`ProtocolError`.
    * Every other frame type requires a valid conn_id.  Passing ``None`` or
      ``""`` for any non-``AGENT_READY`` frame is a programming error and
      raises :exc:`ProtocolError`.

    Injection guard
    ───────────────
    The payload is checked for ``FRAME_SUFFIX`` and ``FRAME_PREFIX`` as a
    defence-in-depth measure.  For current frame types this check never fires
    because base64url payloads cannot contain these sentinels and host:port
    payloads are pre-validated by :func:`encode_host_port`.  The guard exists
    to protect future frame types whose payloads may not be pre-validated.

    Args:
        msg_type: Must be a member of ``_VALID_MSG_TYPES``.
        conn_id:  Must match ``ID_RE`` for all frame types except
                  ``AGENT_READY``, which must pass ``None`` or ``""``.
        payload:  Optional payload string.  Must not contain ``FRAME_SUFFIX``
                  or ``FRAME_PREFIX``.

    Returns:
        A newline-terminated frame string.

    Raises:
        ProtocolError: On any validation failure, conn_id/msg_type mismatch,
            or if the encoded frame content would exceed ``MAX_FRAME_LEN``
            characters.
    """
    if msg_type not in _VALID_MSG_TYPES:
        raise ProtocolError(
            f"Unknown msg_type {msg_type!r}.",
            details={
                "frame_type": msg_type,
                "expected": sorted(_VALID_MSG_TYPES),
            },
        )

    if msg_type in _NO_CONN_ID_TYPES:
        # e.g. AGENT_READY — must not carry a conn_id.
        if conn_id:
            raise ProtocolError(
                f"{msg_type} must not carry a conn_id, got {conn_id!r}.",
                details={"frame_type": msg_type, "expected": "no conn_id"},
            )
    else:
        # Every other frame type requires a valid conn_id.
        if not conn_id:
            raise ProtocolError(
                f"{msg_type} requires a conn_id but none was provided.",
                details={"frame_type": msg_type, "expected": "[cu][0-9a-f]{24}"},
            )
        _validate_id(conn_id, name=msg_type)

    # Defence-in-depth injection guard (see docstring).
    if FRAME_SUFFIX in payload:
        raise ProtocolError(
            f"Payload contains the frame suffix {FRAME_SUFFIX!r}, "
            "which would corrupt the wire format.",
            details={
                "frame_type": msg_type,
                "expected": f"payload free of {FRAME_SUFFIX!r}",
            },
        )
    if FRAME_PREFIX in payload:
        raise ProtocolError(
            f"Payload contains the frame prefix {FRAME_PREFIX!r}, "
            "which would corrupt the wire format.",
            details={
                "frame_type": msg_type,
                "expected": f"payload free of {FRAME_PREFIX!r}",
            },
        )

    if conn_id and payload:
        content = f"{FRAME_PREFIX}{msg_type}:{conn_id}:{payload}{FRAME_SUFFIX}"
    elif conn_id:
        content = f"{FRAME_PREFIX}{msg_type}:{conn_id}{FRAME_SUFFIX}"
    else:
        # No conn_id — only AGENT_READY reaches this branch.
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


# ── Public frame encoders ─────────────────────────────────────────────────────


def encode_conn_open_frame(conn_id: str, host: str, port: int) -> str:
    """Encode a ``CONN_OPEN`` frame.

    The host/port payload is normalised by :func:`encode_host_port`, which
    bracket-quotes IPv6 addresses and rejects frame-unsafe characters.

    Args:
        conn_id: TCP connection ID produced by
            :func:`~exectunnel.protocol.ids.new_conn_id`.
        host:    Destination hostname or IP address.
        port:    Destination TCP port (``[1, 65535]``).

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
        flow_id: UDP flow ID produced by
            :func:`~exectunnel.protocol.ids.new_flow_id`.
        host:    Destination hostname or IP address.
        port:    Destination UDP port (``[1, 65535]``).

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

    Pass ``SESSION_CONN_ID`` as *conn_id* for session-level errors that are
    not associated with a specific connection or flow.

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


# ── Payload decoders ──────────────────────────────────────────────────────────


def decode_binary_payload(payload: str) -> bytes:
    """Decode a base64url payload (no padding) from a ``DATA`` or ``UDP_DATA``
    frame back into raw bytes.

    Padding is re-added before decoding to satisfy the standard library.
    The formula ``(4 - len(s) % 4) % 4`` produces 0, 1, or 2 padding chars;
    the outer ``% 4`` ensures a string already aligned to 4 chars gets 0
    padding rather than 4.

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
            f"Invalid base64url payload: {payload[:64]!r}"
            f"{'...' if len(payload) > 64 else ''}",
            details={
                # Use repr() so non-ASCII bytes injected by a proxy are
                # faithfully represented in the diagnostic rather than
                # silently replaced by the ascii codec's error handler.
                "raw_bytes": repr(payload[:64]),
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
            decoded bytes are not valid UTF-8.  The original exception is
            chained via ``raise ... from exc`` to preserve the full traceback.
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


# ── Frame parser ──────────────────────────────────────────────────────────────


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

    Check order
    ───────────
    1. Strip whitespace.
    2. Check for ``FRAME_PREFIX`` + ``FRAME_SUFFIX`` — if absent, return
       ``None`` regardless of line length (non-frame lines are never errors).
    3. Check length against ``MAX_FRAME_LEN`` — oversized tunnel frames raise
       ``FrameDecodingError``; oversized non-frame lines are logged and
       silently dropped (step 2 already returned ``None`` for them).
    4. Parse ``msg_type``, ``conn_id``, ``payload`` fields.

    This ordering is load-bearing: it preserves the invariant that ``None``
    means "not a tunnel frame" and ``FrameDecodingError`` means "is a tunnel
    frame but corrupt".  Swapping steps 2 and 3 would cause oversized
    non-frame lines to raise instead of returning ``None``.

    Splitting strategy
    ──────────────────
    ``inner.split(":", 2)`` limits the split to at most two colons so that
    OPEN frame payloads containing bracket-quoted IPv6 addresses (e.g.
    ``[2001:db8::1]:443``) are not fragmented at the colons inside the
    address.  base64url payloads (DATA / UDP_DATA / ERROR) never contain
    ``:`` and are unaffected.

    Transport contract
    ──────────────────
    This function expects a single complete line.  The trailing ``\\n`` may
    be present or absent — ``strip()`` handles both.  The transport layer is
    responsible for buffering the byte stream and splitting on ``\\n`` before
    calling this function.  Passing a raw WebSocket message payload that
    contains multiple newline-separated frames will silently misparse.

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

    # ── Step 2: prefix/suffix check — MUST come before the length guard. ──
    # A non-frame line is never an error, regardless of its length.
    #
    # Tolerate proxy-injected suffixes (e.g. RunFlare appends " [trace=… span=…]"
    # after the closing ">>>").  Find the last occurrence of FRAME_SUFFIX and
    # truncate there so the rest of the parser sees a clean frame.
    suffix_pos = line.rfind(FRAME_SUFFIX)
    if suffix_pos != -1:
        line = line[: suffix_pos + len(FRAME_SUFFIX)]
    is_tunnel_frame = line.startswith(FRAME_PREFIX) and line.endswith(FRAME_SUFFIX)

    if not is_tunnel_frame:
        if len(line) > MAX_FRAME_LEN:
            log.debug(
                "parse_frame: dropping oversized non-frame line (%d chars)", len(line)
            )
        return None

    # ── Step 3: length guard — only reached for confirmed tunnel frames. ──
    if len(line) > MAX_FRAME_LEN:
        raise FrameDecodingError(
            f"Oversized tunnel frame ({len(line)} chars, limit {MAX_FRAME_LEN}). "
            "Possible memory-exhaustion or injection attempt.",
            details={
                "raw_bytes": line.encode("ascii", errors="replace").hex()[:128],
                "codec": "frame",
            },
        )

    # ── Step 4: field parsing — from here every error is a protocol fault. ──

    inner = line[len(FRAME_PREFIX) : -len(FRAME_SUFFIX)]
    # maxsplit=2: preserves IPv6 colons inside bracket-quoted OPEN payloads.
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

    # AGENT_READY carries no conn_id; use None (not "") so callers cannot
    # accidentally treat an absent ID as a valid empty string.
    conn_id: str | None = parts[1] if len(parts) > 1 else None
    payload: str = parts[2] if len(parts) > 2 else ""

    # Validate conn_id format for frames that carry one.
    # Note: if a proxy injects spaces or other characters into the frame body
    # (between FRAME_PREFIX and FRAME_SUFFIX), the conn_id field will fail
    # this check.  The error message says "malformed conn_id" but the root
    # cause is proxy corruption, not a genuine protocol violation by the peer.
    if conn_id is not None and not ID_RE.match(conn_id):
        raise FrameDecodingError(
            f"Tunnel frame has malformed conn_id {conn_id!r} (msg_type={msg_type!r}). "
            "If this follows a known-good encode, suspect proxy body corruption.",
            details={
                "raw_bytes": line.encode("utf-8", errors="replace").hex()[:128],
                "codec": "frame",
            },
        )

    return ParsedFrame(msg_type=msg_type, conn_id=conn_id, payload=payload)


def is_ready_frame(line: str) -> bool:
    """Return ``True`` if *line* is the agent-ready sentinel frame.

    This is a pure string predicate.  It does **not** call :func:`parse_frame`
    and therefore never raises.  The bootstrap scanner must be maximally
    tolerant of garbage lines — including lines that accidentally carry the
    tunnel prefix/suffix — so raising during the pre-ready scan would be
    incorrect.

    Policy for corrupt frames seen before ``AGENT_READY``
    ──────────────────────────────────────────────────────
    The bootstrap layer (session / transport) is responsible for deciding
    whether to propagate or log-and-skip :exc:`FrameDecodingError` during the
    pre-ready scan.  A typical bootstrap loop looks like::

        async for line in channel:
            if is_ready_frame(line):  # pure predicate — never raises
                break
            try:
                # Optionally call parse_frame(line) here to detect early
                # protocol faults and log them before the tunnel is up.
                parse_frame(line)
            except FrameDecodingError:
                log.warning("corrupt frame during bootstrap: %r", line)

    Args:
        line: A single line of text from the tunnel channel.

    Returns:
        ``True`` if the line is the ``AGENT_READY`` sentinel (after stripping
        whitespace), ``False`` otherwise.
    """
    stripped = line.strip()
    # Tolerate proxy-injected suffixes using the same rfind-based normalization
    # as parse_frame, so that any character (space, tab, or arbitrary tag text)
    # appended after ">>>" is handled consistently between the two paths.
    suffix_pos = stripped.rfind(FRAME_SUFFIX)
    if suffix_pos != -1:
        stripped = stripped[: suffix_pos + len(FRAME_SUFFIX)]
    return stripped == READY_FRAME
