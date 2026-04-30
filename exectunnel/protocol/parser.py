"""Single-line frame parser and the ``AGENT_READY`` predicate.

:func:`parse_frame` is the only consumer of raw tunnel lines in the
protocol layer. It is deliberately strict about structural validity and
permissive about non-frame shell noise:

* Returns ``None`` for lines that are plainly not tunnel frames
  (shell prompts, log lines, blank lines).
* Returns a :class:`exectunnel.protocol.types.ParsedFrame` for
  structurally valid, recognised tunnel frames.
* Raises :exc:`exectunnel.exceptions.FrameDecodingError` when a line
  carries the tunnel prefix/suffix but its structure is corrupt.

:func:`is_ready_frame` is a pure string predicate used by the bootstrap
scanner; it never raises because the bootstrap path must tolerate
arbitrary pre-handshake garbage.
"""

from __future__ import annotations

import logging
from typing import Final

from exectunnel.exceptions import FrameDecodingError

from .codecs import _hex_preview  # noqa: PLC2701 — sibling-module helper
from .constants import (
    FRAME_PREFIX,
    FRAME_SUFFIX,
    MAX_TUNNEL_FRAME_CHARS,
    NO_CONN_ID_TYPES,
    NO_CONN_ID_WITH_PAYLOAD_TYPES,
    PAYLOAD_FORBIDDEN_TYPES,
    PAYLOAD_PREVIEW_LEN,
    PAYLOAD_REQUIRED_TYPES,
    READY_FRAME,
    VALID_MSG_TYPES,
)
from .ids import CONN_FLOW_ID_RE, SESSION_CONN_ID
from .types import ParsedFrame

__all__ = ["is_ready_frame", "parse_frame"]

_log: Final[logging.Logger] = logging.getLogger(__name__)

# Maximum number of ``:``-splits performed on a frame's inner content.
# A value of 2 means ``split(":", 2)`` yields at most 3 elements:
# ``msg_type``, ``conn_id``, ``payload`` — preserving any ``:`` inside
# bracket-quoted IPv6 payloads (e.g. ``[2001:db8::1]:443``).
_INNER_MAX_SPLITS: Final[int] = 2

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


def _validate_id_namespace(msg_type: str, conn_id: str, raw_hex: str) -> None:
    """Validate TCP/UDP ID prefix semantics for a decoded frame."""
    if conn_id == SESSION_CONN_ID and msg_type != "ERROR":
        raise FrameDecodingError(
            f"Tunnel frame {msg_type!r} uses the session-level sentinel "
            f"conn_id {SESSION_CONN_ID!r}; this sentinel is only valid for "
            "ERROR frames.",
            details={"raw_bytes": raw_hex, "codec": "frame"},
        )

    if msg_type in _TCP_FRAME_TYPES and not conn_id.startswith(_TCP_ID_PREFIX):
        raise FrameDecodingError(
            f"Tunnel frame {msg_type!r} requires a TCP connection ID with "
            f"prefix {_TCP_ID_PREFIX!r}, got {conn_id!r}.",
            details={"raw_bytes": raw_hex, "codec": "frame"},
        )

    if msg_type in _UDP_FRAME_TYPES and not conn_id.startswith(_UDP_ID_PREFIX):
        raise FrameDecodingError(
            f"Tunnel frame {msg_type!r} requires a UDP flow ID with prefix "
            f"{_UDP_ID_PREFIX!r}, got {conn_id!r}.",
            details={"raw_bytes": raw_hex, "codec": "frame"},
        )


def _strip_proxy_suffix(line: str) -> tuple[str, bool]:
    """Strip proxy-injected suffixes from a candidate tunnel frame.

    Only attempts truncation when the line starts with :data:`FRAME_PREFIX`
    to avoid false positives from shell output that happens to contain
    ``>>>``.

    Args:
        line: A raw (already :py:meth:`str.strip`-ed) line from the
            tunnel channel.

    Returns:
        A ``(cleaned_line, is_tunnel_frame)`` tuple where
        *is_tunnel_frame* is ``True`` if the line has both prefix and
        suffix after cleaning.
    """
    if not line.startswith(FRAME_PREFIX):
        return line, False

    # Strip everything after the first complete tunnel frame. Some reverse
    # proxies / exec adapters append textual status material after the frame;
    # using rfind() keeps that material when it itself contains ">>>", turning
    # an otherwise valid frame into a corrupt one.
    suffix_pos = line.find(FRAME_SUFFIX)

    if suffix_pos != -1:
        line = line[: suffix_pos + len(FRAME_SUFFIX)]
    return line, line.endswith(FRAME_SUFFIX)


