"""Tests for exectunnel.protocol.enums."""

from __future__ import annotations

import pytest
from exectunnel.protocol import (
    AddrType,
    AuthMethod,
    Cmd,
    Reply,
    UserPassStatus,
)


class TestStrictIntEnum:
    """_StrictIntEnum raises ValueError immediately on unknown wire values."""

    @pytest.mark.parametrize(
        ("cls", "value"),
        [
            (AuthMethod, 0x03),
            (AuthMethod, 0xFE),
            (Cmd, 0x00),
            (Cmd, 0x04),
            (AddrType, 0x00),
            (AddrType, 0x02),
            (Reply, 0x09),
            (Reply, 0xFF),
        ],
    )
    def test_unknown_wire_value_raises(self, cls, value):
        with pytest.raises(ValueError, match="is not a valid"):
            cls(value)

    @pytest.mark.parametrize(
        ("cls", "value", "expected"),
        [
            (AuthMethod, 0x00, AuthMethod.NO_AUTH),
            (AuthMethod, 0x01, AuthMethod.GSSAPI),
            (AuthMethod, 0x02, AuthMethod.USERNAME_PASSWORD),
            (AuthMethod, 0xFF, AuthMethod.NO_ACCEPT),
            (Cmd, 0x01, Cmd.CONNECT),
            (Cmd, 0x02, Cmd.BIND),
            (Cmd, 0x03, Cmd.UDP_ASSOCIATE),
            (AddrType, 0x01, AddrType.IPV4),
            (AddrType, 0x03, AddrType.DOMAIN),
            (AddrType, 0x04, AddrType.IPV6),
            (Reply, 0x00, Reply.SUCCESS),
            (Reply, 0x08, Reply.ADDR_NOT_SUPPORTED),
        ],
    )
    def test_known_wire_value_resolves(self, cls, value, expected):
        assert cls(value) is expected


class TestAuthMethodIsSupported:
    """is_supported() reflects the negotiation capability of the tunnel."""

    def test_no_auth_is_supported(self):
        assert AuthMethod.NO_AUTH.is_supported() is True

    def test_gssapi_is_not_supported(self):
        assert AuthMethod.GSSAPI.is_supported() is False

    def test_username_password_is_not_supported(self):
        assert AuthMethod.USERNAME_PASSWORD.is_supported() is False

    def test_no_accept_is_not_supported(self):
        """NO_ACCEPT is a server rejection sentinel, not a negotiable method."""
        assert AuthMethod.NO_ACCEPT.is_supported() is False


class TestCmdIsSupported:
    """is_supported() reflects implemented SOCKS5 commands."""

    def test_connect_is_supported(self):
        assert Cmd.CONNECT.is_supported() is True

    def test_udp_associate_is_supported(self):
        assert Cmd.UDP_ASSOCIATE.is_supported() is True

    def test_bind_is_not_supported(self):
        assert Cmd.BIND.is_supported() is False


class TestUserPassStatus:
    """UserPassStatus maps non-zero bytes to FAILURE per RFC 1929 §2."""

    def test_zero_is_success(self):
        assert UserPassStatus(0x00) is UserPassStatus.SUCCESS

    def test_0xff_is_failure(self):
        assert UserPassStatus(0xFF) is UserPassStatus.FAILURE

    @pytest.mark.parametrize("value", [0x01, 0x02, 0x7F, 0xFE])
    def test_non_zero_byte_maps_to_failure(self, value):
        assert UserPassStatus(value) is UserPassStatus.FAILURE

    @pytest.mark.parametrize("value", [-1, 256, "x", None])
    def test_invalid_value_raises(self, value):
        with pytest.raises((ValueError, TypeError)):
            UserPassStatus(value)
