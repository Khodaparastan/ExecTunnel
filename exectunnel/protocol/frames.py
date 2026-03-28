"""Frame encode/decode and protocol constants."""
from __future__ import annotations

# ── Frame protocol constants ──────────────────────────────────────────────────

FRAME_PREFIX: str = "<<<EXECTUNNEL:"
FRAME_SUFFIX: str = ">>>"
READY_FRAME: str = "<<<EXECTUNNEL:AGENT_READY>>>"

# Size of base64-encoded chunks sent during bootstrap.
# 200 chars is safely under most shell input-buffer limits (512 bytes POSIX
# minimum) and avoids splitting multi-byte UTF-8 sequences in the b64 alphabet.
BOOTSTRAP_CHUNK_SIZE_CHARS: int = 200

# Read chunk size used when piping raw TCP streams (CONNECT relay).
PIPE_READ_CHUNK_BYTES: int = 4096

# ── Type alias ────────────────────────────────────────────────────────────────

# A newline-terminated protocol frame string.
FrameStr = str


# ── Frame codec ───────────────────────────────────────────────────────────────


def encode_frame(msg_type: str, conn_id: str, payload: str = "") -> FrameStr:
    """
    Encode a single protocol frame.
    Returns a newline-terminated string ready to be sent over the wire.
    """
    if payload:
        return f"{FRAME_PREFIX}{msg_type}:{conn_id}:{payload}{FRAME_SUFFIX}\n"
    return f"{FRAME_PREFIX}{msg_type}:{conn_id}{FRAME_SUFFIX}\n"


def encode_conn_open_frame(conn_id: str, host: str, port: int) -> FrameStr:
    """Encode a CONN_OPEN frame with host/port payload.

    IPv6 addresses are embedded as-is (e.g. ``2001:db8::1:8080``).  The agent
    parses the payload with ``rpartition(":")`` so the last colon always
    separates the port — both sides must use ``rpartition`` for this to work.
    """
    return encode_frame("CONN_OPEN", conn_id, f"{host}:{port}")


def encode_udp_open_frame(flow_id: str, host: str, port: int) -> FrameStr:
    """Encode a UDP_OPEN frame with host/port payload.

    Same IPv6 / ``rpartition`` convention as :func:`encode_conn_open_frame`.
    """
    return encode_frame("UDP_OPEN", flow_id, f"{host}:{port}")


def encode_data_frame(conn_id: str, payload_b64: str) -> FrameStr:
    """Encode a DATA frame from a base64 payload string."""
    return encode_frame("DATA", conn_id, payload_b64)


def encode_udp_data_frame(flow_id: str, payload_b64: str) -> FrameStr:
    """Encode a UDP_DATA frame from a base64 payload string."""
    return encode_frame("UDP_DATA", flow_id, payload_b64)


def encode_udp_close_frame(flow_id: str) -> FrameStr:
    """Encode a UDP_CLOSE frame."""
    return encode_frame("UDP_CLOSE", flow_id)


def parse_frame(line: str) -> tuple[str, str, str] | None:
    """
    Parse one line into ``(msg_type, conn_id, payload)`` or return ``None``.

    ``payload`` is an empty string when absent.  The function is intentionally
    lenient — unrecognised frames are silently dropped by callers.
    """
    line = line.strip()
    if not (line.startswith(FRAME_PREFIX) and line.endswith(FRAME_SUFFIX)):
        return None
    inner = line[len(FRAME_PREFIX) : -len(FRAME_SUFFIX)]
    # Split into at most 3 parts so base64 payloads with ":" are not truncated.
    parts = inner.split(":", 2)
    msg_type = parts[0]
    conn_id = parts[1] if len(parts) > 1 else ""
    payload = parts[2] if len(parts) > 2 else ""
    return msg_type, conn_id, payload
