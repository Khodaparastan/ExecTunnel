"""Config file loader — format detection and raw dict parsing.

Supports ``.toml``, ``.yaml``, and ``.yml`` files.  Returns a raw
``dict`` that is passed directly to :meth:`TunnelFile.model_validate`.

Format is determined by file extension only — no content sniffing.

Note: Python 3.11+ ``tomllib`` is used directly; no ``tomli`` fallback
is needed since this package targets Python 3.13+.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["ConfigFileError", "load_config_file"]


class ConfigFileError(Exception):
    """Raised when a config file cannot be read or parsed.

    Attributes:
        path:   Path to the file that failed.
        reason: Human-readable description of the failure.
    """

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Config file error [{path}]: {reason}")


def _load_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file and return its contents as a dict."""
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except Exception as exc:
        raise ConfigFileError(path, f"TOML parse error: {exc}") from exc


def _load_yaml(path: Path) -> dict[str, Any]:
    """Parse a YAML file and return its contents as a dict."""
    try:
        import yaml  # noqa: PLC0415
    except ImportError as exc:
        raise ConfigFileError(
            path,
            "YAML support requires 'PyYAML'. Install it with: pip install PyYAML",
        ) from exc

    try:
        with path.open("r", encoding="utf-8") as fh:
            result = yaml.safe_load(fh)
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise ConfigFileError(
                path,
                f"Expected a YAML mapping at the top level, got {type(result).__name__}.",
            )
        return result
    except ConfigFileError:
        raise
    except Exception as exc:
        raise ConfigFileError(path, f"YAML parse error: {exc}") from exc


_LOADERS: dict[str, Callable[[Path], dict[str, Any]]] = {
    ".toml": _load_toml,
    ".yaml": _load_yaml,
    ".yml": _load_yaml,
}


def load_config_file(path: Path) -> dict[str, Any]:
    """Load a config file and return its raw contents as a ``dict``.

    Format is determined by file extension:

    * ``.toml``            — parsed with stdlib ``tomllib``
    * ``.yaml`` / ``.yml`` — parsed with ``PyYAML``

    Args:
        path: Path to the config file.

    Returns:
        Raw parsed contents as a ``dict``.

    Raises:
        ConfigFileError: If the file does not exist, has an unsupported
                         extension, or fails to parse.
    """
    if not path.exists():
        raise ConfigFileError(path, "File not found.")

    if not path.is_file():
        raise ConfigFileError(path, "Path exists but is not a file.")

    suffix = path.suffix.lower()
    loader = _LOADERS.get(suffix)

    if loader is None:
        supported = ", ".join(sorted(_LOADERS))
        raise ConfigFileError(
            path,
            f"Unsupported file extension {suffix!r}. Supported formats: {supported}",
        )

    return loader(path)
