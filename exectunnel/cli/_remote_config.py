"""Remote config source + provider/gateway health watcher.

Two collaborating pieces of the parent-only control plane:

* :class:`RemoteConfigClient` — fetches the tunnel config from the central
  identity API and writes it to a process-private cache file that the existing
  :class:`~exectunnel.cli._context.AppContext` loader can pick up. The same
  client also fetches ``/api/v1/health`` for provider/gateway state.
* :class:`ProviderHealthWatcher` — long-running asyncio task that polls
  ``/api/v1/health`` at a cadence that adapts to the observed state and
  publishes :class:`ProviderHealth` snapshots to subscribers (typically the
  :class:`Supervisor` and the :class:`UnifiedDashboard`).

Both pieces are STRICTLY parent-only. Workers must never reach into them.

Wire contract (from the issue):

* ``GET {base}/api/v1/configs/identities?format={toml|yaml}&identity={id}``
  returns the raw TOML/YAML body for the identity's tunnel config.
* ``GET {base}/api/v1/health`` returns the provider/gateway health JSON
  with ``status`` and per-component fields.

Auth: the client may send ``Authorization: Bearer <token>`` if configured.
This token is independent of any per-tunnel WSS auth headers — it is only
used to talk to the identity / health API.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import logging
import os
import random
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal

import httpx

__all__ = [
    "DEFAULT_REMOTE_CONFIG_TIMEOUT_SECS",
    "ProbeResult",
    "ProviderHealth",
    "ProviderHealthWatcher",
    "ProviderState",
    "RemoteConfigClient",
    "RemoteConfigError",
    "RemoteConfigSource",
    "default_cache_path_for",
]

logger = logging.getLogger("exectunnel.cli.remote_config")


DEFAULT_REMOTE_CONFIG_TIMEOUT_SECS: Final[float] = 10.0

#: Polling cadence by provider state (seconds).
_POLL_INTERVALS: Final[dict[str, float]] = {
    "ok": 60.0,
    "degraded": 15.0,
    "down": 5.0,
}

_POLL_JITTER_RATIO: Final[float] = 0.15

_HEALTH_PATH: Final[str] = "/api/v1/health"
_CONFIGS_PATH: Final[str] = "/api/v1/configs/identities"

_IDENTITY_FILENAME_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9_.-]")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RemoteConfigError(Exception):
    """Raised when a remote config or health fetch fails.

    Attributes:
        url:        URL that was attempted.
        status:     HTTP status code if a response was received, else ``None``.
        retryable:  ``True`` for transient (5xx, network) failures, ``False``
                    for permanent (auth, bad request) failures.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status = status
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Health models
# ---------------------------------------------------------------------------


