"""Unit tests for :mod:`exectunnel.session._state`."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
from exectunnel.session._state import AckStatus, PendingConnect

# ── AckStatus enum ────────────────────────────────────────────────────────────


class TestAckStatusEnum:
    """Pin the StrEnum values — they leak into metric labels and dashboards."""

    @pytest.mark.parametrize(
        "member,value",
        [
            (AckStatus.OK, "ok"),
            (AckStatus.TIMEOUT, "timeout"),
            (AckStatus.WS_CLOSED, "ws_closed"),
            (AckStatus.AGENT_ERROR, "agent_error"),
            (AckStatus.AGENT_CLOSED, "agent_closed"),
            (AckStatus.WS_SEND_TIMEOUT, "ws_send_timeout"),
            (AckStatus.PRE_ACK_OVERFLOW, "pre_ack_overflow"),
            (AckStatus.LIBRARY_ERROR, "library_error"),
            (AckStatus.UNEXPECTED_ERROR, "unexpected_error"),
        ],
    )
    def test_member_value(self, member: AckStatus, value: str) -> None:
        assert member.value == value
        # StrEnum: equality with the underlying string is intentional.
        assert member == value

    def test_str_enum_inherits_from_str(self) -> None:
        from enum import StrEnum

        assert issubclass(AckStatus, StrEnum)

    def test_no_unexpected_members(self) -> None:
        # Pin the closed set so accidental additions surface in code review.
        members = {m.name for m in AckStatus}
        assert members == {
            "OK",
            "TIMEOUT",
            "WS_CLOSED",
            "AGENT_ERROR",
            "AGENT_CLOSED",
            "WS_SEND_TIMEOUT",
            "PRE_ACK_OVERFLOW",
            "LIBRARY_ERROR",
            "UNEXPECTED_ERROR",
        }


# ── PendingConnect dataclass ──────────────────────────────────────────────────


class TestPendingConnect:
    """Frozen + slots dataclass carrying host/port + ack future."""

    async def test_construction(self) -> None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[AckStatus] = loop.create_future()
        pc = PendingConnect(host="example.com", port=80, ack_future=fut)
        assert pc.host == "example.com"
        assert pc.port == 80
        assert pc.ack_future is fut

    async def test_is_frozen(self) -> None:
        loop = asyncio.get_running_loop()
        pc = PendingConnect(
            host="example.com", port=80, ack_future=loop.create_future()
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            pc.host = "evil.com"  # type: ignore[misc]

    def test_uses_slots(self) -> None:
        assert PendingConnect.__dataclass_params__.slots is True

    async def test_ack_future_round_trip(self) -> None:
        # Resolving the future via the dataclass reference must propagate
        # to anyone awaiting the same future object.
        loop = asyncio.get_running_loop()
        pc = PendingConnect(host="h", port=1, ack_future=loop.create_future())
        pc.ack_future.set_result(AckStatus.OK)
        result = await pc.ack_future
        assert result is AckStatus.OK
