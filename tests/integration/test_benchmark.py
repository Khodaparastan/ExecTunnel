"""Benchmark and pressure tests for ExecTunnel.

These tests measure throughput, latency, and concurrency limits under load.
They are designed to be run manually or in CI with a dedicated benchmark job:

    python -m pytest tests/integration/test_benchmark.py -v -s

Results are printed to stdout.  No assertions on absolute numbers — only
sanity checks (e.g. no data corruption, no hangs) so the suite stays green
on slow CI machines.

Benchmarks
----------
B1. Throughput — measure MB/s for a 10 MiB transfer.
B2. Latency    — measure round-trip time for 100 sequential 1-byte pings.
B3. Concurrency — 100 parallel connections, all echo correctly.
B4. Sustained load — 50 connections × 10 round-trips each.
B5. Send-queue backpressure — slow consumer, verify no data loss.
B6. Connection churn — open/close 200 connections sequentially.
B7. Frame flood — agent sends 10 000 DATA frames rapidly; verify all received.
"""

from __future__ import annotations

import asyncio
import time

from tests.integration.harness import (
    FakeAgent,
    TunnelHarness,
    socks5_connect,
)

# ── B1: Throughput ────────────────────────────────────────────────────────────


async def test_benchmark_throughput() -> None:
    """Measure MB/s for a 10 MiB echo transfer."""
    payload = b"T" * (10 * 1024 * 1024)

    async with TunnelHarness() as h:
        reader, writer = await socks5_connect(h.socks_addr, "bench.test", 80)
        try:
            t0 = time.perf_counter()
            writer.write(payload)
            await writer.drain()

            received = bytearray()
            while len(received) < len(payload):
                chunk = await asyncio.wait_for(reader.read(65536), timeout=30.0)
                if not chunk:
                    break
                received.extend(chunk)

            elapsed = time.perf_counter() - t0
            mb = len(payload) / (1024 * 1024)
            mbps = mb / elapsed
            print(f"\n[B1] Throughput: {mbps:.2f} MB/s ({mb:.0f} MiB in {elapsed:.3f}s)")

            assert bytes(received) == payload, "data corruption detected"
            assert elapsed < 60.0, f"transfer took too long: {elapsed:.1f}s"
        finally:
            writer.close()
            await writer.wait_closed()


# ── B2: Latency ───────────────────────────────────────────────────────────────


async def test_benchmark_latency() -> None:
    """Measure round-trip latency for 100 sequential 1-byte pings."""
    n = 100

    async with TunnelHarness() as h:
        reader, writer = await socks5_connect(h.socks_addr, "latency.test", 80)
        try:
            latencies: list[float] = []
            for _ in range(n):
                t0 = time.perf_counter()
                writer.write(b"\x00")
                await writer.drain()
                await asyncio.wait_for(reader.readexactly(1), timeout=3.0)
                latencies.append(time.perf_counter() - t0)

            avg_ms = (sum(latencies) / len(latencies)) * 1000
            p99_ms = sorted(latencies)[int(0.99 * len(latencies))] * 1000
            print(
                f"\n[B2] Latency over {n} pings: "
                f"avg={avg_ms:.2f}ms  p99={p99_ms:.2f}ms"
            )
            assert avg_ms < 500.0, f"average latency too high: {avg_ms:.1f}ms"
        finally:
            writer.close()
            await writer.wait_closed()


# ── B3: Concurrency ───────────────────────────────────────────────────────────


async def test_benchmark_concurrency() -> None:
    """100 parallel connections all echo correctly."""
    n = 100

    async with TunnelHarness() as h:
        errors: list[str] = []

        async def one_flow(i: int) -> None:
            payload = f"concurrent-{i}".encode()
            try:
                reader, writer = await socks5_connect(
                    h.socks_addr, f"c{i}.test", 80
                )
                try:
                    writer.write(payload)
                    await writer.drain()
                    data = await asyncio.wait_for(reader.read(64), timeout=5.0)
                    if data != payload:
                        errors.append(f"flow {i}: got {data!r}, want {payload!r}")
                finally:
                    writer.close()
                    await writer.wait_closed()
            except Exception as exc:
                errors.append(f"flow {i}: {exc}")

        t0 = time.perf_counter()
        await asyncio.gather(*[one_flow(i) for i in range(n)])
        elapsed = time.perf_counter() - t0
        print(f"\n[B3] {n} concurrent connections in {elapsed:.3f}s")

        assert not errors, "errors in concurrent flows:\n" + "\n".join(errors)


# ── B4: Sustained load ────────────────────────────────────────────────────────


async def test_benchmark_sustained_load() -> None:
    """50 connections × 10 round-trips each."""
    n_conns = 50
    n_trips = 10

    async with TunnelHarness() as h:
        errors: list[str] = []

        async def one_conn(i: int) -> None:
            try:
                reader, writer = await socks5_connect(
                    h.socks_addr, f"sustained{i}.test", 80
                )
                try:
                    for j in range(n_trips):
                        msg = f"conn{i}-trip{j}".encode()
                        writer.write(msg)
                        await writer.drain()
                        data = await asyncio.wait_for(reader.read(64), timeout=5.0)
                        if data != msg:
                            errors.append(
                                f"conn {i} trip {j}: got {data!r}, want {msg!r}"
                            )
                finally:
                    writer.close()
                    await writer.wait_closed()
            except Exception as exc:
                errors.append(f"conn {i}: {exc}")

        t0 = time.perf_counter()
        await asyncio.gather(*[one_conn(i) for i in range(n_conns)])
        elapsed = time.perf_counter() - t0
        total = n_conns * n_trips
        print(
            f"\n[B4] Sustained load: {total} round-trips "
            f"({n_conns} conns × {n_trips} trips) in {elapsed:.3f}s"
        )

        assert not errors, "errors in sustained load:\n" + "\n".join(errors)


