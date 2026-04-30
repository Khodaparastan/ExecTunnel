"""Fixtures for the ``exectunnel.transport`` test suite."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ._helpers import (
    TCP_CONN_ID,
    UDP_FLOW_ID,
    MockWsSend,
    make_mock_writer,
    make_reader,
)


@pytest.fixture
def ws_send() -> MockWsSend:
    return MockWsSend()


@pytest.fixture
def tcp_writer() -> MagicMock:
    return make_mock_writer()


@pytest.fixture
def tcp_registry() -> dict:
    return {}


@pytest.fixture
def udp_registry() -> dict:
    return {}


def _make_eof_reader() -> MagicMock:
    """Return a mock StreamReader that immediately yields EOF."""
    reader = MagicMock(spec=asyncio.StreamReader)
    reader.read = AsyncMock(return_value=b"")
    return reader


@pytest.fixture
def make_tcp_conn(ws_send, tcp_writer, tcp_registry):
    """Factory that wires a ``TcpConnection`` to the shared test fixtures."""
    from exectunnel.defaults import Defaults
    from exectunnel.transport import TcpConnection

    def _factory(
        conn_id: str = TCP_CONN_ID,
        reader: asyncio.StreamReader | MagicMock | None = None,
        *,
        pre_ack_buffer_cap_bytes: int = Defaults.PRE_ACK_BUFFER_CAP_BYTES,
    ) -> TcpConnection:
        r = reader if reader is not None else _make_eof_reader()
        conn = TcpConnection(
            conn_id,
            r,
            tcp_writer,
            ws_send,
            tcp_registry,
            pre_ack_buffer_cap_bytes=pre_ack_buffer_cap_bytes,
        )
        tcp_registry[conn_id] = conn
        return conn

    return _factory


@pytest.fixture
def make_udp_flow(ws_send, udp_registry):
    """Factory that wires a ``UdpFlow`` to the shared test fixtures."""
    from exectunnel.transport import UdpFlow

    def _factory(
        flow_id: str = UDP_FLOW_ID,
        host: str = "127.0.0.1",
        port: int = 53,
    ) -> UdpFlow:
        flow = UdpFlow(flow_id, host, port, ws_send, udp_registry)
        udp_registry[flow_id] = flow
        return flow

    return _factory


@pytest.fixture(autouse=True)
def patch_observability():
    """Suppress all observability side effects for every transport test."""

    @asynccontextmanager
    async def _noop_aspan(_name: str):
        yield

    with (
        patch("exectunnel.transport.tcp.metrics_inc"),
        patch("exectunnel.transport.tcp.metrics_gauge_dec"),
        patch("exectunnel.transport.tcp.metrics_observe"),
        patch("exectunnel.transport.tcp.aspan", _noop_aspan),
        patch("exectunnel.transport.udp.metrics_inc"),
        patch("exectunnel.transport.udp.metrics_gauge_dec"),
    ):
        yield
