"""Supervisor / worker process model for multi-tunnel runs.

Phase 1 architecture:

* The parent ``run`` command is a process supervisor.
* Each tunnel runs in its own worker subprocess (``exectunnel _worker``).
* Communication is one-way NDJSON over the worker's stdout.
* Worker stderr is reserved for emergency unstructured output.

This package owns:

* :mod:`.ipc`         — versioned NDJSON frame models.
* :mod:`.worker`      — child-side: runs exactly one tunnel, emits frames.
* :mod:`.supervisor`  — parent-side: spawns workers, ingests frames, feeds
  the dashboard via parent-owned snapshots.
"""

from __future__ import annotations

from .ipc import (
    IPC_PROTOCOL_VERSION,
    ExitFrame,
    Frame,
    HealthFrame,
    LogFrame,
    MetricFrame,
    StatusFrame,
    decode_frame,
    encode_frame,
)
from .supervisor import Supervisor, SupervisorResult
from .worker import run_worker

__all__ = [
    "IPC_PROTOCOL_VERSION",
    "ExitFrame",
    "Frame",
    "HealthFrame",
    "LogFrame",
    "MetricFrame",
    "StatusFrame",
    "Supervisor",
    "SupervisorResult",
    "decode_frame",
    "encode_frame",
    "run_worker",
]
