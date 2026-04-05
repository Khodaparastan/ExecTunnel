"""SOCKS5 protocol enumerations (RFC 1928 / RFC 1929).

All enums use ``_missing_`` to raise an informative ``ValueError`` on unknown
wire values rather than silently returning ``None``.  This surfaces protocol
violations at the earliest possible point.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Never

__all__ = [
    "AddrType",
    "AuthMethod",
    "Cmd",
    "Reply",
    "UserPassStatus",
]


class AuthMethod(IntEnum):
    """SOCKS5 authentication method codes (RFC 1928 §3).

    Note:
        ``GSSAPI`` is defined for wire-format completeness only.
        This tunnel implementation does **not** support GSSAPI negotiation;
        peers advertising only GSSAPI will receive ``NO_ACCEPT``.

    Use :meth:`is_supported` to programmatically check whether a method is
    implemented before attempting negotiation.
    """

    NO_AUTH = 0x00
    GSSAPI = 0x01
    USERNAME_PASSWORD = 0x02
    NO_ACCEPT = 0xFF

    @classmethod
    def _missing_(cls, value: object) -> Never:
        raise ValueError(
            f"{value!r} is not a valid {cls.__name__} "
            f"(expected one of {[m.value for m in cls]})"
        )

    def is_supported(self) -> bool:
        """Return ``True`` if this method is implemented by this tunnel.

        ``GSSAPI`` is defined for wire-format completeness only and is **not**
        supported.  Callers should check this before attempting negotiation.
        """
        return self not in _AUTH_METHOD_UNSUPPORTED


# Populated after class definition to avoid forward-reference issues.
_AUTH_METHOD_UNSUPPORTED: frozenset[AuthMethod] = frozenset({AuthMethod.GSSAPI})


class Cmd(IntEnum):
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

    @classmethod
    def _missing_(cls, value: object) -> Never:
        raise ValueError(
            f"{value!r} is not a valid {cls.__name__} "
            f"(expected one of {[m.value for m in cls]})"
        )

    def is_supported(self) -> bool:
        """Return ``True`` if this command is implemented by this tunnel.

        ``BIND`` is defined for wire-format completeness only and is **not**
        supported.  Callers should check this before dispatching.
        """
        return self not in _CMD_UNSUPPORTED


# Populated after class definition to avoid forward-reference issues.
_CMD_UNSUPPORTED: frozenset[Cmd] = frozenset({Cmd.BIND})


class AddrType(IntEnum):
    """SOCKS5 address type codes (RFC 1928 §4)."""

    IPV4 = 0x01
    DOMAIN = 0x03
    IPV6 = 0x04

    @classmethod
    def _missing_(cls, value: object) -> Never:
        raise ValueError(
            f"{value!r} is not a valid {cls.__name__} "
            f"(expected one of {[m.value for m in cls]})"
        )


class Reply(IntEnum):
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

    @classmethod
    def _missing_(cls, value: object) -> Never:
        raise ValueError(
            f"{value!r} is not a valid {cls.__name__} "
            f"(expected one of {[m.value for m in cls]})"
        )


class UserPassStatus(IntEnum):
    """RFC 1929 §2 username/password sub-negotiation reply codes.

    Note:
        RFC 1929 §2 states that any non-zero status value indicates failure,
        not just ``0xFF``.  ``_missing_`` therefore maps any non-zero byte
        value in ``[0x01, 0xFE]`` to ``FAILURE`` so that non-standard peers
        are handled correctly rather than raising ``ValueError``.
    """

    SUCCESS = 0x00
    FAILURE = 0xFF

    @classmethod
    def _missing_(cls, value: object) -> "UserPassStatus":
        # RFC 1929 §2: any non-zero value means failure.
        if isinstance(value, int) and 0x01 <= value <= 0xFE:
            return cls.FAILURE
        raise ValueError(
            f"{value!r} is not a valid {cls.__name__} "
            f"(expected 0x00 for success or any non-zero byte for failure)"
        )
