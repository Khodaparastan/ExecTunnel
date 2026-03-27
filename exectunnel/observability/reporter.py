from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os

from exectunnel.observability.exporters import (
    EXPORTER_ERRORS,
    Exporter,
    build_exporters,
    build_obs_payload,
    parse_bool_env,
)
from exectunnel.observability.metrics import metrics_snapshot


async def run_metrics_reporter(
    interval_sec: float,
    stop_event: asyncio.Event,
    *,
    logger_name: str = "exectunnel.metrics",
) -> None:
    logger = logging.getLogger(logger_name)
    verbose = parse_bool_env("EXECTUNNEL_METRICS_VERBOSE", False)
    try:
        top_n = max(1, int(os.getenv("EXECTUNNEL_METRICS_TOP_N", "12")))
    except ValueError:
        top_n = 12

    log_level_env = os.getenv("EXECTUNNEL_METRICS_LOG_LEVEL", "debug").strip().lower()
    log_fn = logger.info if log_level_env == "info" else logger.debug
    obs_platform = os.getenv("EXECTUNNEL_OBS_PLATFORM", "generic").strip() or "generic"
    obs_service = (
        os.getenv("EXECTUNNEL_OBS_SERVICE", "exectunnel").strip() or "exectunnel"
    )

    def emit_log(snapshot: dict[str, object], payload: dict[str, object]) -> None:
        final = bool(payload.get("final", False))
        keys = sorted(snapshot)
        top_keys = keys[:top_n]
        summary = {
            "total_metrics": len(keys),
            "sample_keys": top_keys,
            "truncated": len(keys) > top_n,
            "final": final,
        }
        log_fn("metrics_summary=%s", json.dumps(summary, sort_keys=True))
        if verbose:
            logger.debug("metrics_snapshot=%s", json.dumps(snapshot, sort_keys=True))

    exporters = build_exporters(logger, log_emit=emit_log)
    if not exporters:

        async def emit_log_only(payload: dict[str, object]) -> None:
            snapshot = payload.get("metrics")
            if isinstance(snapshot, dict):
                emit_log(snapshot, payload)

        exporters = [Exporter(name="log", emit=emit_log_only)]

    async def emit_snapshot(final: bool) -> None:
        snapshot = metrics_snapshot()
        payload = build_obs_payload(
            snapshot,
            final=final,
            platform=obs_platform,
            service=obs_service,
        )
        for exporter in exporters:
            try:
                await exporter.emit(payload)
                if exporter.failures > 0:
                    logger.info(
                        "observability exporter '%s' recovered after %d failures",
                        exporter.name,
                        exporter.failures,
                    )
                    exporter.failures = 0
            except EXPORTER_ERRORS as exc:
                exporter.failures += 1
                if exporter.failures == 1 or exporter.failures % 20 == 0:
                    logger.warning(
                        "observability exporter '%s' failed (%d): %s",
                        exporter.name,
                        exporter.failures,
                        exc,
                    )
                    logger.debug(
                        "observability exporter '%s' traceback",
                        exporter.name,
                        exc_info=True,
                    )
            except Exception as exc:
                exporter.failures += 1
                if exporter.failures == 1 or exporter.failures % 20 == 0:
                    logger.warning(
                        "observability exporter '%s' unexpected failure (%d): %s",
                        exporter.name,
                        exporter.failures,
                        exc,
                    )
                    logger.debug(
                        "observability exporter '%s' unexpected traceback",
                        exporter.name,
                        exc_info=True,
                    )

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
            break
        except TimeoutError:
            await emit_snapshot(final=False)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug("metrics reporter failure: %s", exc, exc_info=True)
    with contextlib.suppress(Exception):
        await emit_snapshot(final=True)
