"""Tests for exectunnel.core.config — env var parsing and config building."""
from __future__ import annotations

import ssl

import pytest

from exectunnel.config import (
    BridgeConfig,
    TunnelConfig,
    create_ssl_context,
    get_app_config,
    get_wss_url,
    parse_bool_env,
    parse_float_env,
    parse_int_env,
)
from exectunnel.exceptions import ConfigError

# ── parse_bool_env ────────────────────────────────────────────────────────────


class TestParseBoolEnv:
    @pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "on"])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("TEST_BOOL", value)
        assert parse_bool_env("TEST_BOOL") is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_falsy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("TEST_BOOL", value)
        assert parse_bool_env("TEST_BOOL") is False

    def test_missing_returns_default_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_BOOL", raising=False)
        assert parse_bool_env("TEST_BOOL") is False

    def test_missing_returns_custom_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_BOOL", raising=False)
        assert parse_bool_env("TEST_BOOL", default=True) is True


# ── parse_float_env ───────────────────────────────────────────────────────────


class TestParseFloatEnv:
    def test_valid_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_FLOAT", "3.14")
        assert parse_float_env("TEST_FLOAT", 0.0) == pytest.approx(3.14)

    def test_missing_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_FLOAT", raising=False)
        assert parse_float_env("TEST_FLOAT", 42.0) == 42.0

    def test_invalid_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_FLOAT", "not-a-float")
        assert parse_float_env("TEST_FLOAT", 7.5) == 7.5

    def test_below_min_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_FLOAT", "0.01")
        assert parse_float_env("TEST_FLOAT", 7.5, min_value=0.1) == 7.5


# ── parse_int_env ─────────────────────────────────────────────────────────────


class TestParseIntEnv:
    def test_valid_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT", "42")
        assert parse_int_env("TEST_INT", 0) == 42

    def test_missing_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_INT", raising=False)
        assert parse_int_env("TEST_INT", 99) == 99

    def test_invalid_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT", "abc")
        assert parse_int_env("TEST_INT", 5) == 5

    def test_below_min_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT", "-1")
        assert parse_int_env("TEST_INT", 5, min_value=0) == 5


# ── get_wss_url ───────────────────────────────────────────────────────────────


class TestGetWssUrl:
    def test_returns_url_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WSS_URL", "wss://example.com/ws")
        assert get_wss_url() == "wss://example.com/ws"

    def test_raises_config_error_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WSS_URL", raising=False)
        with pytest.raises(ConfigError, match="WSS_URL"):
            get_wss_url()

    def test_raises_config_error_when_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WSS_URL", "")
        with pytest.raises(ConfigError):
            get_wss_url()

    def test_raises_config_error_when_bad_scheme(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WSS_URL", "https://example.com/ws")
        with pytest.raises(ConfigError, match="ws:// or wss://"):
            get_wss_url()

    def test_raises_config_error_when_missing_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WSS_URL", "wss:///ws")
        with pytest.raises(ConfigError, match="must include a host"):
            get_wss_url()


# ── get_app_config ────────────────────────────────────────────────────────────────


class TestGetConfig:
    def test_builds_app_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WSS_URL", "wss://k8s.local/api/v1/exec")
        monkeypatch.delenv("WSS_INSECURE", raising=False)
        cfg = get_app_config()
        assert cfg.wss_url == "wss://k8s.local/api/v1/exec"
        assert cfg.insecure is False
        assert isinstance(cfg.bridge, BridgeConfig)

    def test_insecure_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WSS_URL", "wss://example.com")
        monkeypatch.setenv("WSS_INSECURE", "1")
        cfg = get_app_config()
        assert cfg.insecure is True

    def test_custom_ping_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WSS_URL", "wss://example.com")
        monkeypatch.setenv("WSS_PING_INTERVAL", "60")
        cfg = get_app_config()
        assert cfg.bridge.ping_interval == 60

    def test_reconnect_delay_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WSS_URL", "wss://example.com")
        monkeypatch.setenv("WSS_RECONNECT_BASE_DELAY", "5")
        monkeypatch.setenv("WSS_RECONNECT_MAX_DELAY", "1")
        cfg = get_app_config()
        assert cfg.bridge.reconnect_base_delay == 5.0
        assert cfg.bridge.reconnect_max_delay == 5.0


# ── create_ssl_context ────────────────────────────────────────────────────────


class TestCreateSslContext:
    def test_secure_context(self) -> None:
        ctx = create_ssl_context(insecure=False)
        assert ctx.check_hostname is True
        assert ctx.verify_mode == ssl.CERT_REQUIRED

    def test_insecure_context(self) -> None:
        ctx = create_ssl_context(insecure=True)
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE


# ── TunnelConfig defaults ─────────────────────────────────────────────────────


class TestTunnelConfig:
    def test_default_exclusions_are_rfc1918(self) -> None:
        cfg = TunnelConfig()
        networks = {str(n) for n in cfg.exclude}
        assert "10.0.0.0/8" in networks
        assert "172.16.0.0/12" in networks
        assert "192.168.0.0/16" in networks
        assert "127.0.0.0/8" in networks

    def test_exclusion_lists_are_independent(self) -> None:
        """Each TunnelConfig instance must have its own exclusion list."""
        cfg1 = TunnelConfig()
        cfg2 = TunnelConfig()
        assert cfg1.exclude is not cfg2.exclude
