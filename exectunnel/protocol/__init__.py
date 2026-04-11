"""Protocol domain: frame codec, ID generation, SOCKS5 enums.

Public surface
──────────────
This package exposes everything a caller needs to speak the ExecTunnel wire
protocol:

* **Frame constants** — ``FRAME_PREFIX``, ``FRAME_SUFFIX``, ``READY_FRAME``,
  ``SESSION_CONN_ID``, ``MAX_FRAME_LEN``
* **Frame encoders** — one typed function per frame type; all raise
  ``ProtocolError`` on invalid arguments
* **Frame decoder** — ``parse_frame`` (returns ``ParsedFrame | None``,
  raises ``FrameDecodingError`` on corrupt tunnel frames) and
  ``is_ready_frame`` (pure predicate — never raises)
* **Payload helpers** — ``decode_binary_payload``, ``decode_error_payload``,
  ``encode_host_port``, ``parse_host_port``
* **ID generators** — ``new_conn_id``, ``new_flow_id``, ``new_session_id``,
  ``ID_RE``, ``SESSION_ID_RE``
* **SOCKS5 enums** — ``AddrType``, ``AuthMethod``, ``Cmd``, ``Reply``,
  ``UserPassStatus``

Layer contract
──────────────
The protocol layer knows about frame format and SOCKS5 enumerations.
It does **not** know about I/O, threads, asyncio, WebSocket, DNS, or sessions.
Every function is a pure transformation — safe to call from any execution
context including the in-pod agent.

Exception contract
──────────────────
Exactly two exception types are raised by this package:

* ``ProtocolError`` — an encoder received invalid arguments.  This is always
  a bug in the calling layer.  Never catch silently.
* ``FrameDecodingError`` — a decoder received corrupt wire data from the
  remote peer.  Always propagate; never discard.

SOCKS5 enum notes
─────────────────
``AuthMethod``, ``Cmd``, ``AddrType``, and ``Reply`` inherit from the private
``_StrictIntEnum`` base, which raises ``ValueError`` immediately on unknown
wire values rather than returning ``None``.  The proxy layer must catch
``ValueError`` and map it to the appropriate ``Reply`` code.

``UserPassStatus`` inherits plain ``IntEnum`` because RFC 1929 §2 requires
that any non-zero byte be treated as failure rather than rejected — its
``_missing_`` maps ``[0x01, 0xFE]`` to ``FAILURE``.

``AuthMethod`` and ``Cmd`` expose ``is_supported() -> bool`` to
programmatically detect wire-defined but unimplemented values (``GSSAPI``,
``BIND``) without relying on documentation.
"""

from __future__ import annotations

from .enums import (
    AddrType,
    AuthMethod,
    Cmd,
    Reply,
    UserPassStatus,
)
from .frames import (
    FRAME_PREFIX,
    FRAME_SUFFIX,
    MAX_FRAME_LEN,
    PORT_UNSPECIFIED,
    READY_FRAME,
    SESSION_CONN_ID,
    ParsedFrame,
    decode_binary_payload,
    decode_error_payload,
    encode_conn_ack_frame,
    encode_conn_close_frame,
    encode_conn_open_frame,
    encode_data_frame,
    encode_error_frame,
    encode_host_port,
    encode_keepalive_frame,
    encode_udp_close_frame,
    encode_udp_data_frame,
    encode_udp_open_frame,
    is_ready_frame,
    parse_frame,
    parse_host_port,
)
from .ids import (
    CONN_FLOW_ID_RE,
    ID_RE,
    SESSION_ID_RE,
    new_conn_id,
    new_flow_id,
    new_session_id,
)

__all__ = [
    # ── Frame constants ────────────────────────────────────────────────────
    "FRAME_PREFIX",
    "FRAME_SUFFIX",
    "MAX_FRAME_LEN",
    "PORT_UNSPECIFIED",
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
    "encode_conn_ack_frame",
    "encode_conn_close_frame",
    "encode_conn_open_frame",
    "encode_data_frame",
    "encode_error_frame",
    "encode_keepalive_frame",
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
    # ── ID generators & validators ─────────────────────────────────────────
    "CONN_FLOW_ID_RE",
    "ID_RE",
    "SESSION_ID_RE",
    "new_conn_id",
    "new_flow_id",
    "new_session_id",
]
