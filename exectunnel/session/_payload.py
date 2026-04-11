"""Agent payload loading helpers for the session layer.

``load_agent_b64`` / ``load_go_agent_b64`` are called once per process
lifetime (cached).  Both return URL-safe base64 with standard ``=``
padding so callers need only convert ``- → +`` and ``_ → /`` before
decoding on the remote.

Call ``clear_caches()`` to force a reload (useful during development or
after a hot-swap of the payload on disk).
"""

from __future__ import annotations

import base64
import functools
import importlib.resources
import logging
import threading
from typing import Final

from exectunnel.exceptions import ConfigurationError

__all__ = [
    "clear_caches",
    "load_agent_b64",
    "load_go_agent_b64",
]

logger = logging.getLogger(__name__)

# Minimum payload sizes (bytes) to catch empty / truncated resources early.
_MIN_PYTHON_AGENT_SIZE: Final[int] = 256
# Static Go binaries are always several MB; 512 KB is a safe lower bound
# that catches stubs/placeholders while allowing stripped minimal builds.
_MIN_GO_AGENT_SIZE: Final[int] = 524_288  # 512 KiB

# Protects lru_cache clear + re-prime against concurrent callers.
_CACHE_LOCK: threading.Lock = threading.Lock()


# ── Private helpers ───────────────────────────────────────────────────────────


def _load_resource_bytes(
    resource_path: tuple[str, ...],
    error_code_prefix: str,
    not_found_message: str,
    not_found_hint: str,
    min_size: int = 0,
) -> bytes:
    """Load a package resource and return its raw bytes.

    Args:
        resource_path:      Non-empty path components relative to the
                            ``exectunnel`` package root.
        error_code_prefix:  Prefix for ``ConfigurationError.error_code``.
        not_found_message:  Human-readable message for ``FileNotFoundError``.
        not_found_hint:     Hint string for ``FileNotFoundError``.
        min_size:           Minimum expected byte count.  A resource smaller
                            than this is treated as corrupt/truncated.

    Returns:
        The raw resource bytes.

    Raises:
        ConfigurationError: On any I/O or validation failure.
        ValueError:         If *resource_path* is empty.
    """
    if not resource_path:
        raise ValueError("resource_path must not be empty")

    pkg = importlib.resources.files("exectunnel")
    node = pkg
    for part in resource_path:
        node /= part

    # Used only for logging / error messages — not for actual I/O.
    resource_str = "/".join(("exectunnel", *resource_path))

    try:
        data: bytes = node.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigurationError(
            not_found_message,
            error_code=f"{error_code_prefix}_missing",
            details={"resource_path": resource_str},
            hint=not_found_hint,
        ) from exc
    except PermissionError as exc:
        raise ConfigurationError(
            f"Insufficient permissions to read {resource_str!r} from package resources.",
            error_code=f"{error_code_prefix}_permission_denied",
            details={"resource_path": resource_str},
            hint=(
                "Check the file permissions of the installed package directory "
                "and ensure the current user can read it."
            ),
        ) from exc
    except OSError as exc:
        raise ConfigurationError(
            f"Unexpected I/O error while loading {resource_str!r} from package resources.",
            error_code=f"{error_code_prefix}_load_failed",
            details={"resource_path": resource_str, "cause": repr(exc)},
            hint="Reinstall the package and check for filesystem or packaging issues.",
        ) from exc

    if min_size and len(data) < min_size:
        raise ConfigurationError(
            f"Resource {resource_str!r} is only {len(data):,} bytes — "
            f"expected at least {min_size:,}. The file may be truncated or corrupt.",
            error_code=f"{error_code_prefix}_truncated",
            details={
                "resource_path": resource_str,
                "actual_size": len(data),
                "min_size": min_size,
            },
            hint="Reinstall the package or rebuild the agent payload.",
        )

    return data


def _encode_urlsafe_b64(data: bytes) -> str:
    """Encode *data* as URL-safe base64 **with standard ``=`` padding**.

    URL-safe base64 uses ``-`` instead of ``+`` and ``_`` instead of ``/``,
    making the string safe for ``printf '%s'`` in all POSIX shells.

    Standard padding (``=``) is preserved so the bootstrapper can decode
    with ``sed 's/-/+/g; s/_/\\//g' | base64 -d`` without needing to
    re-compute padding characters.

    Returns:
        An ASCII string containing only ``[A-Za-z0-9_\\-=]``.
    """
    return base64.urlsafe_b64encode(data).decode("ascii")


# ── Public API ────────────────────────────────────────────────────────────────


@functools.lru_cache(maxsize=1)
def load_agent_b64() -> str:
    """Load ``payload/agent.py`` and return it as a padded URL-safe base64 string.

    The result is cached after the first call — the agent payload never
    changes at runtime.  Call :func:`clear_caches` to force a reload.

    Returns:
        The agent source as a padded URL-safe base64 string.

    Raises:
        ConfigurationError:
            * ``config.agent_payload_missing``           — resource not found.
            * ``config.agent_payload_truncated``         — too small.
            * ``config.agent_payload_permission_denied`` — unreadable.
            * ``config.agent_payload_load_failed``       — any other I/O error.
    """
    data = _load_resource_bytes(
        resource_path=("payload", "agent.py"),
        error_code_prefix="config.agent_payload",
        not_found_message=(
            "Agent payload not found in package resources — "
            "the installation may be incomplete or corrupted."
        ),
        not_found_hint=(
            "Reinstall the package and verify that payload/agent.py is "
            "included in the distribution. If using an editable install, "
            "ensure the source tree is intact."
        ),
        min_size=_MIN_PYTHON_AGENT_SIZE,
    )
    result = _encode_urlsafe_b64(data)
    logger.debug(
        "loaded agent.py payload: %d bytes → %d base64 chars",
        len(data),
        len(result),
    )
    return result


@functools.lru_cache(maxsize=1)
def load_go_agent_b64() -> str:
    """Load the pre-built Go agent binary as a padded URL-safe base64 string.

    The binary must be pre-built via::

        CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o agent_linux_amd64 .

    and placed at ``payload/go_agent/agent_linux_amd64`` inside the package.

    Returns:
        The binary as a padded URL-safe base64 string.

    Raises:
        ConfigurationError:
            * ``config.go_agent_payload_missing``           — binary not found.
            * ``config.go_agent_payload_truncated``         — too small (< 512 KiB).
            * ``config.go_agent_payload_permission_denied`` — unreadable.
            * ``config.go_agent_payload_load_failed``       — any other I/O error.
    """
    data = _load_resource_bytes(
        resource_path=("payload", "go_agent", "agent_linux_amd64"),
        error_code_prefix="config.go_agent_payload",
        not_found_message=(
            "Go agent binary not found in package resources — "
            "build it first with: "
            "CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o agent_linux_amd64 ."
        ),
        not_found_hint=(
            "Run 'make build-go-agent' from the project root to compile "
            "the Go agent for Linux/amd64 deployment."
        ),
        min_size=_MIN_GO_AGENT_SIZE,
    )
    result = _encode_urlsafe_b64(data)
    logger.debug(
        "loaded Go agent payload: %d bytes → %d base64 chars",
        len(data),
        len(result),
    )
    return result


def clear_caches() -> None:
    """Evict cached payloads so the next call reloads from disk.

    Thread-safe: acquires an internal lock so that a concurrent
    ``load_*`` call cannot observe a partially-cleared cache.
    """
    with _CACHE_LOCK:
        load_agent_b64.cache_clear()
        load_go_agent_b64.cache_clear()
