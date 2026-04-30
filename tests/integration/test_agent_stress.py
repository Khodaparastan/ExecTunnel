from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import sys
from pathlib import Path

import pytest

READY_FRAME = "<<<EXECTUNNEL:AGENT_READY>>>"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.agent,
    pytest.mark.slow,
]


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def agent_script(repo_root: Path) -> Path:
    path = repo_root / "exectunnel" / "payload" / "agent.py"
    assert path.exists(), f"agent script not found: {path}"
    return path


@pytest.fixture
def python_exe() -> str:
    return sys.executable


async def _spawn_agent(
    python_exe: str,
    agent_script: Path,
    *args: str,
) -> asyncio.subprocess.Process:
    env = os.environ.copy()
    env.setdefault("EXECTUNNEL_AGENT_LOG_LEVEL", "debug")
    return await asyncio.create_subprocess_exec(
        python_exe,
        str(agent_script),
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )


async def _read_stdout_line(
    proc: asyncio.subprocess.Process,
    timeout: float = 2.0,
) -> str:
    assert proc.stdout is not None
    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
    return raw.decode("utf-8", errors="replace").rstrip("\r\n")


async def _write_stdin(proc: asyncio.subprocess.Process, text: str) -> None:
    assert proc.stdin is not None
    proc.stdin.write(text.encode("utf-8"))
    await proc.stdin.drain()


async def _close_stdin(proc: asyncio.subprocess.Process) -> None:
    assert proc.stdin is not None
    proc.stdin.close()
    await proc.stdin.wait_closed()


async def _terminate(proc: asyncio.subprocess.Process, timeout: float = 3.0) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await _wait_process(proc, timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await _wait_process(proc, timeout=timeout)


async def _shutdown_agent(
    proc: asyncio.subprocess.Process, timeout: float = 3.0
) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(BrokenPipeError, ConnectionResetError, RuntimeError):
        await _close_stdin(proc)
    try:
        await _wait_process(proc, timeout=timeout)
    except TimeoutError:
        await _terminate(proc, timeout=timeout)


async def _drain_stream(stream: asyncio.StreamReader) -> list[str]:
    lines: list[str] = []
    try:
        while True:
            raw = await stream.readline()
            if not raw:
                break
            lines.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
    except Exception:
        return lines
    return lines


async def _await_drain(task: asyncio.Task[list[str]], timeout: float = 2.0) -> None:
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=timeout)


async def _wait_process(proc: asyncio.subprocess.Process, timeout: float) -> int:
    return await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=timeout)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = (4 - len(value) % 4) % 4
    return base64.urlsafe_b64decode(value + ("=" * padding))


def _conn_open(conn_id: str, host: str, port: int) -> str:
    return f"<<<EXECTUNNEL:CONN_OPEN:{conn_id}:{host}:{port}>>>\n"


def _conn_close(conn_id: str) -> str:
    return f"<<<EXECTUNNEL:CONN_CLOSE:{conn_id}>>>\n"


def _data(conn_id: str, payload: bytes) -> str:
    return f"<<<EXECTUNNEL:DATA:{conn_id}:{_b64url(payload)}>>>\n"


def _udp_open(flow_id: str, host: str, port: int) -> str:
    return f"<<<EXECTUNNEL:UDP_OPEN:{flow_id}:{host}:{port}>>>\n"


def _udp_close(flow_id: str) -> str:
    return f"<<<EXECTUNNEL:UDP_CLOSE:{flow_id}>>>\n"


def _udp_data(flow_id: str, payload: bytes) -> str:
    return f"<<<EXECTUNNEL:UDP_DATA:{flow_id}:{_b64url(payload)}>>>\n"


def _parse_conn_ack(line: str) -> str | None:
    prefix = "<<<EXECTUNNEL:CONN_ACK:"
    if not (line.startswith(prefix) and line.endswith(">>>")):
        return None
    return line.removeprefix(prefix).removesuffix(">>>")


def _parse_conn_close(line: str) -> str | None:
    prefix = "<<<EXECTUNNEL:CONN_CLOSE:"
    if not (line.startswith(prefix) and line.endswith(">>>")):
        return None
    return line.removeprefix(prefix).removesuffix(">>>")


def _parse_data_frame(line: str, conn_id: str) -> bytes | None:
    prefix = f"<<<EXECTUNNEL:DATA:{conn_id}:"
    if not (line.startswith(prefix) and line.endswith(">>>")):
        return None
    payload = line.removeprefix(prefix).removesuffix(">>>")
    return _b64url_decode(payload)


