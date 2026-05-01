from __future__ import annotations

import asyncio

import pytest
from exectunnel.exceptions import CtrlBackpressureError
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


@pytest.mark.asyncio
async def test_ctrl_queue_full_raises_ctrl_backpressure_error_no_session_kill() -> None:
    ws = DummyWs()
    closed = asyncio.Event()
    cfg = SessionConfig(
        wss_url="ws://example",
        send_queue_cap=4,
        control_queue_cap=2,
        send_timeout=1.0,
    )
    sender = WsSender(ws, cfg, closed)

    # Do NOT start the sender so the ctrl queue cannot drain.
    await sender.send("ctrl-1\n", control=True)
    await sender.send("ctrl-2\n", control=True)

    with pytest.raises(CtrlBackpressureError) as exc_info:
        await sender.send("ctrl-3\n", control=True)

    err = exc_info.value
    assert err.retryable is False
    assert err.error_code == "transport.ctrl_backpressure"
    assert closed.is_set() is False, (
        "Ctrl-queue overflow must NOT declare the session unhealthy."
    )

    # Cleanup so the test does not leak the unstarted sender.
    closed.set()
    await sender.stop()


@pytest.mark.asyncio
async def test_ws_sender_weighted_interleaving_does_not_starve_data() -> None:
    ws = DummyWs()
    closed = asyncio.Event()
    cfg = SessionConfig(
        wss_url="ws://example",
        send_queue_cap=32,
        control_queue_cap=64,
        send_timeout=1.0,
        ctrl_burst_ratio=4,
    )
    sender = WsSender(ws, cfg, closed)

    for i in range(16):
        await sender.send(f"ctrl-{i}\n", control=True)
    for i in range(4):
        await sender.send(f"data-{i}\n")

    sender.start()
    await asyncio.sleep(0.1)
    await sender.stop()

    # All 20 frames must have been sent.
    assert len(ws.sent) == 20

    # The first data frame must arrive no later than position
    # ``ctrl_burst_ratio`` (i.e. after at most N ctrl frames).
    first_data_idx = next(i for i, f in enumerate(ws.sent) if f.startswith("data-"))
    assert first_data_idx <= cfg.ctrl_burst_ratio, (
        f"Data starved: first data at index {first_data_idx}, "
        f"expected <= {cfg.ctrl_burst_ratio}"
    )

    # FIFO within each queue must be preserved.
    ctrl_seq = [f for f in ws.sent if f.startswith("ctrl-")]
    data_seq = [f for f in ws.sent if f.startswith("data-")]
    assert ctrl_seq == [f"ctrl-{i}\n" for i in range(16)]
    assert data_seq == [f"data-{i}\n" for i in range(4)]
