"""Unit tests for :mod:`exectunnel.exceptions`.

The exception hierarchy is the public taxonomy contract — every error
code, retryable flag, and ``to_dict`` shape is consumed by callers,
metric emitters, and serialisation pipelines (Sentry, OTel, JSON
buses). These tests pin the contract.

The newer ``CtrlBackpressureError`` and ``PreAckBufferOverflowError``
are already covered by ``tests/unit/test_new_exceptions.py``; this
module covers everything else.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from exectunnel import exceptions as exc_mod
from exectunnel.exceptions import (
    AgentReadyTimeoutError,
    AgentSyntaxError,
    AgentTimeoutError,
    AgentVersionMismatchError,
    AuthenticationError,
    AuthorizationError,
    BootstrapError,
    ConfigError,
    ConfigurationError,
    ConnectionClosedError,
    ExecTunnelError,
    ExecutionError,
    ExecutionTimeoutError,
    FrameDecodingError,
    ProtocolError,
    ReconnectExhaustedError,
    RemoteProcessError,
    TransportError,
    UnexpectedFrameError,
    WebSocketSendTimeout,
    WebSocketSendTimeoutError,
)

# ── Base ExecTunnelError contract ─────────────────────────────────────────────


class TestBaseContract:
    """The abstract behaviour every subclass relies on."""

    def test_default_message_empty_string(self) -> None:
        err = ExecTunnelError()
        assert err.message == ""
        assert "exectunnel.error" in str(err)

    def test_message_is_part_of_str(self) -> None:
        err = ExecTunnelError("boom")
        s = str(err)
        assert "boom" in s
        assert "[exectunnel.error]" in s

    def test_repr_includes_class_and_error_id(self) -> None:
        err = ExecTunnelError("boom")
        r = repr(err)
        assert "ExecTunnelError" in r
        assert "error_id" in r
        assert err.error_id in r

    def test_default_retryable_is_false(self) -> None:
        err = ExecTunnelError()
        assert err.retryable is False

    def test_explicit_retryable_overrides_default(self) -> None:
        err = ExecTunnelError(retryable=True)
        assert err.retryable is True

    def test_explicit_error_code_overrides_default(self) -> None:
        err = ExecTunnelError(error_code="custom.thing")
        assert err.error_code == "custom.thing"

    def test_details_default_is_empty_dict(self) -> None:
        # Documented contract: details is always a dict, never None.
        assert ExecTunnelError().details == {}
        assert isinstance(ExecTunnelError().details, dict)

    def test_hint_default_is_none(self) -> None:
        assert ExecTunnelError().hint is None

    def test_hint_appears_in_str(self) -> None:
        err = ExecTunnelError("boom", hint="check the wire")
        s = str(err)
        assert "hint=" in s
        assert "check the wire" in s

    def test_details_appear_in_str(self) -> None:
        err = ExecTunnelError("boom", details={"k": "v"})
        s = str(err)
        assert "details=" in s
        assert "k=" in s and "'v'" in s

    def test_error_id_is_uuid4_shaped(self) -> None:
        err = ExecTunnelError()
        # 36-char canonical UUID with 4 dashes.
        assert len(err.error_id) == 36
        assert err.error_id.count("-") == 4

    def test_timestamp_is_iso8601_utc(self) -> None:
        err = ExecTunnelError()
        # Should round-trip through fromisoformat without raising.
        parsed = _dt.datetime.fromisoformat(err.timestamp)
        assert parsed.tzinfo is not None  # UTC is set


# ── to_dict / from_dict round-trip ────────────────────────────────────────────


class TestSerialisation:
    """``to_dict`` / ``from_dict`` is the cross-process error contract."""

    def test_to_dict_keys(self) -> None:
        err = ExecTunnelError(
            "boom",
            error_code="x.y",
            details={"a": 1},
            hint="try X",
            retryable=True,
        )
        d = err.to_dict()
        assert {
            "error_id",
            "timestamp",
            "type",
            "error_code",
            "message",
            "retryable",
            "hint",
            "details",
            "cause",
            "traceback",
        } <= d.keys()
        assert d["type"] == "ExecTunnelError"
        assert d["error_code"] == "x.y"
        assert d["details"] == {"a": 1}
        assert d["retryable"] is True

    def test_from_dict_round_trip_preserves_identity_fields(self) -> None:
        err = ExecTunnelError("boom", details={"a": 1})
        d = err.to_dict()
        clone = ExecTunnelError.from_dict(d)
        assert clone.message == err.message
        assert clone.error_code == err.error_code
        assert clone.details == err.details
        # Identity fields must round-trip so log correlation works.
        assert clone.error_id == err.error_id
        assert clone.timestamp == err.timestamp

    def test_to_dict_records_cause(self) -> None:
        try:
            try:
                raise ValueError("root cause")
            except ValueError as e:
                raise ExecTunnelError("wrapped") from e
        except ExecTunnelError as outer:
            d = outer.to_dict()
            assert d["cause"] is not None
            assert "ValueError" in d["cause"]

    def test_to_dict_traceback_only_present_during_active_exception(self) -> None:
        # Outside an except: block, traceback is None.
        err = ExecTunnelError("boom")
        assert err.to_dict()["traceback"] is None


# ── Hierarchy & error-code matrix ─────────────────────────────────────────────


# (subclass, expected default error_code, expected retryable)
_HIERARCHY: list[tuple[type[ExecTunnelError], str, bool]] = [
    (ConfigurationError, "config.invalid", False),
    (BootstrapError, "bootstrap.failed", True),
    (AgentReadyTimeoutError, "bootstrap.agent_ready_timeout", True),
    (AgentSyntaxError, "bootstrap.agent_syntax_error", False),
    (AgentVersionMismatchError, "bootstrap.version_mismatch", False),
    (TransportError, "transport.error", True),
    (WebSocketSendTimeoutError, "transport.ws_send_timeout", True),
    (ConnectionClosedError, "transport.connection_closed", True),
    (ReconnectExhaustedError, "transport.reconnect_exhausted", False),
    (ProtocolError, "protocol.error", False),
    (UnexpectedFrameError, "protocol.unexpected_frame", False),
    (FrameDecodingError, "protocol.frame_decoding_error", False),
    (ExecutionError, "execution.error", False),
    (RemoteProcessError, "execution.remote_process_error", False),
    (ExecutionTimeoutError, "execution.timeout", True),
    (AuthenticationError, "auth.authentication_failed", False),
    (AuthorizationError, "auth.authorization_failed", False),
]


@pytest.mark.parametrize(
    "cls,code,retry", _HIERARCHY, ids=lambda v: getattr(v, "__name__", str(v))
)
class TestHierarchyMatrix:
    """One parametrised pin per public exception class."""

    def test_default_error_code(self, cls, code, retry) -> None:
        assert cls.default_error_code == code

    def test_default_retryable(self, cls, code, retry) -> None:
        assert cls.default_retryable is retry

    def test_inherits_exec_tunnel_error(self, cls, code, retry) -> None:
        assert issubclass(cls, ExecTunnelError)


# ── Subclass-of relations ─────────────────────────────────────────────────────


class TestSubclassRelations:
    """Pin "X is also a Y" relations callers rely on for ``except`` clauses."""

    def test_bootstrap_subtree(self) -> None:
        assert issubclass(AgentReadyTimeoutError, BootstrapError)
        assert issubclass(AgentSyntaxError, BootstrapError)
        assert issubclass(AgentVersionMismatchError, BootstrapError)

    def test_transport_subtree(self) -> None:
        assert issubclass(WebSocketSendTimeoutError, TransportError)
        assert issubclass(ConnectionClosedError, TransportError)
        assert issubclass(ReconnectExhaustedError, TransportError)

    def test_protocol_subtree(self) -> None:
        assert issubclass(UnexpectedFrameError, ProtocolError)
        assert issubclass(FrameDecodingError, ProtocolError)

    def test_execution_subtree(self) -> None:
        assert issubclass(RemoteProcessError, ExecutionError)
        assert issubclass(ExecutionTimeoutError, ExecutionError)


# ── Backwards-compatible aliases ──────────────────────────────────────────────


class TestAliases:
    """The historical public names must still work — they leak into user code."""

    def test_config_error_is_configuration_error(self) -> None:
        assert ConfigError is ConfigurationError

    def test_websocket_send_timeout_is_websocket_send_timeout_error(self) -> None:
        assert WebSocketSendTimeout is WebSocketSendTimeoutError

    def test_agent_timeout_error_is_agent_ready_timeout_error(self) -> None:
        assert AgentTimeoutError is AgentReadyTimeoutError


# ── __all__ surface guarantees public names cannot quietly disappear ──────────


class TestAllSurface:
    def test_all_is_a_list(self) -> None:
        assert isinstance(exc_mod.__all__, list)

    def test_all_entries_are_attributes(self) -> None:
        missing = [n for n in exc_mod.__all__ if not hasattr(exc_mod, n)]
        assert missing == []

    def test_known_names_in_all(self) -> None:
        for name in [
            "ExecTunnelError",
            "ConfigurationError",
            "TransportError",
            "ProtocolError",
            "ExecutionError",
            "AuthenticationError",
        ]:
            assert name in exc_mod.__all__
