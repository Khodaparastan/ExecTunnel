"""WebSocket doubles for unit and in-memory integration tests.

Two doubles live here:

* :class:`MockWsSend` — a callable that records every frame sent and
  supports per-call exception injection. Suitable for unit tests of
  components that *only* call ``ws_send`` (transport, sender,
  dispatcher slow paths).

* :class:`QueueWs` — a fuller bidirectional in-memory WebSocket that
  speaks both ``send`` and ``recv``-shaped APIs through asyncio queues.
  Used by the in-memory ``Session`` smoke suite under
  :mod:`tests.integration.test_session_inmemory`.
"""

from __future__ import annotations

import asyncio
from typing import Final


# ── Recording-only ws_send double ────────────────────────────────────────────


class MockWsSend:
    """Records every frame sent and supports per-call side-effect injection.

    The callable signature mirrors what the transport / dispatcher
    layer expects (``frame: str, *, must_queue: bool, control: bool``).
    Setting :attr:`side_effect` to an exception will cause the *next*
    call to raise that exception instead of recording the frame; this
    is one-shot — clear it manually after raising.
    """

    __slots__ = ("frames", "side_effect")

    def __init__(self) -> None:
        self.frames: list[str] = []
        self.side_effect: BaseException | None = None

    async def __call__(
        self,
        frame: str,
        *,
        must_queue: bool = False,
        control: bool = False,
    ) -> None:
        if self.side_effect is not None:
            raise self.side_effect
        self.frames.append(frame)

    def frames_of_type(self, msg_type: str) -> list[str]:
        """Return every recorded frame whose type field matches *msg_type*."""
        token = f":{msg_type}:"
        return [f for f in self.frames if token in f]

    def has_frame_type(self, msg_type: str) -> bool:
        """Whether any recorded frame matches *msg_type*."""
        return bool(self.frames_of_type(msg_type))

    def clear(self) -> None:
        """Reset the recorded frame list and any pending ``side_effect``."""
        self.frames.clear()
        self.side_effect = None


# ── Bidirectional in-memory websocket double ─────────────────────────────────


class QueueWs:
    """Asyncio-queue-backed websocket double for full Session smoke tests.

    Models a tiny subset of the :class:`websockets.WebSocketClientProtocol`
    surface:

    * :meth:`send` — what the session-side sender writes to "the wire".
    * :meth:`recv` — what the session-side receiver reads.
    * :meth:`close` — cooperative termination, surfaces as
      :class:`websockets.exceptions.ConnectionClosed`-equivalent on the
      next ``recv``.

    The "remote" half — the agent we are pretending to be — interacts
    via :meth:`feed_to_session` (push a frame the session will read)
    and :meth:`pop_from_session` (consume a frame the session has
    sent).  This split keeps test direction obvious.
    """

    _CLOSED_SENTINEL: Final[object] = object()

    def __init__(self, *, send_queue_max: int = 1024) -> None:
        # Frames flowing session → "wire".
        self._sent_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=send_queue_max)
        # Frames flowing "wire" → session.
        self._recv_queue: asyncio.Queue[str | object] = asyncio.Queue()
        self._closed = False

    # ── Session-facing API ──────────────────────────────────────────────────

    async def send(self, frame: str) -> None:
        """Receive a frame from the session-side sender."""
        if self._closed:
            raise ConnectionResetError("QueueWs closed")
        await self._sent_queue.put(frame)

    async def recv(self) -> str:
        """Deliver the next frame to the session-side receiver."""
        item = await self._recv_queue.get()
        if item is self._CLOSED_SENTINEL:
            # Re-arm so subsequent recv calls also see closure.
            self._recv_queue.put_nowait(self._CLOSED_SENTINEL)
            raise ConnectionResetError("QueueWs closed")
        return item  # type: ignore[return-value]

    # ── Async iterator (matches ``websockets`` client API) ──────────────────

    def __aiter__(self) -> "QueueWs":
        """Return self so ``async for msg in ws:`` works.

        The :class:`exectunnel.session._receiver.FrameReceiver` consumes
        the websocket via the ``async for msg in self._ws`` idiom.  We
        mirror that surface here so tests can drive the receiver with
        :meth:`feed_to_session` and observe normal close-on-EOF
        semantics: when :meth:`close` has been called and the queue is
        drained, ``__anext__`` raises :class:`StopAsyncIteration`
        (matching ``websockets``' behaviour on a graceful close).
        """
        return self

    async def __anext__(self) -> str:
        """Return the next inbound frame or end the iteration.

        Blocks until a frame is fed via :meth:`feed_to_session` *or*
        :meth:`close` is invoked.  After ``close``, the queue's closed
        sentinel triggers :class:`StopAsyncIteration` so the consuming
        ``async for`` loop terminates cleanly.
        """
        item = await self._recv_queue.get()
        if item is self._CLOSED_SENTINEL:
            # Re-arm for any other consumers waiting on closure.
            self._recv_queue.put_nowait(self._CLOSED_SENTINEL)
            raise StopAsyncIteration
        return item  # type: ignore[return-value]

    async def close(self) -> None:
        """Cooperatively close — pending and future ``recv``s see closure."""
        if self._closed:
            return
        self._closed = True
        await self._recv_queue.put(self._CLOSED_SENTINEL)

    # ── Test-side API ───────────────────────────────────────────────────────

    def feed_to_session(self, frame: str) -> None:
        """Push *frame* so the session-side ``recv`` will return it next."""
        self._recv_queue.put_nowait(frame)

    async def pop_from_session(self, *, timeout: float = 1.0) -> str:
        """Pop the next frame the session-side ``send`` enqueued."""
        return await asyncio.wait_for(self._sent_queue.get(), timeout=timeout)

    @property
    def closed(self) -> bool:
        return self._closed
