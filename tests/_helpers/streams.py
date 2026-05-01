"""Stream / writer / datagram-transport stubs reused across the test suite.

These deliberately use ``MagicMock`` only where the production code
checks ``spec=asyncio.StreamWriter`` etc.; pure data sinks (``DummyWriter``,
``DummyDatagramTransport``) are concrete classes so assertions can read
captured bytes directly without ``mock_calls`` introspection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from unittest.mock import AsyncMock, MagicMock

# ── StreamReader factories ────────────────────────────────────────────────────


def make_stream_reader(
    data: bytes | Iterable[bytes] = b"",
    *,
    eof: bool = True,
) -> asyncio.StreamReader:
    """Return a :class:`asyncio.StreamReader` pre-loaded with *data*.

    Args:
        data: Either a single ``bytes`` object or an iterable of
            ``bytes`` chunks. Each chunk is fed in order, mimicking
            multi-packet socket arrival timing.
        eof: When ``True`` (default), feed an EOF marker after the
            chunks so a consuming loop terminates naturally. Set to
            ``False`` for tests that exercise the "still open, no
            bytes available" path.

    Returns:
        A ready-to-read :class:`asyncio.StreamReader`.
    """
    reader = asyncio.StreamReader()
    if isinstance(data, (bytes, bytearray)):
        if data:
            reader.feed_data(bytes(data))
    else:
        for chunk in data:
            reader.feed_data(chunk)
    if eof:
        reader.feed_eof()
    return reader


def make_eof_reader() -> MagicMock:
    """Return a :class:`MagicMock` shaped like ``StreamReader`` that yields EOF.

    Cheaper than :func:`make_stream_reader` when the test does not care
    about reader internals — used by ``TcpConnection`` factory fixtures
    where the read loop is expected to short-circuit immediately.
    """
    reader = MagicMock(spec=asyncio.StreamReader)
    reader.read = AsyncMock(return_value=b"")
    return reader


# ── StreamWriter doubles ──────────────────────────────────────────────────────


def make_mock_writer() -> MagicMock:
    """Return a :class:`MagicMock` shaped like :class:`asyncio.StreamWriter`."""
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    writer.can_write_eof = MagicMock(return_value=True)
    writer.write_eof = MagicMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    writer.is_closing = MagicMock(return_value=False)
    return writer


class DummyWriter:
    """Concrete asyncio-StreamWriter-like sink that captures bytes.

    Tests that need to assert on written bytes can read
    :attr:`buffer` directly instead of poking ``writer.write.call_args``.
    Set :attr:`_closing` (via :meth:`close`) to simulate a writer that
    has begun teardown — subsequent ``write`` / ``drain`` calls raise
    :class:`OSError` to mirror real-world error surfaces.
    """

    __slots__ = (
        "buffer",
        "closed",
        "eof_written",
        "_closing",
        "drain_calls",
    )

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


# ── Datagram transport double ─────────────────────────────────────────────────


class DummyDatagramTransport:
    """Concrete ``DatagramTransport`` stand-in that captures sent datagrams.

    The captured tuples ``(payload, (host, port))`` are appended to
    :attr:`sent` in the order :meth:`sendto` was called; tests can
    assert ordering and content without mock introspection.
    """

    __slots__ = ("sent", "closed")

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((data, addr))

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed
