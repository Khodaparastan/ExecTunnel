"""Tests for ``exectunnel.transport._validation``."""

from __future__ import annotations

import pytest
from exectunnel.exceptions import TransportError
from exectunnel.transport._validation import require_bytes


class TestRequireBytes:
    def test_returns_bytes_object_unchanged(self):
        data = b"payload"
        assert require_bytes(data, "h1", "feed") is data

    def test_accepts_empty_bytes(self):
        assert require_bytes(b"", "h1", "feed") == b""

    @pytest.mark.parametrize(
        ("bad_value", "type_name"),
        [
            ("string", "str"),
            (bytearray(b"ba"), "bytearray"),
            (memoryview(b"mv"), "memoryview"),
            (42, "int"),
            (3.14, "float"),
            (None, "NoneType"),
            ([], "list"),
        ],
    )
    def test_raises_transport_error_for_non_bytes(self, bad_value, type_name):
        with pytest.raises(TransportError) as exc_info:
            require_bytes(bad_value, "conn-1", "feed")
        exc = exc_info.value
        assert exc.error_code == "transport.invalid_payload_type"
        assert exc.details["received_type"] == type_name

    def test_details_include_handler_id(self):
        with pytest.raises(TransportError) as exc_info:
            require_bytes("bad", "my-conn", "feed")
        assert exc_info.value.details["handler_id"] == "my-conn"

    def test_details_include_method(self):
        with pytest.raises(TransportError) as exc_info:
            require_bytes("bad", "my-conn", "send_datagram")
        assert exc_info.value.details["method"] == "send_datagram"

    def test_error_message_includes_handler_and_method(self):
        with pytest.raises(TransportError) as exc_info:
            require_bytes(99, "flow-xyz", "recv")
        assert "flow-xyz" in str(exc_info.value)
        assert "recv" in str(exc_info.value)
