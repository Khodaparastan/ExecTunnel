"""SOCKS5 protocol enumerations (RFC 1928 / RFC 1929).

All enums except ``UserPassStatus`` inherit from ``_StrictIntEnum``, which
raises an informative ``ValueError`` on unknown wire values rather than
silently returning ``None``.  This surfaces protocol violations at the
earliest possible point — the moment the byte is read off the wire.

``UserPassStatus`` uses a custom ``_missing_`` that maps any non-zero byte
to ``FAILURE`` per RFC 1929 §2, so it cannot share the strict base.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Final, Never

__all__ = [
    "AddrType",
    "AuthMethod",
    "Cmd",
    "Reply",
    "UserPassStatus",
]


# ── Base mixin ────────────────────────────────────────────────────────────────


class _StrictIntEnum(IntEnum):
    """``IntEnum`` that raises ``ValueError`` immediately on unknown wire values.

    Subclasses inherit this ``_missing_`` automatically.  Do **not** override
    ``_missing_`` in a subclass unless the RFC explicitly requires a different
    mapping for unknown values (see ``UserPassStatus`` for the one exception).
    """

    @classmethod
    def _missing_(cls, value: object) -> Never:
        raise ValueError(
            f"{value!r} is not a valid {cls.__name__} "
            f"(expected one of {[m.value for m in cls]})"
        )


# ── Enumerations ──────────────────────────────────────────────────────────────


class AuthMethod(_StrictIntEnum):
    """SOCKS5 authentication method codes (RFC 1928 §3).

    Note:
        ``GSSAPI`` and ``USERNAME_PASSWORD`` are defined for wire-format
        completeness only.  This tunnel implementation accepts **only**
        ``NO_AUTH``; peers advertising only unsupported methods will receive
        ``NO_ACCEPT``.

    Use :meth:`is_supported` to programmatically check whether a method is
    negotiated by this tunnel before attempting negotiation.
    """

    NO_AUTH = 0x00
    GSSAPI = 0x01
    USERNAME_PASSWORD = 0x02
    NO_ACCEPT = 0xFF

    def is_supported(self) -> bool:
        """Return ``True`` if this method is negotiated by this tunnel.

        Only ``NO_AUTH`` is negotiated.  ``GSSAPI`` and ``USERNAME_PASSWORD``
        are defined for wire-format completeness only and are **not** accepted
        by the server.  Callers should check this before attempting negotiation.
        """
        return self not in _AUTH_METHOD_UNSUPPORTED


_AUTH_METHOD_UNSUPPORTED: Final[frozenset[AuthMethod]] = frozenset(
    {AuthMethod.GSSAPI, AuthMethod.USERNAME_PASSWORD}
)


class Cmd(_StrictIntEnum):
    """SOCKS5 command codes (RFC 1928 §4).

    Note:
        ``BIND`` is defined for wire-format completeness only.
        This tunnel implementation does **not** support the BIND command;
        clients requesting BIND will receive a ``CMD_NOT_SUPPORTED`` reply.

    Use :meth:`is_supported` to programmatically check whether a command is
    implemented before dispatching.
    """

    CONNECT = 0x01
    BIND = 0x02
    UDP_ASSOCIATE = 0x03

    def is_supported(self) -> bool:
        """Return ``True`` if this command is implemented by this tunnel.

        ``BIND`` is defined for wire-format completeness only and is **not**
        supported.  Callers should check this before dispatching.
        """
        return self not in _CMD_UNSUPPORTED


_CMD_UNSUPPORTED: Final[frozenset[Cmd]] = frozenset({Cmd.BIND})


class AddrType(_StrictIntEnum):
    """SOCKS5 address type codes (RFC 1928 §4)."""

    IPV4 = 0x01
    DOMAIN = 0x03
    IPV6 = 0x04


class Reply(_StrictIntEnum):
    """SOCKS5 reply codes (RFC 1928 §6)."""

    SUCCESS = 0x00
    GENERAL_FAILURE = 0x01
    NOT_ALLOWED = 0x02
    NET_UNREACHABLE = 0x03
    HOST_UNREACHABLE = 0x04
    REFUSED = 0x05
    TTL_EXPIRED = 0x06
    CMD_NOT_SUPPORTED = 0x07
    ADDR_NOT_SUPPORTED = 0x08


class UserPassStatus(IntEnum):
    """RFC 1929 §2 username/password sub-negotiation reply codes.

    Inherits directly from ``IntEnum`` rather than ``_StrictIntEnum`` because
    RFC 1929 §2 requires that any non-zero status byte be treated as failure,
    not rejected.  This is the only enum in this module with a permissive
    ``_missing_``.
    """

    SUCCESS = 0x00
    FAILURE = 0xFF

    @classmethod
    def _missing_(cls, value: object) -> UserPassStatus:
        # RFC 1929 §2: any non-zero value means failure.
        if isinstance(value, int) and 0x01 <= value <= 0xFE:
            return cls.FAILURE
        raise ValueError(
            f"{value!r} is not a valid {cls.__name__} "
            f"(expected 0x00 for success or any non-zero byte for failure)"
        )
