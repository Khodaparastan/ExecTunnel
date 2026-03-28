# Changelog
All notable changes to this project will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

## [Unreleased]

## [1.1.0a0] — unreleased (alpha / PoC)

### Added
- `py.typed` marker (PEP 561) — package now declares itself as typed to downstream consumers.
- `CHANGELOG.md` — structured changelog following Keep a Changelog format.
- `protocol/` sub-package: `frames.py` (frame codec), `ids.py` (ID generation), `enums.py` (SOCKS5 enums).
- `config/` sub-package replacing `core/`: `settings.py` (dataclasses), `defaults.py` (constants by domain), `env.py` (env parsing helpers), `exclusions.py` (CIDR exclusions).
- `proxy/` sub-package replacing flat `socks5.py`: `server.py`, `relay.py`, `request.py`, `_codec.py`.
- `transport/` sub-package extracting from `tunnel.py`: `session.py`, `connection.py`, `udp_flow.py`, `dns_forwarder.py`, `send_loop.py`, `recv_loop.py`, `models.py`.
- `tests/unit/` and `tests/integration/` directory structure mirroring source layout.
- `tests/conftest.py` with shared fixtures.

### Changed
- Constants renamed with units suffixes: `WS_PING_INTERVAL_SECS`, `WS_PING_TIMEOUT_SECS`, `TCP_INBOUND_QUEUE_CAP`, `CONNECT_PACE_CF_INTERVAL_SECS`, `BOOTSTRAP_CHUNK_SIZE_CHARS`, `PIPE_READ_CHUNK_BYTES`, `UDP_SEND_QUEUE_CAP`.
- Exception classes renamed: `ConfigError` → `ConfigurationError`, `AgentTimeoutError` → `AgentReadyTimeoutError`, `WebSocketSendTimeout` → `WebSocketSendTimeoutError`.
- Class renames: `_ConnHandler` → `_TcpConnectionHandler`, `PendingConnect` → `PendingConnectState`, `_Hist` → `_Histogram`, `_JsonFormatter` → `_JsonLogFormatter`, `_ContextFilter` → `_TraceContextFilter`, `_StdoutWriter` → `_FrameWriter`, `Connection` (agent) → `TcpConnectionWorker`, `UdpFlow` (agent) → `UdpFlowWorker`.
- Function renames: `get_config()` → `get_app_config()`, `build_parser()` → `build_arg_parser()`, `cmd_tunnel()` → `run_tunnel_command()`, `default_exclusion_networks()` → `get_default_exclusion_networks()`, and others per PEP 8 conventions.
- `WSS_URL` env var renamed to `EXECTUNNEL_WSS_URL` for consistency.
- `core/` sub-package replaced by `config/` (more descriptive).
- `helpers.py` catch-all replaced by `protocol/frames.py` and `protocol/ids.py`.

### Removed
- `_DEFAULT_EXCLUDE` legacy alias (use `get_default_exclusion_networks()` instead).
- `parse_bool_env` duplication between `core/config.py` and `observability/exporters.py` — canonical location is `config/env.py`.
- `wtf` debug file from project root.

## [1.0.1a0] — unreleased (alpha / PoC)

### Changed
- Removed `pyrefly` from runtime dependencies (type-checker; moved to dev group)
- Removed redundant `[project.optional-dependencies]` extras that duplicated core deps
- Relaxed `colorama` upper bound from `<0.5.0` to `<1.0.0`
- Switched to dynamic versioning via `poetry-dynamic-versioning`; `_version.py` is the single source of truth
- Multi-line dependency list in `pyproject.toml` for readability

### Fixed
- Version mismatch between `pyproject.toml` and `exectunnel/_version.py`

## [1.0.0a0] — unreleased (alpha / PoC)

### Added
- Local SOCKS5 server supporting `CONNECT` and `UDP ASSOCIATE`
- TCP/UDP relay over a single framed WebSocket stream
- Optional DNS forwarding through the tunnel (`--dns`)
- CIDR exclusions for local direct routing
- Auto-reconnect with ACK-timeout health recovery
- Structured observability: structured logging (`structlog`), in-process metrics, distributed tracing, and pluggable exporters (log, file, HTTP — with Datadog/Splunk/New Relic payload shaping)
- `exectunnel tunnel` CLI entry point with environment-variable configuration
- Full unit and integration test suite (`pytest-asyncio`)
- `mypy` strict type checking and `ruff` linting configured