# ── B5: Backpressure (slow consumer) ─────────────────────────────────────────


async def test_benchmark_backpressure_no_data_loss() -> None:
    """Slow consumer: agent sends data faster than client reads; verify no loss."""
    chunk = b"BP" * 512  # 1 KiB per frame
    n_frames = 200
    total = len(chunk) * n_frames

    frames_sent = asyncio.Event()

    async def flood_data(agent: FakeAgent, parts: list[str]) -> None:
        conn_id = parts[1]
        await agent.send_conn_ack(conn_id)
        for _ in range(n_frames):
            await agent.send_data(conn_id, chunk)
        frames_sent.set()
        await agent.send_conn_closed_ack(conn_id)

    agent = FakeAgent(on_conn_open=flood_data)
    async with TunnelHarness(agent=agent) as h:
        reader, writer = await socks5_connect(h.socks_addr, "bp.test", 80)
        try:
            # Slow read: 4 KiB at a time with a small delay.
            received = bytearray()
            while len(received) < total:
                await asyncio.sleep(0.002)  # simulate slow consumer
                chunk_in = await asyncio.wait_for(reader.read(4096), timeout=10.0)
                if not chunk_in:
                    break
                received.extend(chunk_in)

            print(
                f"\n[B5] Backpressure: received {len(received)} / {total} bytes"
            )
            assert len(received) == total, (
                f"data loss: got {len(received)}, expected {total}"
            )
        finally:
            writer.close()
            await writer.wait_closed()


# ── B6: Connection churn ──────────────────────────────────────────────────────


async def test_benchmark_connection_churn() -> None:
    """Open and close 200 connections sequentially; measure rate."""
    n = 200

    async with TunnelHarness() as h:
        t0 = time.perf_counter()
        for i in range(n):
            reader, writer = await socks5_connect(h.socks_addr, f"churn{i}.test", 80)
            writer.write(b"x")
            await writer.drain()
            await asyncio.wait_for(reader.read(1), timeout=3.0)
            writer.close()
            await writer.wait_closed()

        elapsed = time.perf_counter() - t0
        rate = n / elapsed
        print(f"\n[B6] Connection churn: {n} conns in {elapsed:.3f}s ({rate:.1f} conn/s)")
        assert elapsed < 120.0, f"churn took too long: {elapsed:.1f}s"


# ── B7: Frame flood ───────────────────────────────────────────────────────────


async def test_benchmark_frame_flood() -> None:
    """Agent sends 1000 DATA frames rapidly; verify all received without loss."""
    n_frames = 1000
    frame_data = b"F" * 128
    total = n_frames * len(frame_data)

    async def flood(agent: FakeAgent, parts: list[str]) -> None:
        conn_id = parts[1]
        await agent.send_conn_ack(conn_id)
        for _ in range(n_frames):
            await agent.send_data(conn_id, frame_data)
        await agent.send_conn_closed_ack(conn_id)

    agent = FakeAgent(on_conn_open=flood)
    async with TunnelHarness(agent=agent) as h:
        reader, writer = await socks5_connect(h.socks_addr, "flood.test", 80)
        try:
            received = bytearray()
            while len(received) < total:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=10.0)
                if not chunk:
                    break
                received.extend(chunk)

            print(
                f"\n[B7] Frame flood: {n_frames} frames × {len(frame_data)} B "
                f"= {total} B received {len(received)} B"
            )
            assert len(received) == total, (
                f"frame loss: got {len(received)}, expected {total}"
            )
        finally:
            writer.close()
            await writer.wait_closed()


# ── B8: ACK timeout pressure ──────────────────────────────────────────────────


async def test_benchmark_ack_timeout_pressure() -> None:
    """50 connections that all time out; verify tunnel stays alive after."""
    n = 50

    async def never_ack(_agent: FakeAgent, _parts: list[str]) -> None:
        # Return without ACKing — the tunnel's conn_ack_timeout fires and
        # returns a SOCKS5 error.  No sleep needed; the timeout is on the
        # tunnel side, not the agent side.
        return

    agent = FakeAgent(on_conn_open=never_ack)
    async with TunnelHarness(agent=agent, conn_ack_timeout=0.1) as h:
        errors: list[str] = []

        async def one_timeout(i: int) -> None:
            reader, writer = await asyncio.open_connection(*h.socks_addr)
            try:
                writer.write(b"\x05\x01\x00")
                await writer.drain()
                await reader.readexactly(2)
                host = f"t{i}.test".encode()
                writer.write(
                    bytes([0x05, 0x01, 0x00, 0x03, len(host)])
                    + host
                    + b"\x00\x50"
                )
                await writer.drain()
                header = await asyncio.wait_for(reader.readexactly(4), timeout=3.0)
                if header[1] == 0x00:
                    errors.append(f"conn {i}: expected error, got success")
            except Exception as exc:
                errors.append(f"conn {i}: unexpected exception: {exc}")
            finally:
                writer.close()
                await writer.wait_closed()

        t0 = time.perf_counter()
        await asyncio.gather(*[one_timeout(i) for i in range(n)])
        elapsed = time.perf_counter() - t0
        print(f"\n[B8] ACK timeout pressure: {n} timeouts in {elapsed:.3f}s")

        assert not errors, "unexpected results:\n" + "\n".join(errors)
        # The forced-reconnect path closes the WS session (max_retries=0),
        # so we only assert that all 50 connections received a SOCKS5 error
        # reply — the tunnel liveness-after-reconnect scenario is covered by
        # test_session_ends_when_ws_closes in test_scenarios.py.
