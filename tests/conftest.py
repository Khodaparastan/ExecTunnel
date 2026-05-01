"""Project-wide pytest fixtures.

This conftest is intentionally thin: it auto-resets the observability
state between tests, exposes a few generic factory fixtures, and
re-exports the most-used helpers from :mod:`tests._helpers`. Domain-
specific fixtures (``mock_observability``, ``make_tcp_conn``,
``make_udp_flow``, etc.) live in the per-package conftests.

Adding a new fixture? See :doc:`tests/README.md`'s "Where does this
belong?" section.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest
from exectunnel.observability import metrics_reset

from tests._helpers import (
    DummyDatagramTransport as _DummyDatagramTransport,
)
from tests._helpers import (
    DummyWriter as _DummyWriter,
)
from tests._helpers import (
    make_stream_reader as _make_stream_reader,
)

# Public re-export — older tests imported these names directly from
# ``tests.conftest`` (and a couple still might).  Re-exporting from
# the helpers package keeps backwards compatibility.
DummyWriter = _DummyWriter
DummyDatagramTransport = _DummyDatagramTransport


# ── Autouse: keep observability counters isolated per test ───────────────────


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    """Reset every metric / counter / gauge before each test.

    Without this fixture, counters incremented in one test would leak
    into the next, making any "exactly N events emitted" assertion
    order-dependent.  Autouse keeps the contract invisible to test
    authors.
    """
    metrics_reset()


# ── Generic fixtures (light, side-effect free) ────────────────────────────────


@pytest.fixture
def ws_send_mock() -> AsyncMock:
    """An :class:`AsyncMock` shaped like ``ws_send``.

    Suitable for tests that only need to record call args.  Tests that
    need framing-aware introspection should use
    ``tests._helpers.MockWsSend`` instead.
    """
    return AsyncMock()


@pytest.fixture
def dummy_writer() -> DummyWriter:
    """Return a fresh :class:`tests._helpers.DummyWriter`."""
    return DummyWriter()


@pytest.fixture
def dummy_datagram_transport() -> DummyDatagramTransport:
    """Return a fresh :class:`tests._helpers.DummyDatagramTransport`."""
    return DummyDatagramTransport()


@pytest.fixture
def make_stream_reader() -> Callable[..., asyncio.StreamReader]:
    """Factory fixture re-exporting :func:`tests._helpers.make_stream_reader`.

    The factory accepts the same ``data: bytes | Iterable[bytes]`` and
    keyword-only ``eof: bool`` arguments as the underlying helper, but
    is exposed as a fixture so tests can keep the ``def test(...,
    make_stream_reader): ...`` argument-injection idiom.
    """
    return _make_stream_reader
