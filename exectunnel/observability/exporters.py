"""Metric-snapshot exporters for exectunnel observability.

Three built-in exporters are provided:

* **log** — emits a summary via the Python logger.
* **file** — appends JSONL to a local file (bounded by a size guard).
* **http** — POSTs JSON to a remote endpoint with retry + back-off.

Platform-specific payload wrappers are available for Datadog, Splunk,
and New Relic; the default is a generic envelope.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib import error as urllib_error
from urllib import request as urllib_request

from .tracing import current_span_id, current_trace_id
from .utils import parse_int_env

__all__ = [
    "EXPORTER_ERRORS",
    "Exporter",
    "build_exporters",
    "build_obs_payload",
]

# Maximum JSONL file size before the file exporter refuses to write.
_FILE_MAX_BYTES_DEFAULT = 256 * 1024 * 1024  # 256 MiB

# HTTP retry defaults.
_HTTP_MAX_RETRIES_DEFAULT = 3
_HTTP_RETRY_BACKOFF_BASE = 0.5  # seconds


# ------------------------------------------------------------------
# Header parsing
# ------------------------------------------------------------------


def parse_headers(raw: str) -> dict[str, str]:
    """Parse semicolon-separated ``key=value`` pairs into a header dict.

    Example input: ``"Authorization=Bearer tok123; X-Custom=foo"``
    """
    headers: dict[str, str] = {}
    for part in raw.split(";"):
        token = part.strip()
        if not token or "=" not in token:
            continue
        key, value = token.split("=", 1)
        key, value = key.strip(), value.strip()
        if key:
            headers[key] = value
    return headers


# ------------------------------------------------------------------
# Timestamp helper
# ------------------------------------------------------------------


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------
# Payload builders
# ------------------------------------------------------------------


def build_obs_payload(
    snapshot: dict[str, object],
    *,
    final: bool,
    platform: str,
    service: str,
) -> dict[str, object]:
    """Wrap a metrics *snapshot* in a platform-specific envelope."""
    base: dict[str, object] = {
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


# ------------------------------------------------------------------
# Exporter data class
# ------------------------------------------------------------------


@dataclass
class Exporter:
    """A named async callable that ships an observability payload somewhere."""

    name: str
    emit: Callable[[dict[str, object]], Awaitable[None]]
    failures: int = field(default=0, repr=False)


# ------------------------------------------------------------------
# Exporter errors
# ------------------------------------------------------------------

EXPORTER_ERRORS: tuple[type[Exception], ...] = (
    RuntimeError,
    OSError,
    urllib_error.URLError,
)


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------


def build_exporters(
    logger: logging.Logger,
    *,
    log_emit: Callable[[dict[str, object], dict[str, object]], object],
) -> list[Exporter]:
    """Build a list of :class:`Exporter` instances from environment config.

    Reads ``EXECTUNNEL_OBS_EXPORTERS`` (comma-separated, default ``"log"``).
    """
    names_raw = os.getenv("EXECTUNNEL_OBS_EXPORTERS", "log")
    names = [n.strip().lower() for n in names_raw.split(",") if n.strip()]
    if not names:
        names = ["log"]

    # -- shared config --
    file_path = os.getenv(
        "EXECTUNNEL_OBS_FILE_PATH",
        "exectunnel-observability.jsonl",
    )
    file_max_bytes = parse_int_env(
        "EXECTUNNEL_OBS_FILE_MAX_BYTES",
        _FILE_MAX_BYTES_DEFAULT,
        min_value=1024,
    )
    http_url = os.getenv("EXECTUNNEL_OBS_HTTP_URL", "").strip()
    try:
        http_timeout = max(0.1, float(os.getenv("EXECTUNNEL_OBS_HTTP_TIMEOUT", "5")))
    except ValueError:
        http_timeout = 5.0
    http_max_retries = parse_int_env(
        "EXECTUNNEL_OBS_HTTP_MAX_RETRIES",
        _HTTP_MAX_RETRIES_DEFAULT,
        min_value=0,
        max_value=10,
    )
    extra_headers = parse_headers(os.getenv("EXECTUNNEL_OBS_HTTP_HEADERS", ""))

    # ------------------------------------------------------------------
    # Log exporter
    # ------------------------------------------------------------------

    async def _emit_log(payload: dict[str, object]) -> None:
        snapshot = payload.get("metrics")
        if isinstance(snapshot, dict):
            log_emit(snapshot, payload)

    # ------------------------------------------------------------------
    # File exporter
    # ------------------------------------------------------------------

    def _write_file(payload: dict[str, object]) -> None:
        # NOTE: There is an inherent TOCTOU race between getsize() and the
        # subsequent open().  This is acceptable as a best-effort safety
        # guard; exact enforcement would require advisory locking.
        try:
            current_size = os.path.getsize(file_path)
        except FileNotFoundError:
            current_size = 0
        if current_size >= file_max_bytes:
            raise OSError(
                f"observability file {file_path!r} reached size limit "
                f"({current_size} >= {file_max_bytes} bytes); skipping write",
            )
        with open(file_path, "a", encoding="utf-8") as fp:
            fp.write(
                json.dumps(
                    payload,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )

    async def _emit_file(payload: dict[str, object]) -> None:
        await asyncio.to_thread(_write_file, payload)

    # ------------------------------------------------------------------
    # HTTP exporter (retry + exponential back-off)
    # ------------------------------------------------------------------

    def _post_http(payload: dict[str, object]) -> None:
        body = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", **extra_headers}
        last_exc: Exception | None = None
        attempts = max(1, http_max_retries + 1)
        for attempt in range(attempts):
            req = urllib_request.Request(
                http_url,
                data=body,
                headers=headers,
                method="POST",
            )
            try:
                with urllib_request.urlopen(req, timeout=http_timeout) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(
                            f"HTTP exporter error status: {resp.status}",
                        )
                return
            except (urllib_error.URLError, OSError, RuntimeError) as exc:
                last_exc = exc
                if attempt < http_max_retries:
                    time.sleep(_HTTP_RETRY_BACKOFF_BASE * (2**attempt))
        raise last_exc  # type: ignore[misc]

    async def _emit_http(payload: dict[str, object]) -> None:
        if not http_url:
            raise RuntimeError("EXECTUNNEL_OBS_HTTP_URL is not set")
        await asyncio.to_thread(_post_http, payload)

    # ------------------------------------------------------------------
    # Assemble
    # ------------------------------------------------------------------

    exporters: list[Exporter] = []
    for name in names:
        match name:
            case "log":
                exporters.append(Exporter(name="log", emit=_emit_log))
            case "file":
                exporters.append(Exporter(name="file", emit=_emit_file))
            case "http":
                if not http_url:
                    logger.warning(
                        "observability exporter 'http' requested but "
                        "EXECTUNNEL_OBS_HTTP_URL is empty; skipping",
                    )
                    continue
                exporters.append(Exporter(name="http", emit=_emit_http))
            case _:
                logger.warning("unknown observability exporter %r; skipping", name)
    return exporters
