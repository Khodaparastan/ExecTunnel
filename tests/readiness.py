from __future__ import annotations

import asyncio
import collections
import importlib.util
import logging
import ssl
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _ensure_namespace_package(name: str, path: Path) -> None:
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = module


def _load_module(module_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / rel_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module {module_name!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_ensure_namespace_package("exectunnel", ROOT / "exectunnel")
_ensure_namespace_package("exectunnel.proxy", ROOT / "exectunnel" / "proxy")
_ensure_namespace_package("exectunnel.protocol", ROOT / "exectunnel" / "protocol")
_ensure_namespace_package("exectunnel.transport", ROOT / "exectunnel" / "transport")
_ensure_namespace_package("exectunnel.cli", ROOT / "exectunnel" / "cli")
_ensure_namespace_package("exectunnel.session", ROOT / "exectunnel" / "session")
_ensure_namespace_package(
    "exectunnel.observability",
    ROOT / "exectunnel" / "observability",
)

defaults_mod = _load_module("exectunnel.defaults", "exectunnel/defaults.py")
exceptions_mod = _load_module("exectunnel.exceptions", "exectunnel/exceptions.py")

proxy_constants_mod = _load_module(
    "exectunnel.proxy._constants",
    "exectunnel/proxy/_constants.py",
)
proxy_config_mod = _load_module("exectunnel.proxy.config", "exectunnel/proxy/config.py")
_load_module("exectunnel.observability.tracing", "exectunnel/observability/tracing.py")
_load_module("exectunnel.observability.utils", "exectunnel/observability/utils.py")
obs_metrics_mod = _load_module(
    "exectunnel.observability.metrics",
    "exectunnel/observability/metrics.py",
)
obs_exporters_mod = _load_module(
    "exectunnel.observability.exporters",
    "exectunnel/observability/exporters.py",
)
obs_logging_mod = _load_module(
    "exectunnel.observability.logging",
    "exectunnel/observability/logging.py",
)
_load_module(
    "exectunnel.observability.reporter", "exectunnel/observability/reporter.py"
)
_load_module("exectunnel.observability", "exectunnel/observability/__init__.py")
_load_module("exectunnel.protocol.enums", "exectunnel/protocol/enums.py")
protocol_frames_mod = _load_module(
    "exectunnel.protocol.frames",
    "exectunnel/protocol/frames.py",
)
_load_module("exectunnel.protocol.ids", "exectunnel/protocol/ids.py")
_load_module("exectunnel.protocol", "exectunnel/protocol/__init__.py")
supervisor_mod = _load_module(
    "exectunnel.cli.supervisor", "exectunnel/cli/supervisor.py"
)
cli_metrics_mod = _load_module("exectunnel.cli.metrics", "exectunnel/cli/metrics.py")
proxy_udp_relay_mod = _load_module(
    "exectunnel.proxy.udp_relay", "exectunnel/proxy/udp_relay.py"
)
transport_tcp_mod = _load_module(
    "exectunnel.transport.tcp", "exectunnel/transport/tcp.py"
)
transport_udp_mod = _load_module(
    "exectunnel.transport.udp", "exectunnel/transport/udp.py"
)
_load_module("exectunnel.transport", "exectunnel/transport/__init__.py")
proxy_server_mod = _load_module("exectunnel.proxy.server", "exectunnel/proxy/server.py")
proxy_request_mod = _load_module(
    "exectunnel.proxy.request", "exectunnel/proxy/request.py"
)
proxy_pkg = sys.modules["exectunnel.proxy"]
setattr(proxy_pkg, "Socks5Request", proxy_request_mod.Socks5Request)
setattr(proxy_pkg, "Socks5Server", proxy_server_mod.Socks5Server)
setattr(proxy_pkg, "Socks5ServerConfig", proxy_config_mod.Socks5ServerConfig)
setattr(proxy_pkg, "UdpRelay", proxy_udp_relay_mod.UdpRelay)
_load_module("exectunnel.session._routing", "exectunnel/session/_routing.py")
session_config_mod = _load_module(
    "exectunnel.session._config", "exectunnel/session/_config.py"
)
_load_module("exectunnel.session._payload", "exectunnel/session/_payload.py")
session_bootstrap_mod = _load_module(
    "exectunnel.session._bootstrap", "exectunnel/session/_bootstrap.py"
)
session_sender_mod = _load_module(
    "exectunnel.session._sender", "exectunnel/session/_sender.py"
)
session_dns_mod = _load_module("exectunnel.session._dns", "exectunnel/session/_dns.py")
session_state_mod = _load_module(
    "exectunnel.session._state", "exectunnel/session/_state.py"
)
session_dispatcher_mod = _load_module(
    "exectunnel.session._dispatcher",
    "exectunnel/session/_dispatcher.py",
)
session_session_mod = _load_module(
    "exectunnel.session.session", "exectunnel/session/session.py"
)
session_pkg = sys.modules["exectunnel.session"]
setattr(session_pkg, "SessionConfig", session_config_mod.SessionConfig)
setattr(session_pkg, "TunnelConfig", session_config_mod.TunnelConfig)
setattr(
    session_pkg,
    "get_default_exclusion_networks",
    session_config_mod.get_default_exclusion_networks,
)
cli_config_mod = _load_module("exectunnel.cli._config", "exectunnel/cli/_config.py")

Defaults = defaults_mod.Defaults
ExecTunnelError = exceptions_mod.ExecTunnelError
FrameDecodingError = exceptions_mod.FrameDecodingError
BootstrapError = exceptions_mod.BootstrapError
ConnectionClosedError = exceptions_mod.ConnectionClosedError
TransportError = exceptions_mod.TransportError
Socks5ServerConfig = proxy_config_mod.Socks5ServerConfig
build_exporters = obs_exporters_mod.build_exporters
_JsonLogFormatter = obs_logging_mod._JsonLogFormatter
UdpRelay = proxy_udp_relay_mod.UdpRelay
TcpConnection = transport_tcp_mod.TcpConnection
UdpFlow = transport_udp_mod.UdpFlow
Socks5Server = proxy_server_mod.Socks5Server
DnsForwarder = session_dns_mod.DnsForwarder
AgentBootstrapper = session_bootstrap_mod.AgentBootstrapper
WsSender = session_sender_mod.WsSender
SessionConfig = session_config_mod.SessionConfig
TunnelConfig = session_config_mod.TunnelConfig
RequestDispatcher = session_dispatcher_mod.RequestDispatcher
AckStatus = session_state_mod.AckStatus
TunnelSession = session_session_mod.TunnelSession
TunnelMetrics = supervisor_mod.TunnelMetrics
TunnelSpec = supervisor_mod.TunnelSpec
TunnelWorker = supervisor_mod.TunnelWorker
ManagerConfig = supervisor_mod.ManagerConfig
HealthMonitor = cli_metrics_mod.HealthMonitor
MAX_UDP_PAYLOAD_BYTES = proxy_constants_mod.MAX_UDP_PAYLOAD_BYTES


def test_defaults_expose_constants_as_class_attributes() -> None:
    assert isinstance(Defaults.WS_RECONNECT_MAX_RETRIES, int)
    assert isinstance(Defaults.WS_RECONNECT_BASE_DELAY_SECS, float)


def test_exec_tunnel_error_to_dict_omits_placeholder_traceback() -> None:
    payload = ExecTunnelError("boom").to_dict()
    assert payload["traceback"] is None


def test_socks5_config_url_brackets_ipv6_literal() -> None:
    cfg = Socks5ServerConfig(host="2001:db8::1", port=1080)
    assert cfg.url == "tcp://[2001:db8::1]:1080"


@pytest.mark.asyncio
async def test_file_exporter_uses_compact_utf8_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "obs.jsonl"
    monkeypatch.setenv("EXECTUNNEL_OBS_EXPORTERS", "file")
    monkeypatch.setenv("EXECTUNNEL_OBS_FILE_PATH", str(out))

    exporters = build_exporters(logging.getLogger("test.obs"), log_emit=lambda *_: None)
    assert len(exporters) == 1

    payload: dict[str, object] = {
        "message": "привет",
        "metrics": {"alpha": 1, "beta": 2},
    }
    await exporters[0].emit(payload)

    line = out.read_text(encoding="utf-8").strip()
    assert "\\u" not in line
    assert ": " not in line
    assert ", " not in line


def test_json_log_formatter_uses_compact_utf8_json() -> None:
    formatter = _JsonLogFormatter()
    record = logging.LogRecord(
        name="exectunnel.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="مرحبا",
        args=(),
        exc_info=None,
    )

    rendered = formatter.format(record)
    assert "\\u" not in rendered
    assert ": " not in rendered
    assert ", " not in rendered


def test_logging_all_exports_install_ring_buffer() -> None:
    assert "install_ring_buffer" in obs_logging_mod.__all__


def test_metrics_unregister_listener_is_idempotent() -> None:
    calls: list[tuple[str, int]] = []

    def _listener(metric: str, **_tags: object) -> None:
        calls.append((metric, int(_tags.get("value", 0))))

    obs_metrics_mod.unregister_all_listeners()
    obs_metrics_mod.register_metric_listener(_listener)
    # Duplicate registration should be ignored.
    obs_metrics_mod.register_metric_listener(_listener)

    obs_metrics_mod.metrics_inc("test.listener.once", value=3)
    assert calls == [("test.listener.once", 3)]

    obs_metrics_mod.unregister_metric_listener(_listener)
    obs_metrics_mod.metrics_inc("test.listener.twice")
    assert calls == [("test.listener.once", 3)]

    # Should be safe when called repeatedly.
    obs_metrics_mod.unregister_metric_listener(_listener)
    obs_metrics_mod.unregister_all_listeners()


def test_install_ring_buffer_is_idempotent() -> None:
    logger = logging.getLogger("exectunnel")
    old_level = logger.level
    logger.setLevel(logging.DEBUG)

    before = sum(
        1
        for handler in logger.handlers
        if getattr(handler, "_exectunnel_ring_buffer", False)
    )

    try:
        first = obs_logging_mod.install_ring_buffer(maxlen=64, level=logging.INFO)
        after_first = sum(
            1
            for handler in logger.handlers
            if getattr(handler, "_exectunnel_ring_buffer", False)
        )
        logger.warning("ring-buffer-entry")
        assert first.entries()

        second = obs_logging_mod.install_ring_buffer(maxlen=128, level=logging.DEBUG)
        after_second = sum(
            1
            for handler in logger.handlers
            if getattr(handler, "_exectunnel_ring_buffer", False)
        )

        assert first is second
        assert after_first >= max(before, 1)
        assert after_second == after_first
        assert second.entries() == []
    finally:
        logger.setLevel(old_level)


def test_supervisor_tunnel_metrics_from_dict_coerces_types() -> None:
    metrics = TunnelMetrics.from_dict({
        "connected": 1,
        "frames_sent": "3",
        "uptime_secs": "2.5",
        "reconnect_delay_avg": "1.25",
        "tcp_total": "not-an-int",
        "unknown": "ignored",
    })

    assert metrics.connected is True
    assert metrics.frames_sent == 3
    assert metrics.uptime_secs == 2.5
    assert metrics.reconnect_delay_avg == 1.25
    assert metrics.tcp_total == 0


def test_supervisor_tunnel_metrics_from_dict_parses_bool_strings() -> None:
    metrics = TunnelMetrics.from_dict({
        "connected": "false",
        "bootstrap_ok": "1",
        "socks_ok": "0",
        "dns_enabled": "true",
    })

    assert metrics.connected is False
    assert metrics.bootstrap_ok is True
    assert metrics.socks_ok is False
    assert metrics.dns_enabled is True


def test_supervisor_worker_build_cmd_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = TunnelSpec(
        index=1,
        wss_url="wss://example.test/ws",
        socks_port=18080,
        insecure=True,
        log_level="TRACE",
        env_overrides={
            "EXECTUNNEL_WSS_URL": "wss://ignored.invalid/ws",
            "EXECTUNNEL_TOKEN": "token-123",
            "EXECTUNNEL_PING_INTERVAL": "17",
        },
    )
    cfg = ManagerConfig(tunnels=[spec])
    worker = TunnelWorker(spec=spec, cfg=cfg, on_state_change=lambda _state: None)

    cmd = worker._build_cmd()
    log_level_idx = cmd.index("--log-level")
    assert cmd[log_level_idx + 1] == "info"
    assert "--insecure" in cmd

    monkeypatch.setenv("EXECTUNNEL_LOG_LEVEL", "debug")
    env = worker._build_env()
    assert env["EXECTUNNEL_WSS_URL"] == "wss://example.test/ws"
    assert env["WSS_INSECURE"] == "1"
    assert env["EXECTUNNEL_METRICS_REPORT"] == "1"
    assert env["EXECTUNNEL_LOG_LEVEL"] == "TRACE"
    assert env["EXECTUNNEL_TOKEN"] == "token-123"
    assert env["EXECTUNNEL_PING_INTERVAL"] == "17"


def test_supervisor_parse_env_file_handles_comments_quotes_and_malformed(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "manager.env"
    env_file.write_text(
        "\n".join([
            "export WSS_URL_1=wss://example.test/ws # trailing comment",
            "LABEL_1='prod tunnel'",
            'LOG_LEVEL_1="warning"',
            "BROKEN_LINE",
        ]),
        encoding="utf-8",
    )

    parsed = supervisor_mod._parse_env_file(env_file)
    assert parsed["WSS_URL_1"] == "wss://example.test/ws"
    assert parsed["LABEL_1"] == "prod tunnel"
    assert parsed["LOG_LEVEL_1"] == "warning"
    assert "BROKEN_LINE" not in parsed


def test_supervisor_load_manager_config_validates_tunnel_log_levels(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "manager.env"
    env_file.write_text(
        "\n".join([
            "WSS_URL_1=wss://one.example/ws",
            "WSS_URL_2=wss://two.example/ws",
            "LOG_LEVEL=TRACE",
            "LOG_LEVEL_1=BOGUS",
        ]),
        encoding="utf-8",
    )

    cfg = supervisor_mod._load_manager_config(env_file, log_level="INFO", tui=False)

    assert cfg.tunnels[0].log_level == "info"
    assert cfg.tunnels[1].log_level == "info"


def test_supervisor_load_manager_config_rejects_invalid_wss_scheme(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "manager-invalid-scheme.env"
    env_file.write_text(
        "WSS_URL_1=https://example.invalid/ws\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must start with ws:// or wss://"):
        supervisor_mod._load_manager_config(env_file, log_level="INFO", tui=False)


def test_supervisor_load_manager_config_rejects_duplicate_socks_ports(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "manager-duplicate-ports.env"
    env_file.write_text(
        "\n".join([
            "WSS_URL_1=wss://one.example/ws",
            "WSS_URL_2=wss://two.example/ws",
            "SOCKS_PORT_1=18080",
            "SOCKS_PORT_2=18080",
        ]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate SOCKS port"):
        supervisor_mod._load_manager_config(env_file, log_level="INFO", tui=False)


def test_supervisor_load_manager_config_builds_env_overrides(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "manager-overrides.env"
    env_file.write_text(
        "\n".join([
            "WSS_URL_1=wss://one.example/ws",
            "LOG_LEVEL=warning",
            "LOG_LEVEL_1=debug",
            "WSS_INSECURE=0",
            "WSS_INSECURE_1=1",
            "EXECTUNNEL_TOKEN=global-token",
            "EXECTUNNEL_TOKEN_1=tunnel-token",
            "EXECTUNNEL_PING_INTERVAL=29",
            "EXECTUNNEL_WSS_URL_1=wss://override.example/ws",
        ]),
        encoding="utf-8",
    )

    cfg = supervisor_mod._load_manager_config(env_file, log_level="INFO", tui=False)
    spec = cfg.tunnels[0]

    assert cfg.raw_env["EXECTUNNEL_TOKEN"] == "global-token"
    assert spec.env_overrides["EXECTUNNEL_TOKEN"] == "tunnel-token"
    assert spec.env_overrides["EXECTUNNEL_PING_INTERVAL"] == "29"
    assert spec.env_overrides["EXECTUNNEL_WSS_URL"] == "wss://override.example/ws"
    assert spec.env_overrides["WSS_INSECURE"] == "1"
    assert spec.env_overrides["EXECTUNNEL_LOG_LEVEL"] == "debug"


def test_supervisor_emit_provides_snapshot_copy() -> None:
    spec = TunnelSpec(index=1, wss_url="wss://example/ws", socks_port=1080)
    cfg = ManagerConfig(tunnels=[spec])
    captured: list[object] = []
    worker = TunnelWorker(spec=spec, cfg=cfg, on_state_change=captured.append)

    worker._state.status = "starting"
    worker._emit()
    worker._state.status = "running"

    assert len(captured) == 1
    assert captured[0] is not worker._state
    assert captured[0].status == "starting"


@pytest.mark.asyncio
async def test_supervisor_drain_output_emits_state_on_non_metrics_line() -> None:
    spec = TunnelSpec(index=1, wss_url="wss://example/ws", socks_port=1080)
    cfg = ManagerConfig(tunnels=[spec])
    captured: list[object] = []
    worker = TunnelWorker(spec=spec, cfg=cfg, on_state_change=captured.append)

    class _FakeStdout:
        def __init__(self) -> None:
            self._lines = iter([b"plain child log\n"])

        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            try:
                return next(self._lines)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        async def readline(self) -> bytes:
            return b""

    class _FakeProc:
        stdout = _FakeStdout()

    await worker._drain_output(_FakeProc())

    assert captured
    assert captured[-1].log_lines[-1] == "plain child log"


@pytest.mark.asyncio
async def test_dispatcher_ack_health_threshold_requests_reconnect() -> None:
    reconnect_reasons: list[str] = []

    async def _ws_send(_frame: str) -> None:
        return

    async def _request_reconnect(reason: str) -> None:
        reconnect_reasons.append(reason)

    dispatcher = RequestDispatcher(
        tun_cfg=TunnelConfig(
            ack_timeout_reconnect_threshold=1,
            ack_timeout_window_secs=30.0,
        ),
        ws_send=_ws_send,
        ws_closed=asyncio.Event(),
        tcp_registry={},
        pending_connects={},
        udp_registry={},
        pre_ack_buffer_cap_bytes=1024,
        request_reconnect=_request_reconnect,
    )
    dispatcher._record_ack_failure(
        "conn-1",
        AckStatus.TIMEOUT,
        host="example.test",
        port=443,
    )

    await asyncio.sleep(0)
    dispatcher.close()

    assert reconnect_reasons == ["ack_health:timeout"]


@pytest.mark.asyncio
async def test_tunnel_session_request_reconnect_closes_active_websocket() -> None:
    class _FakeWs:
        def __init__(self) -> None:
            self.calls: list[tuple[int, str]] = []

        async def close(self, *, code: int, reason: str) -> None:
            self.calls.append((code, reason))

    session = TunnelSession(
        SessionConfig(wss_url="ws://example.test/ws"), TunnelConfig()
    )
    ws = _FakeWs()
    session._ws = ws

    await session._request_reconnect("ack_health:timeout")

    assert ws.calls == [(Defaults.WS_CLOSE_CODE_UNHEALTHY, "ack_health:timeout")]


@pytest.mark.asyncio
async def test_supervisor_restart_count_increments_before_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = TunnelSpec(index=1, wss_url="wss://example/ws", socks_port=1080)
    cfg = ManagerConfig(tunnels=[spec], restart_delay=0.1)
    worker = TunnelWorker(spec=spec, cfg=cfg, on_state_change=lambda _state: None)

    async def _fake_run_once(self: TunnelWorker, stop_event: asyncio.Event) -> None:
        return

    seen_during_sleep: list[int] = []
    stop_event = asyncio.Event()

    async def _fake_sleep(_delay: float) -> None:
        seen_during_sleep.append(worker._state.restart_count)
        stop_event.set()

    monkeypatch.setattr(TunnelWorker, "_run_once", _fake_run_once)
    monkeypatch.setattr(supervisor_mod.asyncio, "sleep", _fake_sleep)

    await worker.run_forever(stop_event)

    assert seen_during_sleep
    assert seen_during_sleep[0] == 1


@pytest.mark.asyncio
async def test_socks5_server_start_does_not_mark_started_on_bind_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Socks5Server(Socks5ServerConfig(host="127.0.0.1", port=1080))

    async def _raise_bind_error(*_args: object, **_kwargs: object) -> object:
        raise OSError("address already in use")

    monkeypatch.setattr(proxy_server_mod.asyncio, "start_server", _raise_bind_error)

    with pytest.raises(TransportError):
        await server.start()

    assert server._started is False


@pytest.mark.asyncio
async def test_dns_forwarder_start_does_not_mark_started_on_bind_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _ws_send(_line: str) -> None:
        return

    class _FakeLoop:
        async def create_datagram_endpoint(
            self,
            _factory: object,
            *,
            local_addr: tuple[str, int],
        ) -> tuple[object, object]:
            _ = local_addr
            raise OSError("address already in use")

    monkeypatch.setattr(
        session_dns_mod.asyncio,
        "get_running_loop",
        lambda: _FakeLoop(),
    )

    forwarder = DnsForwarder(
        local_port=54545,
        upstream="8.8.8.8",
        ws_send=_ws_send,
        udp_registry={},
    )

    with pytest.raises(TransportError):
        await forwarder.start()

    assert forwarder._started is False


@pytest.mark.asyncio
async def test_dns_forward_query_does_not_double_decrement_udp_flow_gauge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    obs_metrics_mod.metrics_reset()

    async def _ws_send(_line: str) -> None:
        return

    class _FakeTransport:
        def is_closing(self) -> bool:
            return False

        def sendto(self, _data: bytes, _addr: tuple[str, int]) -> None:
            return

    class _FakeUdpFlow:
        def __init__(self, registry: dict[str, object], fid: str) -> None:
            self.is_closed = False
            self._registry = registry
            self._flow_id = fid

        async def open(self) -> None:
            return

        async def send_datagram(self, _query: bytes) -> None:
            return

        async def recv_datagram(self) -> bytes | None:
            return b"resp"

        async def close(self) -> None:
            self.is_closed = True
            self._registry.pop(self._flow_id, None)
            obs_metrics_mod.metrics_gauge_dec("session.active.udp_flows")

        def on_remote_closed(self) -> None:
            self.is_closed = True
            self._registry.pop(self._flow_id, None)
            obs_metrics_mod.metrics_gauge_dec("session.active.udp_flows")

    udp_registry: dict[str, object] = {}
    forwarder = DnsForwarder(
        local_port=5353,
        upstream="8.8.8.8",
        ws_send=_ws_send,
        udp_registry=udp_registry,  # type: ignore[arg-type]
    )
    forwarder._transport = _FakeTransport()  # type: ignore[assignment]

    flow_id = "u0123456789abcdef01234567"

    def _make_flow(*_args: object, **_kwargs: object) -> _FakeUdpFlow:
        return _FakeUdpFlow(udp_registry, flow_id)

    monkeypatch.setattr(session_dns_mod, "UdpFlow", _make_flow)

    await forwarder._forward_query(b"query", ("127.0.0.1", 53000), flow_id)

    snap = obs_metrics_mod.metrics_snapshot()
    assert snap.get("session.active.udp_flows", 0.0) == 0.0


class _FakeBootstrapWs:
    def __init__(self, chunks_by_iter: list[list[str | bytes]]) -> None:
        self._chunks_by_iter = collections.deque(chunks_by_iter)
        self._active: collections.deque[str | bytes] = collections.deque()
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    def __aiter__(self) -> _FakeBootstrapWs:
        if self._chunks_by_iter:
            self._active = collections.deque(self._chunks_by_iter.popleft())
        else:
            self._active = collections.deque()
        return self

    async def __anext__(self) -> str | bytes:
        if not self._active:
            raise StopAsyncIteration
        return self._active.popleft()


@pytest.mark.asyncio
async def test_bootstrap_resolve_remote_python_uses_first_available_candidate() -> None:
    marker = f"{session_bootstrap_mod._FENCE_PREFIX}:PY:python3"
    ws = _FakeBootstrapWs(chunks_by_iter=[["noise\n"], [f"{marker}\n"]])

    bootstrapper = object.__new__(AgentBootstrapper)
    bootstrapper._ws = ws
    bootstrapper._cfg = SessionConfig(wss_url="wss://example/ws")
    bootstrapper._diag = collections.deque(maxlen=8)
    bootstrapper._start = asyncio.get_running_loop().time()
    bootstrapper._stash_lines = []
    bootstrapper._stash_carry = ""

    selected = await bootstrapper._resolve_remote_python()

    assert selected == "python3"
    assert any("command -v python3.12" in cmd for cmd in ws.sent)
    assert any("command -v python3 >/dev/null 2>&1" in cmd for cmd in ws.sent)
    assert "noise" in list(bootstrapper._diag)


@pytest.mark.asyncio
async def test_bootstrap_resolve_remote_python_caps_stash_lines() -> None:
    noise_lines = [
        f"noise-{i}\n" for i in range(session_bootstrap_mod._MAX_STASH_LINES + 50)
    ]
    marker = f"{session_bootstrap_mod._FENCE_PREFIX}:PY:python3"
    ws = _FakeBootstrapWs(chunks_by_iter=[noise_lines, [f"{marker}\n"]])

    bootstrapper = object.__new__(AgentBootstrapper)
    bootstrapper._ws = ws
    bootstrapper._cfg = SessionConfig(wss_url="wss://example/ws")
    bootstrapper._diag = collections.deque(maxlen=8)
    bootstrapper._start = asyncio.get_running_loop().time()
    bootstrapper._stash_lines = []
    bootstrapper._stash_carry = ""

    selected = await bootstrapper._resolve_remote_python()

    assert selected == "python3"
    assert len(bootstrapper._stash_lines) == session_bootstrap_mod._MAX_STASH_LINES
    assert bootstrapper._stash_lines[0] == "noise-50"


@pytest.mark.asyncio
async def test_bootstrap_resolve_remote_python_raises_clear_error_when_missing() -> (
    None
):
    ws = _FakeBootstrapWs(chunks_by_iter=[[], [], []])

    bootstrapper = object.__new__(AgentBootstrapper)
    bootstrapper._ws = ws
    bootstrapper._cfg = SessionConfig(wss_url="wss://example/ws")
    bootstrapper._diag = collections.deque(maxlen=8)
    bootstrapper._start = asyncio.get_running_loop().time()
    bootstrapper._stash_lines = []
    bootstrapper._stash_carry = ""

    with pytest.raises(BootstrapError) as err:
        await bootstrapper._resolve_remote_python()

    payload = err.value.to_dict()
    assert payload["details"]["candidates"] == ["python3.12", "python3", "python"]
    assert "No supported Python interpreter" in payload["message"]


def test_health_monitor_snapshot_counts_receiver_cleanup_metrics() -> None:
    monitor = HealthMonitor(
        pod_spec=None,
        ws_url="wss://example/ws",
        socks_host="127.0.0.1",
        socks_port=1080,
    )
    monitor.on_metric("session.cleanup.tcp", value=2)
    monitor.on_metric("session.recv.cleanup.tcp", value=3)
    monitor.on_metric("session.cleanup.udp", value=1)
    monitor.on_metric("session.recv.cleanup.udp", value=4)

    snap = monitor.snapshot()
    assert snap.cleanup_tcp == 5
    assert snap.cleanup_udp == 5


def test_health_monitor_on_metric_ignores_byte_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = HealthMonitor(
        pod_spec=None,
        ws_url="wss://example/ws",
        socks_host="127.0.0.1",
        socks_port=1080,
    )
    monitor.on_metric("session.frames.outbound.bytes", value=123)
    monitor.on_metric("session.frames.bytes.in", value=456)

    assert monitor._counter("bytes.up") == 0
    assert monitor._counter("bytes.down") == 0

    monkeypatch.setattr(
        cli_metrics_mod,
        "metrics_snapshot",
        lambda: {
            "session.frames.outbound.bytes": 7,
            "session.frames.bytes.in": 11,
        },
    )
    snap = monitor.snapshot()
    assert snap.bytes_up_total == 7
    assert snap.bytes_down_total == 11


def test_build_session_config_supports_namespaced_wss_insecure_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXECTUNNEL_WSS_INSECURE", "1")
    monkeypatch.setenv("WSS_INSECURE", "0")

    cfg = cli_config_mod.build_session_config(wss_url="wss://example/ws")

    assert cfg.ssl_context_override is not None
    assert cfg.ssl_context_override.verify_mode == ssl.CERT_NONE


def test_decode_binary_payload_wraps_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_value_error(_: str) -> bytes:
        raise ValueError("invalid payload")

    monkeypatch.setattr(
        protocol_frames_mod.base64, "urlsafe_b64decode", _raise_value_error
    )

    with pytest.raises(FrameDecodingError):
        protocol_frames_mod.decode_binary_payload("abc")


def test_parse_frame_accepts_conn_open_with_required_payload() -> None:
    conn_id = "c0123456789abcdef01234567"
    frame = protocol_frames_mod.encode_conn_open_frame(conn_id, "example.com", 443)

    parsed = protocol_frames_mod.parse_frame(frame)

    assert parsed is not None
    assert parsed.msg_type == "CONN_OPEN"
    assert parsed.conn_id == conn_id
    assert parsed.payload != ""


def test_parse_frame_rejects_conn_ack_with_payload() -> None:
    conn_id = "c0123456789abcdef01234567"
    frame = (
        f"{protocol_frames_mod.FRAME_PREFIX}CONN_ACK:{conn_id}:junk"
        f"{protocol_frames_mod.FRAME_SUFFIX}"
    )

    with pytest.raises(FrameDecodingError):
        protocol_frames_mod.parse_frame(frame)


def test_parse_frame_rejects_data_without_payload() -> None:
    conn_id = "c0123456789abcdef01234567"
    frame = (
        f"{protocol_frames_mod.FRAME_PREFIX}DATA:{conn_id}"
        f"{protocol_frames_mod.FRAME_SUFFIX}"
    )

    with pytest.raises(FrameDecodingError):
        protocol_frames_mod.parse_frame(frame)


def test_udp_relay_size_guard_applies_to_payload_not_datagram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay = UdpRelay()
    relay._queue = asyncio.Queue(maxsize=1)

    payload = b"ok"
    monkeypatch.setattr(
        proxy_udp_relay_mod,
        "parse_udp_header",
        lambda _data: (payload, "example.com", 53),
    )

    oversized_datagram = b"x" * (MAX_UDP_PAYLOAD_BYTES + 32)
    relay.on_datagram(oversized_datagram, ("127.0.0.1", 40000))

    assert relay._queue.get_nowait() == (payload, "example.com", 53)


def test_udp_flow_evict_decrements_gauge_only_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = object.__new__(UdpFlow)
    flow._id = "u-test"
    flow._registry = {}

    calls: list[str] = []
    monkeypatch.setattr(
        transport_udp_mod,
        "metrics_gauge_dec",
        lambda metric: calls.append(metric),
    )

    flow._evict()
    assert calls == []

    flow._registry = {"u-test": object()}
    flow._evict()
    assert calls == ["session.active.udp_flows"]


class _DummyWriter:
    def close(self) -> None:
        return

    async def wait_closed(self) -> None:
        return


def _build_tcp_for_cleanup(registry: dict[str, object]) -> TcpConnection:
    conn = object.__new__(TcpConnection)
    conn._id = "c-test"
    conn._registry = registry
    conn._closed = asyncio.Event()
    conn._upstream_task = None
    conn._downstream_task = None
    conn._writer = _DummyWriter()
    conn._bytes_upstream = 0
    conn._bytes_downstream = 0
    conn._drop_count = 0

    async def _send_close_frame_once() -> None:
        return

    conn._send_close_frame_once = _send_close_frame_once
    return conn


@pytest.mark.asyncio
async def test_tcp_cleanup_decrements_gauge_only_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gauge_calls: list[str] = []
    monkeypatch.setattr(
        transport_tcp_mod,
        "metrics_gauge_dec",
        lambda metric: gauge_calls.append(metric),
    )
    monkeypatch.setattr(
        transport_tcp_mod, "metrics_inc", lambda *_args, **_kwargs: None
    )

    missing = _build_tcp_for_cleanup(registry={})
    await missing._cleanup()
    assert gauge_calls == []

    present = _build_tcp_for_cleanup(registry={"c-test": object()})
    await present._cleanup()
    assert gauge_calls == ["session.active.tcp_connections"]


@pytest.mark.asyncio
async def test_tcp_feed_async_full_queue_unblocks_on_close() -> None:
    conn = object.__new__(TcpConnection)
    conn._id = "c-test"
    conn._closed = asyncio.Event()
    conn._inbound = asyncio.Queue(maxsize=1)
    conn._inbound.put_nowait(b"first")

    task = asyncio.create_task(conn.feed_async(b"second"))
    await asyncio.sleep(0)
    conn._closed.set()

    with pytest.raises(ConnectionClosedError) as exc_info:
        await asyncio.wait_for(task, timeout=1.0)

    assert getattr(exc_info.value, "error_code", "") == (
        "transport.feed_async_closed_during_enqueue"
    )


class _FakeSenderWs:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.entered_send = asyncio.Event()
        self.allow_send = asyncio.Event()

    async def send(self, frame: str) -> None:
        self.entered_send.set()
        await self.allow_send.wait()
        self.sent.append(frame)


@pytest.mark.asyncio
async def test_ws_sender_stop_gracefully_drains_control_frame() -> None:
    ws = _FakeSenderWs()
    ws_closed = asyncio.Event()
    sender = WsSender(
        ws, SessionConfig(wss_url="wss://example/ws", send_timeout=1.0), ws_closed
    )
    sender.start()

    frame = protocol_frames_mod.encode_keepalive_frame()
    await sender.send(frame, control=True)
    await asyncio.wait_for(ws.entered_send.wait(), timeout=1.0)

    stop_task = asyncio.create_task(sender.stop())
    await asyncio.sleep(0)
    ws.allow_send.set()
    await asyncio.wait_for(stop_task, timeout=1.0)

    assert ws.sent == [frame]
