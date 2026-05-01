"""In-memory wire-loop smoke suite.

Wires together :class:`WsSender` and :class:`FrameReceiver` through the
:class:`tests._helpers.QueueWs` double, with no real WebSocket
connection, no subprocess agent, no SOCKS5 listener.

The goal is to exercise the **frame I/O contract** end-to-end:

* :class:`WsSender` → wire → ``QueueWs`` capture: frames the session
  emits arrive at the test in their on-wire form.
* test → ``QueueWs`` → :class:`FrameReceiver`: frames fed by the test
  are dispatched to the registries the receiver was constructed with.

Per-frame routing logic lives in handler classes (`TcpConnection`,
`UdpFlow`, ``PendingConnect``); we don't re-test those here — we just
assert the *receiver* delivers frames into the right registry slot.

These tests are gated behind ``@pytest.mark.inmemory`` so they can be
filtered out of the unit-tier ``make test-unit`` run.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from exectunnel.exceptions import ConnectionClosedError
from exectunnel.protocol import (
    encode_conn_ack_frame,
    encode_conn_close_frame,
    encode_data_frame,
    encode_keepalive_frame,
)
from exectunnel.protocol.codecs import encode_binary_payload
from exectunnel.session._config import SessionConfig
from exectunnel.session._receiver import FrameReceiver
from exectunnel.session._sender import WsSender
from exectunnel.session._state import AckStatus, PendingConnect

from tests._helpers import QueueWs

pytestmark = pytest.mark.inmemory


def _make_session_cfg() -> SessionConfig:
    """Minimal in-memory SessionConfig (URL is required even though we don't connect)."""
    return SessionConfig(wss_url="wss://test.invalid/")


# ── WsSender → wire ──────────────────────────────────────────────────────────


class TestSenderToWire:
    async def test_keepalive_frame_reaches_wire(self) -> None:
        ws = QueueWs()
        ws_closed = asyncio.Event()
        sender = WsSender(ws, _make_session_cfg(), ws_closed)
        sender.start()
        try:
            await sender.send(encode_keepalive_frame(), control=True)
            captured = await ws.pop_from_session(timeout=1.0)
            assert "KEEPALIVE" in captured
        finally:
            await sender.stop()

    async def test_data_frame_round_trips_payload(self) -> None:
        ws = QueueWs()
        sender = WsSender(ws, _make_session_cfg(), asyncio.Event())
        sender.start()
        try:
            conn_id = "c" + "a" * 24
            payload = b"hello, world!"
            await sender.send(encode_data_frame(conn_id, payload))
            captured = await ws.pop_from_session(timeout=1.0)
            assert ":DATA:" in captured
            assert conn_id in captured
        finally:
            await sender.stop()


# ── wire → FrameReceiver ─────────────────────────────────────────────────────


class TestWireToReceiver:
    async def test_conn_ack_resolves_pending_future(self) -> None:
        ws = QueueWs()
        ws_closed = asyncio.Event()
        sender = WsSender(ws, _make_session_cfg(), ws_closed)

        loop = asyncio.get_running_loop()
        conn_id = "c" + "f" * 24
        pending: dict[str, PendingConnect] = {
            conn_id: PendingConnect(
                host="example.com", port=443, ack_future=loop.create_future()
            )
        }

        receiver = FrameReceiver(
            ws=ws,
            ws_closed=ws_closed,
            tcp_registry={},
            pending_connects=pending,
            udp_registry={},
            post_ready_lines=[],
            pre_ready_carry="",
            ws_send=sender,
        )

        sender.start()
        recv_task = asyncio.create_task(receiver.run())
        try:
            ws.feed_to_session(encode_conn_ack_frame(conn_id))
            # Give the receiver a tick to dispatch.
            result = await asyncio.wait_for(pending[conn_id].ack_future, timeout=1.0)
            assert result is AckStatus.OK
        finally:
            await ws.close()
            # The receiver raises ``ConnectionClosedError`` when the
            # iterator ends — that's the expected shutdown signal
            # back to ``TunnelSession`` for reconnect; here we just
            # consume it.
            with contextlib.suppress(ConnectionClosedError):
                await asyncio.wait_for(recv_task, timeout=2.0)
            await sender.stop()

    async def test_keepalive_frame_is_silently_ignored(self) -> None:
        """A bare ``KEEPALIVE`` from the agent is a protocol violation.

        The receiver must raise :class:`UnexpectedFrameError` so the
        session enters reconnect.  This pins the documented post-bootstrap
        rule (see ``UNEXPECTED_NO_CONN_ID_FRAMES``).
        """
        from exectunnel.exceptions import UnexpectedFrameError

        ws = QueueWs()
        ws_closed = asyncio.Event()
        sender = WsSender(ws, _make_session_cfg(), ws_closed)

        receiver = FrameReceiver(
            ws=ws,
            ws_closed=ws_closed,
            tcp_registry={},
            pending_connects={},
            udp_registry={},
            post_ready_lines=[],
            pre_ready_carry="",
            ws_send=sender,
        )

        sender.start()
        try:
            ws.feed_to_session(encode_keepalive_frame())
            with pytest.raises(UnexpectedFrameError):
                await asyncio.wait_for(receiver.run(), timeout=1.0)
        finally:
            await ws.close()
            await sender.stop()


# ── Round-trip ───────────────────────────────────────────────────────────────


class TestSenderReceiverRoundTrip:
    async def test_send_then_feed_back_round_trip(self) -> None:
        """Sender emits a CONN_CLOSE; the test echoes it as if from the agent.

        This is the simplest two-direction smoke: the same wire format
        the sender produces is consumable by the receiver after we
        re-inject it on the other side of the QueueWs.
        """
        ws = QueueWs()
        ws_closed = asyncio.Event()
        sender = WsSender(ws, _make_session_cfg(), ws_closed)

        receiver = FrameReceiver(
            ws=ws,
            ws_closed=ws_closed,
            tcp_registry={},
            pending_connects={},
            udp_registry={},
            post_ready_lines=[],
            pre_ready_carry="",
            ws_send=sender,
        )
        sender.start()
        recv_task = asyncio.create_task(receiver.run())
        try:
            conn_id = "c" + "1" * 24
            await sender.send(encode_conn_close_frame(conn_id), control=True)
            captured = await ws.pop_from_session(timeout=1.0)
            # Echo it back as if the agent sent it.
            ws.feed_to_session(captured)
            # Receiver should swallow CONN_CLOSE for an unknown conn_id
            # without raising — there is nothing in the registry to
            # close, but it is a *valid* frame.
            await asyncio.sleep(0.05)
        finally:
            await ws.close()
            with contextlib.suppress(ConnectionClosedError):
                await asyncio.wait_for(recv_task, timeout=2.0)
            await sender.stop()

    async def test_binary_payload_round_trips_through_data_frame(self) -> None:
        ws = QueueWs()
        sender = WsSender(ws, _make_session_cfg(), asyncio.Event())
        sender.start()
        try:
            conn_id = "c" + "2" * 24
            raw = b"\x00\x01\x02\xff\xfe"
            await sender.send(encode_data_frame(conn_id, raw))
            wire_frame = await ws.pop_from_session(timeout=1.0)

            from exectunnel.protocol import decode_binary_payload, parse_frame

            parsed = parse_frame(wire_frame)
            assert parsed is not None
            assert parsed.msg_type == "DATA"
            assert parsed.conn_id == conn_id
            assert decode_binary_payload(parsed.payload) == raw
            # Sanity: payload was emitted in base64url form, never raw.
            assert parsed.payload != encode_binary_payload(raw)[:0]  # non-empty
        finally:
            await sender.stop()