class ProviderState(StrEnum):
    """Tri-state provider/gateway/probe health, mirrored from the API."""

    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"

    @classmethod
    def parse(cls, raw: object) -> ProviderState:
        """Parse loosely — unknown values map to ``DOWN`` (most conservative)."""
        if isinstance(raw, ProviderState):
            return raw
        if isinstance(raw, str):
            try:
                return cls(raw.strip().lower())
            except ValueError:
                pass
        return cls.DOWN


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One per-target probe result from the health payload."""

    alias: str
    target: str
    status: ProviderState
    latency_ms: float | None
    http_status: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Parsed snapshot of ``/api/v1/health``.

    The ``raw`` dict is preserved verbatim so downstream consumers (e.g. the
    dashboard) can render any vendor extensions without round-tripping through
    typed fields.
    """

    state: ProviderState
    checked_at: _dt.datetime
    cached: bool

    gateway_status: ProviderState = ProviderState.DOWN
    gateway_version: str | None = None
    gateway_uptime_seconds: float | None = None

    identities_status: ProviderState = ProviderState.DOWN
    identities_authenticated: tuple[str, ...] = ()
    identities_unauthenticated: tuple[str, ...] = ()

    provider_name: str | None = None
    provider_base_url: str | None = None
    probes: tuple[ProbeResult, ...] = ()

    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ProviderHealth:
        """Build a :class:`ProviderHealth` from a parsed JSON object."""
        gateway = payload.get("gateway", {}) or {}
        identities = payload.get("identities", {}) or {}
        provider = payload.get("provider", {}) or {}

        probes_raw = provider.get("probes", []) or []
        probes: list[ProbeResult] = []
        for entry in probes_raw:
            if not isinstance(entry, dict):
                continue
            probes.append(
                ProbeResult(
                    alias=str(entry.get("alias") or ""),
                    target=str(entry.get("target") or ""),
                    status=ProviderState.parse(entry.get("status")),
                    latency_ms=_to_float(entry.get("latency_ms")),
                    http_status=_to_int(entry.get("http_status")),
                    error=entry.get("error")
                    if isinstance(entry.get("error"), str)
                    else None,
                )
            )

        checked_at_raw = payload.get("checked_at")
        checked_at = _parse_iso8601(checked_at_raw) or _dt.datetime.now(_dt.UTC)

        return cls(
            state=ProviderState.parse(payload.get("status")),
            checked_at=checked_at,
            cached=bool(payload.get("cached", False)),
            gateway_status=ProviderState.parse(gateway.get("status")),
            gateway_version=_to_str_or_none(gateway.get("version")),
            gateway_uptime_seconds=_to_float(gateway.get("uptime_seconds")),
            identities_status=ProviderState.parse(identities.get("status")),
            identities_authenticated=_to_str_tuple(identities.get("authenticated")),
            identities_unauthenticated=_to_str_tuple(identities.get("unauthenticated")),
            provider_name=_to_str_or_none(provider.get("name")),
            provider_base_url=_to_str_or_none(provider.get("base_url")),
            probes=tuple(probes),
            raw=payload,
        )

    @property
    def is_blocking(self) -> bool:
        """``True`` when new worker spawns should be paused."""
        return self.state == ProviderState.DOWN

    @property
    def is_degraded(self) -> bool:
        """``True`` when the provider is reachable but unhealthy."""
        return self.state == ProviderState.DEGRADED


def _parse_iso8601(value: object) -> _dt.datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # datetime.fromisoformat in Python 3.11+ handles trailing 'Z' from 3.13+.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def _to_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _to_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _to_str_or_none(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _to_str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str))


# ---------------------------------------------------------------------------
# RemoteConfigSource — frozen dataclass with the immutable bits
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RemoteConfigSource:
    """Immutable configuration for a remote config endpoint.

    Args:
        base_url:    Base URL of the identity API (no trailing slash needed).
        identity:    Identity / user id passed via ``identity=`` query param.
        fmt:         ``"toml"`` or ``"yaml"`` — wire format for the config body.
        token:       Optional bearer token sent as ``Authorization`` header.
        timeout:     Per-request timeout in seconds.
        verify_tls:  When ``False``, disables TLS verification (NOT recommended).
    """

    base_url: str
    identity: str
    fmt: Literal["toml", "yaml"] = "toml"
    token: str | None = None
    timeout: float = DEFAULT_REMOTE_CONFIG_TIMEOUT_SECS
    verify_tls: bool = True

    def __post_init__(self) -> None:  # pragma: no cover — dataclass hook
        if not self.base_url:
            raise ValueError("RemoteConfigSource.base_url must not be empty")
        if not self.identity:
            raise ValueError("RemoteConfigSource.identity must not be empty")
        if self.fmt not in ("toml", "yaml"):
            raise ValueError(
                f"RemoteConfigSource.fmt must be 'toml' or 'yaml', got {self.fmt!r}"
            )
        if self.timeout <= 0:
            raise ValueError("RemoteConfigSource.timeout must be > 0")

    @property
    def normalized_base_url(self) -> str:
        """Return ``base_url`` with any trailing slash stripped."""
        return self.base_url.rstrip("/")

    def auth_headers(self) -> dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}


def default_cache_path_for(source: RemoteConfigSource) -> Path:
    """Return the default on-disk cache path for ``source``.

    Lives under ``$XDG_CACHE_HOME/exectunnel`` (or ``~/.cache/exectunnel``)
    and is namespaced by identity so multiple identities don't clobber each
    other.
    """
    base_env = os.environ.get("XDG_CACHE_HOME")
    base = Path(base_env).expanduser() if base_env else Path.home() / ".cache"
    safe_identity = _IDENTITY_FILENAME_RE.sub("_", source.identity) or "default"
    return base / "exectunnel" / f"remote-{safe_identity}.{source.fmt}"


