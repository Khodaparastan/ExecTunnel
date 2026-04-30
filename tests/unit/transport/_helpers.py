"""Shared constants, mocks, and stream builders for the transport test suite."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

TCP_CONN_ID = "c" + "a" * 24
UDP_FLOW_ID = "u" + "b" * 24


class MockWsSend:
    """Records every frame sent and supports per-call side-effect injection."""

    def __init__(self) -> None:
        self.frames: list[str] = []
        self.side_effect: Exception | None = None

    async def __call__(
        self, frame: str, *, must_queue: bool = False, control: bool = False
    ) -> None:
        if self.side_effect is not None:
            raise self.side_effect
        self.frames.append(frame)

    def frames_of_type(self, msg_type: str) -> list[str]:
        """Return all recorded frames whose type field matches ``msg_type``."""
        return [f for f in self.frames if f":{msg_type}:" in f]

    def has_frame_type(self, msg_type: str) -> bool:
        return bool(self.frames_of_type(msg_type))


def make_reader(*chunks: bytes, eof: bool = True) -> asyncio.StreamReader:
    """Return a ``StreamReader`` pre-loaded with *chunks* and an optional EOF."""
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    if eof:
        reader.feed_eof()
    return reader


def make_mock_writer() -> MagicMock:
    """Return a ``MagicMock`` shaped like ``asyncio.StreamWriter``."""
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    writer.can_write_eof = MagicMock(return_value=True)
    writer.write_eof = MagicMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    return writer
