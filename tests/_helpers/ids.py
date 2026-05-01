"""Canonical connection / flow IDs used across the test suite."""

from __future__ import annotations

from typing import Final

# Both IDs match :data:`exectunnel.protocol.ids.CONN_FLOW_ID_RE` =
# ``[cu][0-9a-f]{24}``.  We keep them lexically distinct so that
# accidental cross-namespace use surfaces in test failures.
TCP_CONN_ID: Final[str] = "c" + "a" * 24
UDP_FLOW_ID: Final[str] = "u" + "b" * 24