# ---------------------------------------------------------------------------
# RemoteConfigClient — the actual HTTP work
# ---------------------------------------------------------------------------


class RemoteConfigClient:
    """Async client for the identity / health API.

    Owns no global state. Each call opens a short-lived :class:`httpx.AsyncClient`
    so misbehaving servers or DNS hiccups don't leak connection pools across
    long supervisor lifetimes.
    """

    __slots__ = ("_source",)

    def __init__(self, source: RemoteConfigSource) -> None:
        self._source = source

    @property
    def source(self) -> RemoteConfigSource:
        return self._source

    async def fetch_config(self) -> bytes:
        """Fetch the raw config body for the configured identity.

        Returns:
            Raw response body bytes — caller is responsible for writing it
            to disk and triggering re-parse.

        Raises:
            RemoteConfigError: For any non-200 response or transport failure.
        """
        url = f"{self._source.normalized_base_url}{_CONFIGS_PATH}"
        params = {"format": self._source.fmt, "identity": self._source.identity}
        headers = self._source.auth_headers()

        try:
            async with httpx.AsyncClient(
                timeout=self._source.timeout,
                verify=self._source.verify_tls,
            ) as client:
                response = await client.get(url, params=params, headers=headers)
        except (httpx.RequestError, OSError) as exc:
            raise RemoteConfigError(
                f"Network error fetching remote config: {exc}",
                url=url,
                status=None,
                retryable=True,
            ) from exc

        if response.status_code in (401, 403):
            raise RemoteConfigError(
                f"Remote config rejected with HTTP {response.status_code}",
                url=str(response.request.url),
                status=response.status_code,
                retryable=False,
            )

        if response.status_code >= 500:
            raise RemoteConfigError(
                f"Remote config server error HTTP {response.status_code}",
                url=str(response.request.url),
                status=response.status_code,
                retryable=True,
            )

        if response.status_code != 200:
            raise RemoteConfigError(
                f"Remote config unexpected HTTP {response.status_code}",
                url=str(response.request.url),
                status=response.status_code,
                retryable=False,
            )

        return response.content

    async def fetch_health(self) -> ProviderHealth:
        """Fetch the provider/gateway health snapshot."""
        url = f"{self._source.normalized_base_url}{_HEALTH_PATH}"
        headers = self._source.auth_headers()

        try:
            async with httpx.AsyncClient(
                timeout=self._source.timeout,
                verify=self._source.verify_tls,
            ) as client:
                response = await client.get(url, headers=headers)
        except (httpx.RequestError, OSError) as exc:
            # Treat transport failures as a synthetic ``down`` snapshot rather
            # than raising; the watcher uses the snapshot for gating decisions.
            return ProviderHealth(
                state=ProviderState.DOWN,
                checked_at=_dt.datetime.now(_dt.UTC),
                cached=False,
                raw={"error": str(exc)},
            )

        if response.status_code != 200:
            return ProviderHealth(
                state=ProviderState.DOWN,
                checked_at=_dt.datetime.now(_dt.UTC),
                cached=False,
                raw={
                    "error": f"http_{response.status_code}",
                    "status_code": response.status_code,
                },
            )

        try:
            payload = response.json()
        except ValueError as exc:
            logger.debug("health payload not JSON: %s", exc)
            return ProviderHealth(
                state=ProviderState.DOWN,
                checked_at=_dt.datetime.now(_dt.UTC),
                cached=False,
                raw={"error": "invalid_json"},
            )

        if not isinstance(payload, dict):
            return ProviderHealth(
                state=ProviderState.DOWN,
                checked_at=_dt.datetime.now(_dt.UTC),
                cached=False,
                raw={"error": "non_object_payload"},
            )

        return ProviderHealth.from_payload(payload)


# ---------------------------------------------------------------------------
# Atomic cache write helper
# ---------------------------------------------------------------------------


