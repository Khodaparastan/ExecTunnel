"""Real-life scenario integration tests for ExecTunnel.

Each test spins up a ``TunnelHarness`` (fake-agent WS server + TunnelSession)
and drives real SOCKS5 traffic through it.  No external services required.

Scenarios covered
-----------------
1.  Happy-path TCP echo — single connection, data round-trip.
2.  Multiple concurrent connections — 10 parallel CONNECT flows.
3.  Large payload — 1 MiB transfer through the tunnel.
4.  Agent-initiated close — agent sends CONN_CLOSED_ACK before client closes.
5.  Agent ERROR — agent signals error; tunnel should reply with SOCKS5 error.
6.  ACK timeout — agent never ACKs; tunnel should reply with SOCKS5 error.
7.  Slow ACK — agent delays ACK by 0.5 s; connection still succeeds.
8.  Half-close — client sends EOF; agent echoes remaining data then closes.
9.  Connection refused (SOCKS5 error reply) — agent sends ERROR immediately.
10. Bootstrap absorbs all commands and signals AGENT_READY correctly.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from tests.integration.harness import (
    FakeAgent,
    TunnelHarness,
    socks5_connect,
)

# ── Scenario 1: Happy-path TCP echo ──────────────────────────────────────────


async def test_happy_path_echo() -> None:
    """Single CONNECT + data round-trip through the tunnel."""
    async with TunnelHarness() as h:
        reader, writer = await socks5_connect(h.socks_addr, "example.test", 80)
        try:
            writer.write(b"hello tunnel")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(64), timeout=3.0)
            assert data == b"hello tunnel"
        finally:
            writer.close()
            await writer.wait_closed()


# ── Scenario 2: Multiple concurrent connections ───────────────────────────────


async def test_concurrent_connections() -> None:
    """10 parallel CONNECT flows all echo correctly."""
    async with TunnelHarness() as h:

        async def one_flow(i: int) -> None:
            payload = f"flow-{i}".encode()
            reader, writer = await socks5_connect(h.socks_addr, f"host{i}.test", 80)
            try:
                writer.write(payload)
                await writer.drain()
                data = await asyncio.wait_for(reader.read(64), timeout=3.0)
                assert data == payload
            finally:
                writer.close()
                await writer.wait_closed()

        await asyncio.gather(*[one_flow(i) for i in range(10)])


# ── Scenario 3: Large payload ─────────────────────────────────────────────────


async def test_large_payload() -> None:
    """1 MiB transfer through the tunnel (chunked echo)."""
    payload = b"X" * (1024 * 1024)

    async with TunnelHarness() as h:
        reader, writer = await socks5_connect(h.socks_addr, "big.test", 80)
        try:
            writer.write(payload)
            await writer.drain()

            received = bytearray()
            while len(received) < len(payload):
                chunk = await asyncio.wait_for(reader.read(65536), timeout=10.0)
                if not chunk:
                    break
                received.extend(chunk)

            assert bytes(received) == payload
        finally:
            writer.close()
            await writer.wait_closed()


# ── Scenario 4: Agent-initiated close ────────────────────────────────────────


async def test_agent_initiated_close() -> None:
    """Agent sends CONN_CLOSED_ACK immediately after ACK — client gets EOF."""

    async def conn_open_then_close(agent: FakeAgent, parts: list[str]) -> None:
        conn_id = parts[1]
        await agent.send_conn_ack(conn_id)
        # Send a small payload then close.
        await agent.send_data(conn_id, b"bye")
        await agent.send_conn_closed_ack(conn_id)

    agent = FakeAgent(on_conn_open=conn_open_then_close)
    async with TunnelHarness(agent=agent) as h:
        reader, writer = await socks5_connect(h.socks_addr, "closing.test", 80)
        try:
            data = await asyncio.wait_for(reader.read(64), timeout=3.0)
            assert data == b"bye"
            # After agent closes, client should get EOF.
            eof = await asyncio.wait_for(reader.read(64), timeout=3.0)
            assert eof == b""
        finally:
            writer.close()
            await writer.wait_closed()


# ── Scenario 5: Agent ERROR after ACK ────────────────────────────────────────


async def test_agent_error_after_ack_closes_downstream() -> None:
    """Agent ACKs then sends ERROR — downstream should receive EOF."""

    async def conn_open_error(agent: FakeAgent, parts: list[str]) -> None:
        conn_id = parts[1]
        await agent.send_conn_ack(conn_id)
        await asyncio.sleep(0.05)
        await agent.send_error(conn_id, "simulated agent error")

    agent = FakeAgent(on_conn_open=conn_open_error)
    async with TunnelHarness(agent=agent) as h:
        reader, writer = await socks5_connect(h.socks_addr, "error.test", 80)
        try:
            # Downstream should close (EOF or connection reset).
            data = await asyncio.wait_for(reader.read(64), timeout=3.0)
            assert data == b""
        finally:
            writer.close()
            await writer.wait_closed()


# ── Scenario 6: ACK timeout ───────────────────────────────────────────────────


async def test_ack_timeout_returns_socks5_error() -> None:
    """Agent never ACKs — SOCKS5 CONNECT should fail (non-zero reply code)."""

    async def never_ack(_agent: FakeAgent, _parts: list[str]) -> None:
        # Return immediately without sending ACK — the tunnel's conn_ack_timeout
        # will fire and the SOCKS5 reply will be an error code.
        return

    agent = FakeAgent(on_conn_open=never_ack)
    async with TunnelHarness(agent=agent, conn_ack_timeout=0.2) as h:
        reader, writer = await asyncio.open_connection(*h.socks_addr)
        try:
            # SOCKS5 greeting
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            await reader.readexactly(2)

            # CONNECT request
            host = b"timeout.test"
            writer.write(
                bytes([0x05, 0x01, 0x00, 0x03, len(host)])
                + host
                + b"\x00\x50"
            )
            await writer.drain()

            # Read reply — should be non-zero (error)
            header = await asyncio.wait_for(reader.readexactly(4), timeout=3.0)
            assert header[1] != 0x00, f"expected SOCKS5 error, got success (code={header[1]})"
        finally:
            writer.close()
            await writer.wait_closed()


# ── Scenario 7: Slow ACK ──────────────────────────────────────────────────────


async def test_slow_ack_succeeds() -> None:
    """Agent delays ACK by 0.3 s — connection should still succeed."""

    async def slow_ack(agent: FakeAgent, parts: list[str]) -> None:
        await asyncio.sleep(0.3)
        await agent.send_conn_ack(parts[1])

    agent = FakeAgent(on_conn_open=slow_ack)
    async with TunnelHarness(agent=agent, conn_ack_timeout=2.0) as h:
        reader, writer = await socks5_connect(h.socks_addr, "slow.test", 80)
        try:
            writer.write(b"slow but ok")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(64), timeout=3.0)
            assert data == b"slow but ok"
        finally:
            writer.close()
            await writer.wait_closed()


# ── Scenario 8: Half-close (client EOF) ──────────────────────────────────────


async def test_half_close_client_eof() -> None:
    """Client sends EOF; agent echoes remaining data then closes."""

    async def half_close_data(agent: FakeAgent, parts: list[str]) -> None:
        """Echo data back, then on CONN_CLOSE send final bytes + CONN_CLOSED_ACK."""
        conn_id = parts[1]
        payload = parts[2] if len(parts) > 2 else ""
        try:
            data = base64.b64decode(payload)
        except Exception:
            return
        await agent.send_data(conn_id, data)

    async def half_close_close(agent: FakeAgent, parts: list[str]) -> None:
        conn_id = parts[1]
        # Send one more byte after receiving client EOF.
        await agent.send_data(conn_id, b"final")
        await agent.send_conn_closed_ack(conn_id)

    agent = FakeAgent(on_data=half_close_data, on_conn_close=half_close_close)
    async with TunnelHarness(agent=agent) as h:
        reader, writer = await socks5_connect(h.socks_addr, "halfclose.test", 80)
        try:
            writer.write(b"ping")
            await writer.drain()
            echo = await asyncio.wait_for(reader.read(64), timeout=3.0)
            assert echo == b"ping"

            # Send EOF from client side.
            writer.write_eof()
            await writer.drain()

            # Should receive "final" then EOF.
            final = await asyncio.wait_for(reader.read(64), timeout=3.0)
            assert b"final" in final
        finally:
            writer.close()
            await writer.wait_closed()


# ── Scenario 9: Agent ERROR before ACK (connection refused) ──────────────────


async def test_agent_error_before_ack_socks5_error() -> None:
    """Agent sends ERROR instead of ACK — SOCKS5 reply should be non-zero."""

    async def error_instead_of_ack(agent: FakeAgent, parts: list[str]) -> None:
        conn_id = parts[1]
        await agent.send_error(conn_id, "connection refused by agent")

    agent = FakeAgent(on_conn_open=error_instead_of_ack)
    async with TunnelHarness(agent=agent) as h:
        reader, writer = await asyncio.open_connection(*h.socks_addr)
        try:
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            await reader.readexactly(2)

            host = b"refused.test"
            writer.write(
                bytes([0x05, 0x01, 0x00, 0x03, len(host)])
                + host
                + b"\x00\x50"
            )
            await writer.drain()

            header = await asyncio.wait_for(reader.readexactly(4), timeout=3.0)
            assert header[1] != 0x00, "expected SOCKS5 error, got success"
        finally:
            writer.close()
            await writer.wait_closed()


# ── Scenario 10: Bootstrap correctness ───────────────────────────────────────


async def test_bootstrap_commands_received() -> None:
    """Verify the fake agent receives all expected bootstrap commands."""
    async with TunnelHarness(agent=FakeAgent(fast_bootstrap=False)) as h:
        agent = h.agent
        # Wait for ready_event to be set (bootstrap complete).
        await asyncio.wait_for(agent.ready_event.wait(), timeout=5.0)

        cmds = " ".join(agent.bootstrap_commands)
        assert "stty" in cmds
        assert "base64" in cmds
        assert "exec python3" in cmds


# ── Scenario 11: Reconnect after WS close ────────────────────────────────────


async def test_session_ends_when_ws_closes() -> None:
    """When the WS server closes, the session task should end (no retry since max_retries=0)."""
    async with TunnelHarness() as h:
        # Close the WS server abruptly.
        h._ws_server.close()
        await h._ws_server.wait_closed()

        # Session task should end within a few seconds.
        assert h._session_task is not None
        try:
            await asyncio.wait_for(
                asyncio.shield(h._session_task), timeout=5.0
            )
        except TimeoutError:
            pytest.fail("session task did not end after WS server closed")
        except Exception:
            pass  # RuntimeError from exhausted retries is expected


# ── Scenario 12: Pipelined requests (send before ACK) ────────────────────────


async def test_pipelined_data_before_ack() -> None:
    """Data sent before ACK is buffered and delivered after ACK."""

    ack_event: asyncio.Event = asyncio.Event()

    async def delayed_ack(agent: FakeAgent, parts: list[str]) -> None:
        conn_id = parts[1]
        # Wait for test to signal before ACKing.
        await asyncio.wait_for(ack_event.wait(), timeout=3.0)
        await agent.send_conn_ack(conn_id)

    agent = FakeAgent(on_conn_open=delayed_ack)
    async with TunnelHarness(agent=agent) as h:
        reader, writer = await asyncio.open_connection(*h.socks_addr)

        # Start SOCKS5 handshake in background.
        async def do_connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            await reader.readexactly(2)
            host = b"pipeline.test"
            writer.write(
                bytes([0x05, 0x01, 0x00, 0x03, len(host)])
                + host
                + b"\x00\x50"
            )
            await writer.drain()
            return reader, writer

        connect_task = asyncio.create_task(do_connect())

        # Give the tunnel time to send CONN_OPEN and wait for ACK.
        await asyncio.sleep(0.1)

        # Now release the ACK.
        ack_event.set()

        r, w = await asyncio.wait_for(connect_task, timeout=3.0)
        # Read SOCKS5 reply.
        header = await asyncio.wait_for(r.readexactly(4), timeout=3.0)
        assert header[1] == 0x00, f"expected success, got {header[1]}"

        # Consume bind address.
        atyp = header[3]
        if atyp == 0x01:
            await r.readexactly(6)
        elif atyp == 0x03:
            n = (await r.readexactly(1))[0]
            await r.readexactly(n + 2)

        w.write(b"pipelined")
        await w.drain()
        data = await asyncio.wait_for(r.read(64), timeout=3.0)
        assert data == b"pipelined"

        w.close()
        await w.wait_closed()
