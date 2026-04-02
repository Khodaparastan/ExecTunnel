"""SOCKS5 protocol enumerations (RFC 1928 / RFC 1929)."""

from __future__ import annotations

from enum import IntEnum


class AuthMethod(IntEnum):
    """SOCKS5 authentication method codes (RFC 1928 §3)."""

    NO_AUTH = 0x00
    GSSAPI = 0x01
    USERNAME_PASSWORD = 0x02
    NO_ACCEPT = 0xFF


class Cmd(IntEnum):
    """SOCKS5 command codes (RFC 1928 §4)."""

    CONNECT = 0x01
    BIND = 0x02
    UDP_ASSOCIATE = 0x03


class AddrType(IntEnum):
    """SOCKS5 address type codes (RFC 1928 §4)."""

    IPV4 = 0x01
    DOMAIN = 0x03
    IPV6 = 0x04


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
