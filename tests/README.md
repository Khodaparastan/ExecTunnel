# ExecTunnel test suite

This document is the entry point for anyone working on the test
package: it explains the layout, the marker tiers, the shared fixtures
catalogue, and how to add a new test in the right place.

The configuration lives entirely in `pyproject.toml`
(`[tool.pytest.ini_options]` and `[tool.coverage.*]`); there is no
`pytest.ini`, `conftest.py` per-test-file overrides should be a last
resort, and there is no `tests/setup.cfg`.

---

### Layout

```
tests/
├── README.md                       ← you are here
├── __init__.py                     ← marks tests/ as a package so
│                                     `tests._helpers` resolves
├── conftest.py                     ← project-wide fixtures (autouse
│                                     metrics-reset, generic factories)
│
├── _helpers/                       ← shared, importable test helpers
│   ├── __init__.py                 ←   re-exports the public ABI
│   ├── ids.py                      ←   canonical TCP/UDP IDs
│   ├── network.py                  ←   free_port, free_udp_port,
│   │                                   loopback_addr
│   ├── streams.py                  ←   make_stream_reader,
│   │                                   make_eof_reader,
│   │                                   make_mock_writer,
│   │                                   DummyWriter,
│   │                                   DummyDatagramTransport
│   └── ws.py                       ←   MockWsSend, QueueWs
│
├── integration/                    ← @pytest.mark.integration tier
│   ├── __init__.py
│   ├── test_agent_subprocess.py    ← @agent — black-box agent.py
│   ├── test_agent_stress.py        ← @agent + @slow stress
│   └── test_wire_loop_inmemory.py  ← @inmemory — Sender↔Receiver
│                                     round-trip with QueueWs
│
└── unit/
    ├── __init__.py
    ├── test_defaults.py            ← Defaults invariants
    ├── test_exceptions.py          ← exception hierarchy + to_dict
    ├── test_new_exceptions.py      ← Ctrl-backpressure / pre-ACK overflow
    │
    ├── observability/
    │   └── test_utils.py           ← env-var parsers
    │
    ├── protocol/
    │   ├── __init__.py
    │   ├── test_codecs.py          ← host:port + binary codec
    │   ├── test_codecs_property.py ← @property — Hypothesis round-trips
    │   ├── test_enums.py
    │   ├── test_frames.py          ← encoder + parser round-trips
    │   ├── test_ids.py
    │   ├── test_parser.py          ← parser malformed-frame paths
    │   └── test_types.py           ← ParsedFrame contract
    │
    ├── proxy/
    │   ├── __init__.py
    │   ├── conftest.py             ← autouse observability mocks
    │   ├── test_config.py
    │   ├── test_constants.py
    │   ├── test_io.py
    │   ├── test_server.py
    │   ├── test_tcp_relay.py
    │   ├── test_udp_relay.py
    │   └── test_wire.py
    │
    ├── session/
    │   ├── __init__.py
    │   ├── test_constants.py
    │   ├── test_dispatcher_udp.py
    │   ├── test_lru.py             ← LruDict MRU promotion contract
    │   ├── test_routing.py         ← is_host_excluded boundary
    │   ├── test_sender.py
    │   ├── test_state.py           ← AckStatus + PendingConnect
    │   └── test_strip_ansi.py      ← ECMA-48 stripping
    │
    └── transport/
        ├── __init__.py
        ├── _helpers.py             ← thin re-export shim → tests._helpers
        ├── conftest.py             ← TCP/UDP factory fixtures
        ├── test_errors.py          ← log_task_exception match-arms
        ├── test_init.py
        ├── test_ordering_repro.py
        ├── test_tcp.py
        ├── test_types.py           ← public-facing type surface
        ├── test_types_dataclasses.py ← Protocol runtime check
        ├── test_udp.py
        ├── test_validation.py
        └── test_waiting.py         ← wait_first / suppress_loser
```

---

### Marker tiers

