"""Shared application context threaded through Typer commands via ``ctx.obj``."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from exectunnel.config import TunnelFile

__all__ = [
    "AppContext",
    "LogFormat",
    "LogLevel",
    "_CLI_DEFAULT_LOG_FORMAT",
    "_CLI_DEFAULT_LOG_LEVEL",
]

logger = logging.getLogger(__name__)

_CLI_DEFAULT_LOG_LEVEL: Literal["info"] = "info"
_CLI_DEFAULT_LOG_FORMAT: Literal["text"] = "text"

LogLevel = Literal["debug", "info", "warning", "error"]
LogFormat = Literal["text", "json"]

_DEFAULT_CONFIG_CANDIDATES: tuple[str, ...] = (
    "exectunnel/config.toml",
    "exectunnel/config.yaml",
    "exectunnel/config.yml",
)


def _normalize_path(path: Path) -> Path:
    """Expand ``~`` and return an absolute, normalized path."""
    return path.expanduser().resolve(strict=False)


def _resolve_path_relative_to(base_file: Path, value: Path) -> Path:
    """Resolve ``value`` relative to ``base_file.parent`` when not absolute."""
    if value.is_absolute():
        return _normalize_path(value)
    return _normalize_path(base_file.parent / value)


def _candidate_config_dirs() -> tuple[Path, ...]:
    """Return deduplicated config search roots in lookup order."""
    seen: set[Path] = set()
    ordered: list[Path] = []

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        candidate = _normalize_path(Path(xdg_config_home))
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)

    fallback = _normalize_path(Path.home() / ".config")
    if fallback not in seen:
        seen.add(fallback)
        ordered.append(fallback)

    return tuple(ordered)


def _find_default_config() -> Path | None:
    """Search config roots for the first matching config file."""
    for base_dir in _candidate_config_dirs():
        for rel_path in _DEFAULT_CONFIG_CANDIDATES:
            candidate = base_dir / rel_path
            if candidate.is_file():
                return candidate
    return None


def _format_validation_error(
    config_path: Path,
    errors: list[dict[str, object]],
) -> str:
    lines = [f"Config validation failed ({config_path}):"]
    for error in errors:
        loc_tuple = error.get("loc", ())
        loc = " → ".join(str(part) for part in loc_tuple) if loc_tuple else "<root>"
        msg = str(error.get("msg", "unknown validation error"))
        lines.append(f"  • {loc}: {msg}")
    return "\n".join(lines)


@dataclass(slots=True)
class AppContext:
    """Shared state passed through ``ctx.obj`` to every Typer subcommand."""

    config_path: Path | None = None
    tunnel_file: TunnelFile | None = None
    config_load_error: str | None = None
    log_level: LogLevel = field(default=_CLI_DEFAULT_LOG_LEVEL)
    log_format: LogFormat = field(default=_CLI_DEFAULT_LOG_FORMAT)
    log_file: Path | None = None
    log_level_from_cli: bool = False
    log_format_from_cli: bool = False
    log_file_from_cli: bool = False

    @classmethod
    def minimal(
        cls,
        *,
        log_level: LogLevel,
        log_format: LogFormat,
        log_file: Path | None,
        log_level_from_cli: bool = False,
        log_format_from_cli: bool = False,
        log_file_from_cli: bool = False,
    ) -> AppContext:
        """Construct a context without config discovery or loading.

        Useful for internal commands such as the hidden worker subprocess entry
        point, where normal CLI config loading and logging setup are undesirable.
        """
        normalized_log_file = (
            _normalize_path(log_file) if log_file is not None else None
        )

        return cls(
            config_path=None,
            tunnel_file=None,
            config_load_error=None,
            log_level=log_level,
            log_format=log_format,
            log_file=normalized_log_file,
            log_level_from_cli=log_level_from_cli,
            log_format_from_cli=log_format_from_cli,
            log_file_from_cli=log_file_from_cli,
        )

    @classmethod
    def load(
        cls,
        config_path: Path | None,
        log_level: LogLevel,
        log_format: LogFormat,
        log_file: Path | None,
        *,
        log_level_from_cli: bool = False,
        log_format_from_cli: bool = False,
        log_file_from_cli: bool = False,
    ) -> AppContext:
        """Construct an :class:`AppContext`, loading config if present."""
        from pydantic import ValidationError  # noqa: PLC0415

        from exectunnel.config import (  # noqa: PLC0415
            ConfigFileError,
            TunnelFile,
            load_config_file,
        )

        resolved_path = (
            _normalize_path(config_path)
            if config_path is not None
            else _find_default_config()
        )
        normalized_log_file = (
            _normalize_path(log_file) if log_file is not None else None
        )

        tunnel_file: TunnelFile | None = None
        config_load_error: str | None = None

        if resolved_path is not None:
            try:
                raw = load_config_file(resolved_path)
                tunnel_file = TunnelFile.model_validate(raw)

                global_config = tunnel_file.global_config

                if global_config.log_level is not None and not log_level_from_cli:
                    log_level = global_config.log_level

                if global_config.log_format is not None and not log_format_from_cli:
                    log_format = global_config.log_format

                if global_config.log_file is not None and not log_file_from_cli:
                    normalized_log_file = _resolve_path_relative_to(
                        resolved_path,
                        global_config.log_file,
                    )

            except ConfigFileError as exc:
                logger.debug("Config file error for %s: %s", resolved_path, exc)
                config_load_error = str(exc)

            except ValidationError as exc:
                logger.debug(
                    "Config validation error for %s",
                    resolved_path,
                    exc_info=True,
                )
                config_load_error = _format_validation_error(
                    resolved_path,
                    exc.errors(),
                )

        return cls(
            config_path=resolved_path,
            tunnel_file=tunnel_file,
            config_load_error=config_load_error,
            log_level=log_level,
            log_format=log_format,
            log_file=normalized_log_file,
            log_level_from_cli=log_level_from_cli,
            log_format_from_cli=log_format_from_cli,
            log_file_from_cli=log_file_from_cli,
        )
