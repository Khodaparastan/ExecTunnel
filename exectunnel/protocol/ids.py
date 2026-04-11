"""TCP connection, UDP flow, and session ID generation.

IDs are cryptographically random, prefix-namespaced strings that are safe
to embed in any frame field without escaping.

Format
──────
    <prefix><24 lowercase hex chars>

    TCP connection IDs: ``c<24 hex>``  e.g. ``ca1b2c3d4e5f6a7b8c9d0e1f``
    UDP flow IDs:       ``u<24 hex>``  e.g. ``ua1b2c3d4e5f6a7b8c9d0e1f``
    Session IDs:        ``s<24 hex>``  e.g. ``s3f7a1c9e2b4d6f8a0c2e4b6``

Entropy
───────
    12 bytes → 96-bit token → birthday bound ≈ 2^48 before 50 % collision
    probability; safe for high-concurrency, long-lived tunnel sessions.

    ``secrets.token_hex`` is guaranteed to return lowercase hexadecimal, so
    the post-generation asserts are development-time sanity checks only.
    They are stripped in optimised builds (``python -O``) and will never
    fire in practice.
"""

from __future__ import annotations

import re
import secrets
from typing import Final

__all__ = [
    "CONN_FLOW_ID_RE",
    "ID_RE",
    "SESSION_CONN_ID",
    "SESSION_ID_RE",
    "new_conn_id",
    "new_flow_id",
    "new_session_id",
]

# ── Internal constants ────────────────────────────────────────────────────────

_TCP_PREFIX: Final[str] = "c"
_UDP_PREFIX: Final[str] = "u"
_SESSION_PREFIX: Final[str] = "s"

# 12 bytes → 24 hex chars → 96-bit entropy.
_TOKEN_BYTES: Final[int] = 12
_HEX_LEN: Final[int] = _TOKEN_BYTES * 2  # 24

# ── Compiled patterns ─────────────────────────────────────────────────────────

# Validates TCP connection IDs (c-prefix) and UDP flow IDs (u-prefix).
# Used by frames.py to validate conn_id / flow_id fields on the wire.
# re.ASCII ensures [0-9a-f] never matches Unicode digits on exotic builds.
CONN_FLOW_ID_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[{_TCP_PREFIX}{_UDP_PREFIX}][0-9a-f]{{{_HEX_LEN}}}$", re.ASCII
)

# Backward-compatible alias — existing code imports ``ID_RE`` from this module.
ID_RE: Final[re.Pattern[str]] = CONN_FLOW_ID_RE

# Validates session IDs (s-prefix).
SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(
    rf"^{_SESSION_PREFIX}[0-9a-f]{{{_HEX_LEN}}}$", re.ASCII
)

# ── Sentinels ─────────────────────────────────────────────────────────────────

# Session-level error sentinel: a valid conn_id-shaped value that is
# deliberately outside the random space (all-zero token) so callers can
# distinguish session-level errors from per-connection errors.
#
# Safety assumption: ``secrets.token_hex`` draws from a CSPRNG and will
# never return the all-zero string in practice.
SESSION_CONN_ID: Final[str] = _TCP_PREFIX + "0" * _HEX_LEN


# ── ID generators ─────────────────────────────────────────────────────────────


def new_session_id() -> str:
    """Generate a unique session ID for log and task correlation.

    Session IDs use the ``s`` prefix to distinguish them from TCP connection
    IDs (``c``) and UDP flow IDs (``u``).

    Returns:
        A string of the form ``s<24 hex chars>``.
    """
    result = _SESSION_PREFIX + secrets.token_hex(_TOKEN_BYTES)
    assert SESSION_ID_RE.match(result), (  # noqa: S101
        f"new_session_id produced invalid ID: {result!r}"
    )
    return result


def new_conn_id() -> str:
    """Generate a unique TCP connection ID.

    Returns:
        A string of the form ``c<24 hex chars>``.
    """
    result = _TCP_PREFIX + secrets.token_hex(_TOKEN_BYTES)
    assert ID_RE.match(result), f"new_conn_id produced invalid ID: {result!r}"  # noqa: S101
    return result


def new_flow_id() -> str:
    """Generate a unique UDP flow ID.

    Returns:
        A string of the form ``u<24 hex chars>``.
    """
    result = _UDP_PREFIX + secrets.token_hex(_TOKEN_BYTES)
    assert ID_RE.match(result), f"new_flow_id produced invalid ID: {result!r}"  # noqa: S101
    return result
