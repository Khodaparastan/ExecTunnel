"""SOCKS5 protocol enumerations (RFC 1928 / RFC 1929).

All enums except :class:`UserPassStatus` inherit from
``_StrictIntEnum``, which raises :exc:`ValueError` on unknown wire values
at the earliest possible point — the moment the byte is read off the wire.

:class:`UserPassStatus` uses a permissive ``_missing_`` that maps any
non-zero byte to :attr:`UserPassStatus.FAILURE` per RFC 1929 §2, so it
cannot share the strict base.
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

# ── Internal range constants for RFC 1929 §2 permissive mapping ───────────────

_USERPASS_FAILURE_MIN: Final[int] = 0x01
_USERPASS_FAILURE_MAX: Final[int] = 0xFE


class _StrictIntEnum(IntEnum):
    """``IntEnum`` that raises :exc:`ValueError` on unknown wire values.

    Subclasses inherit this behaviour automatically. Do not override
    ``_missing_`` unless the RFC explicitly requires a different mapping —
    :class:`UserPassStatus` is the sole exception in this package.
    """

    @classmethod
    def _missing_(cls, value: object) -> Never:  # type: ignore[override]
        raise ValueError(
            f"{value!r} is not a valid {cls.__name__} "
            f"(expected one of {[m.value for m in cls]})"
        )


class AuthMethod(_StrictIntEnum):
    """SOCKS5 authentication method codes (RFC 1928 §3).

    Only :attr:`NO_AUTH` is negotiated by this tunnel. :attr:`GSSAPI` and
    :attr:`USERNAME_PASSWORD` are defined for wire-format completeness.
    :attr:`NO_ACCEPT` is the server-side rejection sentinel returned when
    no proposed method is acceptable — it is **not** a negotiable method.

    Use :meth:`is_supported` to check whether a method is accepted before
    attempting negotiation.
    """

    NO_AUTH = 0x00
    GSSAPI = 0x01
    USERNAME_PASSWORD = 0x02
    NO_ACCEPT = 0xFF

    def is_supported(self) -> bool:
        """Check whether this method is negotiated by this tunnel.

        Returns:
            ``True`` for :attr:`NO_AUTH` only. :attr:`GSSAPI`,
            :attr:`USERNAME_PASSWORD`, and :attr:`NO_ACCEPT` all return
            ``False``.
        """
        return self not in _AUTH_METHOD_UNSUPPORTED


_AUTH_METHOD_UNSUPPORTED: Final[frozenset[AuthMethod]] = frozenset({
    AuthMethod.GSSAPI,
    AuthMethod.USERNAME_PASSWORD,
    AuthMethod.NO_ACCEPT,
})


class Cmd(_StrictIntEnum):
    """SOCKS5 command codes (RFC 1928 §4).

    :attr:`BIND` is defined for wire-format completeness only. Clients
    requesting :attr:`BIND` receive a ``CMD_NOT_SUPPORTED`` reply.

    Use :meth:`is_supported` to check whether a command is implemented
    before dispatching.
    """

    CONNECT = 0x01
    BIND = 0x02
    UDP_ASSOCIATE = 0x03

    def is_supported(self) -> bool:
        """Check whether this command is implemented by this tunnel.

        Returns:
            ``True`` for :attr:`CONNECT` and :attr:`UDP_ASSOCIATE`.
            :attr:`BIND` returns ``False``.
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

    Inherits from plain :class:`IntEnum` rather than ``_StrictIntEnum``
    because RFC 1929 §2 requires any non-zero status byte to be treated as
    failure, not rejected outright.
    """

    SUCCESS = 0x00
    FAILURE = 0xFF

    @classmethod
    def _missing_(cls, value: object) -> UserPassStatus:
        if (
            isinstance(value, int)
            and _USERPASS_FAILURE_MIN <= value <= _USERPASS_FAILURE_MAX
        ):
            return cls.FAILURE
        raise ValueError(
            f"{value!r} is not a valid {cls.__name__} "
            "(expected 0x00 for success or any non-zero byte for failure)"
        )
