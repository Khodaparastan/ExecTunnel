"""Tests for exectunnel.proxy.config."""

from __future__ import annotations

import pytest
from exectunnel.exceptions import ConfigurationError
from exectunnel.proxy.config import Socks5ServerConfig


class TestSocks5ServerConfigDefaults:
    def test_default_host(self):
        assert Socks5ServerConfig().host == "127.0.0.1"

    def test_default_port(self):
        assert Socks5ServerConfig().port == 1080

    def test_default_handshake_timeout(self):
        assert Socks5ServerConfig().handshake_timeout == 30.0

    def test_default_request_queue_capacity(self):
        assert Socks5ServerConfig().request_queue_capacity == 256

    def test_default_udp_relay_queue_capacity(self):
        assert Socks5ServerConfig().udp_relay_queue_capacity == 2_048

    def test_default_queue_put_timeout(self):
        assert Socks5ServerConfig().queue_put_timeout == 5.0

    def test_default_udp_drop_warn_interval(self):
        assert Socks5ServerConfig().udp_drop_warn_interval == 1_000

    def test_immutable(self):
        cfg = Socks5ServerConfig()
        with pytest.raises(Exception):
            cfg.port = 9999  # type: ignore[misc]


class TestSocks5ServerConfigCustomValues:
    def test_custom_host_and_port(self):
        cfg = Socks5ServerConfig(
            host="10.0.0.1",
            port=9050,
            allow_non_loopback=True,
            udp_bind_host="10.0.0.1",
            udp_advertise_host="10.0.0.1",
        )
        assert cfg.host == "10.0.0.1"
        assert cfg.port == 9050

    def test_custom_handshake_timeout(self):
        cfg = Socks5ServerConfig(handshake_timeout=10.0)
        assert cfg.handshake_timeout == 10.0

    def test_custom_capacities(self):
        cfg = Socks5ServerConfig(request_queue_capacity=1, udp_relay_queue_capacity=1)
        assert cfg.request_queue_capacity == 1
        assert cfg.udp_relay_queue_capacity == 1

    def test_custom_advanced_fields(self):
        cfg = Socks5ServerConfig(
            allow_non_loopback=True,
            listen_backlog=128,
            max_concurrent_handshakes=100,
            udp_bind_host="0.0.0.0",
            udp_advertise_host="1.2.3.4",
        )
        assert cfg.allow_non_loopback is True
        assert cfg.listen_backlog == 128
        assert cfg.max_concurrent_handshakes == 100
        assert cfg.udp_bind_host == "0.0.0.0"
        assert cfg.udp_advertise_host == "1.2.3.4"
        assert cfg.effective_udp_advertise_host == "1.2.3.4"

    def test_effective_udp_advertise_host_defaults_to_bind(self):
        cfg = Socks5ServerConfig(udp_bind_host="127.0.0.1", udp_advertise_host=None)
        assert cfg.effective_udp_advertise_host == "127.0.0.1"

    def test_port_1_is_valid(self):
        cfg = Socks5ServerConfig(port=1)
        assert cfg.port == 1

    def test_port_65535_is_valid(self):
        cfg = Socks5ServerConfig(port=65_535)
        assert cfg.port == 65_535


