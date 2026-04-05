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

from .tracing import current_span_id, current_trace_id
from .utils import parse_int_env

# Maximum JSONL file size before the file exporter stops writing (safety guard).
# Prevents unbounded disk growth when rotation is handled externally (logrotate).
_FILE_MAX_BYTES_DEFAULT = 256 * 1024 * 1024  # 256 MiB

# HTTP retry settings
_HTTP_MAX_RETRIES = 3
_HTTP_RETRY_BACKOFF_BASE = 0.5  # seconds


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
    log_emit: Callable[[dict[str, object], dict[str, object]], object],
) -> list[Exporter]:
    names_raw = os.getenv("EXECTUNNEL_OBS_EXPORTERS", "log")
    names = [n.strip().lower() for n in names_raw.split(",") if n.strip()]
    if not names:
        names = ["log"]

    file_path = os.getenv("EXECTUNNEL_OBS_FILE_PATH", "exectunnel-observability.jsonl")
    file_max_bytes = parse_int_env(
        "EXECTUNNEL_OBS_FILE_MAX_BYTES",
        _FILE_MAX_BYTES_DEFAULT,
        min_value=1024,
    )
    http_url = os.getenv("EXECTUNNEL_OBS_HTTP_URL", "").strip()
    http_timeout_raw = os.getenv("EXECTUNNEL_OBS_HTTP_TIMEOUT", "5")
    try:
        http_timeout = max(0.1, float(http_timeout_raw))
    except ValueError:
        http_timeout = 5.0
    http_max_retries = parse_int_env(
        "EXECTUNNEL_OBS_HTTP_MAX_RETRIES", _HTTP_MAX_RETRIES, min_value=0, max_value=10
    )
    extra_headers = parse_headers(os.getenv("EXECTUNNEL_OBS_HTTP_HEADERS", ""))

    # ------------------------------------------------------------------
    # log exporter
    # ------------------------------------------------------------------

    async def emit_log(payload: dict[str, object]) -> None:
        snapshot = payload.get("metrics")
        if isinstance(snapshot, dict):
            log_emit(snapshot, payload)

    # ------------------------------------------------------------------
    # file exporter
    # ------------------------------------------------------------------

    def _write_file(payload: dict[str, object]) -> None:
        try:
            current_size = os.path.getsize(file_path)
        except FileNotFoundError:
            current_size = 0
        if current_size >= file_max_bytes:
            raise OSError(
                f"observability file {file_path!r} reached size limit "
                f"({current_size} >= {file_max_bytes} bytes); skipping write"
            )
        with open(file_path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")

    async def emit_file(payload: dict[str, object]) -> None:
        await asyncio.to_thread(_write_file, payload)

    # ------------------------------------------------------------------
    # HTTP exporter (with retry + exponential backoff)
    # ------------------------------------------------------------------

    def _post_http(payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        headers.update(extra_headers)
        req = urllib_request.Request(
            http_url, data=body, headers=headers, method="POST"
        )
        last_exc: Exception | None = None
        for attempt in range(max(1, http_max_retries + 1)):
            try:
                with urllib_request.urlopen(req, timeout=http_timeout) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(
                            f"HTTP exporter error status: {resp.status}"
                        )
                return  # success
            except (urllib_error.URLError, OSError, RuntimeError) as exc:
                last_exc = exc
                if attempt < http_max_retries:
                    time.sleep(_HTTP_RETRY_BACKOFF_BASE * (2**attempt))
        raise last_exc  # type: ignore[misc]

    async def emit_http(payload: dict[str, object]) -> None:
        if not http_url:
            raise RuntimeError("EXECTUNNEL_OBS_HTTP_URL is not set")
        await asyncio.to_thread(_post_http, payload)

    # ------------------------------------------------------------------
    # Assemble exporter list
    # ------------------------------------------------------------------

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
                    "observability exporter 'http' requested but "
                    "EXECTUNNEL_OBS_HTTP_URL is empty; skipping"
                )
                continue
            exporters.append(Exporter(name="http", emit=emit_http))
            continue
        logger.warning("unknown observability exporter %r; skipping", name)
    return exporters


EXPORTER_ERRORS = (RuntimeError, OSError, urllib_error.URLError)
