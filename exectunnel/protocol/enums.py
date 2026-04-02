"""SOCKS5 protocol enumerations."""

from __future__ import annotations

from enum import IntEnum


class AuthMethod(IntEnum):
    NO_AUTH = 0x00
    NO_ACCEPT = 0xFF


class Cmd(IntEnum):
    CONNECT = 0x01
    BIND = 0x02
    UDP_ASSOCIATE = 0x03


class AddrType(IntEnum):
    IPV4 = 0x01
    DOMAIN = 0x03
    IPV6 = 0x04


class Reply(IntEnum):
    SUCCESS = 0x00
    GENERAL_FAILURE = 0x01
    NOT_ALLOWED = 0x02
    NET_UNREACHABLE = 0x03
    HOST_UNREACHABLE = 0x04
    REFUSED = 0x05
    CMD_NOT_SUPPORTED = 0x07
    ADDR_NOT_SUPPORTED = 0x08