Markers are declared in `[tool.pytest.ini_options].markers`.
`--strict-markers` is enabled, so a typo fails collection.

| Marker        | Where it lives                           | Purpose                                                        |
|---------------|------------------------------------------|----------------------------------------------------------------|
| `unit`        | implicit default for `tests/unit/`       | Single-module focused tests; no sockets, no subprocesses.      |
| `integration` | `tests/integration/`                     | Spawns subprocesses or opens real sockets; slower than unit.   |
| `agent`       | `tests/integration/test_agent_*.py`      | Black-box subprocess test of the bundled `payload/agent.py`.   |
| `inmemory`    | `tests/integration/test_wire_loop_*`     | Session components wired together via `QueueWs` — fast, no IO. |
| `property`    | `tests/unit/protocol/test_*_property.py` | Hypothesis-driven round-trip tests.                            |
| `slow`        | mostly stress tests                      | Excluded from `make test-unit` and `make test-fast`.           |
| `ci_only`     | reserved                                 | Heavy fuzzing seeds intended for CI only.                      |

#### Make targets

| Target                  | Effect                                                                               |
|-------------------------|--------------------------------------------------------------------------------------|
| `make test`             | Full collection (unit + integration + property).                                     |
| `make test-unit`        | Unit tier only — `-m "not integration and not agent and not slow and not inmemory"`. |
| `make test-integration` | `-m integration` over `tests/integration/`.                                          |
| `make test-inmemory`    | `-m inmemory`.                                                                       |
| `make test-property`    | `-m property`.                                                                       |
| `make test-fast`        | `make test-unit` with `-x` (stop on first failure).                                  |
| `make test-cov`         | Full collection with `--cov=exectunnel` plus term/html/xml reports.                  |
| `make test-parallel`    | Full collection across all CPU cores (requires `pytest-xdist`).                      |
| `make test-debug`       | `-vv --tb=long --showlocals -p no:randomly`.                                         |

---

### Shared fixtures catalogue

| Fixture                    | Where                                        | Returns                                                                   |
|----------------------------|----------------------------------------------|---------------------------------------------------------------------------|
| `_reset_metrics`           | `tests/conftest.py` (autouse)                | `None` — resets every observability counter / gauge before each test.     |
| `ws_send_mock`             | `tests/conftest.py`                          | `AsyncMock` shaped like `ws_send` (no framing introspection).             |
| `dummy_writer`             | `tests/conftest.py`                          | Fresh `DummyWriter` (concrete `StreamWriter`-shaped sink).                |
| `dummy_datagram_transport` | `tests/conftest.py`                          | Fresh `DummyDatagramTransport`.                                           |
| `make_stream_reader`       | `tests/conftest.py`                          | Factory function; pre-loads bytes into an `asyncio.StreamReader`.         |
| `mock_observability`       | `tests/unit/proxy/conftest.py` (autouse)     | Patches every `metrics_*`/`aspan` reference under `exectunnel.proxy`.     |
| `mock_writer`              | `tests/unit/proxy/conftest.py`               | `MagicMock` shaped like `asyncio.StreamWriter`.                           |
| `make_tcp_conn`            | `tests/unit/transport/conftest.py`           | Factory: builds a `TcpConnection` wired to `ws_send` + `tcp_writer`.      |
| `make_udp_flow`            | `tests/unit/transport/conftest.py`           | Factory: builds a `UdpFlow` wired to `ws_send` + `udp_registry`.          |
| `ws_send`                  | `tests/unit/transport/conftest.py`           | Fresh `MockWsSend` (recording sender double).                             |
| `tcp_writer`               | `tests/unit/transport/conftest.py`           | Fresh mock writer.                                                        |
| `tcp_registry`             | `tests/unit/transport/conftest.py`           | Empty `dict[str, TcpConnection]`.                                         |
| `udp_registry`             | `tests/unit/transport/conftest.py`           | Empty `dict[str, UdpFlow]`.                                               |
| `patch_observability`      | `tests/unit/transport/conftest.py` (autouse) | Patches every `metrics_*`/`aspan` reference under `exectunnel.transport`. |

