"""
Typed exception hierarchy for the entire package.

Catching ``ExecTunnelError`` is sufficient to handle any library error.
"""

from __future__ import annotations


class ExecTunnelError(Exception):
    """Base class for all exectunnel errors."""


# ── Configuration ──────────────────────────────────────────────────────────────


class ConfigError(ExecTunnelError):
    """Raised when required configuration is missing or invalid."""


# ── Bootstrap ─────────────────────────────────────────────────────────────────


class BootstrapError(ExecTunnelError):
    """Raised when the remote agent script fails to start."""


class AgentTimeoutError(BootstrapError):
    """AGENT_READY signal was not received within the configured timeout."""


class AgentSyntaxError(BootstrapError):
    """Remote Python reported a SyntaxError while loading agent.py."""


# ── Transport ─────────────────────────────────────────────────────────────────


class TransportError(ExecTunnelError):
    """Base class for WebSocket / TCP transport errors."""


class WebSocketSendTimeout(TransportError):
    """A WebSocket send operation timed out (connection stalled)."""


# ── Protocol ─────────────────────────────────────────────────────────────────


class ProtocolError(ExecTunnelError):
    """Raised when an unexpected or malformed frame is received."""
