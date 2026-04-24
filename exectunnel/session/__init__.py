"""``exectunnel.session`` — public surface of the session package.

Exports the configuration dataclasses, the main session class, routing
helpers, and the agent stats callback type so callers do not need to import
from private sub-modules.
"""

from ._config import SessionConfig, TunnelConfig
from ._routing import DEFAULT_EXCLUDE_CIDRS, get_default_exclusion_networks
from ._state import AckStatus
from ._types import AgentStatsCallable
from .session import TunnelSession

__all__ = [
    "AckStatus",
    "AgentStatsCallable",
    "DEFAULT_EXCLUDE_CIDRS",
    "SessionConfig",
    "TunnelConfig",
    "TunnelSession",
    "get_default_exclusion_networks",
]
