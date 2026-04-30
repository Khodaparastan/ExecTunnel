from __future__ import annotations

import asyncio

import pytest
from exectunnel.session._config import SessionConfig
from exectunnel.session._sender import WsSender


class DummyWs:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, frame: str) -> None:
        self.sent.append(frame)


@pytest.mark.asyncio
async def test_ws_sender_prioritizes_control_frames() -> None:
    ws = DummyWs()
    closed = asyncio.Event()
    cfg = SessionConfig(
        wss_url="ws://example",
        send_queue_cap=8,
        send_timeout=1.0,
    )
    sender = WsSender(ws, cfg, closed)

    await sender.send("data-1\n")
    await sender.send("ctrl-1\n", control=True)
    await sender.send("data-2\n")

    sender.start()
    await asyncio.sleep(0.05)
    await sender.stop()

    assert ws.sent[0] == "ctrl-1\n"


@pytest.mark.asyncio
async def test_ws_sender_must_queue_raises_when_closed() -> None:
    ws = DummyWs()
    closed = asyncio.Event()
    closed.set()
    cfg = SessionConfig(
        wss_url="ws://example",
        send_queue_cap=1,
        send_timeout=1.0,
    )
    sender = WsSender(ws, cfg, closed)

    with pytest.raises(Exception):
        await sender.send("frame\n", must_queue=True)


@pytest.mark.asyncio
async def test_ws_sender_stop_is_idempotent() -> None:
    ws = DummyWs()
    closed = asyncio.Event()
    cfg = SessionConfig(
        wss_url="ws://example",
        send_queue_cap=4,
        send_timeout=1.0,
    )
    sender = WsSender(ws, cfg, closed)
    sender.start()

    await sender.stop()
    await sender.stop()

    assert True