def parse_frame(line: str) -> ParsedFrame | None:
    """Parse a single line into a :class:`ParsedFrame` or return ``None``.

    Return semantics:

    * ``None`` — the line is not a tunnel frame at all (shell noise,
      blank lines, log output).
    * :class:`ParsedFrame` — a structurally valid, recognised tunnel
      frame.
    * :exc:`FrameDecodingError` — the line carries the tunnel
      prefix/suffix but its structure is corrupt.

    Args:
        line: A single line of text from the tunnel channel.

    Returns:
        A :class:`ParsedFrame` on success, or ``None`` if the line is
        not a tunnel frame.

    Raises:
        FrameDecodingError: If the line has the tunnel prefix/suffix but
            contains a malformed structure.
    """
    line = line.strip()
    line, is_tunnel_frame = _strip_proxy_suffix(line)

    if not is_tunnel_frame:
        if len(line) > MAX_TUNNEL_FRAME_CHARS:
            _log.debug(
                "parse_frame: dropping oversized non-frame line (%d chars)",
                len(line),
            )
        return None

    if len(line) > MAX_TUNNEL_FRAME_CHARS:
        raise FrameDecodingError(
            f"Oversized tunnel frame ({len(line)} chars, limit {MAX_TUNNEL_FRAME_CHARS}). "
            "Possible memory-exhaustion or injection attempt.",
            details={"raw_bytes": _hex_preview(line), "codec": "frame"},
        )

    inner = line[len(FRAME_PREFIX) : -len(FRAME_SUFFIX)]
    parts = inner.split(":", _INNER_MAX_SPLITS)
    msg_type = parts[0]
    raw_hex = _hex_preview(line)

    if msg_type not in VALID_MSG_TYPES:
        raise FrameDecodingError(
            f"Tunnel frame carries unrecognised msg_type {msg_type!r}.",
            details={"raw_bytes": raw_hex, "codec": "frame"},
        )

    # ── No-conn-id, no-payload frames (AGENT_READY, KEEPALIVE) ────────────────
    if msg_type in NO_CONN_ID_TYPES:
        if len(parts) > 1:
            raise FrameDecodingError(
                f"Tunnel frame {msg_type!r} must not carry a conn_id or payload, "
                f"but {len(parts) - 1} extra field(s) found.",
                details={"raw_bytes": raw_hex, "codec": "frame"},
            )
        return ParsedFrame(msg_type=msg_type, conn_id=None, payload="")

    # ── Session-scoped frames with payload but no conn_id (STATS) ─────────────
    if msg_type in NO_CONN_ID_WITH_PAYLOAD_TYPES:
        # Re-join parts[1:] so payloads containing ':' survive intact.
        payload = ":".join(parts[1:]) if len(parts) > 1 else ""
        if not payload:
            raise FrameDecodingError(
                f"Tunnel frame {msg_type!r} requires a payload but none was found.",
                details={"raw_bytes": raw_hex, "codec": "frame"},
            )
        return ParsedFrame(msg_type=msg_type, conn_id=None, payload=payload)

    # ── Standard per-connection frames ────────────────────────────────────────
    conn_id: str | None = parts[1] if len(parts) > 1 else None
    payload: str = parts[2] if len(parts) > _INNER_MAX_SPLITS else ""

    if conn_id is None:
        raise FrameDecodingError(
            f"Tunnel frame {msg_type!r} requires a conn_id but none was found.",
            details={"raw_bytes": raw_hex, "codec": "frame"},
        )

    if not CONN_FLOW_ID_RE.match(conn_id):
        raise FrameDecodingError(
            f"Tunnel frame has malformed conn_id {conn_id!r} "
            f"(msg_type={msg_type!r}). If this follows a known-good encode, "
            "suspect proxy body corruption.",
            details={"raw_bytes": raw_hex, "codec": "frame"},
        )

    _validate_id_namespace(msg_type, conn_id, raw_hex)

    if msg_type in PAYLOAD_REQUIRED_TYPES and not payload:
        raise FrameDecodingError(
            f"Tunnel frame {msg_type!r} requires a payload but none was found.",
            details={"raw_bytes": raw_hex, "codec": "frame"},
        )

    if msg_type in PAYLOAD_FORBIDDEN_TYPES and payload:
        truncated = (
            payload[:PAYLOAD_PREVIEW_LEN] + "..."
            if len(payload) > PAYLOAD_PREVIEW_LEN
            else payload
        )
        raise FrameDecodingError(
            f"Tunnel frame {msg_type!r} must not carry a payload, "
            f"but got {truncated!r}.",
            details={"raw_bytes": raw_hex, "codec": "frame"},
        )

    return ParsedFrame(msg_type=msg_type, conn_id=conn_id, payload=payload)


def is_ready_frame(line: str) -> bool:
    """Return ``True`` if *line* is the agent-ready sentinel frame.

    Pure string predicate — never raises. The bootstrap scanner must be
    maximally tolerant of garbage lines.

    Handles proxy-injected suffixes: any material after the closing
    ``>>>`` is stripped before comparison, consistent with
    :func:`parse_frame`.

    Args:
        line: A single line of text from the tunnel channel.

    Returns:
        ``True`` if the line is the ``AGENT_READY`` sentinel.
    """
    stripped = line.strip()
    cleaned, is_tunnel_frame = _strip_proxy_suffix(stripped)
    return is_tunnel_frame and cleaned == READY_FRAME
