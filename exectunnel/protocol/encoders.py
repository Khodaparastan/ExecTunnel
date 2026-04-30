"""Typed frame encoders.

Every public encoder in this module is a pure, non-failing-on-valid-input
function that produces a newline-terminated frame string. Invalid
arguments raise :exc:`exectunnel.exceptions.ProtocolError` immediately —
they indicate a bug in the calling layer and must never be swallowed.

Private helper :func:`_encode_frame` performs the shared structural
validation and assembly; all public encoders delegate to it.
"""

from __future__ import annotations

from typing import Final

from exectunnel.exceptions import ProtocolError

from .codecs import encode_binary_payload, encode_host_port
from .constants import (
    FRAME_PREFIX,
    FRAME_SUFFIX,
    KEEPALIVE_FRAME,
    MAX_TUNNEL_FRAME_CHARS,
    NO_CONN_ID_TYPES,
    NO_CONN_ID_WITH_PAYLOAD_TYPES,
    PAYLOAD_FORBIDDEN_TYPES,
    PAYLOAD_REQUIRED_TYPES,
    READY_FRAME,
    VALID_MSG_TYPES,
)
from .ids import CONN_FLOW_ID_RE, SESSION_CONN_ID

__all__ = [
    "encode_agent_ready_frame",
    "encode_conn_ack_frame",
    "encode_conn_close_frame",
    "encode_conn_open_frame",
    "encode_data_frame",
    "encode_error_frame",
    "encode_keepalive_frame",
    "encode_stats_frame",
    "encode_udp_close_frame",
    "encode_udp_data_frame",
    "encode_udp_open_frame",
]

# ── Private constants ─────────────────────────────────────────────────────────

# Strings that, if they appeared inside a payload, would corrupt the wire
# format or enable injection. ``\r`` is included in addition to ``\n`` so
# that a mis-configured TTY PTY allocation cannot split a frame at the
# receiver.
_PAYLOAD_INJECTION_GUARDS: Final[tuple[tuple[str, str], ...]] = (
    (FRAME_SUFFIX, f"frame suffix {FRAME_SUFFIX!r}"),
    (FRAME_PREFIX, f"frame prefix {FRAME_PREFIX!r}"),
    ("\n", "newline"),
    ("\r", "carriage return"),
)

_AGENT_READY_FRAME: Final[str] = READY_FRAME + "\n"

_TCP_ID_PREFIX: Final[str] = "c"
_UDP_ID_PREFIX: Final[str] = "u"
_TCP_FRAME_TYPES: Final[frozenset[str]] = frozenset({
    "CONN_OPEN",
    "CONN_ACK",
    "CONN_CLOSE",
    "DATA",
})
_UDP_FRAME_TYPES: Final[frozenset[str]] = frozenset({
    "UDP_OPEN",
    "UDP_DATA",
    "UDP_CLOSE",
})

# ── Private validation helpers ────────────────────────────────────────────────


def _validate_conn_id(value: str, frame_type: str) -> None:
    """Validate a conn/flow ID string against :data:`CONN_FLOW_ID_RE`.

    Accepts both normal random IDs and
    :data:`exectunnel.protocol.ids.SESSION_CONN_ID` (the all-zero sentinel)
    because both match :data:`CONN_FLOW_ID_RE`. Callers that need to
    distinguish the sentinel must compare explicitly.

    Args:
        value: The conn_id string to validate.
        frame_type: Frame type name used in the error message.

    Raises:
        ProtocolError: If *value* does not match :data:`CONN_FLOW_ID_RE`.
    """
    if not CONN_FLOW_ID_RE.match(value):
        raise ProtocolError(
            f"Invalid conn_id {value!r} for frame type {frame_type!r}: "
            "must match [cu][0-9a-f]{24}.",
            details={
                "frame_type": frame_type,
                "expected": "[cu][0-9a-f]{24}",
                "got": value,
            },
        )


