from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest
from exectunnel.observability import metrics_reset


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    metrics_reset()


@pytest.fixture
def ws_send_mock() -> AsyncMock:
    return AsyncMock()


class DummyWriter:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False
        self.eof_written = False
        self._closing = False
        self.drain_calls = 0

    def write(self, data: bytes) -> None:
        if self._closing:
            raise OSError("writer closing")
        self.buffer.extend(data)

    async def drain(self) -> None:
        if self._closing:
            raise OSError("writer closing")
        self.drain_calls += 1

    def can_write_eof(self) -> bool:
        return True

    def write_eof(self) -> None:
        self.eof_written = True

    def close(self) -> None:
        self._closing = True
        self.closed = True

    async def wait_closed(self) -> None:
        return

    def is_closing(self) -> bool:
        return self._closing


@pytest.fixture
def dummy_writer() -> DummyWriter:
    return DummyWriter()


@pytest.fixture
def make_stream_reader() -> Callable[[bytes], asyncio.StreamReader]:
    def _make(data: bytes) -> asyncio.StreamReader:
        reader = asyncio.StreamReader()
        if data:
            reader.feed_data(data)
        reader.feed_eof()
        return reader

    return _make


class DummyDatagramTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((data, addr))

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


@pytest.fixture
def dummy_datagram_transport() -> DummyDatagramTransport:
    return DummyDatagramTransport()
