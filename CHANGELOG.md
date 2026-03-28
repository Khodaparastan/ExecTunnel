# Changelog
All notable changes to this project will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

## [Unreleased]

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