def _validate_id_namespace(value: str, frame_type: str) -> None:
    """Validate TCP/UDP ID prefix semantics for a frame type.

    Args:
        value: The already shape-validated connection or flow ID.
        frame_type: Frame type name used in the error message.

    Raises:
        ProtocolError: If a TCP frame carries a UDP flow ID, a UDP frame
            carries a TCP connection ID, or the session-level sentinel is
            used outside an ERROR frame.
    """
    if value == SESSION_CONN_ID and frame_type != "ERROR":
        raise ProtocolError(
            f"{frame_type} must not use the session-level sentinel conn_id "
            f"{SESSION_CONN_ID!r}.",
            details={
                "frame_type": frame_type,
                "expected": "non-sentinel TCP/UDP ID",
                "got": value,
            },
        )

    if frame_type in _TCP_FRAME_TYPES and not value.startswith(_TCP_ID_PREFIX):
        raise ProtocolError(
            f"{frame_type} requires a TCP connection ID with prefix "
            f"{_TCP_ID_PREFIX!r}, got {value!r}.",
            details={
                "frame_type": frame_type,
                "expected": "c[0-9a-f]{24}",
                "got": value,
            },
        )

    if frame_type in _UDP_FRAME_TYPES and not value.startswith(_UDP_ID_PREFIX):
        raise ProtocolError(
            f"{frame_type} requires a UDP flow ID with prefix "
            f"{_UDP_ID_PREFIX!r}, got {value!r}.",
            details={
                "frame_type": frame_type,
                "expected": "u[0-9a-f]{24}",
                "got": value,
            },
        )


def _encode_frame(msg_type: str, conn_id: str | None, payload: str = "") -> str:
    """Assemble a validated, newline-terminated frame string.

    This function is intentionally private. All external callers must use
    the typed high-level ``encode_*_frame`` helpers.

    Args:
        msg_type: Must be a member of
            :data:`exectunnel.protocol.constants.VALID_MSG_TYPES`.
        conn_id: Must match :data:`CONN_FLOW_ID_RE` for all frame types
            except those in
            :data:`exectunnel.protocol.constants.NO_CONN_ID_TYPES`, which
            must pass ``None``. Pass
            :data:`exectunnel.protocol.ids.SESSION_CONN_ID` for
            session-level ``ERROR`` frames.
        payload: Optional payload string. Must not contain
            :data:`FRAME_PREFIX`, :data:`FRAME_SUFFIX`, newlines, or
            carriage returns.

    Returns:
        A newline-terminated frame string.

    Raises:
        ProtocolError: On any validation failure.
    """
    if msg_type not in VALID_MSG_TYPES:
        raise ProtocolError(
            f"Unknown msg_type {msg_type!r}.",
            details={"frame_type": msg_type, "expected": sorted(VALID_MSG_TYPES)},
        )

    if msg_type in NO_CONN_ID_TYPES:
        if conn_id is not None:
            raise ProtocolError(
                f"{msg_type} must not carry a conn_id, got {conn_id!r}.",
                details={"frame_type": msg_type, "expected": "conn_id=None"},
            )
        if payload:
            raise ProtocolError(
                f"{msg_type} must not carry a payload, got {payload!r}.",
                details={"frame_type": msg_type, "expected": "no payload"},
            )
    elif msg_type in NO_CONN_ID_WITH_PAYLOAD_TYPES:
        if conn_id is not None:
            raise ProtocolError(
                f"{msg_type} must not carry a conn_id, got {conn_id!r}.",
                details={"frame_type": msg_type, "expected": "conn_id=None"},
            )
        if not payload:
            raise ProtocolError(
                f"{msg_type} requires a payload.",
                details={"frame_type": msg_type, "expected": "non-empty payload"},
            )
    else:
        if not conn_id:
            raise ProtocolError(
                f"{msg_type} requires a conn_id but none was provided.",
                details={"frame_type": msg_type, "expected": "[cu][0-9a-f]{24}"},
            )
        _validate_conn_id(conn_id, frame_type=msg_type)
        _validate_id_namespace(conn_id, frame_type=msg_type)

    if msg_type in PAYLOAD_REQUIRED_TYPES and not payload:
        raise ProtocolError(
            f"{msg_type} requires a non-empty payload.",
            details={"frame_type": msg_type, "expected": "non-empty payload"},
        )

    if msg_type in PAYLOAD_FORBIDDEN_TYPES and payload:
        raise ProtocolError(
            f"{msg_type} must not carry a payload.",
            details={"frame_type": msg_type, "expected": "empty payload"},
        )

    for forbidden, description in _PAYLOAD_INJECTION_GUARDS:
        if forbidden in payload:
            raise ProtocolError(
                f"Payload for {msg_type!r} contains the {description}, "
                "which would corrupt the wire format.",
                details={
                    "frame_type": msg_type,
                    "expected": f"payload free of {forbidden!r}",
                },
            )

    if conn_id and payload:
        content = f"{FRAME_PREFIX}{msg_type}:{conn_id}:{payload}{FRAME_SUFFIX}"
    elif conn_id:
        content = f"{FRAME_PREFIX}{msg_type}:{conn_id}{FRAME_SUFFIX}"
    elif payload:
        content = f"{FRAME_PREFIX}{msg_type}:{payload}{FRAME_SUFFIX}"
    else:
        content = f"{FRAME_PREFIX}{msg_type}{FRAME_SUFFIX}"

    if len(content) > MAX_TUNNEL_FRAME_CHARS:
        raise ProtocolError(
            f"Encoded frame length {len(content)} exceeds "
            f"MAX_TUNNEL_FRAME_CHARS={MAX_TUNNEL_FRAME_CHARS}.",
            details={
                "frame_type": msg_type,
                "expected": f"frame content ≤ {MAX_TUNNEL_FRAME_CHARS} chars",
                "got": len(content),
            },
        )

    return content + "\n"


