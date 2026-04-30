"""Shared fixtures for the exectunnel.proxy test suite.

Required pyproject.toml configuration::

    [tool.pytest.ini_options]
    asyncio_mode = "auto"
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import exectunnel.proxy.server
import exectunnel.proxy.tcp_relay
import exectunnel.proxy.udp_relay
import pytest


@asynccontextmanager
async def _noop_aspan(*args: object, **kwargs: object):
    yield None


@pytest.fixture(autouse=True)
def mock_observability():
    """Replace every observability call in the proxy package with no-ops."""
    with (
        patch("exectunnel.proxy.server.metrics_inc"),
        patch("exectunnel.proxy.server.metrics_gauge_inc"),
        patch("exectunnel.proxy.server.metrics_gauge_dec"),
        patch("exectunnel.proxy.server.metrics_observe"),
        patch("exectunnel.proxy.server.start_trace"),
        patch("exectunnel.proxy.server.aspan", _noop_aspan),
        patch("exectunnel.proxy.udp_relay.metrics_inc"),
        patch("exectunnel.proxy.udp_relay.metrics_gauge_inc"),
        patch("exectunnel.proxy.udp_relay.metrics_gauge_dec"),
        patch("exectunnel.proxy.udp_relay.metrics_observe"),
        patch("exectunnel.proxy.tcp_relay.metrics_inc"),
    ):
        yield


@pytest.fixture
def mock_writer() -> MagicMock:
    """Mock ``asyncio.StreamWriter`` with all methods stubbed."""
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    writer.is_closing.return_value = False
    return writer


def make_stream_reader(data: bytes) -> asyncio.StreamReader:
    """Return an ``asyncio.StreamReader`` pre-loaded with *data* and EOF."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def free_port() -> int:
    """Return an available TCP port on ``127.0.0.1``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]
