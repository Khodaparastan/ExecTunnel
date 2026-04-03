"""TCP connection and UDP flow ID generation."""

from __future__ import annotations

import re
import secrets

__all__ = [
    "ID_RE",
    "new_conn_id",
    "new_flow_id",
]

# Prefix characters that namespace IDs by type, preventing any cross-type
# collision even if the underlying token bytes happen to be identical.
_TCP_PREFIX: str = "c"
_UDP_PREFIX: str = "u"

# 12 bytes → 96-bit entropy → birthday bound ~2^48 before 50 % collision
# probability; safe for high-concurrency, long-lived tunnel sessions.
_TOKEN_BYTES: int = 12

# conn_id / flow_id: one prefix char + 24 lowercase hex chars (96-bit token).
# re.ASCII ensures [0-9a-f] never matches Unicode digits on exotic builds.
ID_RE: re.Pattern[str] = re.compile(r"^[cu][0-9a-f]{24}$", re.ASCII)


def _new_id(prefix: str) -> str:
    """
    Generate a cryptographically random, prefix-namespaced tunnel ID.

    The returned string is composed entirely of lowercase hex digits plus the
    single-character prefix, making it safe to embed in any frame field
    without escaping.

    Args:
        prefix: Single ASCII character prepended to the hex token.

    Returns:
        A string of the form ``<prefix><24 hex chars>``.
    """
    return prefix + secrets.token_hex(_TOKEN_BYTES)


def new_conn_id() -> str:
    """
    Generate a unique TCP connection ID.

    Returns:
        A string of the form ``c<24 hex chars>``,
        e.g. ``ca1b2c3d4e5f6a7b8c9d0e1f``.
    """
    return _new_id(_TCP_PREFIX)


def new_flow_id() -> str:
    """
    Generate a unique UDP flow ID.

    Returns:
        A string of the form ``u<24 hex chars>``,
        e.g. ``ua1b2c3d4e5f6a7b8c9d0e1f``.
    """
    return _new_id(_UDP_PREFIX)
