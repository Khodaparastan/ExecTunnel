"""Frame encode/decode and protocol constants.

Wire format (newline-terminated)
─────────────────────────────────
    <<<EXECTUNNEL:{msg_type}:{conn_id}[:{payload}]>>>\\n

Rules
─────
* ``msg_type`` and ``conn_id`` MUST NOT contain ``:``, ``<``, or ``>``.
* ``payload`` is always base64url (no padding) for DATA/UDP_DATA frames,
  and ``{host}:{port}`` for OPEN frames (IPv6 hosts are bracket-quoted).
* ``parse_frame`` splits on ``:`` at most twice so base64 payloads that
  contain ``:`` are never truncated.
* Host strings in OPEN frames are validated/normalised by
  ``encode_host_port`` before embedding; ``parse_host_port`` is the
  canonical inverse used by both sides.

Frame catalogue
───────────────
    AGENT_READY   — agent bootstrap complete (no conn_id / payload)
    CONN_OPEN     — open a TCP connection  payload: [host]:port | host:port
    CONN_CLOSE    — close a TCP connection (no payload)
    DATA          — TCP data chunk         payload: base64url
    UDP_OPEN      — open a UDP flow        payload: [host]:port | host:port
    UDP_DATA      — UDP datagram           payload: base64url
    UDP_CLOSE     — close a UDP flow       (no payload)
    ERROR         — agent error report     payload: base64url-encoded UTF-8 msg
"""

from __future__ import annotations
import binascii
import base64
import ipaddress
import re
from typing import NamedTuple
from .ids import ID_RE
# ── Frame protocol constants ──────────────────────────────────────────────────

FRAME_PREFIX: str = "<<<EXECTUNNEL:"
FRAME_SUFFIX: str = ">>>"

# Sentinel emitted by the agent once it is ready to accept tunnel frames.
READY_FRAME: str = "<<<EXECTUNNEL:AGENT_READY>>>"

# Size of base64-encoded chunks sent during bootstrap.
# 200 chars is safely under most shell input-buffer limits (512 bytes POSIX
# minimum) and avoids splitting multi-byte UTF-8 sequences in the b64 alphabet.
BOOTSTRAP_CHUNK_SIZE_CHARS: int = 200

# Read chunk size used when piping raw TCP streams (CONNECT relay).
PIPE_READ_CHUNK_BYTES: int = 4_096

# session-level error sentinel constant
SESSION_CONN_ID: str = "c" + "0" * 24

# ── Allowed message types ─────────────────────────────────────────────────────

