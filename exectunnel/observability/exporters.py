from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib import error as urllib_error
from urllib import request as urllib_request

from exectunnel.observability.tracing import current_span_id, current_trace_id


def parse_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for part in raw.split(";"):
        token = part.strip()
        if not token or "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            headers[key] = value
    return headers


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_obs_payload(
    snapshot: dict[str, object],
    *,
    final: bool,
    platform: str,
    service: str,
) -> dict[str, object]:
    base = {
        "timestamp": utc_now_iso(),
        "service": service,
        "platform": platform,
        "trace_id": current_trace_id() or "-",
        "span_id": current_span_id() or "-",
        "final": final,
        "metrics": snapshot,
    }
    normalized = platform.strip().lower()
    if normalized == "datadog":
        return {
            "ddsource": "exectunnel",
            "service": service,
            "message": "exectunnel.metrics_snapshot",
            "attributes": base,
        }
    if normalized == "splunk":
        return {
            "time": time.time(),
            "host": socket.gethostname(),
            "source": "exectunnel",
            "event": base,
        }
    if normalized == "newrelic":
        return {
            "common": {"attributes": {"service.name": service, "platform": platform}},
            "events": [base],
        }
    return base


@dataclass
class Exporter:
    name: str
    emit: Callable[[dict[str, object]], Awaitable[None]]
    failures: int = 0


def build_exporters(
    logger: logging.Logger,
    *,
    log_emit: Callable[..., object],  # sync or async helper
) -> list[Exporter]:
    names_raw = os.getenv("EXECTUNNEL_OBS_EXPORTERS", "log")
    names = [n.strip().lower() for n in names_raw.split(",") if n.strip()]
    if not names:
        names = ["log"]

    file_path = os.getenv("EXECTUNNEL_OBS_FILE_PATH", "exectunnel-observability.jsonl")
    http_url = os.getenv("EXECTUNNEL_OBS_HTTP_URL", "").strip()
    http_timeout_raw = os.getenv("EXECTUNNEL_OBS_HTTP_TIMEOUT", "5")
    try:
        http_timeout = max(0.1, float(http_timeout_raw))
    except ValueError:
        http_timeout = 5.0
    extra_headers = parse_headers(os.getenv("EXECTUNNEL_OBS_HTTP_HEADERS", ""))

    async def emit_log(payload: dict[str, object]) -> None:
        snapshot = payload.get("metrics")
        if isinstance(snapshot, dict):
            log_emit(snapshot, payload)

    def _write_file(payload: dict[str, object]) -> None:
        with open(file_path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")

    async def emit_file(payload: dict[str, object]) -> None:
        await asyncio.to_thread(_write_file, payload)

    def _post_http(payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        headers.update(extra_headers)
        req = urllib_request.Request(
            http_url, data=body, headers=headers, method="POST"
        )
        with urllib_request.urlopen(req, timeout=http_timeout) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP exporter error status: {resp.status}")

    async def emit_http(payload: dict[str, object]) -> None:
        if not http_url:
            raise RuntimeError("EXECTUNNEL_OBS_HTTP_URL is not set")
        await asyncio.to_thread(_post_http, payload)

    exporters: list[Exporter] = []
    for name in names:
        if name == "log":
            exporters.append(Exporter(name="log", emit=emit_log))
            continue
        if name == "file":
            exporters.append(Exporter(name="file", emit=emit_file))
            continue
        if name == "http":
            if not http_url:
                logger.warning(
                    "observability exporter 'http' requested but EXECTUNNEL_OBS_HTTP_URL is empty; skipping"
                )
                continue
            exporters.append(Exporter(name="http", emit=emit_http))
            continue
        logger.warning("unknown observability exporter %r; skipping", name)
    return exporters


EXPORTER_ERRORS = (RuntimeError, OSError, urllib_error.URLError)