def _parse_udp_flow_id(line: str) -> str | None:
    if not line.startswith("<<<EXECTUNNEL:UDP_DATA:"):
        return None
    parts = line.split(":")
    if len(parts) < 4:
        return None
    return parts[2]


async def _start_tcp_echo_server() -> tuple[asyncio.AbstractServer, int]:
    async def handle_echo(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle_echo, "127.0.0.1", 0)
    sock = server.sockets[0]
    port = int(sock.getsockname()[1])
    return server, port


async def _start_udp_echo_server() -> tuple[asyncio.DatagramTransport, int]:
    class EchoProtocol(asyncio.DatagramProtocol):
        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            self.transport = transport

        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            self.transport.sendto(data, addr)

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        EchoProtocol,
        local_addr=("127.0.0.1", 0),
    )
    port = int(transport.get_extra_info("sockname")[1])
    return transport, port


async def _drain_until(
    proc: asyncio.subprocess.Process,
    predicate,
    *,
    timeout: float = 5.0,
    max_lines: int = 10000,
) -> list[str]:
    lines: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    for _ in range(max_lines):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        line = await _read_stdout_line(proc, timeout=remaining)
        lines.append(line)
        if predicate(line, lines):
            return lines
    raise AssertionError(f"predicate not satisfied; collected {len(lines)} lines")


async def test_agent_handles_many_small_tcp_round_trips(
    python_exe: str,
    agent_script: Path,
) -> None:
    server, port = await _start_tcp_echo_server()
    proc = await _spawn_agent(python_exe, agent_script)
    assert proc.stderr is not None
    stderr_task = asyncio.create_task(_drain_stream(proc.stderr))
    try:
        assert await _read_stdout_line(proc) == READY_FRAME

        total = 50
        conn_ids = [f"c{i:024x}"[:25] for i in range(1, total + 1)]

        for conn_id in conn_ids:
            await _write_stdin(proc, _conn_open(conn_id, "127.0.0.1", port))

        acked: set[str] = set()
        while len(acked) < total:
            line = await _read_stdout_line(proc, timeout=5.0)
            conn_id = _parse_conn_ack(line)
            if conn_id is not None:
                acked.add(conn_id)

        assert acked == set(conn_ids)

        for idx, conn_id in enumerate(conn_ids):
            payload = f"msg-{idx}".encode()
            await _write_stdin(proc, _data(conn_id, payload))

        echoed: set[str] = set()
        while len(echoed) < total:
            line = await _read_stdout_line(proc, timeout=5.0)
            if line.startswith("<<<EXECTUNNEL:DATA:"):
                parts = line.split(":")
                if len(parts) >= 4:
                    echoed.add(parts[2])

        assert echoed == set(conn_ids)

        for conn_id in conn_ids:
            await _write_stdin(proc, _conn_close(conn_id))

        closed: set[str] = set()
        while len(closed) < total:
            line = await _read_stdout_line(proc, timeout=5.0)
            conn_id = _parse_conn_close(line)
            if conn_id is not None:
                closed.add(conn_id)

        assert closed == set(conn_ids)
        assert proc.returncode is None
    finally:
        await _shutdown_agent(proc)
        server.close()
        await server.wait_closed()
        await _await_drain(stderr_task)


async def test_agent_handles_large_tcp_payload_burst(
    python_exe: str,
    agent_script: Path,
) -> None:
    server, port = await _start_tcp_echo_server()
    proc = await _spawn_agent(python_exe, agent_script)
    assert proc.stderr is not None
    stderr_task = asyncio.create_task(_drain_stream(proc.stderr))
    conn_id = "c" + "a" * 24
    try:
        assert await _read_stdout_line(proc) == READY_FRAME

        await _write_stdin(proc, _conn_open(conn_id, "127.0.0.1", port))
        assert await _read_stdout_line(proc, timeout=5.0) == (
            f"<<<EXECTUNNEL:CONN_ACK:{conn_id}>>>"
        )

        chunks = [b"x" * 4096 for _ in range(32)]
        for chunk in chunks:
            await _write_stdin(proc, _data(conn_id, chunk))

        expected_total = sum(len(chunk) for chunk in chunks)
        received_total = 0
        while received_total < expected_total:
            line = await _read_stdout_line(proc, timeout=10.0)
            data = _parse_data_frame(line, conn_id)
            if data is not None:
                received_total += len(data)

        assert received_total >= expected_total
        assert proc.returncode is None
    finally:
        await _shutdown_agent(proc)
        server.close()
        await server.wait_closed()
        await _await_drain(stderr_task)