_VALID_MSG_TYPES: frozenset[str] = frozenset({
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
_FRAME_UNSAFE_RE = re.compile(r"[:<>]")
MAX_FRAME_LEN: int = 8_192

def _validate_id(value: str, name: str = "id") -> None:
    """Raise ``ValueError`` if *value* is not a well-formed tunnel ID."""
    if not ID_RE.match(value):
        raise ValueError(
            f"Invalid tunnel {name} {value!r}: must match [cu][0-9a-f]{{24}}"
        )


def _validate_msg_type(msg_type: str) -> None:
    """Raise ``ValueError`` if *msg_type* is not in the allowed catalogue."""
    if msg_type not in _VALID_MSG_TYPES:
        raise ValueError(
            f"Unknown msg_type {msg_type!r}. Allowed: {sorted(_VALID_MSG_TYPES)}"
        )


# ── Host / port codec ─────────────────────────────────────────────────────────


def encode_host_port(host: str, port: int) -> str:
    """
    Encode a host + port into the canonical ``[host]:port`` / ``host:port``
    payload string used in OPEN frames.

    * IPv6 addresses are bracket-quoted: ``[2001:db8::1]:8080``
    * IPv4 addresses and domain names are left bare: ``example.com:8080``
    * The port is validated to be in ``[1, 65535]``.
    * Domain names are checked for frame-unsafe characters (``:``, ``<``, ``>``).

    Args:
        host: Destination hostname or IP address string.
        port: Destination TCP/UDP port number.

    Returns:
        A string safe to embed as the payload of a ``CONN_OPEN`` or
        ``UDP_OPEN`` frame.

    Raises:
        ValueError: If *port* is out of range or *host* contains characters
            that would corrupt the frame wire format.
    """
    if not (1 <= port <= 65_535):
        raise ValueError(f"Port {port} is out of range [1, 65535]")

    # Normalise and bracket-quote IPv6 addresses.
    try:
        addr = ipaddress.ip_address(host)
        if isinstance(addr, ipaddress.IPv6Address):
            return f"[{addr.compressed}]:{port}"
        # IPv4 — bare is unambiguous.
        return f"{addr.compressed}:{port}"
    except ValueError:
        pass  # Not an IP literal — treat as domain name.

    # Domain name: reject any character that is structurally significant in
    # the frame format.  A legitimate FQDN will never contain these.
    if _FRAME_UNSAFE_RE.search(host):
        raise ValueError(
            f"Host {host!r} contains frame-unsafe characters "
            f"(':', '<', '>').  Possible injection attempt."
        )
    return f"{host}:{port}"


def parse_host_port(payload: str) -> tuple[str, int]:
    """
    Parse a ``[host]:port`` or ``host:port`` payload string.

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
        ValueError: If the payload is malformed or the port is non-numeric /
            out of range.
    """
    if payload.startswith("["):
        # Bracket-quoted IPv6: [addr]:port
        bracket_end = payload.find("]")
        if bracket_end == -1 or payload[bracket_end + 1 : bracket_end + 2] != ":":
            raise ValueError(f"Malformed bracketed host in payload: {payload!r}")
        host = payload[1:bracket_end]
        port_str = payload[bracket_end + 2 :]
    else:
        # IPv4 or domain — use rpartition so bare IPv6 literals (legacy, no
        # brackets) still split on the *last* colon.
        host, sep, port_str = payload.rpartition(":")
        if not sep:
            raise ValueError(f"Missing port separator in payload: {payload!r}")

    try:
        port = int(port_str)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            f"Non-numeric port {port_str!r} in payload: {payload!r}"
        ) from exc

    if not (1 <= port <= 65_535):
        raise ValueError(f"Port {port} out of range in payload: {payload!r}")

    return host, port


# ── Parsed frame result type ──────────────────────────────────────────────────


class ParsedFrame(NamedTuple):
    """
    The structured result of :func:`parse_frame`.

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
    """
    Low-level frame encoder — validates inputs and returns a newline-terminated
    frame string.

    This function is intentionally private.  All callers outside this module
    should use the typed high-level helpers (``encode_conn_open_frame``, etc.).

    Args:
        msg_type: Must be a member of ``_VALID_MSG_TYPES``.
        conn_id:  Must match ``ID_RE``, or be ``""`` for ``AGENT_READY``.
        payload:  Optional payload string.  Must not contain ``FRAME_SUFFIX``.

    Returns:
        A newline-terminated frame string.

    Raises:
        ValueError: On any validation failure.
    """
    _validate_msg_type(msg_type)

    # AGENT_READY is the only frame that legitimately has no conn_id.
    if conn_id:
        _validate_id(conn_id)

    # Guard against payload accidentally containing the frame suffix, which
    # would allow a crafted payload to terminate the frame early and inject
    # a second frame on the same line.
    if FRAME_SUFFIX in payload:
        raise ValueError(
            f"Payload contains the frame suffix {FRAME_SUFFIX!r}, "
            "which would corrupt the wire format."
        )

    if conn_id and payload:
        return f"{FRAME_PREFIX}{msg_type}:{conn_id}:{payload}{FRAME_SUFFIX}\n"
    if conn_id:
        return f"{FRAME_PREFIX}{msg_type}:{conn_id}{FRAME_SUFFIX}\n"
    # No conn_id (AGENT_READY).
    return f"{FRAME_PREFIX}{msg_type}{FRAME_SUFFIX}\n"


def encode_conn_open_frame(conn_id: str, host: str, port: int) -> str:
    """
    Encode a ``CONN_OPEN`` frame.

    The host/port payload is normalised by :func:`encode_host_port`, which
    bracket-quotes IPv6 addresses and rejects frame-unsafe characters.

    Args:
        conn_id: TCP connection ID produced by :func:`~exectunnel.protocol.ids.new_conn_id`.
        host:    Destination hostname or IP address.
        port:    Destination TCP port.

    Returns:
        A newline-terminated ``CONN_OPEN`` frame string.
    """
    return _encode_frame("CONN_OPEN", conn_id, encode_host_port(host, port))


def encode_conn_close_frame(conn_id: str) -> str:
    """
    Encode a ``CONN_CLOSE`` frame.

    Signals explicit TCP connection teardown.  Both sides MUST emit this frame
    when closing a connection so the peer can release associated resources
    without relying on timeout heuristics.

    Args:
        conn_id: TCP connection ID of the connection being closed.

    Returns:
        A newline-terminated ``CONN_CLOSE`` frame string.
    """
    return _encode_frame("CONN_CLOSE", conn_id)


def encode_data_frame(conn_id: str, data: bytes) -> str:
    """
    Encode a ``DATA`` frame.

    The raw bytes are base64url-encoded (no padding) before embedding so that
    the payload is guaranteed to be free of frame-unsafe characters.

    Args:
        conn_id: TCP connection ID.
        data:    Raw bytes to transmit.

    Returns:
        A newline-terminated ``DATA`` frame string.
    """
    payload_b64 = base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
    return _encode_frame("DATA", conn_id, payload_b64)


def encode_udp_open_frame(flow_id: str, host: str, port: int) -> str:
    """
    Encode a ``UDP_OPEN`` frame.

    Same host/port normalisation rules as :func:`encode_conn_open_frame`.

    Args:
        flow_id: UDP flow ID produced by :func:`~exectunnel.protocol.ids.new_flow_id`.
        host:    Destination hostname or IP address.
        port:    Destination UDP port.

    Returns:
        A newline-terminated ``UDP_OPEN`` frame string.
    """
    return _encode_frame("UDP_OPEN", flow_id, encode_host_port(host, port))


def encode_udp_data_frame(flow_id: str, data: bytes) -> str:
    """
    Encode a ``UDP_DATA`` frame.

    Args:
        flow_id: UDP flow ID.
        data:    Raw datagram bytes to transmit.

    Returns:
        A newline-terminated ``UDP_DATA`` frame string.
    """
    payload_b64 = base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
    return _encode_frame("UDP_DATA", flow_id, payload_b64)


def encode_udp_close_frame(flow_id: str) -> str:
    """
    Encode a ``UDP_CLOSE`` frame.

    Args:
        flow_id: UDP flow ID of the flow being closed.

    Returns:
        A newline-terminated ``UDP_CLOSE`` frame string.
    """
    return _encode_frame("UDP_CLOSE", flow_id)


def encode_error_frame(conn_id: str, message: str) -> str:
    """
    Encode an ``ERROR`` frame.

    The error message is UTF-8 encoded then base64url-encoded so that
    arbitrary diagnostic text (including newlines and non-ASCII characters)
    can be transmitted without corrupting the frame stream.

    Args:
        conn_id: The connection / flow ID associated with the error, or a
            sentinel such as ``"c" + "0" * 24`` for session-level errors.
        message: Human-readable error description.

    Returns:
        A newline-terminated ``ERROR`` frame string.
    """
    payload_b64 = (
        base64.urlsafe_b64encode(message.encode("utf-8")).rstrip(b"=").decode("ascii")
    )
    return _encode_frame("ERROR", conn_id, payload_b64)


def decode_data_payload(payload: str) -> bytes:
    """
    Decode a base64url payload (no padding) from a ``DATA``, ``UDP_DATA``,
    or ``ERROR`` frame back into raw bytes.

    Padding is re-added before decoding to satisfy the standard library.

    Args:
        payload: The raw payload string from a parsed frame.

    Returns:
        Decoded bytes.

    Raises:
        ValueError: If *payload* is not valid base64url.
    """
    # Re-add stripped padding.
    padding = (4 - len(payload) % 4) % 4
    try:
        return base64.urlsafe_b64decode(payload + "=" * padding)
    except Exception as exc:
        raise ValueError(f"Invalid base64url payload: {payload!r}") from exc


def parse_frame(line: str) -> ParsedFrame | None:
    """
    Parse one line into a :class:`ParsedFrame` or return ``None``.

    The function is intentionally lenient about *unrecognised* lines (returns
    ``None``) but strict about *structurally valid* frames that carry an
    unknown ``msg_type`` — those are also returned as ``None`` so callers can
    safely ignore them without special-casing.

    Splitting strategy
    ──────────────────
    ``inner.split(":", 2)`` limits the split to at most two colons, so a
    base64url payload that happens to contain ``:`` is never truncated.

    Args:
        line: A single line of text from the tunnel channel (may or may not
            be newline-terminated).

    Returns:
        A :class:`ParsedFrame` on success, ``None`` otherwise.
    """
    line = line.strip()
    if len(line) > MAX_FRAME_LEN:
        return None
    if not (line.startswith(FRAME_PREFIX) and line.endswith(FRAME_SUFFIX)):
        return None

    inner = line[len(FRAME_PREFIX) : -len(FRAME_SUFFIX)]
    parts = inner.split(":", 2)

    msg_type = parts[0]

    # Silently drop frames with unknown message types so that future protocol
    # versions can add new frame types without breaking older peers.
    if msg_type not in _VALID_MSG_TYPES:
        return None

    conn_id = parts[1] if len(parts) > 1 else ""
    payload = parts[2] if len(parts) > 2 else ""

    # Validate conn_id format for frames that carry one.
    if conn_id and not ID_RE.match(conn_id):
        return None

    return ParsedFrame(msg_type=msg_type, conn_id=conn_id, payload=payload)


def is_ready_frame(line: str) -> bool:
    """
    Return ``True`` if *line* is the agent-ready sentinel frame.

    Prefer this helper over comparing against ``READY_FRAME`` directly so
    that the check is robust to trailing whitespace and future format changes.

    Args:
        line: A single line of text from the tunnel channel.

    Returns:
        ``True`` if the line is the ``AGENT_READY`` sentinel.
    """
    parsed = parse_frame(line)
    return parsed is not None and parsed.msg_type == "AGENT_READY"
