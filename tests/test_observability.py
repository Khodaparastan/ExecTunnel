"""Tests for exectunnel.observability."""
from __future__ import annotations

import asyncio
import json

import pytest

from exectunnel.observability import (
    METRICS,
    metrics_inc,
    metrics_observe,
    metrics_reset,
    run_metrics_reporter,
)
from exectunnel.observability.exporters import build_obs_payload


def test_metrics_reset_clears_registry() -> None:
    metrics_reset()
    metrics_inc("test.counter")
    metrics_observe("test.hist", 1.0)
    assert METRICS.snapshot()
    metrics_reset()
    assert METRICS.snapshot() == {}


def test_build_obs_payload_variants() -> None:
    snapshot = {"demo.metric": 1}

    generic = build_obs_payload(
        snapshot,
        final=False,
        platform="generic",
        service="exectunnel",
    )
    assert generic["metrics"] == snapshot
    assert generic["platform"] == "generic"

    datadog = build_obs_payload(
        snapshot,
        final=True,
        platform="datadog",
        service="exectunnel",
    )
    assert datadog["ddsource"] == "exectunnel"
    assert "attributes" in datadog

    splunk = build_obs_payload(
        snapshot,
        final=False,
        platform="splunk",
        service="exectunnel",
    )
    assert "event" in splunk
    assert "time" in splunk

    newrelic = build_obs_payload(
        snapshot,
        final=False,
        platform="newrelic",
        service="exectunnel",
    )
    assert "events" in newrelic
    assert "common" in newrelic


@pytest.mark.asyncio
async def test_run_metrics_reporter_exports_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    metrics_reset()
    out_path = tmp_path / "obs.jsonl"
    monkeypatch.setenv("EXECTUNNEL_OBS_EXPORTERS", "file")
    monkeypatch.setenv("EXECTUNNEL_OBS_FILE_PATH", str(out_path))
    monkeypatch.setenv("EXECTUNNEL_OBS_PLATFORM", "generic")
    monkeypatch.setenv("EXECTUNNEL_OBS_SERVICE", "exectunnel-test")

    metrics_inc("demo.counter")
    stop_event = asyncio.Event()
    task = asyncio.create_task(run_metrics_reporter(0.05, stop_event))
    await asyncio.sleep(0.07)
    stop_event.set()
    await task

    lines = [line for line in out_path.read_text(encoding="utf-8").splitlines() if line]
    assert lines
    payload = json.loads(lines[-1])
    assert payload["service"] == "exectunnel-test"
    assert "metrics" in payload
    assert "demo.counter" in payload["metrics"]
