"""TCP connection and UDP flow ID generation.

IDs are cryptographically random, prefix-namespaced strings that are safe
to embed in any frame field without escaping.

Format
──────
    <prefix><24 lowercase hex chars>

    TCP connection IDs: ``c<24 hex>``  e.g. ``ca1b2c3d4e5f6a7b8c9d0e1f2a3b``
    UDP flow IDs:       ``u<24 hex>``  e.g. ``ua1b2c3d4e5f6a7b8c9d0e1f2a3b``

Entropy
───────
    12 bytes → 96-bit token → birthday bound ≈ 2^48 before 50 % collision
    probability; safe for high-concurrency, long-lived tunnel sessions.
"""

from __future__ import annotations

import re
import secrets
from typing import Final

__all__ = [
    "ID_RE",
    "SESSION_CONN_ID",
    "new_conn_id",
    "new_flow_id",
]

# ── Internal constants ────────────────────────────────────────────────────────

# Prefix characters that namespace IDs by type, preventing any cross-type
# collision even if the underlying token bytes happen to be identical.
_TCP_PREFIX: Final[str] = "c"
_UDP_PREFIX: Final[str] = "u"

# 12 bytes → 24 hex chars → 96-bit entropy.
_TOKEN_BYTES: Final[int] = 12

# ── Public API ────────────────────────────────────────────────────────────────

# Session-level error sentinel: a valid conn_id-shaped value that is
# deliberately outside the random space (all-zero token) so callers can
# distinguish session-level errors from per-connection errors.
SESSION_CONN_ID: Final[str] = _TCP_PREFIX + "0" * (_TOKEN_BYTES * 2)

# Compiled pattern for validating conn_id / flow_id values.
# re.ASCII ensures [0-9a-f] never matches Unicode digits on exotic builds.
# Exported so that frames.py and the agent can share the same validator
# without importing the generator functions.
ID_RE: Final[re.Pattern[str]] = re.compile(r"^[cu][0-9a-f]{24}$", re.ASCII)


def new_conn_id() -> str:
    """Generate a unique TCP connection ID.

    Returns:
        A string of the form ``c<24 hex chars>``,
        e.g. ``ca1b2c3d4e5f6a7b8c9d0e1f2a3b``.
    """
    result = _TCP_PREFIX + secrets.token_hex(_TOKEN_BYTES)
    assert ID_RE.match(result), f"new_conn_id produced invalid ID: {result!r}"  # noqa: S101
    return result


def new_flow_id() -> str:
    """Generate a unique UDP flow ID.

    Returns:
        A string of the form ``u<24 hex chars>``,
        e.g. ``ua1b2c3d4e5f6a7b8c9d0e1f2a3b``.
    """
    result = _UDP_PREFIX + secrets.token_hex(_TOKEN_BYTES)
    assert ID_RE.match(result), f"new_flow_id produced invalid ID: {result!r}"  # noqa: S101
    return result
