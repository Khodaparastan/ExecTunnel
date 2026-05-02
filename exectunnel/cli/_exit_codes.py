"""Canonical CLI/worker exit codes — single source of truth.

These values are part of the public CLI contract and are exchanged across the
parent/worker process boundary via :class:`exectunnel.cli._supervisor.ipc.ExitFrame`
and the OS-level worker exit code. They MUST stay numerically stable.

Stability guarantees
--------------------
* Numeric values never change between releases.
* New codes are appended; old codes are not reused.
* ``EXIT_INTERRUPTED`` follows the Unix convention (``128 + SIGINT``).

Mapping
-------
* ``0`` ``EXIT_OK``                  Clean shutdown.
* ``1`` ``EXIT_ERROR``               Generic / unexpected failure.
* ``2`` ``EXIT_BOOTSTRAP``           Bootstrap failure NOT due to auth.
* ``3`` ``EXIT_RECONNECT_EXHAUSTED`` Reconnect budget exhausted.
* ``4`` ``EXIT_AUTH_FAILURE``        WSS handshake rejected with 401/403.
* ``130`` ``EXIT_INTERRUPTED``       SIGINT / SIGTERM / ``CancelledError``.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "EXIT_AUTH_FAILURE",
    "EXIT_BOOTSTRAP",
    "EXIT_ERROR",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_RECONNECT_EXHAUSTED",
    "RETRYABLE_EXIT_CODES",
    "FATAL_EXIT_CODES",
]

EXIT_OK: Final[int] = 0
EXIT_ERROR: Final[int] = 1
EXIT_BOOTSTRAP: Final[int] = 2
EXIT_RECONNECT_EXHAUSTED: Final[int] = 3
EXIT_AUTH_FAILURE: Final[int] = 4
EXIT_INTERRUPTED: Final[int] = 130

#: Exit codes that the supervisor MAY consider for restart policy decisions.
#:
#: ``EXIT_AUTH_FAILURE`` is included intentionally: with a remote-config source
#: configured the parent will fetch a fresh config before respawning. Without
#: one, the restart policy still counts the attempt but the refreshed config
#: never arrives and the slot will exhaust its window quickly.
RETRYABLE_EXIT_CODES: Final[frozenset[int]] = frozenset({
    EXIT_ERROR,
    EXIT_BOOTSTRAP,
    EXIT_RECONNECT_EXHAUSTED,
    EXIT_AUTH_FAILURE,
})

#: Exit codes that MUST NOT trigger a restart, regardless of policy.
FATAL_EXIT_CODES: Final[frozenset[int]] = frozenset({
    EXIT_INTERRUPTED,
})