async def test_agent_udp_many_datagrams_single_flow(
    python_exe: str,
    agent_script: Path,
) -> None:
    transport, port = await _start_udp_echo_server()
    proc = await _spawn_agent(python_exe, agent_script)
    assert proc.stderr is not None
    stderr_task = asyncio.create_task(_drain_stream(proc.stderr))
    flow_id = "u" + "b" * 24
    try:
        assert await _read_stdout_line(proc) == READY_FRAME

        await _write_stdin(proc, _udp_open(flow_id, "127.0.0.1", port))
        await asyncio.sleep(0.05)

        total = 100
        for i in range(total):
            await _write_stdin(proc, _udp_data(flow_id, f"dgram-{i}".encode()))

        received = 0
        while received < total:
            line = await _read_stdout_line(proc, timeout=10.0)
            if line.startswith(f"<<<EXECTUNNEL:UDP_DATA:{flow_id}:"):
                received += 1

        assert received == total

        await _write_stdin(proc, _udp_close(flow_id))
        close_line = await _read_stdout_line(proc, timeout=5.0)
        assert close_line == f"<<<EXECTUNNEL:UDP_CLOSE:{flow_id}>>>"
    finally:
        await _shutdown_agent(proc)
        transport.close()
        await _await_drain(stderr_task)


async def test_agent_udp_many_parallel_flows(
    python_exe: str,
    agent_script: Path,
) -> None:
    transport, port = await _start_udp_echo_server()
    proc = await _spawn_agent(python_exe, agent_script)
    assert proc.stderr is not None
    stderr_task = asyncio.create_task(_drain_stream(proc.stderr))
    try:
        assert await _read_stdout_line(proc) == READY_FRAME

        flow_ids = [f"u{i:024x}"[:25] for i in range(1, 21)]

        for flow_id in flow_ids:
            await _write_stdin(proc, _udp_open(flow_id, "127.0.0.1", port))

        await asyncio.sleep(0.1)

        for idx, flow_id in enumerate(flow_ids):
            await _write_stdin(proc, _udp_data(flow_id, f"udp-{idx}".encode()))

        seen: set[str] = set()
        while len(seen) < len(flow_ids):
            line = await _read_stdout_line(proc, timeout=10.0)
            flow_id = _parse_udp_flow_id(line)
            if flow_id is not None:
                seen.add(flow_id)

        assert seen == set(flow_ids)
    finally:
        await _shutdown_agent(proc)
        transport.close()
        await _await_drain(stderr_task)


async def test_agent_survives_malformed_frame_flood(
    python_exe: str,
    agent_script: Path,
) -> None:
    proc = await _spawn_agent(python_exe, agent_script)
    assert proc.stderr is not None
    stderr_task = asyncio.create_task(_drain_stream(proc.stderr))
    try:
        assert await _read_stdout_line(proc) == READY_FRAME

        for _ in range(500):
            await _write_stdin(proc, "not-a-frame\n")
            await _write_stdin(proc, "<<<EXECTUNNEL:BROKEN>>>\n")
            await _write_stdin(proc, "<<<EXECTUNNEL:DATA:badid:%%%>>>\n")
            await _write_stdin(proc, "<<<EXECTUNNEL:UDP_OPEN:badid:host:notaport>>>\n")

        await asyncio.sleep(0.5)
        assert proc.returncode is None
    finally:
        await _shutdown_agent(proc)
        await _await_drain(stderr_task)


