from __future__ import annotations

import asyncio
import base64
import contextlib

from exectunnel.connection import _ConnHandler
from exectunnel.core.config import (
    AppConfig,
    BridgeConfig,
    get_tunnel_config,
)
from exectunnel.core.consts import Cmd
from exectunnel.core.models import PendingConnect
from exectunnel.helpers import encode_frame
from exectunnel.observability import metrics_reset, metrics_snapshot
from exectunnel.socks5 import Socks5Request
from exectunnel.tunnel import TunnelSession


class _Writer:
    def __init__(self) -> None:
        self.closed = False

    def write(self, _: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def can_write_eof(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.closed = True


async def _noop_ws_send(_: str, *, control: bool = False) -> None:
    _ = control


def _make_session(conn_ack_timeout: float = 0.05) -> TunnelSession:
    app = AppConfig(
        wss_url="wss://example.test/ws",
        insecure=False,
        bridge=BridgeConfig(send_queue_cap=8),
    )
    tun = get_tunnel_config(app, conn_ack_timeout=conn_ack_timeout)
    return TunnelSession(app, tun)


async def test_dispatch_pre_ack_overflow_marks_pending_failure() -> None:
    session = _make_session()
    conn_id = "c123abc"
    req = Socks5Request(
        cmd=Cmd.CONNECT,
        host="example.com",
        port=443,
        reader=asyncio.StreamReader(),
        writer=_Writer(),  # type: ignore[arg-type]
    )
    loop = asyncio.get_running_loop()
    ack_future: asyncio.Future[str] = loop.create_future()
    handler = _ConnHandler(
        conn_id,
        req.reader,
        req.writer,  # type: ignore[arg-type]
        _noop_ws_send,
        session._conn_handlers,  # type: ignore[attr-defined]
        pre_ack_buffer_cap_bytes=8,
    )
    session._conn_handlers[conn_id] = handler  # type: ignore[attr-defined]
    session._pending_connects[conn_id] = PendingConnect(  # type: ignore[attr-defined]
        host=req.host,
        request=req,
        handler=handler,
        ack_future=ack_future,
    )

    payload = base64.b64encode(b"123456789").decode()
    frame = encode_frame("DATA", conn_id, payload).rstrip("\n")
    await session._dispatch_frame_async(frame)  # type: ignore[attr-defined]

    assert ack_future.done()
    assert ack_future.result() == "pre_ack_overflow"


async def test_dispatch_pre_ack_data_ignored_when_handler_closed() -> None:
    """DATA arriving after handler is already closed must NOT set ack_future."""
    session = _make_session()
    conn_id = "c456def"
    req = Socks5Request(
        cmd=Cmd.CONNECT,
        host="example.com",
        port=443,
        reader=asyncio.StreamReader(),
        writer=_Writer(),  # type: ignore[arg-type]
    )
    loop = asyncio.get_running_loop()
    ack_future: asyncio.Future[str] = loop.create_future()
    handler = _ConnHandler(
        conn_id,
        req.reader,
        req.writer,  # type: ignore[arg-type]
        _noop_ws_send,
        session._conn_handlers,  # type: ignore[attr-defined]
        pre_ack_buffer_cap_bytes=8,
    )
    # Simulate the handler already being torn down (e.g. from a prior ERROR frame).
    handler._closed.set()  # type: ignore[attr-defined]
    session._conn_handlers[conn_id] = handler  # type: ignore[attr-defined]
    session._pending_connects[conn_id] = PendingConnect(  # type: ignore[attr-defined]
        host=req.host,
        request=req,
        handler=handler,
        ack_future=ack_future,
    )

    payload = base64.b64encode(b"hello").decode()
    frame = encode_frame("DATA", conn_id, payload).rstrip("\n")
    await session._dispatch_frame_async(frame)  # type: ignore[attr-defined]

    # ack_future must remain unresolved — the DATA was silently dropped.
    assert not ack_future.done()


async def test_handle_connect_timeout_cleans_pending_state() -> None:
    metrics_reset()
    session = _make_session(conn_ack_timeout=0.01)
    req = Socks5Request(
        cmd=Cmd.CONNECT,
        host="challenges.cloudflare.com",
        port=443,
        reader=asyncio.StreamReader(),
        writer=_Writer(),  # type: ignore[arg-type]
    )

    await session._handle_connect(req)  # type: ignore[attr-defined]

    assert session._pending_connects == {}  # type: ignore[attr-defined]
    assert session._conn_handlers == {}  # type: ignore[attr-defined]
    metrics = metrics_snapshot()
    assert any(k.startswith("connect_ack_timeout") for k in metrics)


def test_cloudflare_host_gate_uses_dedicated_limit(monkeypatch) -> None:
    monkeypatch.setenv("EXECTUNNEL_CONNECT_MAX_PENDING_PER_HOST", "16")
    monkeypatch.setenv("EXECTUNNEL_CONNECT_MAX_PENDING_CF", "3")
    session = _make_session()
    gate = session._connect_host_gate("challenges.cloudflare.com")  # type: ignore[attr-defined]
    assert gate._value == 3  # type: ignore[attr-defined]


def test_cloudflare_host_pace_interval_uses_env(monkeypatch) -> None:
    monkeypatch.setenv("EXECTUNNEL_CONNECT_PACE_CF_MS", "25")
    session = _make_session()
    assert session._connect_host_pace_interval("challenges.cloudflare.com") == 0.025  # type: ignore[attr-defined]
    assert session._connect_host_pace_interval("example.com") == 0.0  # type: ignore[attr-defined]


def test_data_send_timeout_removed() -> None:
    # data_send_timeout was removed from TunnelConfig: data frames are sent
    # without a deadline so TCP backpressure (via the bounded send queue)
    # controls the rate instead of dropping frames on timeout.
    session = _make_session()
    assert not hasattr(session, "_data_send_timeout")
    # The config field must also be gone so operators don't set
    # EXECTUNNEL_DATA_SEND_TIMEOUT expecting it to have an effect.
    from exectunnel.core.config import TunnelConfig
    assert not hasattr(TunnelConfig(), "data_send_timeout")


class _WaitQueue:
    def __init__(self, wait_results: list[str | None]) -> None:
        self._wait_results = wait_results
        self.items: list[str | None] = []

    def get_nowait(self) -> str | None:
        raise asyncio.QueueEmpty

    async def get(self) -> str | None:
        if self._wait_results:
            return self._wait_results.pop(0)
        await asyncio.Event().wait()
        return None

    def put_nowait(self, item: str | None) -> None:
        self.items.append(item)


class _FakeWs:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.sent_event = asyncio.Event()

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)
        self.sent_event.set()


async def test_send_loop_prioritizes_control_when_both_ready() -> None:
    session = _make_session()
    ws = _FakeWs()
    ctrl_q = _WaitQueue(["<<<EXECTUNNEL:CONN_OPEN:c1:example.com:443>>>\n"])
    data_q = _WaitQueue(["<<<EXECTUNNEL:DATA:c1:ZGF0YQ==>>>\n"])

    session._ws = ws  # type: ignore[attr-defined]
    session._send_ctrl_queue = ctrl_q  # type: ignore[attr-defined]
    session._send_data_queue = data_q  # type: ignore[attr-defined]

    task = asyncio.create_task(session._send_loop())  # type: ignore[attr-defined]
    await asyncio.wait_for(ws.sent_event.wait(), timeout=0.2)
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert len(ws.sent) >= 2
    assert b"<<<EXECTUNNEL:CONN_OPEN:" in ws.sent[0]
    assert b"<<<EXECTUNNEL:DATA:c1:ZGF0YQ==>>>" in ws.sent[1]


async def test_dispatch_frame_async_data_blocks_on_full_queue() -> None:
    """_dispatch_frame_async must block (not abort) when the inbound queue is full.

    This verifies the backpressure contract: the WS reader stalls until the
    downstream consumer drains the queue, instead of aborting the connection.
    """
    session = _make_session()
    conn_id = "c999abc"
    req = Socks5Request(
        cmd=Cmd.CONNECT,
        host="example.com",
        port=443,
        reader=asyncio.StreamReader(),
        writer=_Writer(),  # type: ignore[arg-type]
    )
    handler = _ConnHandler(
        conn_id,
        req.reader,
        req.writer,  # type: ignore[arg-type]
        _noop_ws_send,
        session._conn_handlers,  # type: ignore[attr-defined]
    )
    handler._started = True  # type: ignore[attr-defined]
    # Fill the queue to capacity.
    for _ in range(handler._inbound.maxsize):  # type: ignore[attr-defined]
        handler._inbound.put_nowait(b"x")  # type: ignore[attr-defined]

    session._conn_handlers[conn_id] = handler  # type: ignore[attr-defined]

    payload = base64.b64encode(b"backpressure").decode()
    frame = encode_frame("DATA", conn_id, payload).rstrip("\n")

    # Dispatch must block because the queue is full.
    dispatch_task = asyncio.create_task(
        session._dispatch_frame_async(frame)  # type: ignore[attr-defined]
    )
    await asyncio.sleep(0.05)
    assert not dispatch_task.done(), "dispatch should be blocked on full queue"

    # Drain one item — dispatch should now complete.
    handler._inbound.get_nowait()  # type: ignore[attr-defined]
    await asyncio.wait_for(dispatch_task, timeout=0.2)
    assert dispatch_task.done() and not dispatch_task.cancelled()