class TestSocks5ServerConfigValidation:
    def test_empty_host_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(host="")

    def test_port_zero_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(port=0)

    def test_port_above_65535_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(port=65_536)

    def test_port_negative_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(port=-1)

    def test_handshake_timeout_zero_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(handshake_timeout=0.0)

    def test_handshake_timeout_negative_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(handshake_timeout=-1.0)

    def test_request_queue_capacity_zero_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(request_queue_capacity=0)

    def test_udp_relay_queue_capacity_zero_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(udp_relay_queue_capacity=0)

    def test_queue_put_timeout_zero_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(queue_put_timeout=0.0)

    def test_queue_put_timeout_negative_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(queue_put_timeout=-0.1)

    def test_udp_drop_warn_interval_zero_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(udp_drop_warn_interval=0)

    def test_non_loopback_without_flag_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(host="0.0.0.0", allow_non_loopback=False)

    def test_listen_backlog_zero_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(listen_backlog=0)

    def test_max_concurrent_handshakes_zero_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(max_concurrent_handshakes=0)

    def test_empty_udp_bind_host_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(udp_bind_host="")

    def test_invalid_udp_advertise_host_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(udp_advertise_host="not-an-ip")

    def test_unspecified_udp_advertise_host_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(udp_advertise_host="0.0.0.0")

    def test_loopback_udp_advertise_host_with_non_loopback_tcp_raises(self):
        with pytest.raises(ConfigurationError):
            Socks5ServerConfig(
                host="10.0.0.1",
                allow_non_loopback=True,
                udp_advertise_host="127.0.0.1",
            )

    def test_non_loopback_udp_bind_without_explicit_advertise_raises(self):
        with pytest.raises(ConfigurationError) as exc_info:
            Socks5ServerConfig(
                host="10.0.0.1",
                allow_non_loopback=True,
                udp_bind_host="10.0.0.1",
                # udp_advertise_host left as None (the default)
            )
        # Must point the operator at the *advertise* field, not the bind
        # field, and must mention the security concern in the hint.
        msg = exc_info.value.args[0]
        assert "udp_advertise_host" in msg
        details = exc_info.value.details
        assert details["field"] == "udp_advertise_host"
        assert "binding race" in (exc_info.value.hint or "")

    def test_non_loopback_udp_bind_with_unspecified_advertise_raises(self):
        with pytest.raises(ConfigurationError) as exc_info:
            Socks5ServerConfig(
                host="10.0.0.1",
                allow_non_loopback=True,
                udp_bind_host="10.0.0.1",
                udp_advertise_host="0.0.0.0",
            )
        details = exc_info.value.details
        assert details["field"] == "udp_advertise_host"

    def test_non_loopback_udp_bind_with_explicit_advertise_accepted(self):
        # Sanity: setting an explicit reachable advertise host satisfies
        # the new check and the rest of the validation block.
        cfg = Socks5ServerConfig(
            host="10.0.0.1",
            allow_non_loopback=True,
            udp_bind_host="10.0.0.1",
            udp_advertise_host="10.0.0.1",
        )
        assert cfg.effective_udp_advertise_host == "10.0.0.1"

    def test_error_message_contains_field_name(self):
        with pytest.raises(ConfigurationError) as exc_info:
            Socks5ServerConfig(port=0)
        assert "port" in exc_info.value.args[0]


class TestSocks5ServerConfigIsLoopback:
    def test_loopback_ipv4(self):
        assert Socks5ServerConfig(host="127.0.0.1").is_loopback is True

    def test_loopback_ipv4_alt(self):
        assert Socks5ServerConfig(host="127.0.0.2").is_loopback is True

    def test_non_loopback_ipv4(self):
        assert (
            Socks5ServerConfig(
                host="10.0.0.1",
                allow_non_loopback=True,
                udp_bind_host="10.0.0.1",
                udp_advertise_host="10.0.0.1",
            ).is_loopback
            is False
        )

    def test_private_ipv4_is_not_loopback(self):
        assert (
            Socks5ServerConfig(
                host="172.16.0.1",
                allow_non_loopback=True,
                udp_bind_host="172.16.0.1",
                udp_advertise_host="172.16.0.1",
            ).is_loopback
            is False
        )

    def test_loopback_ipv6(self):
        assert Socks5ServerConfig(host="::1").is_loopback is True

    def test_non_loopback_ipv6(self):
        assert (
            Socks5ServerConfig(
                host="2001:db8::1",
                allow_non_loopback=True,
                udp_bind_host="2001:db8::1",
                udp_advertise_host="2001:db8::1",
            ).is_loopback
            is False
        )

    def test_hostname_localhost_is_loopback(self):
        assert Socks5ServerConfig(host="localhost").is_loopback is True


class TestSocks5ServerConfigUrl:
    def test_ipv4_url(self):
        assert (
            Socks5ServerConfig(host="127.0.0.1", port=1080).url
            == "tcp://127.0.0.1:1080"
        )

    def test_ipv6_url_uses_brackets(self):
        assert Socks5ServerConfig(host="::1", port=1080).url == "tcp://[::1]:1080"

    def test_hostname_url(self):
        assert (
            Socks5ServerConfig(host="localhost", port=1080).url
            == "tcp://localhost:1080"
        )

    def test_custom_port_in_url(self):
        assert (
            Socks5ServerConfig(host="127.0.0.1", port=9050).url
            == "tcp://127.0.0.1:9050"
        )
