"""Protocol domain: frame codec, ID generation, SOCKS5 enums.

Public surface
──────────────
This package exposes everything a caller needs to speak the ExecTunnel wire
protocol:

* **Frame constants** — ``FRAME_PREFIX``, ``FRAME_SUFFIX``, ``READY_FRAME``,
  ``SESSION_CONN_ID``, ``MAX_FRAME_LEN``
* **Frame encoders** — one typed function per frame type
* **Frame decoder** — ``parse_frame`` / ``is_ready_frame``
* **Payload helpers** — ``decode_binary_payload``, ``decode_error_payload``,
  ``encode_host_port``, ``parse_host_port``
* **ID generators** — ``new_conn_id``, ``new_flow_id``
* **SOCKS5 enums** — ``AddrType``, ``AuthMethod``, ``Cmd``, ``Reply``,
  ``UserPassStatus``

Layer contract
──────────────
The protocol layer knows about frame format and SOCKS5 enumerations.
It does **not** know about I/O, threads, asyncio, WebSocket, DNS, or sessions.
"""

from __future__ import annotations

from exectunnel.protocol.enums import (
    AddrType,
    AuthMethod,
    Cmd,
    Reply,
    UserPassStatus,
)
from exectunnel.protocol.frames import (
    FRAME_PREFIX,
    FRAME_SUFFIX,
    MAX_FRAME_LEN,
    READY_FRAME,
    SESSION_CONN_ID,
    ParsedFrame,
    decode_binary_payload,
    decode_error_payload,
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
from exectunnel.protocol.ids import new_conn_id, new_flow_id

__all__ = [
    # ── Frame constants ────────────────────────────────────────────────────
    "FRAME_PREFIX",
    "FRAME_SUFFIX",
    "MAX_FRAME_LEN",
    "READY_FRAME",
    "SESSION_CONN_ID",
    # ── SOCKS5 enums ───────────────────────────────────────────────────────
    "AddrType",
    "AuthMethod",
    "Cmd",
    "Reply",
    "UserPassStatus",
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
    # ── ID generators ──────────────────────────────────────────────────────
    "new_conn_id",
    "new_flow_id",
]
