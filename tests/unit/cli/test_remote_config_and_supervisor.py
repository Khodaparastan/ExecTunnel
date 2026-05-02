"""Tests for the new CLI surfaces added in the remote-config / restart-policy
phase: ``_exit_codes``, ``_remote_config``, ``_runner.exception_exit_code``,
``_supervisor.RestartPolicy``, and the new ``AuthFailureFrame`` IPC frame.

These tests are deliberately mock-driven — no real HTTP, no real subprocess,
no real asyncio event-loop scheduling beyond what ``asyncio.run`` provides.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from exectunnel.cli._exit_codes import (
    EXIT_AUTH_FAILURE,
    EXIT_BOOTSTRAP,
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_RECONNECT_EXHAUSTED,
    FATAL_EXIT_CODES,
    RETRYABLE_EXIT_CODES,
)
from exectunnel.cli._remote_config import (
    ProbeResult,
    ProviderHealth,
    ProviderHealthWatcher,
    ProviderState,
    RemoteConfigClient,
    RemoteConfigError,
    RemoteConfigSource,
    default_cache_path_for,
    write_cache_atomically,
)
from exectunnel.cli._supervisor.ipc import (
    AuthFailureFrame,
    ExitFrame,
    HealthFrame,
    MetricFrame,
    StatusFrame,
    decode_frame,
    encode_frame,
)
from exectunnel.cli._supervisor.supervisor import RestartPolicy
from exectunnel.cli.runner import (
    AUTH_FAILURE_HTTP_CODES,
    exception_exit_code,
)
from exectunnel.exceptions import (
    BootstrapError,
    ReconnectExhaustedError,
)

# ─────────────────────────────────────────────────────────────────────────────
# Exit codes
# ─────────────────────────────────────────────────────────────────────────────


class TestExitCodes:
    def test_constants_are_stable(self):
        assert EXIT_OK == 0
        assert EXIT_ERROR == 1
        assert EXIT_BOOTSTRAP == 2
        assert EXIT_RECONNECT_EXHAUSTED == 3
        assert EXIT_AUTH_FAILURE == 4
        assert EXIT_INTERRUPTED == 130

    def test_retryable_set(self):
        assert EXIT_BOOTSTRAP in RETRYABLE_EXIT_CODES
        assert EXIT_AUTH_FAILURE in RETRYABLE_EXIT_CODES
        assert EXIT_RECONNECT_EXHAUSTED in RETRYABLE_EXIT_CODES
        assert EXIT_ERROR in RETRYABLE_EXIT_CODES

    def test_fatal_set(self):
        assert EXIT_INTERRUPTED in FATAL_EXIT_CODES
        assert EXIT_OK not in FATAL_EXIT_CODES


# ─────────────────────────────────────────────────────────────────────────────
# IPC frames
# ─────────────────────────────────────────────────────────────────────────────


class TestIpcFrames:
    def test_auth_failure_frame_roundtrip(self):
        frame = AuthFailureFrame(
            tunnel="t1",
            http_status=401,
            wss_url="wss://example.com/foo",
            message="token expired",
        )
        encoded = encode_frame(frame)
        assert encoded.endswith(b"\n")
        decoded = decode_frame(encoded)
        assert isinstance(decoded, AuthFailureFrame)
        assert decoded.tunnel == "t1"
        assert decoded.http_status == 401
        assert decoded.wss_url == "wss://example.com/foo"
        assert decoded.message == "token expired"

    def test_auth_failure_frame_rejects_non_4xx(self):
        with pytest.raises(Exception):
            AuthFailureFrame(tunnel="t1", http_status=500)

    def test_metric_frame_value_widens_to_float(self):
        # int input is silently widened to float.
        frame = MetricFrame(
            tunnel="t1",
            name="x.y",
            kind="counter",
            value=3,
        )
        assert isinstance(frame.value, float)
        assert frame.value == 3.0

    def test_decode_unknown_frame_returns_none(self):
        line = json.dumps({"v": 1, "type": "future_frame", "tunnel": "t1"})
        assert decode_frame(line) is None

    def test_decode_status_health_exit(self):
        for cls, extra in (
            (StatusFrame, {"status": "running"}),
            (HealthFrame, {"connected": True}),
            (ExitFrame, {"code": 0}),
        ):
            f = cls(tunnel="t1", **extra)  # type: ignore[arg-type]
            assert decode_frame(encode_frame(f)) == f


# ─────────────────────────────────────────────────────────────────────────────
# exception_exit_code mapping
# ─────────────────────────────────────────────────────────────────────────────


class TestExceptionExitCode:
    def test_cancelled_to_interrupted(self):
        assert exception_exit_code(asyncio.CancelledError()) == EXIT_INTERRUPTED

    def test_bootstrap_401_to_auth_failure(self):
        exc = BootstrapError(
            "rejected",
            details={"http_status": 401},
        )
        assert exception_exit_code(exc) == EXIT_AUTH_FAILURE

    def test_bootstrap_403_to_auth_failure(self):
        exc = BootstrapError(
            "rejected",
            details={"http_status": 403},
        )
        assert exception_exit_code(exc) == EXIT_AUTH_FAILURE

    def test_bootstrap_500_to_bootstrap(self):
        exc = BootstrapError("rejected", details={"http_status": 500})
        assert exception_exit_code(exc) == EXIT_BOOTSTRAP

    def test_bootstrap_no_details_to_bootstrap(self):
        exc = BootstrapError("oops")
        assert exception_exit_code(exc) == EXIT_BOOTSTRAP

    def test_reconnect_exhausted_classification(self):
        exc = ReconnectExhaustedError("done")
        assert exception_exit_code(exc) == EXIT_RECONNECT_EXHAUSTED

    def test_generic_exception_to_error(self):
        assert exception_exit_code(RuntimeError("x")) == EXIT_ERROR

    def test_auth_failure_codes_are_canonical(self):
        assert frozenset({401, 403}) == AUTH_FAILURE_HTTP_CODES


# ─────────────────────────────────────────────────────────────────────────────
# RestartPolicy
# ─────────────────────────────────────────────────────────────────────────────


class TestRestartPolicy:
    def test_default_disables_clean_exit_restart(self):
        policy = RestartPolicy()
        assert not policy.should_restart(EXIT_OK)

    def test_disabled_policy_never_restarts(self):
        policy = RestartPolicy(enabled=False)
        for code in (
            EXIT_ERROR,
            EXIT_BOOTSTRAP,
            EXIT_RECONNECT_EXHAUSTED,
            EXIT_AUTH_FAILURE,
        ):
            assert not policy.should_restart(code)

    def test_fatal_codes_never_restart(self):
        policy = RestartPolicy()
        assert not policy.should_restart(EXIT_INTERRUPTED)

    def test_retryable_codes_restart(self):
        policy = RestartPolicy()
        for code in (
            EXIT_ERROR,
            EXIT_BOOTSTRAP,
            EXIT_RECONNECT_EXHAUSTED,
            EXIT_AUTH_FAILURE,
        ):
            assert policy.should_restart(code)

    def test_backoff_is_monotonic_until_cap(self):
        policy = RestartPolicy(
            base_backoff_secs=1.0,
            max_backoff_secs=8.0,
            jitter_ratio=0.0,  # determinism
        )
        delays = [policy.backoff_for(n) for n in range(1, 6)]
        # 1, 2, 4, 8, 8 (capped)
        assert delays == [1.0, 2.0, 4.0, 8.0, 8.0]

    def test_backoff_zero_for_attempt_zero(self):
        policy = RestartPolicy()
        assert policy.backoff_for(0) == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# RemoteConfigSource validation
# ─────────────────────────────────────────────────────────────────────────────


class TestRemoteConfigSource:
    def test_minimal_construction(self):
        s = RemoteConfigSource(base_url="https://x", identity="me")
        assert s.fmt == "toml"
        assert s.token is None
        assert s.normalized_base_url == "https://x"

    def test_strips_trailing_slash(self):
        s = RemoteConfigSource(base_url="https://x/", identity="me")
        assert s.normalized_base_url == "https://x"

    def test_auth_headers_when_token(self):
        s = RemoteConfigSource(base_url="https://x", identity="me", token="abc")
        assert s.auth_headers() == {"Authorization": "Bearer abc"}

    def test_auth_headers_empty_when_no_token(self):
        s = RemoteConfigSource(base_url="https://x", identity="me")
        assert s.auth_headers() == {}

    def test_validation_rejects_empty_base_url(self):
        with pytest.raises(ValueError):
            RemoteConfigSource(base_url="", identity="me")

    def test_validation_rejects_bad_format(self):
        with pytest.raises(ValueError):
            RemoteConfigSource(base_url="https://x", identity="me", fmt="json")  # type: ignore[arg-type]

    def test_default_cache_path_namespacing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        s = RemoteConfigSource(base_url="https://x", identity="user/with/slash")
        path = default_cache_path_for(s)
        assert path.parent.name == "exectunnel"
        assert path.suffix == ".toml"
        # Slashes scrubbed for filesystem safety
        assert "/" not in path.name


# ─────────────────────────────────────────────────────────────────────────────
# write_cache_atomically
# ─────────────────────────────────────────────────────────────────────────────


class TestWriteCacheAtomically:
    def test_writes_file(self, tmp_path):
        target = tmp_path / "sub" / "cfg.toml"
        write_cache_atomically(target, b"hello")
        assert target.read_bytes() == b"hello"

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "cfg.toml"
        target.write_bytes(b"old")
        write_cache_atomically(target, b"new")
        assert target.read_bytes() == b"new"


# ─────────────────────────────────────────────────────────────────────────────
# RemoteConfigClient — using httpx.MockTransport
# ─────────────────────────────────────────────────────────────────────────────


def _patch_httpx_transport(monkeypatch, handler):
    """Replace httpx.AsyncClient with a thin subclass that always uses
    the supplied :class:`httpx.MockTransport` handler."""
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    class _MockedClient(real_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _MockedClient)


class TestRemoteConfigClientFetchConfig:
    def test_happy_path(self, monkeypatch):
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, content=b"k = 1\n")

        _patch_httpx_transport(monkeypatch, handler)

        client = RemoteConfigClient(
            RemoteConfigSource(
                base_url="https://api.example",
                identity="alice",
                token="t-1",
            )
        )
        body = asyncio.run(client.fetch_config())
        assert body == b"k = 1\n"
        assert "/api/v1/configs/identities" in captured["url"]
        assert "format=toml" in captured["url"]
        assert "identity=alice" in captured["url"]
        assert captured["headers"]["authorization"] == "Bearer t-1"

    def test_401_raises_non_retryable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=b"")

        _patch_httpx_transport(monkeypatch, handler)
        client = RemoteConfigClient(
            RemoteConfigSource(base_url="https://x", identity="alice")
        )
        with pytest.raises(RemoteConfigError) as ei:
            asyncio.run(client.fetch_config())
        assert ei.value.status == 401
        assert ei.value.retryable is False

    def test_503_raises_retryable(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, content=b"")

        _patch_httpx_transport(monkeypatch, handler)
        client = RemoteConfigClient(
            RemoteConfigSource(base_url="https://x", identity="alice")
        )
        with pytest.raises(RemoteConfigError) as ei:
            asyncio.run(client.fetch_config())
        assert ei.value.status == 503
        assert ei.value.retryable is True


class TestRemoteConfigClientFetchHealth:
    def test_ok_payload(self, monkeypatch):
        payload = {
            "status": "ok",
            "checked_at": "2026-05-01T18:57:31.767Z",
            "cached": False,
            "gateway": {"status": "ok", "version": "1.2.3", "uptime_seconds": 42},
            "identities": {
                "status": "ok",
                "registered": ["alice"],
                "authenticated": ["alice"],
                "unauthenticated": [],
            },
            "provider": {
                "status": "ok",
                "name": "runflare",
                "base_url": "https://prov",
                "probes": [
                    {
                        "alias": "p1",
                        "target": "wss://1",
                        "status": "ok",
                        "latency_ms": 12,
                        "http_status": 200,
                        "error": "",
                    }
                ],
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/health"
            return httpx.Response(200, json=payload)

        _patch_httpx_transport(monkeypatch, handler)
        client = RemoteConfigClient(
            RemoteConfigSource(base_url="https://x", identity="alice")
        )
        snapshot = asyncio.run(client.fetch_health())
        assert isinstance(snapshot, ProviderHealth)
        assert snapshot.state == ProviderState.OK
        assert snapshot.gateway_version == "1.2.3"
        assert snapshot.identities_authenticated == ("alice",)
        assert snapshot.provider_name == "runflare"
        assert len(snapshot.probes) == 1
        assert snapshot.probes[0].latency_ms == 12.0

    def test_non_200_returns_synthetic_down(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"")

        _patch_httpx_transport(monkeypatch, handler)
        client = RemoteConfigClient(
            RemoteConfigSource(base_url="https://x", identity="alice")
        )
        snapshot = asyncio.run(client.fetch_health())
        assert snapshot.state == ProviderState.DOWN
        assert snapshot.cached is False

    def test_invalid_json_returns_synthetic_down(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not-json")

        _patch_httpx_transport(monkeypatch, handler)
        client = RemoteConfigClient(
            RemoteConfigSource(base_url="https://x", identity="alice")
        )
        snapshot = asyncio.run(client.fetch_health())
        assert snapshot.state == ProviderState.DOWN

    def test_unknown_status_parses_as_down(self):
        snapshot = ProviderHealth.from_payload({
            "status": "rocking",
            "checked_at": "2026-01-01T00:00:00Z",
        })
        assert snapshot.state == ProviderState.DOWN


# ─────────────────────────────────────────────────────────────────────────────
# ProviderHealth helpers
# ─────────────────────────────────────────────────────────────────────────────


class TestProviderHealthFlags:
    def _mk(self, state: ProviderState) -> ProviderHealth:
        import datetime as dt

        return ProviderHealth(
            state=state,
            checked_at=dt.datetime.now(dt.UTC),
            cached=False,
        )

    def test_blocking_only_when_down(self):
        assert self._mk(ProviderState.DOWN).is_blocking
        assert not self._mk(ProviderState.DEGRADED).is_blocking
        assert not self._mk(ProviderState.OK).is_blocking

    def test_degraded_flag(self):
        assert self._mk(ProviderState.DEGRADED).is_degraded
        assert not self._mk(ProviderState.OK).is_degraded


# ─────────────────────────────────────────────────────────────────────────────
# ProviderHealthWatcher state machine
# ─────────────────────────────────────────────────────────────────────────────


class _FakeClient:
    def __init__(self, sequence: list[ProviderHealth]) -> None:
        self.sequence = list(sequence)
        self.call_count = 0

    async def fetch_health(self) -> ProviderHealth:
        self.call_count += 1
        if self.sequence:
            return self.sequence.pop(0)
        # Repeat the last observation forever once exhausted.
        return self._last  # pragma: no cover

    @property
    def _last(self) -> ProviderHealth:  # pragma: no cover
        raise RuntimeError("sequence exhausted")


def _ph(state: ProviderState) -> ProviderHealth:
    import datetime as dt

    return ProviderHealth(
        state=state,
        checked_at=dt.datetime.now(dt.UTC),
        cached=False,
    )


class TestProviderHealthWatcher:
    def test_starts_with_immediate_probe_then_publishes(self):
        async def run() -> tuple[ProviderHealth, list[ProviderHealth]]:
            seen: list[ProviderHealth] = []
            client = _FakeClient([_ph(ProviderState.OK)])
            watcher = ProviderHealthWatcher(client)  # type: ignore[arg-type]
            watcher.subscribe(seen.append)
            await watcher.start()
            try:
                latest = watcher.latest
                assert latest is not None
                assert not watcher.is_blocking()
                # subscribe-after-snapshot replays once
                second_seen: list[ProviderHealth] = []
                watcher.subscribe(second_seen.append)
                assert second_seen == [latest]
                return latest, seen
            finally:
                await watcher.stop()

        latest, seen = asyncio.run(run())
        assert latest.state == ProviderState.OK
        assert len(seen) == 1
        assert seen[0].state == ProviderState.OK

    def test_down_blocks_then_recovery_unblocks(self):
        async def run() -> bool:
            client = _FakeClient([_ph(ProviderState.DOWN)])
            watcher = ProviderHealthWatcher(client)  # type: ignore[arg-type]
            await watcher.start()
            try:
                assert watcher.is_blocking()
                # wait_until_unblocked must not return while blocking holds
                wait_task = asyncio.create_task(watcher.wait_until_unblocked())
                await asyncio.sleep(0)
                assert not wait_task.done()
                # simulate a recovery refresh by injecting a new snapshot
                client.sequence.append(_ph(ProviderState.OK))
                await watcher._refresh_once()  # type: ignore[attr-defined]
                # now unblocked
                snap = await asyncio.wait_for(wait_task, timeout=0.5)
                assert snap is not None and snap.state == ProviderState.OK
                return True
            finally:
                await watcher.stop()

        assert asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────────────
# ProbeResult parsing edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestProbeResultParsing:
    def test_missing_fields_default(self):
        snap = ProviderHealth.from_payload({
            "status": "degraded",
            "provider": {"probes": [{"alias": "p"}]},
        })
        assert len(snap.probes) == 1
        p = snap.probes[0]
        assert isinstance(p, ProbeResult)
        assert p.alias == "p"
        assert p.target == ""
        assert p.status == ProviderState.DOWN
        assert p.latency_ms is None
        assert p.http_status is None
        assert p.error is None
