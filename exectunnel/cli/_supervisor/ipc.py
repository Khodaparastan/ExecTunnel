"""NDJSON IPC protocol — versioned frames exchanged between worker and supervisor.

This module is the single source of truth for the wire format used between the
parent ``run`` supervisor and each ``_worker`` subprocess. The protocol is
intentionally one-way (worker → supervisor) and line-oriented:

* one JSON object per line on the worker's stdout
* every frame carries the protocol version (``v``) and a ``type`` discriminator
* the supervisor ignores any line that fails to parse

Frames are strict Pydantic v2 models. Unknown fields are rejected during local
model construction, while the decoder intentionally drops unknown keys before
validation to preserve forward compatibility with newer workers.
"""

from __future__ import annotations

import json
from typing import Final, Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "IPC_PROTOCOL_VERSION",
    "ExitFrame",
    "Frame",
    "HealthFrame",
    "LogFrame",
    "MetricFrame",
    "StatusFrame",
    "decode_frame",
    "encode_frame",
]

IPC_PROTOCOL_VERSION: Final[int] = 1


# ---------------------------------------------------------------------------
# Status values — must match the lifecycle states the dashboard understands.
# ---------------------------------------------------------------------------


TunnelStatus: TypeAlias = Literal[
    "starting",
    "running",
    "healthy",
    "unhealthy",
    "restarting",
    "stopped",
    "failed",
]

LogLevel: TypeAlias = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

MetricKindStr: TypeAlias = Literal["counter", "histogram", "gauge"]


# ---------------------------------------------------------------------------
# Base frame
# ---------------------------------------------------------------------------


_FRAME_MODEL_CONFIG: Final[ConfigDict] = ConfigDict(
    extra="forbid",
    frozen=True,
    str_strip_whitespace=True,
)


class _BaseFrame(BaseModel):
    """Common fields for every IPC frame."""

    model_config = _FRAME_MODEL_CONFIG

    v: int = Field(default=IPC_PROTOCOL_VERSION, ge=1, description="Protocol version.")
    tunnel: str = Field(min_length=1, description="Tunnel name this frame belongs to.")


# ---------------------------------------------------------------------------
# Concrete frames
# ---------------------------------------------------------------------------


class StatusFrame(_BaseFrame):
    """Tunnel lifecycle status transition."""

    type: Literal["status"] = "status"
    status: TunnelStatus


class MetricFrame(_BaseFrame):
    """Projection of one :class:`~exectunnel.observability.MetricEvent`."""

    type: Literal["metric"] = "metric"
    name: str = Field(min_length=1)
    kind: MetricKindStr
    value: int | float
    operation: str = "update"
    current_value: int | float | None = None
    tags: dict[str, str] = Field(default_factory=dict)


class HealthFrame(_BaseFrame):
    """Coarse-grained health snapshot — used for restart policy hints."""

    type: Literal["health"] = "health"
    connected: bool
    socks_ok: bool = False
    bootstrap_ok: bool = False


class LogFrame(_BaseFrame):
    """A structured log line from the worker."""

    type: Literal["log"] = "log"
    level: LogLevel
    logger: str = Field(min_length=1)
    message: str


class ExitFrame(_BaseFrame):
    """Final frame emitted by the worker just before it exits."""

    type: Literal["exit"] = "exit"
    code: int


Frame: TypeAlias = StatusFrame | MetricFrame | HealthFrame | LogFrame | ExitFrame

_FRAME_TYPES: Final[dict[str, type[_BaseFrame]]] = {
    "status": StatusFrame,
    "metric": MetricFrame,
    "health": HealthFrame,
    "log": LogFrame,
    "exit": ExitFrame,
}


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------


def encode_frame(frame: Frame) -> bytes:
    """Serialize ``frame`` to one NDJSON line including the trailing newline."""
    payload = frame.model_dump_json(exclude_none=False)
    return payload.encode("utf-8") + b"\n"


def decode_frame(line: str | bytes) -> Frame | None:
    """Parse one NDJSON line into a :data:`Frame`.

    Returns ``None`` if the line is empty, malformed JSON, unknown frame type,
    or fails schema validation. Unknown keys are silently ignored for forward
    compatibility.
    """
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None

    line = line.strip()
    if not line:
        return None

    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None

    if not isinstance(raw, dict):
        return None

    frame_type = raw.get("type")
    if not isinstance(frame_type, str):
        return None

    model_cls = _FRAME_TYPES.get(frame_type)
    if model_cls is None:
        return None

    allowed = model_cls.model_fields.keys()
    filtered = {key: value for key, value in raw.items() if key in allowed}

    try:
        return cast(Frame, model_cls.model_validate(filtered))
    except ValidationError:
        return None
