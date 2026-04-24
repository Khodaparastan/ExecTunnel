"""Public data types produced by the protocol layer.

Currently limited to :class:`ParsedFrame`, the structured return value of
:func:`exectunnel.protocol.parser.parse_frame`.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ParsedFrame"]


@dataclass(frozen=True, slots=True)
class ParsedFrame:
    """Structured result of a successful frame parse.

    Attributes:
        msg_type: The frame type string, guaranteed to be a member of
            :data:`exectunnel.protocol.constants.VALID_MSG_TYPES`.
        conn_id: Tunnel connection/flow ID, or ``None`` for frame types in
            :data:`exectunnel.protocol.constants.NO_CONN_ID_TYPES` and
            :data:`exectunnel.protocol.constants.NO_CONN_ID_WITH_PAYLOAD_TYPES`.
            May equal :data:`exectunnel.protocol.ids.SESSION_CONN_ID` for
            session-level ``ERROR`` frames.
        payload: Frame payload string, or the empty string when the frame
            type carries no payload.
    """

    msg_type: str
    conn_id: str | None
    payload: str
