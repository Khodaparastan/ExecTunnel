"""Tests for new transport-error classes added by the architectural pass.

These cover the public taxonomy contract of:

* :class:`exectunnel.exceptions.CtrlBackpressureError`
* :class:`exectunnel.exceptions.PreAckBufferOverflowError`
"""

from __future__ import annotations

from exectunnel.exceptions import (
    CtrlBackpressureError,
    ExecTunnelError,
    PreAckBufferOverflowError,
    TransportError,
)


class TestCtrlBackpressureError:
    """CtrlBackpressureError is a non-retryable TransportError subclass."""

    def test_subclass_of_transport_error(self):
        assert issubclass(CtrlBackpressureError, TransportError)
        assert issubclass(CtrlBackpressureError, ExecTunnelError)

    def test_default_error_code(self):
        err = CtrlBackpressureError()
        assert err.error_code == "transport.ctrl_backpressure"

    def test_not_retryable(self):
        err = CtrlBackpressureError()
        assert err.retryable is False

    def test_details_round_trip_through_to_dict(self):
        err = CtrlBackpressureError(
            "ctrl queue full",
            details={"control_queue_cap": 64, "caller": "connect"},
        )
        as_dict = err.to_dict()
        assert as_dict["error_code"] == "transport.ctrl_backpressure"
        assert as_dict["details"]["control_queue_cap"] == 64
        assert as_dict["details"]["caller"] == "connect"


class TestPreAckBufferOverflowError:
    """PreAckBufferOverflowError is a non-retryable TransportError subclass."""

    def test_subclass_of_transport_error(self):
        assert issubclass(PreAckBufferOverflowError, TransportError)
        assert issubclass(PreAckBufferOverflowError, ExecTunnelError)

    def test_default_error_code(self):
        err = PreAckBufferOverflowError()
        assert err.error_code == "transport.pre_ack_buffer_overflow"

    def test_not_retryable(self):
        err = PreAckBufferOverflowError()
        assert err.retryable is False

    def test_details_round_trip_through_to_dict(self):
        err = PreAckBufferOverflowError(
            "pre-ack overflow",
            details={
                "conn_id": "c" + "0" * 24,
                "buffer_cap": 65_536,
                "incoming_bytes": 8_192,
            },
        )
        as_dict = err.to_dict()
        assert as_dict["error_code"] == "transport.pre_ack_buffer_overflow"
        assert as_dict["details"]["conn_id"] == "c" + "0" * 24
        assert as_dict["details"]["buffer_cap"] == 65_536