---

### Helper-package "ABI"

Importable as `from tests._helpers import ...`:

| Symbol                    | Purpose                                                                                                                         |
|---------------------------|---------------------------------------------------------------------------------------------------------------------------------|
| `TCP_CONN_ID`             | Canonical TCP connection ID (`c` + 24×`a`).                                                                                     |
| `UDP_FLOW_ID`             | Canonical UDP flow ID (`u` + 24×`b`).                                                                                           |
| `free_port()`             | Available TCP port on `127.0.0.1`.                                                                                              |
| `free_udp_port()`         | Available UDP port on `127.0.0.1`.                                                                                              |
| `loopback_addr()`         | `"127.0.0.1"` literal.                                                                                                          |
| `make_stream_reader(...)` | `asyncio.StreamReader` pre-loaded with bytes (or chunks); EOF by default.                                                       |
| `make_eof_reader()`       | `MagicMock(spec=StreamReader)` that immediately yields EOF.                                                                     |
| `make_mock_writer()`      | `MagicMock(spec=StreamWriter)` with all methods stubbed.                                                                        |
| `DummyWriter`             | Concrete writer that captures bytes in a `bytearray`.                                                                           |
| `DummyDatagramTransport`  | Concrete datagram transport that captures `sendto` calls.                                                                       |
| `MockWsSend`              | Recording `ws_send` callable with `frames`, `frames_of_type`, `side_effect`.                                                    |
| `QueueWs`                 | Bidirectional in-memory websocket double; supports `__aiter__`, `recv`, `send`, `close`, `feed_to_session`, `pop_from_session`. |

---

### Where does this belong?

When adding a new test, ask:

1. **Does it test a single module's public surface?** →
   `tests/unit/<package>/test_<module>.py`.
2. **Does it spawn a subprocess or open a real socket?** → `tests/integration/`,
   gated behind `@pytest.mark.integration` (and `@pytest.mark.agent` if it
   exec's `payload/agent.py`).
3. **Does it exercise multiple session components together but without
   real IO?** → `tests/integration/`, gated behind `@pytest.mark.inmemory`.
4. **Is it a Hypothesis-driven property test?** → Same directory as the
   matching example-based test, suffix `_property.py`, gated behind
   `@pytest.mark.property`.

When adding a fixture, ask:

1. **Used by exactly one test file?** → Module-level fixture in that file.
2. **Used across one package (e.g. all `tests/unit/transport/*`)?** →
   That package's `conftest.py`.
3. **Used across multiple packages (e.g. shared by transport AND proxy)?**
   → `tests/_helpers/<topic>.py` + re-export in `tests/_helpers/__init__.py`.
4. **Auto-applies invisibly to every test in the project?** → Autouse
   fixture in `tests/conftest.py` (currently only metrics-reset).

---

### Coverage

`pyproject.toml` carries the full `[tool.coverage.*]` config — branch
coverage, parallel data files, an XML report under `coverage.xml`, an
HTML report under `htmlcov/`, and a wide `omit` list (CLI glue, the
`payload/agent.py` subprocess, dashboards).

Per the project's policy, `make test-cov` *reports* but does not
*enforce* a coverage threshold. The CI workflow uploads the XML to
Codecov when `CODECOV_TOKEN` is available.

---

### Strict-warning policy

`pyproject.toml` sets `filterwarnings = ["error", ...]` so any
unexpected warning fails the run. Three third-party deprecations are
explicitly downgraded to `ignore` (`websockets`, `aiohttp`,
`unittest.mock`). Anything else — most importantly, unawaited
coroutines — must be fixed at the source.

---

### Adding a new dev dependency

1. Add to `pyproject.toml [tool.poetry.group.dev.dependencies]`.
2. Run `poetry lock` and commit `poetry.lock`.
3. If the dependency provides a pytest plugin, mention it in this
   README's "Make targets" table.
