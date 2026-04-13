"""Async periodic metrics reporter for exectunnel.

Periodically snapshots the in-process metrics registry and ships the
payload to every configured :class:`~.exporters.Exporter`.
"""

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

__all__ = ["run_metrics_reporter"]

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
        Logger name for debug messages.
    """
    event = stop_event if stop_event is not None else asyncio.Event()

    if interval_sec is None:
        interval_sec = parse_float_env(
            "EXECTUNNEL_METRICS_INTERVAL_SEC",
            _DEFAULT_INTERVAL_SEC,
            min_value=_MIN_INTERVAL_SEC,
            max_value=_MAX_INTERVAL_SEC,
        )

    logger = logging.getLogger(logger_name)
    verbose = parse_bool_env("EXECTUNNEL_METRICS_VERBOSE")
    top_n = parse_int_env("EXECTUNNEL_METRICS_TOP_N", 12, min_value=1)

    log_fn = (
        logger.info
        if parse_bool_env("EXECTUNNEL_METRICS_LOG_LEVEL_INFO")
        else logger.debug
    )

    obs_platform = os.getenv("EXECTUNNEL_OBS_PLATFORM", "generic").strip() or "generic"
    obs_service = (
        os.getenv("EXECTUNNEL_OBS_SERVICE", "exectunnel").strip() or "exectunnel"
    )

    # ------------------------------------------------------------------
    # Log helper shared between the real log exporter and the fallback
    # ------------------------------------------------------------------

    def _emit_log(snapshot: dict[str, object], payload: dict[str, object]) -> None:
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
            logger.debug(
                "metrics_snapshot=%s",
                json.dumps(snapshot, sort_keys=True),
            )

    # ------------------------------------------------------------------
    # Build exporters (fall back to log-only if none configured)
    # ------------------------------------------------------------------

    exporters = build_exporters(logger, log_emit=_emit_log)
    if not exporters:

        async def _fallback_emit(payload: dict[str, object]) -> None:
            snapshot = payload.get("metrics")
            if isinstance(snapshot, dict):
                _emit_log(snapshot, payload)

        exporters = [Exporter(name="log", emit=_fallback_emit)]

    # ------------------------------------------------------------------
    # Emit one snapshot to all exporters
    # ------------------------------------------------------------------

    async def _emit_snapshot(*, final: bool) -> None:
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
                _handle_exporter_failure(exporter, exc, expected=True)
            except Exception as exc:  # noqa: BLE001
                _handle_exporter_failure(exporter, exc, expected=False)

    def _handle_exporter_failure(
        exporter: Exporter,
        exc: Exception,
        *,
        expected: bool,
    ) -> None:
        exporter.failures += 1
        if exporter.failures == 1 or exporter.failures % 20 == 0:
            kind = "" if expected else " unexpected"
            logger.warning(
                "observability exporter '%s'%s failure (%d): %s",
                exporter.name,
                kind,
                exporter.failures,
                exc,
            )
            logger.debug(
                "observability exporter '%s' traceback",
                exporter.name,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    while not event.is_set():
        try:
            await asyncio.wait_for(event.wait(), timeout=interval_sec)
            break  # stop_event was set
        except TimeoutError:
            await _emit_snapshot(final=False)
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001
            logger.debug("metrics reporter failure: %s", exc, exc_info=True)

    # Always attempt a final flush.
    with contextlib.suppress(Exception):
        await _emit_snapshot(final=True)
