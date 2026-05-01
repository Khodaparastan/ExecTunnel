"""Unit tests for :mod:`exectunnel.protocol.types`."""

from __future__ import annotations

import dataclasses

import pytest
from exectunnel.protocol import ParsedFrame


class TestParsedFrame:
    """``ParsedFrame`` is a frozen+slots dataclass — pin both."""

    def test_is_frozen_dataclass(self) -> None:
        params = ParsedFrame.__dataclass_params__
        assert params.frozen is True

    def test_uses_slots(self) -> None:
        assert ParsedFrame.__dataclass_params__.slots is True

    def test_basic_construction(self) -> None:
        frame = ParsedFrame(msg_type="DATA", conn_id="c" + "a" * 24, payload="x")
        assert frame.msg_type == "DATA"
        assert frame.payload == "x"

    def test_conn_id_can_be_none(self) -> None:
        # KEEPALIVE / AGENT_READY / LIVENESS — see NO_CONN_ID_TYPES.
        frame = ParsedFrame(msg_type="KEEPALIVE", conn_id=None, payload="")
        assert frame.conn_id is None

    def test_equality_value_based(self) -> None:
        a = ParsedFrame(msg_type="DATA", conn_id="cabc", payload="x")
        b = ParsedFrame(msg_type="DATA", conn_id="cabc", payload="x")
        assert a == b
        assert hash(a) == hash(b)  # frozen → hashable

    def test_immutable_after_construction(self) -> None:
        frame = ParsedFrame(msg_type="DATA", conn_id="cabc", payload="x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            frame.msg_type = "ERROR"  # type: ignore[misc]

    def test_repr_round_trip_friendly(self) -> None:
        frame = ParsedFrame(msg_type="DATA", conn_id="cabc", payload="hello")
        # Just sanity — every public field must show up in repr().
        r = repr(frame)
        assert "DATA" in r
        assert "cabc" in r
        assert "hello" in r
