"""Global defaults — process-wide configuration with environment variable support.

:class:`GlobalDefaults` is the single source of process-wide tunables.
It inherits from both :class:`~exectunnel.config._mixin.TunnelOverrideMixin`
and :class:`pydantic_settings.BaseSettings`, giving it automatic environment
variable reading under the ``EXECTUNNEL_`` prefix.

Priority (lowest → highest):
    ``Defaults`` class → env vars → config file ``[global]`` section → CLI flags

The ``Defaults`` class values are never stored here — they live exclusively
in :meth:`~exectunnel.config.TunnelFile.resolve` as the base layer.

Environment variables
---------------------
Every field maps to ``EXECTUNNEL_<FIELD_NAME_UPPER>``.  For example::

    export EXECTUNNEL_LOG_LEVEL=debug
    export EXECTUNNEL_SOCKS_HOST=0.0.0.0
    export EXECTUNNEL_RECONNECT_MAX_RETRIES=10

Note: ``env_ignore_empty=True`` means an empty string (e.g.
``EXECTUNNEL_LOG_LEVEL=""``) is treated as unset.  Set the variable to a
valid value or leave it unset entirely.

Note on case sensitivity
------------------------
``case_sensitive=True`` is set for Linux correctness.  On macOS/Windows the
OS itself is case-insensitive for env vars, so ``EXECTUNNEL_LOG_LEVEL`` and
``exectunnel_log_level`` would both match — but we document uppercase as the
canonical form and rely on CI running on Linux.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ._mixin import TunnelOverrideMixin

__all__ = ["GlobalDefaults"]


class GlobalDefaults(TunnelOverrideMixin, BaseSettings):
    """Process-wide configuration with automatic ``EXECTUNNEL_*`` env var support.

    All fields are optional.  Unset fields resolve to the ``Defaults`` class
    values inside :meth:`~exectunnel.config.TunnelFile.resolve`.

    Attributes:
        default_enabled: Default enabled state for tunnel entries that omit ``enabled``.
        log_level:       Logging verbosity.
        log_format:      Output format — ``"text"`` or ``"json"`` (NDJSON).
        log_file:        Optional path to write log records in addition to stderr.
    """

    model_config = SettingsConfigDict(
        env_prefix="EXECTUNNEL_",
        env_ignore_empty=True,
        extra="forbid",
        populate_by_name=True,
        case_sensitive=True,
    )

    # ── Enabled default (global-only) ─────────────────────────────────────────

    default_enabled: bool | None = Field(
        default=None,
        description="Default enabled state for tunnel entries that omit 'enabled'.",
    )

    # ── Logging (global-only, not inheritable by tunnels) ─────────────────────

    log_level: Literal["debug", "info", "warning", "error"] | None = Field(
        default=None,
        description="Log verbosity: debug, info, warning, error.",
    )
    log_format: Literal["text", "json"] | None = Field(
        default=None,
        description="Log output format: 'text' for human-readable, 'json' for NDJSON.",
    )
    # Path | None — consistent with CLIOverrides.log_file.
    # pydantic-settings coerces a string env var to Path automatically.
    log_file: Path | None = Field(
        default=None,
        description="Optional path to write log records in addition to stderr.",
    )
