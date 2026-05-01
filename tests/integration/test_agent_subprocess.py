from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
from contextlib import closing
from pathlib import Path

import pytest

READY_FRAME = "<<<EXECTUNNEL:AGENT_READY>>>"

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.agent]


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
    env_overrides: dict[str, str] | None = None,
) -> asyncio.subprocess.Process:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
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
    timeout: float = 1.0,
) -> str:
    assert proc.stdout is not None
    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
    return raw.decode("utf-8", errors="replace").rstrip("\r\n")


async def _read_stderr_line(
    proc: asyncio.subprocess.Process,
    timeout: float = 1.0,
) -> str:
    assert proc.stderr is not None
    raw = await asyncio.wait_for(proc.stderr.readline(), timeout=timeout)
    return raw.decode("utf-8", errors="replace").rstrip("\r\n")


async def _read_stderr_until_contains(
    proc: asyncio.subprocess.Process,
    needle: str,
    max_lines: int = 20,
    timeout: float = 2.0,
) -> str:
    assert proc.stderr is not None
    lines: list[str] = []
    for _ in range(max_lines):
        raw = await asyncio.wait_for(proc.stderr.readline(), timeout=timeout)
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        lines.append(line)
        if needle in line:
            return line
    raise AssertionError(f"did not find {needle!r} in stderr lines: {lines!r}")


async def _assert_no_stdout_line(
    proc: asyncio.subprocess.Process,
    timeout: float = 0.2,
) -> None:
    assert proc.stdout is not None
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)


async def _write_stdin(proc: asyncio.subprocess.Process, text: str) -> None:
    assert proc.stdin is not None
    proc.stdin.write(text.encode("utf-8"))
    await proc.stdin.drain()


async def _close_stdin(proc: asyncio.subprocess.Process) -> None:
    assert proc.stdin is not None
    proc.stdin.close()
    await proc.stdin.wait_closed()


async def _terminate(proc: asyncio.subprocess.Process, timeout: float = 2.0) -> None:
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=timeout)