# ── Public encoders — session-scoped frames ───────────────────────────────────


def encode_agent_ready_frame() -> str:
    """Encode the ``AGENT_READY`` bootstrap sentinel frame.

    Called by the in-pod agent once its listener is ready to accept
    connections. This is the only frame the agent sends outside of the
    normal request/response cycle.

    Returns:
        Newline-terminated frame string.
    """
    return _AGENT_READY_FRAME


def encode_keepalive_frame() -> str:
    """Encode a ``KEEPALIVE`` frame (client→agent heartbeat).

    Returns:
        Newline-terminated frame string.
    """
    return KEEPALIVE_FRAME


def encode_stats_frame(json_payload: bytes) -> str:
    """Encode a session-scoped STATS frame.

    Args:
        json_payload: UTF-8 JSON bytes.

    Returns:
        Newline-terminated STATS frame.
    """

    if not isinstance(json_payload, bytes):
        raise ProtocolError(
            "STATS frame payload must be bytes.",
            details={
                "frame_type": "STATS",
                "expected": "bytes",
                "got": type(json_payload).__name__,
            },
        )

    if not json_payload:
        raise ProtocolError(
            "STATS frame payload must not be empty.",
            details={"frame_type": "STATS", "expected": "non-empty JSON bytes"},
        )
    return _encode_frame("STATS", None, encode_binary_payload(json_payload))


# ── Public encoders — TCP connection frames ───────────────────────────────────


def encode_conn_open_frame(conn_id: str, host: str, port: int) -> str:
    """Encode a ``CONN_OPEN`` frame.

    Args:
        conn_id: A valid TCP connection ID (``c<24 hex>``).
        host: Destination hostname or IP address.
        port: Destination TCP port in ``[1, 65535]``.

    Returns:
        Newline-terminated frame string.

    Raises:
        ProtocolError: If any argument is invalid.
    """
    return _encode_frame("CONN_OPEN", conn_id, encode_host_port(host, port))


def encode_conn_ack_frame(conn_id: str) -> str:
    """Encode a ``CONN_ACK`` frame.

    Sent by the agent to acknowledge a ``CONN_OPEN`` request.

    Args:
        conn_id: The TCP connection ID from the corresponding
            ``CONN_OPEN``.

    Returns:
        Newline-terminated frame string.

    Raises:
        ProtocolError: If *conn_id* is invalid.
    """
    return _encode_frame("CONN_ACK", conn_id)


