"""ExecTunnel protocol layer: frame codec, ID generation, SOCKS5 enums.

Public surface
--------------
The package exposes everything a caller needs to speak the ExecTunnel
wire protocol, grouped by role:

* **Frame constants** — :data:`FRAME_PREFIX`, :data:`FRAME_SUFFIX`,
  :data:`READY_FRAME`, :data:`SESSION_CONN_ID`, :data:`MAX_TUNNEL_FRAME_CHARS`,
  :data:`PORT_UNSPECIFIED`.
* **Frame encoders** — one typed function per frame type; all raise
  :exc:`exectunnel.exceptions.ProtocolError` on invalid arguments.
* **Frame decoder** — :func:`parse_frame` (returns
  :class:`ParsedFrame` or ``None``; raises
  :exc:`exectunnel.exceptions.FrameDecodingError` on corrupt frames) and
  :func:`is_ready_frame` (pure predicate — never raises).
* **Payload helpers** — :func:`decode_binary_payload`,
  :func:`decode_error_payload`, :func:`encode_host_port`,
  :func:`parse_host_port`.
* **ID generators** — :func:`new_conn_id`, :func:`new_flow_id`,
  :func:`new_session_id`.
* **ID validators** — :data:`CONN_FLOW_ID_RE`, :data:`SESSION_ID_RE`
  (:data:`ID_RE` is a deprecated alias for :data:`CONN_FLOW_ID_RE`).
* **SOCKS5 enums** — :class:`AddrType`, :class:`AuthMethod`,
  :class:`Cmd`, :class:`Reply`, :class:`UserPassStatus`.

Layer contract
--------------
The protocol layer knows about the frame format and SOCKS5 enumerations.
It does **not** know about I/O, threads, asyncio, WebSocket, DNS, or
sessions. Every function is a pure transformation — safe to call from
any execution context including the in-pod agent.

Exception contract
------------------
Exactly two exception types are raised by this package:

* :exc:`exectunnel.exceptions.ProtocolError` — an encoder received
  invalid arguments. Always a bug in the calling layer; never catch
  silently.
* :exc:`exectunnel.exceptions.FrameDecodingError` — a decoder received
  corrupt wire data from the remote peer. Always propagate; never
  discard.

SOCKS5 enum notes
-----------------
:class:`AuthMethod`, :class:`Cmd`, :class:`AddrType`, and :class:`Reply`
inherit from the private ``_StrictIntEnum`` base, which raises
:exc:`ValueError` immediately on unknown wire values. The proxy layer
must catch :exc:`ValueError` and map it to the appropriate :class:`Reply`
code.

:class:`UserPassStatus` inherits plain :class:`enum.IntEnum` because
RFC 1929 §2 requires any non-zero byte to be treated as failure. Its
``_missing_`` maps ``[0x01, 0xFE]`` to :attr:`UserPassStatus.FAILURE`.

:class:`AuthMethod` and :class:`Cmd` expose
``is_supported() -> bool`` to programmatically detect wire-defined but
unimplemented values without relying on documentation.

Session conn_id sentinel
------------------------
:data:`SESSION_CONN_ID` (``c`` + 24 zeros) is a valid
:data:`CONN_FLOW_ID_RE`-shaped sentinel used to signal session-level
errors in ``ERROR`` frames. Callers that need to distinguish
session-level errors from per-connection errors must compare
``conn_id == SESSION_CONN_ID`` explicitly after parsing.

Internal module layout
----------------------
For maintenance, the package is split into focused sub-modules:

* :mod:`exectunnel.protocol.constants` — wire constants, type-classification sets.
* :mod:`exectunnel.protocol.enums`     — SOCKS5 / RFC 1929 enums.
* :mod:`exectunnel.protocol.ids`       — ID generation and validators.
* :mod:`exectunnel.protocol.types`     — :class:`ParsedFrame`.
* :mod:`exectunnel.protocol.codecs`    — host/port + base64url codecs.
* :mod:`exectunnel.protocol.encoders`  — typed ``encode_*_frame`` helpers.
* :mod:`exectunnel.protocol.parser`    — :func:`parse_frame`,
  :func:`is_ready_frame`.

Callers should import exclusively from ``exectunnel.protocol``; the
sub-module boundaries are an implementation detail and are not part of
the stability contract.
"""

from __future__ import annotations

from .codecs import (
    decode_binary_payload,
    decode_error_payload,
    encode_host_port,
    parse_host_port,
)
from .constants import (
    DATA_FRAME_OVERHEAD_CHARS,
    FRAME_PREFIX,
    FRAME_SUFFIX,
    MAX_DATA_PAYLOAD_BYTES,
    MAX_TUNNEL_FRAME_CHARS,
    MAX_UDP_DATA_PAYLOAD_BYTES,
    PORT_UNSPECIFIED,
    READY_FRAME,
    SESSION_CONN_ID,
    UDP_DATA_FRAME_OVERHEAD_CHARS,
)
from .encoders import (
    encode_agent_ready_frame,
    encode_conn_ack_frame,
    encode_conn_close_frame,
    encode_conn_open_frame,
    encode_data_frame,
    encode_error_frame,
    encode_keepalive_frame,
    encode_liveness_frame,
    encode_stats_frame,
    encode_udp_close_frame,
    encode_udp_data_frame,
    encode_udp_open_frame,
)
from .enums import (
    AddrType,
    AuthMethod,
    Cmd,
    Reply,
    UserPassStatus,
)
from .ids import (
    CONN_FLOW_ID_RE,
    ID_RE,
    SESSION_ID_RE,
    new_conn_id,
    new_flow_id,
    new_session_id,
)
from .parser import is_ready_frame, parse_frame
from .types import ParsedFrame

__all__ = [
    # ── Frame constants ──────────────────────────────────────────────────────
    "DATA_FRAME_OVERHEAD_CHARS",
    "FRAME_PREFIX",
    "FRAME_SUFFIX",
    "MAX_DATA_PAYLOAD_BYTES",
    "MAX_TUNNEL_FRAME_CHARS",
    "MAX_UDP_DATA_PAYLOAD_BYTES",
    "PORT_UNSPECIFIED",
    "READY_FRAME",
    "SESSION_CONN_ID",
    "UDP_DATA_FRAME_OVERHEAD_CHARS",
    # ── SOCKS5 enums ─────────────────────────────────────────────────────────
    "AddrType",
    "AuthMethod",
    "Cmd",
    "Reply",
    "UserPassStatus",
    # ── Frame result type ────────────────────────────────────────────────────
    "ParsedFrame",
    # ── Frame encoders ───────────────────────────────────────────────────────
    "encode_agent_ready_frame",
    "encode_conn_ack_frame",
    "encode_conn_close_frame",
    "encode_conn_open_frame",
    "encode_data_frame",
    "encode_error_frame",
    "encode_keepalive_frame",
    "encode_liveness_frame",
    "encode_stats_frame",
    "encode_udp_close_frame",
    "encode_udp_data_frame",
    "encode_udp_open_frame",
    # ── Frame decoder ────────────────────────────────────────────────────────
    "is_ready_frame",
    "parse_frame",
    # ── Payload helpers ──────────────────────────────────────────────────────
    "decode_binary_payload",
    "decode_error_payload",
    "encode_host_port",
    "parse_host_port",
    # ── ID generators ────────────────────────────────────────────────────────
    "new_conn_id",
    "new_flow_id",
    "new_session_id",
    # ── ID validators ────────────────────────────────────────────────────────
    "CONN_FLOW_ID_RE",
    "ID_RE",
    "SESSION_ID_RE",
]