async def test_agent_mixed_valid_and_invalid_traffic_keeps_working(
    python_exe: str,
    agent_script: Path,
) -> None:
    server, port = await _start_tcp_echo_server()
    proc = await _spawn_agent(python_exe, agent_script)
    assert proc.stderr is not None
    stderr_task = asyncio.create_task(_drain_stream(proc.stderr))
    conn_id = "c" + "d" * 24
    try:
        assert await _read_stdout_line(proc) == READY_FRAME

        await _write_stdin(proc, _conn_open(conn_id, "127.0.0.1", port))
        assert await _read_stdout_line(proc, timeout=5.0) == (
            f"<<<EXECTUNNEL:CONN_ACK:{conn_id}>>>"
        )

        for i in range(40):
            await _write_stdin(proc, "garbage\n")
            await _write_stdin(proc, _data(conn_id, f"ok-{i}".encode()))
            await _write_stdin(proc, f"<<<EXECTUNNEL:DATA:{conn_id}:%%%bad%%%>>>\n")

        sentinel = b"final-ok"
        await _write_stdin(proc, _data(conn_id, sentinel))

        echoed = bytearray()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 12.0
        while loop.time() < deadline:
            line = await _read_stdout_line(
                proc, timeout=max(0.1, deadline - loop.time())
            )
            data = _parse_data_frame(line, conn_id)
            if data is not None:
                echoed.extend(data)
            if sentinel in echoed:
                break

        assert sentinel in echoed
        assert proc.returncode is None
    finally:
        await _shutdown_agent(proc)
        server.close()
        await server.wait_closed()
        await _await_drain(stderr_task)


async def test_agent_shutdown_while_busy_tcp(
    python_exe: str,
    agent_script: Path,
) -> None:
    server, port = await _start_tcp_echo_server()
    proc = await _spawn_agent(python_exe, agent_script)
    assert proc.stderr is not None
    stderr_task = asyncio.create_task(_drain_stream(proc.stderr))
    assert proc.stdout is not None
    stdout_task: asyncio.Task[list[str]] | None = None
    conn_id = "c" + "e" * 24
    try:
        assert await _read_stdout_line(proc) == READY_FRAME
        await _write_stdin(proc, _conn_open(conn_id, "127.0.0.1", port))
        assert await _read_stdout_line(proc, timeout=5.0) == (
            f"<<<EXECTUNNEL:CONN_ACK:{conn_id}>>>"
        )

        stdout_task = asyncio.create_task(_drain_stream(proc.stdout))

        for _ in range(60):
            await _write_stdin(proc, _data(conn_id, b"x" * 4096))

        await _close_stdin(proc)
        rc = await _wait_process(proc, timeout=15.0)
        assert rc == 0
    finally:
        server.close()
        await server.wait_closed()
        await _terminate(proc)
        if stdout_task is not None:
            await _await_drain(stdout_task)
        await _await_drain(stderr_task)


async def test_agent_shutdown_while_busy_udp(
    python_exe: str,
    agent_script: Path,
) -> None:
    transport, port = await _start_udp_echo_server()
    proc = await _spawn_agent(python_exe, agent_script)
    assert proc.stderr is not None
    stderr_task = asyncio.create_task(_drain_stream(proc.stderr))
    flow_id = "u" + "f" * 24
    try:
        assert await _read_stdout_line(proc) == READY_FRAME
        await _write_stdin(proc, _udp_open(flow_id, "127.0.0.1", port))
        await asyncio.sleep(0.05)

        for _ in range(200):
            await _write_stdin(proc, _udp_data(flow_id, b"y" * 256))

        await _close_stdin(proc)
        rc = await _wait_process(proc, timeout=10.0)
        assert rc == 0
    finally:
        transport.close()
        await _terminate(proc)
        await _await_drain(stderr_task)


async def test_agent_control_plane_survives_data_pressure(
    python_exe: str,
    agent_script: Path,
) -> None:
    server, port = await _start_tcp_echo_server()
    proc = await _spawn_agent(python_exe, agent_script)
    assert proc.stderr is not None
    stderr_task = asyncio.create_task(_drain_stream(proc.stderr))
    conn_id = "c" + "9" * 24
    try:
        assert await _read_stdout_line(proc) == READY_FRAME
        await _write_stdin(proc, _conn_open(conn_id, "127.0.0.1", port))
        assert await _read_stdout_line(proc, timeout=5.0) == (
            f"<<<EXECTUNNEL:CONN_ACK:{conn_id}>>>"
        )

        for _ in range(30):
            await _write_stdin(proc, _data(conn_id, b"z" * 4096))

        await _write_stdin(proc, _conn_close(conn_id))

        lines = await _drain_until(
            proc,
            lambda line, _lines: line == f"<<<EXECTUNNEL:CONN_CLOSE:{conn_id}>>>",
            timeout=15.0,
        )

        assert lines[-1] == f"<<<EXECTUNNEL:CONN_CLOSE:{conn_id}>>>"
    finally:
        await _shutdown_agent(proc)
        server.close()
        await server.wait_closed()
        await _await_drain(stderr_task)