def write_cache_atomically(path: Path, body: bytes) -> Path:
    """Atomically write ``body`` to ``path``, creating parent dirs as needed.

    Atomicity is achieved with a temp file + ``os.replace`` to ensure
    concurrent readers (e.g. config validators) never see a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        return path
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


# ---------------------------------------------------------------------------
# ProviderHealthWatcher — long-running poller with adaptive cadence
# ---------------------------------------------------------------------------


HealthSubscriber = Callable[[ProviderHealth], None]


class ProviderHealthWatcher:
    """Polls ``/api/v1/health`` and notifies subscribers on every snapshot.

    Cadence adapts to the last observed state:

    * ``ok``       → poll every 60s
    * ``degraded`` → poll every 15s
    * ``down``     → poll every 5s

    A small jitter (±15%) is applied to avoid synchronized retries across
    a fleet of supervisors hitting the same control plane.

    The watcher exposes :meth:`is_blocking` and :meth:`wait_until_unblocked`
    for the supervisor's gate logic — these query the latest snapshot without
    requiring a callback to be installed.
    """

    __slots__ = (
        "_client",
        "_subscribers",
        "_task",
        "_stop",
        "_latest",
        "_unblocked",
        "_lock",
    )

    def __init__(self, client: RemoteConfigClient) -> None:
        self._client = client
        self._subscribers: list[HealthSubscriber] = []
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._latest: ProviderHealth | None = None
        self._unblocked = asyncio.Event()
        # Default to "unblocked" until we know otherwise — matches today's
        # behaviour where workers spawn unconditionally.
        self._unblocked.set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Subscription API (synchronous)
    # ------------------------------------------------------------------

    def subscribe(self, callback: HealthSubscriber) -> None:
        """Register ``callback`` to receive every snapshot.

        Callbacks MUST be cheap and non-raising; this watcher swallows any
        exception they throw to keep polling alive.
        """
        self._subscribers.append(callback)
        if self._latest is not None:
            self._notify_one(callback, self._latest)

    def unsubscribe(self, callback: HealthSubscriber) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(callback)

    @property
    def latest(self) -> ProviderHealth | None:
        return self._latest

    def is_blocking(self) -> bool:
        """``True`` if the latest snapshot is ``down`` (gate held closed)."""
        return self._latest is not None and self._latest.is_blocking

    async def wait_until_unblocked(self) -> ProviderHealth | None:
        """Block until the gate is open. Returns the latest snapshot if any."""
        await self._unblocked.wait()
        return self._latest

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start polling. Performs an immediate first probe before returning."""
        if self._task is not None:
            return

        await self._refresh_once()
        self._task = asyncio.create_task(self._run(), name="provider-health-watcher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        while not self._stop.is_set():
            interval = self._compute_interval()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return  # stop requested during sleep
            except TimeoutError:
                pass

            await self._refresh_once()

    def _compute_interval(self) -> float:
        state = self._latest.state if self._latest is not None else ProviderState.OK
        base = _POLL_INTERVALS.get(state.value, 60.0)
        jitter = base * _POLL_JITTER_RATIO
        return base + random.uniform(-jitter, jitter)

    async def _refresh_once(self) -> None:
        async with self._lock:
            try:
                snapshot = await self._client.fetch_health()
            except Exception as exc:  # defensive — fetch_health doesn't raise today
                logger.warning("health probe raised: %s", exc)
                snapshot = ProviderHealth(
                    state=ProviderState.DOWN,
                    checked_at=_dt.datetime.now(_dt.UTC),
                    cached=False,
                    raw={"error": str(exc)},
                )

            previous = self._latest
            self._latest = snapshot

            if snapshot.is_blocking:
                if previous is None or not previous.is_blocking:
                    logger.warning(
                        "provider health: %s — pausing worker respawns",
                        snapshot.state.value,
                    )
                self._unblocked.clear()
            else:
                if previous is not None and previous.is_blocking:
                    logger.info(
                        "provider health recovered: %s",
                        snapshot.state.value,
                    )
                self._unblocked.set()

            for sub in list(self._subscribers):
                self._notify_one(sub, snapshot)

    @staticmethod
    def _notify_one(sub: HealthSubscriber, snapshot: ProviderHealth) -> None:
        try:
            sub(snapshot)
        except Exception:
            logger.debug("health subscriber raised", exc_info=True)