async def _shutdown_agent(
    proc: asyncio.subprocess.Process, timeout: float = 2.0
) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(BrokenPipeError, ConnectionResetError, RuntimeError):
        await _close_stdin(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        await _terminate(proc, timeout=timeout)


async def _start_tcp_echo_server() -> tuple[asyncio.AbstractServer, int]:
    async def handle_echo(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                data = await reader.read(4096)
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
    transport, _protocol = await loop.create_datagram_endpoint(
        EchoProtocol,
        local_addr=("127.0.0.1", 0),
    )
    sockname = transport.get_extra_info("sockname")
    port = int(sockname[1])
    return transport, port


def _free_tcp_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


async def test_agent_emits_ready_on_startup(
    python_exe: str,
    agent_script: Path,
) -> None:
    proc = await _spawn_agent(python_exe, agent_script)
    try:
        assert await _read_stdout_line(proc) == READY_FRAME
    finally:
        await _shutdown_agent(proc)


async def test_agent_exits_cleanly_on_stdin_eof(
    python_exe: str,
    agent_script: Path,
) -> None:
    proc = await _spawn_agent(python_exe, agent_script)
    try:
        assert await _read_stdout_line(proc) == READY_FRAME
        await _close_stdin(proc)
        rc = await asyncio.wait_for(proc.wait(), timeout=2.0)
        assert rc == 0
    finally:
        await _terminate(proc)


async def test_agent_ignores_non_frame_noise(
    python_exe: str,
    agent_script: Path,
) -> None:
    proc = await _spawn_agent(python_exe, agent_script)
    try:
        assert await _read_stdout_line(proc) == READY_FRAME
        await _write_stdin(proc, "hello from shell noise\n")
        await _assert_no_stdout_line(proc)
        assert proc.returncode is None
    finally:
        await _shutdown_agent(proc)


async def test_agent_discards_keepalive_without_reply(
    python_exe: str,
    agent_script: Path,
) -> None:
    proc = await _spawn_agent(python_exe, agent_script)
    try:
        assert await _read_stdout_line(proc) == READY_FRAME
        await _write_stdin(proc, "<<<EXECTUNNEL:KEEPALIVE>>>\n")
        await _assert_no_stdout_line(proc)
        assert proc.returncode is None
    finally:
        await _shutdown_agent(proc)


async def test_agent_rejects_duplicate_conn_open(
    python_exe: str,
    agent_script: Path,
) -> None:
    server, port = await _start_tcp_echo_server()
    proc = await _spawn_agent(python_exe, agent_script)
    conn_id = "c" + "1" * 24
    try:
        assert await _read_stdout_line(proc) == READY_FRAME

        await _write_stdin(
            proc,
            f"<<<EXECTUNNEL:CONN_OPEN:{conn_id}:127.0.0.1:{port}>>>\n",
        )
        assert await _read_stdout_line(proc, timeout=2.0) == (
            f"<<<EXECTUNNEL:CONN_ACK:{conn_id}>>>"
        )

        await _write_stdin(
            proc,
            f"<<<EXECTUNNEL:CONN_OPEN:{conn_id}:127.0.0.1:{port}>>>\n",
        )
        second = await _read_stdout_line(proc, timeout=2.0)
        assert second.startswith(f"<<<EXECTUNNEL:ERROR:{conn_id}:")
    finally:
        await _shutdown_agent(proc)
        server.close()
        await server.wait_closed()


async def test_agent_invalid_data_base64_emits_error(
    python_exe: str,
    agent_script: Path,
) -> None:
    server, port = await _start_tcp_echo_server()
    proc = await _spawn_agent(python_exe, agent_script)
    conn_id = "c" + "2" * 24
    try:
        assert await _read_stdout_line(proc) == READY_FRAME

        await _write_stdin(
            proc,
            f"<<<EXECTUNNEL:CONN_OPEN:{conn_id}:127.0.0.1:{port}>>>\n",
        )
        assert await _read_stdout_line(proc, timeout=2.0) == (
            f"<<<EXECTUNNEL:CONN_ACK:{conn_id}>>>"
        )

        await _write_stdin(
            proc,
            f"<<<EXECTUNNEL:DATA:{conn_id}:a>>>\n",
        )
        line = await _read_stdout_line(proc, timeout=2.0)
        assert line.startswith(f"<<<EXECTUNNEL:ERROR:{conn_id}:")
    finally:
        await _shutdown_agent(proc)
        server.close()
        await server.wait_closed()


async def test_agent_tcp_echo_round_trip(
    python_exe: str,
    agent_script: Path,
) -> None:
    server, port = await _start_tcp_echo_server()
    proc = await _spawn_agent(python_exe, agent_script)
    conn_id = "c" + "3" * 24
    try:
        assert await _read_stdout_line(proc) == READY_FRAME

        await _write_stdin(
            proc,
            f"<<<EXECTUNNEL:CONN_OPEN:{conn_id}:127.0.0.1:{port}>>>\n",
        )
        assert await _read_stdout_line(proc, timeout=2.0) == (
            f"<<<EXECTUNNEL:CONN_ACK:{conn_id}>>>"
        )

        await _write_stdin(proc, f"<<<EXECTUNNEL:DATA:{conn_id}:aGVsbG8>>>\n")
        assert await _read_stdout_line(proc, timeout=2.0) == (
            f"<<<EXECTUNNEL:DATA:{conn_id}:aGVsbG8>>>"
        )

        await _write_stdin(proc, f"<<<EXECTUNNEL:CONN_CLOSE:{conn_id}>>>\n")
        assert await _read_stdout_line(proc, timeout=2.0) == (
            f"<<<EXECTUNNEL:CONN_CLOSE:{conn_id}>>>"
        )
    finally:
        await _shutdown_agent(proc)
        server.close()
        await server.wait_closed()


async def test_agent_udp_echo_round_trip(
    python_exe: str,
    agent_script: Path,
) -> None:
    transport, port = await _start_udp_echo_server()
    proc = await _spawn_agent(python_exe, agent_script)
    flow_id = "u" + "4" * 24
    try:
        assert await _read_stdout_line(proc) == READY_FRAME

        await _write_stdin(
            proc,
            f"<<<EXECTUNNEL:UDP_OPEN:{flow_id}:127.0.0.1:{port}>>>\n",
        )
        await asyncio.sleep(0.05)

        await _write_stdin(proc, f"<<<EXECTUNNEL:UDP_DATA:{flow_id}:cGluZw>>>\n")
        assert await _read_stdout_line(proc, timeout=2.0) == (
            f"<<<EXECTUNNEL:UDP_DATA:{flow_id}:cGluZw>>>"
        )

        await _write_stdin(proc, f"<<<EXECTUNNEL:UDP_CLOSE:{flow_id}>>>\n")
        assert await _read_stdout_line(proc, timeout=2.0) == (
            f"<<<EXECTUNNEL:UDP_CLOSE:{flow_id}>>>"
        )
    finally:
        transport.close()
        await _shutdown_agent(proc)


async def test_agent_duplicate_udp_open_emits_udp_close(
    python_exe: str,
    agent_script: Path,
) -> None:
    transport, port = await _start_udp_echo_server()
    proc = await _spawn_agent(python_exe, agent_script)
    flow_id = "u" + "5" * 24
    try:
        assert await _read_stdout_line(proc) == READY_FRAME

        await _write_stdin(
            proc,
            f"<<<EXECTUNNEL:UDP_OPEN:{flow_id}:127.0.0.1:{port}>>>\n",
        )
        await asyncio.sleep(0.05)

        await _write_stdin(
            proc,
            f"<<<EXECTUNNEL:UDP_OPEN:{flow_id}:127.0.0.1:{port}>>>\n",
        )
        assert await _read_stdout_line(proc, timeout=2.0) == (
            f"<<<EXECTUNNEL:UDP_CLOSE:{flow_id}>>>"
        )
    finally:
        transport.close()
        await _shutdown_agent(proc)


async def test_agent_portforward_mode_ready(
    python_exe: str,
    agent_script: Path,
) -> None:
    port = _free_tcp_port()
    proc = await _spawn_agent(python_exe, agent_script, "127.0.0.1", str(port))
    try:
        assert await _read_stdout_line(proc) == READY_FRAME
    finally:
        await _shutdown_agent(proc)


async def test_agent_portforward_mode_conn_open_without_host_port(
    python_exe: str,
    agent_script: Path,
) -> None:
    server, port = await _start_tcp_echo_server()
    proc = await _spawn_agent(python_exe, agent_script, "127.0.0.1", str(port))
    conn_id = "c" + "6" * 24
    try:
        assert await _read_stdout_line(proc) == READY_FRAME

        await _write_stdin(proc, f"<<<EXECTUNNEL:CONN_OPEN:{conn_id}>>>\n")
        assert await _read_stdout_line(proc, timeout=2.0) == (
            f"<<<EXECTUNNEL:CONN_ACK:{conn_id}>>>"
        )
    finally:
        await _shutdown_agent(proc)
        server.close()
        await server.wait_closed()


async def test_agent_invalid_cli_port_exits_nonzero(
    python_exe: str,
    agent_script: Path,
) -> None:
    proc = await _spawn_agent(python_exe, agent_script, "127.0.0.1", "not-a-port")
    stderr_line = await _read_stderr_line(proc, timeout=2.0)
    rc = await asyncio.wait_for(proc.wait(), timeout=2.0)

    assert rc == 1
    assert "invalid port" in stderr_line


async def test_agent_invalid_cli_usage_exits_nonzero(
    python_exe: str,
    agent_script: Path,
) -> None:
    proc = await _spawn_agent(python_exe, agent_script, "only-one-arg")
    stderr_line = await _read_stderr_line(proc, timeout=2.0)
    rc = await asyncio.wait_for(proc.wait(), timeout=2.0)

    assert rc == 1
    # The agent prints its banner using its installed entry-point name
    # (``exectunnel_agent.py``); see ``exectunnel/payload/agent.py``'s
    # ``_usage_and_exit`` helper. We assert on the stable ``usage:`` prefix
    # plus the executable stem so a future ``argv[0]`` tweak surfaces here.
    assert "usage: exectunnel_agent.py" in stderr_line


async def test_agent_logs_stdin_eof_to_stderr(
    python_exe: str,
    agent_script: Path,
) -> None:
    proc = await _spawn_agent(
        python_exe,
        agent_script,
        env_overrides={"EXECTUNNEL_AGENT_LOG_LEVEL": "info"},
    )
    try:
        assert await _read_stdout_line(proc) == READY_FRAME
        await _close_stdin(proc)

        stderr_line = await _read_stderr_until_contains(proc, "stdin EOF")
        rc = await asyncio.wait_for(proc.wait(), timeout=2.0)

        assert rc == 0
        assert "stdin EOF" in stderr_line
    finally:
        await _terminate(proc)
