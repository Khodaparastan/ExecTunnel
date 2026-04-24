"""TCP connection, UDP flow, and session ID generation.

IDs are cryptographically random, prefix-namespaced strings safe to embed
in any frame field without escaping.

Wire format:
    ``<prefix><24 lowercase hex chars>``

    * TCP connection IDs: ``c<24 hex>`` e.g. ``ca1b2c3d4e5f6a7b8c9d0e1f``
    * UDP flow IDs:       ``u<24 hex>`` e.g. ``ua1b2c3d4e5f6a7b8c9d0e1f``
    * Session IDs:        ``s<24 hex>`` e.g. ``s3f7a1c9e2b4d6f8a0c2e4b6``

Entropy:
    12 bytes → 96-bit token → birthday bound ≈ 2^48 before 50% collision
    probability; safe for high-concurrency, long-lived tunnel sessions.

Validators:
    :data:`CONN_FLOW_ID_RE` validates both TCP connection IDs (``c``
    prefix) and UDP flow IDs (``u`` prefix). :data:`ID_RE` is a deprecated
    alias; prefer :data:`CONN_FLOW_ID_RE` in new code.

Sentinel:
    :data:`SESSION_CONN_ID` (``c`` + 24 zeros) is a valid
    :data:`CONN_FLOW_ID_RE`-shaped value used to signal session-level
    errors. It is deliberately outside the random space — :func:`secrets.token_hex`
    draws from a CSPRNG and will never produce the all-zero string in practice.
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

# ── Private constants ─────────────────────────────────────────────────────────

_TCP_PREFIX: Final[str] = "c"
_UDP_PREFIX: Final[str] = "u"
_SESSION_PREFIX: Final[str] = "s"

_TOKEN_BYTES: Final[int] = 12
_HEX_LEN: Final[int] = _TOKEN_BYTES * 2

# ── Public validators ─────────────────────────────────────────────────────────

CONN_FLOW_ID_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[{_TCP_PREFIX}{_UDP_PREFIX}][0-9a-f]{{{_HEX_LEN}}}$",
    re.ASCII,
)
"""Matches both TCP connection IDs (``c``) and UDP flow IDs (``u``)."""

#: Deprecated alias for :data:`CONN_FLOW_ID_RE`. Kept for backwards
#: compatibility with callers predating the ``c``/``u`` split. New code
#: must use :data:`CONN_FLOW_ID_RE`.
ID_RE: Final[re.Pattern[str]] = CONN_FLOW_ID_RE

SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(
    rf"^{_SESSION_PREFIX}[0-9a-f]{{{_HEX_LEN}}}$",
    re.ASCII,
)
"""Matches session IDs (``s`` prefix)."""

SESSION_CONN_ID: Final[str] = _TCP_PREFIX + "0" * _HEX_LEN
"""All-zero sentinel conn_id used to tag session-level ``ERROR`` frames."""


# ── Development-time sanity checks ────────────────────────────────────────────


def _assert_valid_conn_flow_id(result: str) -> None:
    """Development-time sanity check; stripped by ``python -O``.

    Args:
        result: The candidate ID string to verify.
    """
    assert CONN_FLOW_ID_RE.match(result), (  # noqa: S101
        f"ID generator produced invalid conn/flow ID: {result!r}"
    )


def _assert_valid_session_id(result: str) -> None:
    """Development-time sanity check; stripped by ``python -O``.

    Args:
        result: The candidate ID string to verify.
    """
    assert SESSION_ID_RE.match(result), (  # noqa: S101
        f"ID generator produced invalid session ID: {result!r}"
    )


# ── Public generators ─────────────────────────────────────────────────────────


def new_session_id() -> str:
    """Generate a unique session ID for log and task correlation.

    Session IDs use the ``s`` prefix to distinguish them from TCP
    connection IDs (``c``) and UDP flow IDs (``u``).

    Returns:
        A string of the form ``s<24 hex chars>``.
    """
    result = _SESSION_PREFIX + secrets.token_hex(_TOKEN_BYTES)
    _assert_valid_session_id(result)
    return result


def new_conn_id() -> str:
    """Generate a unique TCP connection ID.

    Returns:
        A string of the form ``c<24 hex chars>``.
    """
    result = _TCP_PREFIX + secrets.token_hex(_TOKEN_BYTES)
    _assert_valid_conn_flow_id(result)
    return result


def new_flow_id() -> str:
    """Generate a unique UDP flow ID.

    Returns:
        A string of the form ``u<24 hex chars>``.
    """
    result = _UDP_PREFIX + secrets.token_hex(_TOKEN_BYTES)
    _assert_valid_conn_flow_id(result)
    return result