def encode_conn_close_frame(conn_id: str) -> str:
    """Encode a ``CONN_CLOSE`` frame.

    Args:
        conn_id: A valid TCP connection ID.

    Returns:
        Newline-terminated frame string.

    Raises:
        ProtocolError: If *conn_id* is invalid.
    """
    return _encode_frame("CONN_CLOSE", conn_id)


def encode_data_frame(conn_id: str, data: bytes) -> str:
    """Encode a ``DATA`` frame.

    Args:
        conn_id: A valid TCP connection ID.
        data: Raw bytes to transmit. Must be non-empty; use
            :func:`encode_conn_close_frame` to signal EOF.

    Returns:
        Newline-terminated frame string.

    Raises:
        ProtocolError: If *conn_id* is invalid, *data* is empty, or the
            encoded frame exceeds
            :data:`exectunnel.protocol.constants.MAX_TUNNEL_FRAME_CHARS`.
    """
    if not data:
        raise ProtocolError(
            "DATA frame payload must not be empty. "
            "Send CONN_CLOSE to signal end of stream.",
            details={"frame_type": "DATA", "expected": "non-empty bytes"},
        )
    return _encode_frame("DATA", conn_id, encode_binary_payload(data))


# ── Public encoders — UDP flow frames ─────────────────────────────────────────


def encode_udp_open_frame(flow_id: str, host: str, port: int) -> str:
    """Encode a ``UDP_OPEN`` frame.

    Args:
        flow_id: A valid UDP flow ID (``u<24 hex>``).
        host: Destination hostname or IP address.
        port: Destination UDP port in ``[1, 65535]``.

    Returns:
        Newline-terminated frame string.

    Raises:
        ProtocolError: If any argument is invalid.
    """
    return _encode_frame("UDP_OPEN", flow_id, encode_host_port(host, port))


def encode_udp_data_frame(flow_id: str, data: bytes) -> str:
    """Encode a ``UDP_DATA`` frame.

    Args:
        flow_id: A valid UDP flow ID.
        data: Raw datagram bytes. Must be non-empty; UDP datagrams are
            inherently non-empty at the socket layer.

    Returns:
        Newline-terminated frame string.

    Raises:
        ProtocolError: If *flow_id* is invalid, *data* is empty, or the
            encoded frame exceeds
            :data:`exectunnel.protocol.constants.MAX_TUNNEL_FRAME_CHARS`.
    """
    if not data:
        raise ProtocolError(
            "UDP_DATA frame payload must not be empty. "
            "UDP datagrams are never zero-length at the socket layer.",
            details={"frame_type": "UDP_DATA", "expected": "non-empty bytes"},
        )
    return _encode_frame("UDP_DATA", flow_id, encode_binary_payload(data))


def encode_udp_close_frame(flow_id: str) -> str:
    """Encode a ``UDP_CLOSE`` frame.

    Args:
        flow_id: A valid UDP flow ID.

    Returns:
        Newline-terminated frame string.

    Raises:
        ProtocolError: If *flow_id* is invalid.
    """
    return _encode_frame("UDP_CLOSE", flow_id)


# ── Public encoders — error reporting ─────────────────────────────────────────


def encode_error_frame(conn_id: str, message: str) -> str:
    """Encode an ``ERROR`` frame.

    Args:
        conn_id: A valid connection/flow ID, or
            :data:`exectunnel.protocol.ids.SESSION_CONN_ID` for
            session-level errors.
        message: Human-readable UTF-8 error description.

    Returns:
        Newline-terminated frame string.

    Raises:
        ProtocolError: If *conn_id* is invalid or the encoded frame
            exceeds :data:`exectunnel.protocol.constants.MAX_TUNNEL_FRAME_CHARS`.
    """
    return _encode_frame("ERROR", conn_id, encode_binary_payload(message.encode()))
