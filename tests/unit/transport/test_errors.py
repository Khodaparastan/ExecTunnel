"""Unit tests for :mod:`exectunnel.transport._errors`.

``log_task_exception`` is a `match`-style dispatch that maps each
exception class to:

* a metric counter label (``tcp.connection.{direction}.error``);
* a log record at the appropriate level (``warning`` for tunnel /
  library errors, ``debug`` for OS errors);
* a structured ``extra`` payload including ``conn_id``, ``direction``,
  ``bytes_*``, and (for ``ExecTunnelError`` subclasses)
  ``error_code`` / ``error_id``.

These tests pin every match arm.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from exectunnel.exceptions import (
    ConnectionClosedError,
    ExecTunnelError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.transport._errors import log_task_exception

CONN_ID = "c" + "a" * 24


@pytest.fixture
def patched_metrics_inc():
    """Replace the module-level ``metrics_inc`` reference with a mock."""
    with patch("exectunnel.transport._errors.metrics_inc") as m:
        yield m


# ── ws_send_timeout ───────────────────────────────────────────────────────────


class TestWebSocketSendTimeoutBranch:
    def test_metric_label(self, patched_metrics_inc, caplog) -> None:
        caplog.set_level(logging.WARNING, logger="exectunnel.transport.tcp")
        exc = WebSocketSendTimeoutError("send stalled")
        log_task_exception(CONN_ID, "upstream", exc, bytes_transferred=128)
        patched_metrics_inc.assert_called_once_with(
            "tcp.connection.upstream.error", error="ws_send_timeout"
        )

    def test_log_record_carries_error_id_and_code(
        self, patched_metrics_inc, caplog
    ) -> None:
        caplog.set_level(logging.WARNING, logger="exectunnel.transport.tcp")
        exc = WebSocketSendTimeoutError("send stalled")
        log_task_exception(CONN_ID, "downstream", exc, bytes_transferred=64)
        rec = caplog.records[0]
        # ``extra={"error_code": ..., "error_id": ...}`` flat-splats onto
        # the LogRecord.
        assert getattr(rec, "error_code") == exc.error_code
        assert getattr(rec, "error_id") == exc.error_id
        assert getattr(rec, "conn_id") == CONN_ID
        assert getattr(rec, "direction") == "downstream"
        assert getattr(rec, "bytes_recv") == 64


# ── connection_closed ─────────────────────────────────────────────────────────


class TestConnectionClosedBranch:
    def test_metric_label(self, patched_metrics_inc, caplog) -> None:
        caplog.set_level(logging.WARNING, logger="exectunnel.transport.tcp")
        log_task_exception(CONN_ID, "upstream", ConnectionClosedError("ws closed"), 0)
        patched_metrics_inc.assert_called_once_with(
            "tcp.connection.upstream.error", error="connection_closed"
        )


# ── transport_error fallback ──────────────────────────────────────────────────


class TestTransportErrorBranch:
    def test_metric_label_uses_error_code(self, patched_metrics_inc, caplog) -> None:
        caplog.set_level(logging.WARNING, logger="exectunnel.transport.tcp")
        # A generic TransportError that isn't ws-timeout / conn-closed.
        exc = TransportError("custom", error_code="transport.custom_error")
        log_task_exception(CONN_ID, "downstream", exc, 0)
        patched_metrics_inc.assert_called_once_with(
            "tcp.connection.downstream.error", error="transport_custom_error"
        )


# ── os_error ──────────────────────────────────────────────────────────────────


class TestOsErrorBranch:
    def test_metric_label(self, patched_metrics_inc) -> None:
        log_task_exception(CONN_ID, "upstream", OSError("ECONNRESET"), 0)
        patched_metrics_inc.assert_called_once_with(
            "tcp.connection.upstream.error", error="os_error"
        )

    def test_logged_at_debug_level(self, patched_metrics_inc, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="exectunnel.transport.tcp")
        log_task_exception(CONN_ID, "upstream", OSError("EPIPE"), 0)
        # The OS-error branch logs at DEBUG, not WARNING.
        assert any(r.levelno == logging.DEBUG for r in caplog.records), [
            r.levelno for r in caplog.records
        ]


# ── library-level ExecTunnelError ─────────────────────────────────────────────


class TestExecTunnelErrorBranch:
    def test_metric_label(self, patched_metrics_inc, caplog) -> None:
        caplog.set_level(logging.WARNING, logger="exectunnel.transport.tcp")
        exc = ExecTunnelError("misc", error_code="something.weird")
        log_task_exception(CONN_ID, "upstream", exc, 0)
        patched_metrics_inc.assert_called_once_with(
            "tcp.connection.upstream.error", error="something_weird"
        )


# ── catch-all unexpected ──────────────────────────────────────────────────────


class TestUnexpectedBranch:
    def test_metric_label_uses_class_name(self, patched_metrics_inc, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="exectunnel.transport.tcp")
        log_task_exception(CONN_ID, "downstream", ValueError("nope"), 0)
        patched_metrics_inc.assert_called_once_with(
            "tcp.connection.downstream.error", error="ValueError"
        )


# ── byte-key contract ─────────────────────────────────────────────────────────


class TestByteKeyByDirection:
    @pytest.mark.parametrize(
        "direction,key",
        [("upstream", "bytes_sent"), ("downstream", "bytes_recv")],
    )
    def test_byte_key_chosen_by_direction(
        self, patched_metrics_inc, caplog, direction, key
    ) -> None:
        caplog.set_level(logging.WARNING, logger="exectunnel.transport.tcp")
        exc = WebSocketSendTimeoutError("x")
        log_task_exception(CONN_ID, direction, exc, bytes_transferred=99)
        rec = caplog.records[0]
        assert getattr(rec, key) == 99
