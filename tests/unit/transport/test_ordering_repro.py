import asyncio

import pytest
from exectunnel.transport import TcpConnection, UdpFlow

from ._helpers import TCP_CONN_ID, UDP_FLOW_ID, make_mock_writer, make_reader


class MockWsSendWithFlags:
    def __init__(self):
        self.calls = []

    async def __call__(
        self, frame: str, *, must_queue: bool = False, control: bool = False
    ):
        self.calls.append((frame, must_queue, control))


@pytest.mark.asyncio
async def test_tcp_close_ordering_repro():
    ws_send = MockWsSendWithFlags()
    registry = {}
    writer = make_mock_writer()
    reader = make_reader()

    conn = TcpConnection(TCP_CONN_ID, reader, writer, ws_send, registry)
    await conn._send_close_frame_once()

    # NEW BEHAVIOR: sends with control=False, must_queue=True
    frame, must_queue, control = ws_send.calls[0]
    assert "CONN_CLOSE" in frame
    assert control is False
    assert must_queue is True


@pytest.mark.asyncio
async def test_udp_close_ordering_repro():
    ws_send = MockWsSendWithFlags()
    registry = {}

    flow = UdpFlow(UDP_FLOW_ID, "127.0.0.1", 53, ws_send, registry)
    await flow.open()
    await flow.close()

    # Find UDP_CLOSE call
    close_call = next(c for c in ws_send.calls if "UDP_CLOSE" in c[0])
    frame, must_queue, control = close_call
    assert control is False
    assert must_queue is True
