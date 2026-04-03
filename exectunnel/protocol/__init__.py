"""Protocol domain: frame codec, ID generation, SOCKS5 enums."""

from exectunnel.protocol.enums import (
    AddrType,
    AuthMethod,
    Cmd,
    Reply,
    UserPassStatus,
)
from exectunnel.protocol.frames import (
    BOOTSTRAP_CHUNK_SIZE_CHARS,
    FRAME_PREFIX,
    FRAME_SUFFIX,
    MAX_FRAME_LEN,
    PIPE_READ_CHUNK_BYTES,
    READY_FRAME,
    SESSION_CONN_ID,
    ParsedFrame,
    decode_data_payload,
    encode_conn_close_frame,
    encode_conn_open_frame,
    encode_data_frame,
    encode_error_frame,
    encode_host_port,
    encode_udp_close_frame,
    encode_udp_data_frame,
    encode_udp_open_frame,
    is_ready_frame,
    parse_frame,
    parse_host_port,
)
from exectunnel.protocol.ids import ID_RE, new_conn_id, new_flow_id

__all__ = [
    # ── Constants ──────────────────────────────────────────────────────────
    "BOOTSTRAP_CHUNK_SIZE_CHARS",
    "FRAME_PREFIX",
    "FRAME_SUFFIX",
    "MAX_FRAME_LEN",
    "PIPE_READ_CHUNK_BYTES",
    "READY_FRAME",
    "SESSION_CONN_ID",
    # ── SOCKS5 enums ───────────────────────────────────────────────────────
    "AddrType",
    "AuthMethod",
    "Cmd",
    "Reply",
    "UserPassStatus",
    # ── Validation ─────────────────────────────────────────────────────────
    "ID_RE",
    # ── Frame types ────────────────────────────────────────────────────────
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
    "decode_data_payload",
    "encode_host_port",
    "parse_host_port",
    # ── ID generators ──────────────────────────────────────────────────────
    "new_conn_id",
    "new_flow_id",
]
