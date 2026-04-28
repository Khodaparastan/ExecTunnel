"""Small async utilities used by multiple transport handlers.

These helpers capture the "race an awaitable against a close event" idiom
that appears in both :meth:`TcpConnection.feed_async` and
:meth:`UdpFlow.recv_datagram`. The helper is deliberately minimal — code
paths with special reuse semantics (such as the long-lived ``close_task``
in :meth:`TcpConnection._downstream`) keep their inline expansions.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable

__all__ = ["wait_first", "wait_first_suppress_loser"]


async def wait_first[T](
    primary: Awaitable[T],
    event: asyncio.Event,
    *,
    primary_name: str,
    event_name: str,
) -> tuple[bool, T | None]:
    """Race *primary* against *event* and return which one finished first.

    Both tasks are spawned on the running loop. On :exc:`asyncio.CancelledError`
    both tasks are cancelled and their ``CancelledError`` is suppressed
    before the exception propagates.

    The loser is always cancelled cleanly, never leaked.

    Args:
        primary: The coroutine or awaitable to race. Its result is
            returned when it wins.
        event: The :class:`asyncio.Event` whose ``wait()`` is the
            competing side of the race.
        primary_name: Task name for *primary* (diagnostic only).
        event_name: Task name for the ``event.wait()`` task (diagnostic
            only).

    Returns:
        A ``(primary_won, value)`` tuple:

        * ``(True, result)`` — *primary* completed first.
        * ``(False, None)`` — *event* was set first; *primary* was
          cancelled cleanly.

    Raises:
        asyncio.CancelledError: Propagated unchanged after both inner
            tasks have been cancelled and awaited.
        Exception: Any exception raised by *primary* is propagated
            unchanged when *primary* loses the race but has already
            failed; this is rare but possible if *primary* raises
            synchronously during scheduling.
    """
    primary_task = asyncio.ensure_future(primary)
    primary_task.set_name(primary_name)
    event_task = asyncio.create_task(event.wait(), name=event_name)

    try:
        done, _ = await asyncio.wait(
            {primary_task, event_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        primary_task.cancel()
        event_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await primary_task
        with contextlib.suppress(asyncio.CancelledError):
            await event_task
        raise

    if primary_task in done and not primary_task.cancelled():
        event_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await event_task
        return True, primary_task.result()

    primary_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await primary_task
    return False, None


async def wait_first_suppress_loser[T](
    primary: Awaitable[T],
    event: asyncio.Event,
    *,
    primary_name: str,
    event_name: str,
) -> tuple[bool, T | None]:
    """Race *primary* against *event*, suppressing loser exceptions.

    Use this for benign queue-vs-close races where the losing awaitable is
    irrelevant once the close event wins.
    """
    primary_task = asyncio.ensure_future(primary)
    primary_task.set_name(primary_name)
    event_task = asyncio.create_task(event.wait(), name=event_name)

    try:
        done, _ = await asyncio.wait(
            {primary_task, event_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        primary_task.cancel()
        event_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await primary_task
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await event_task
        raise

    if primary_task in done and not primary_task.cancelled():
        event_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await event_task
        return True, primary_task.result()

    primary_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await primary_task
    return False, None
