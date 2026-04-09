from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os

from .exporters import (
    EXPORTER_ERRORS,
    Exporter,
    build_exporters,
    build_obs_payload,
)
from .metrics import metrics_snapshot
from .utils import parse_bool_env, parse_float_env, parse_int_env

# Default reporting interval when the caller does not supply one explicitly.
_DEFAULT_INTERVAL_SEC = 30.0
_MIN_INTERVAL_SEC = 1.0
_MAX_INTERVAL_SEC = 3600.0


async def run_metrics_reporter(
    interval_sec: float | None = None,
    stop_event: asyncio.Event | None = None,
    *,
    logger_name: str = "exectunnel.metrics",
) -> None:
    """Periodically emit a metrics snapshot to all configured exporters.

    Parameters
    ----------
    interval_sec:
        Seconds between snapshots.  When *None* the value is read from
        ``EXECTUNNEL_METRICS_INTERVAL_SEC`` (default 30 s).
    stop_event:
        Signals the reporter to flush a final snapshot and exit.  When
        *None* a private event is created (the reporter runs until cancelled).
    logger_name:
        Logger name for debug messages.  Defaults to ``exectunnel.metrics``.
    """
    event: asyncio.Event = stop_event if stop_event is not None else asyncio.Event()

    if interval_sec is None:
        interval_sec = parse_float_env(
            "EXECTUNNEL_METRICS_INTERVAL_SEC",
            _DEFAULT_INTERVAL_SEC,
            min_value=_MIN_INTERVAL_SEC,
            max_value=_MAX_INTERVAL_SEC,
        )

    logger = logging.getLogger(logger_name)
    verbose = parse_bool_env("EXECTUNNEL_METRICS_VERBOSE", False)
    top_n = parse_int_env("EXECTUNNEL_METRICS_TOP_N", 12, min_value=1)

    log_level_env = parse_bool_env("EXECTUNNEL_METRICS_LOG_LEVEL_INFO", False)
    log_fn = logger.info if log_level_env else logger.debug

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

    while not event.is_set():
        try:
            await asyncio.wait_for(event.wait(), timeout=interval_sec)
            break
        except TimeoutError:
            await emit_snapshot(final=False)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug("metrics reporter failure: %s", exc, exc_info=True)
    with contextlib.suppress(Exception):
        await emit_snapshot(final=True)
