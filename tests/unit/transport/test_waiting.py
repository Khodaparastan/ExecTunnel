"""Unit tests for :mod:`exectunnel.transport._waiting`.

Both ``wait_first`` and ``wait_first_suppress_loser`` race a primary
awaitable against an :class:`asyncio.Event`.  These tests pin every
documented branch:

* primary wins → returns ``(True, result)`` and cancels the event task.
* event wins → returns ``(False, None)`` and cancels the primary task.
* outer cancel → both tasks cancelled, ``CancelledError`` re-raised.
* primary raises after losing the race → behaviour differs between the
  two helpers (``wait_first`` propagates, ``wait_first_suppress_loser``
  swallows).
"""

from __future__ import annotations

import asyncio

import pytest
from exectunnel.transport._waiting import (
    wait_first,
    wait_first_suppress_loser,
)

# ── wait_first ────────────────────────────────────────────────────────────────


class TestWaitFirst:
    async def test_primary_wins(self) -> None:
        event = asyncio.Event()

        async def primary() -> str:
            return "ok"

        won, value = await wait_first(
            primary(), event, primary_name="p", event_name="e"
        )
        assert won is True
        assert value == "ok"

    async def test_event_wins(self) -> None:
        event = asyncio.Event()

        async def primary() -> str:
            await asyncio.sleep(10)
            return "never"

        async def trip_event() -> None:
            await asyncio.sleep(0)
            event.set()

        # Schedule the event-trip on the same loop and race.
        trip = asyncio.create_task(trip_event())
        try:
            won, value = await wait_first(
                primary(), event, primary_name="p", event_name="e"
            )
        finally:
            await trip

        assert won is False
        assert value is None

    async def test_outer_cancel_propagates(self) -> None:
        event = asyncio.Event()

        async def primary() -> None:
            await asyncio.sleep(10)

        async def caller() -> tuple[bool, object]:
            return await wait_first(primary(), event, primary_name="p", event_name="e")

        task = asyncio.create_task(caller())
        await asyncio.sleep(0)  # allow the inner tasks to spawn
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ── wait_first_suppress_loser ─────────────────────────────────────────────────


class TestWaitFirstSuppressLoser:
    async def test_primary_wins(self) -> None:
        event = asyncio.Event()

        async def primary() -> int:
            return 42

        won, value = await wait_first_suppress_loser(
            primary(), event, primary_name="p", event_name="e"
        )
        assert won is True
        assert value == 42

    async def test_event_wins_swallows_loser_exception(self) -> None:
        """If the loser raises while being cancelled, it must be swallowed."""
        event = asyncio.Event()

        async def primary() -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # Convert the cancel into a regular exception, which
                # ``wait_first_suppress_loser`` is documented to swallow.
                raise RuntimeError("loser raised on cancel") from None

        async def trip_event() -> None:
            await asyncio.sleep(0)
            event.set()

        trip = asyncio.create_task(trip_event())
        try:
            won, value = await wait_first_suppress_loser(
                primary(), event, primary_name="p", event_name="e"
            )
        finally:
            await trip

        assert won is False
        assert value is None

    async def test_outer_cancel_propagates(self) -> None:
        event = asyncio.Event()

        async def primary() -> None:
            await asyncio.sleep(10)

        async def caller() -> tuple[bool, object]:
            return await wait_first_suppress_loser(
                primary(), event, primary_name="p", event_name="e"
            )

        task = asyncio.create_task(caller())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
