"""
exceptions.py
─────────────────────────────────────────────────────────────────────────────
Typed exception hierarchy for the exectunnel package.

Design goals
────────────
* Single catch-all  – ``except ExecTunnelError`` handles every library error.
* Structured context – every exception carries a machine-readable ``error_code``,
  an optional ``details`` dict, and an optional ``hint`` for operators.
* Exception chaining – always raise with ``raise XxxError(...) from cause`` so
  the original traceback is never silently swallowed.
* Serialisable       – ``to_dict()`` / ``from_dict()`` for structured logging,
  Sentry, OpenTelemetry, or any JSON-based error bus.
* Retryability flag  – ``retryable`` lets callers decide whether to back-off
  without pattern-matching on the class name.
* Backward compat    – legacy aliases are preserved and clearly marked.
"""
from __future__ import annotations

import datetime
import traceback
import uuid
from typing import Any

__all__ = [
    # Base
    "ExecTunnelError",
    # Configuration
    "ConfigurationError",
    "ConfigError",  # alias
    # Bootstrap
    "BootstrapError",
    "AgentReadyTimeoutError",
    "AgentTimeoutError",  # alias
    "AgentSyntaxError",
    "AgentVersionMismatchError",
    # Transport
    "TransportError",
    "WebSocketSendTimeoutError",
    "WebSocketSendTimeout",  # alias
    "ConnectionClosedError",
    "ReconnectExhaustedError",
    # Protocol
    "ProtocolError",
    "UnexpectedFrameError",
    "FrameDecodingError",
    # Execution
    "ExecutionError",
    "RemoteProcessError",
    "ExecutionTimeoutError",
    # Auth / Security
    "AuthenticationError",
    "AuthorizationError",
]


# ── Helpers ───────────────────────────────────────────────────────────────────


def _utc_now() -> str:
    """Return the current UTC timestamp as an ISO-8601 string."""
    return datetime.datetime.now(datetime.UTC).isoformat()


# ── Base ──────────────────────────────────────────────────────────────────────


class ExecTunnelError(Exception):
    """
    Base class for **all** exectunnel errors.

    Attributes
    ----------
    message:
        Human-readable description of what went wrong.
    error_code:
        Stable, dot-namespaced identifier (e.g. ``"transport.ws_send_timeout"``).
        Safe to match programmatically; never changes between releases.
    details:
        Arbitrary key/value pairs that give extra context (host, port, frame
        type, exit code, …).  Always a ``dict``; never ``None``.
    hint:
        Optional operator-facing remediation advice.
    retryable:
        ``True`` when the operation *may* succeed if retried (e.g. a transient
        network blip).  ``False`` for permanent failures (e.g. syntax errors).
    error_id:
        A per-instance UUID v4.  Correlate this with your log aggregator.
    timestamp:
        UTC ISO-8601 string recorded at raise time.
    """

    #: Override in subclasses to give every class a stable default code.
    default_error_code: str = "exectunnel.error"
    #: Override in subclasses; callers use this to decide on retry logic.
    default_retryable: bool = False

    def __init__(
        self,
        message: str = "",
        *,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
        hint: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message: str = message
        self.error_code: str = error_code or self.default_error_code
        self.details: dict[str, Any] = details or {}
        self.hint: str | None = hint
        self.retryable: bool = retryable if retryable is not None else self.default_retryable
        self.error_id: str = str(uuid.uuid4())
        self.timestamp: str = _utc_now()

    # ── Representation ────────────────────────────────────────────────────────

    def __str__(self) -> str:
        parts = [f"[{self.error_code}] {self.message}"]
        if self.details:
            kv = ", ".join(f"{k}={v!r}" for k, v in self.details.items())
            parts.append(f"details=({kv})")
        if self.hint:
            parts.append(f"hint={self.hint!r}")
        parts.append(f"error_id={self.error_id}")
        return " | ".join(parts)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"message={self.message!r}, "
            f"error_code={self.error_code!r}, "
            f"retryable={self.retryable!r}, "
            f"error_id={self.error_id!r}"
            f")"
        )

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """
        Return a fully serialisable representation suitable for structured
        logging, Sentry ``extra``, or an OpenTelemetry span attribute.
        """
        cause = self.__cause__ or self.__context__
        return {
            "error_id": self.error_id,
            "timestamp": self.timestamp,
            "type": type(self).__name__,
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
            "hint": self.hint,
            "details": self.details,
            "cause": repr(cause) if cause else None,
            "traceback": traceback.format_exc() or None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecTunnelError:
        """
        Reconstruct an exception from a serialised ``to_dict()`` payload.
        Useful when deserialising errors from a remote agent or a message queue.
        """
        instance = cls(
            message=data.get("message", ""),
            error_code=data.get("error_code"),
            details=data.get("details"),
            hint=data.get("hint"),
            retryable=data.get("retryable"),
        )
        # Restore identity fields so log correlation still works.
        instance.error_id = data.get("error_id", instance.error_id)
        instance.timestamp = data.get("timestamp", instance.timestamp)
        return instance


# ── Configuration ─────────────────────────────────────────────────────────────


class ConfigurationError(ExecTunnelError):
    """
    Raised when required configuration is missing or invalid.

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``field``   – the offending config key (e.g. ``"tunnel.host"``).
    ``value``   – the bad value that was supplied (redact secrets!).
    ``expected``– a description of what was expected.
    """

    default_error_code = "config.invalid"
    default_retryable = False


# Backward-compat alias.
ConfigError = ConfigurationError


# ── Bootstrap ─────────────────────────────────────────────────────────────────


class BootstrapError(ExecTunnelError):
    """
    Raised when the remote agent script fails to start.

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``host``        – remote host address.
    ``elapsed_s``   – seconds elapsed before the failure was detected.
    """

    default_error_code = "bootstrap.failed"
    default_retryable = True


class AgentReadyTimeoutError(BootstrapError):
    """
    ``AGENT_READY`` signal was not received within the configured timeout.

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``timeout_s``   – the timeout that was exceeded (seconds).
    ``host``        – remote host address.
    """

    default_error_code = "bootstrap.agent_ready_timeout"
    default_retryable = True


# Backward-compat alias.
AgentTimeoutError = AgentReadyTimeoutError


class AgentSyntaxError(BootstrapError):
    """
    Remote Python reported a ``SyntaxError`` while loading ``agent.py``.

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``lineno``      – line number of the syntax error.
    ``filename``    – remote file path.
    ``text``        – offending source line.
    """

    default_error_code = "bootstrap.agent_syntax_error"
    default_retryable = False


class AgentVersionMismatchError(BootstrapError):
    """
    The remote agent version is incompatible with the local client version.

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``local_version``   – version string of the local client.
    ``remote_version``  – version string reported by the remote agent.
    ``minimum_version`` – minimum agent version required by the client.
    """

    default_error_code = "bootstrap.version_mismatch"
    default_retryable = False


# ── Transport ─────────────────────────────────────────────────────────────────


class TransportError(ExecTunnelError):
    """
    Base class for WebSocket / TCP transport errors.

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``host``    – remote host.
    ``port``    – remote port.
    ``url``     – full WebSocket URL.
    """

    default_error_code = "transport.error"
    default_retryable = True


class WebSocketSendTimeoutError(TransportError):
    """
    A WebSocket send operation timed out (connection stalled).

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``timeout_s``       – configured send timeout in seconds.
    ``payload_bytes``   – size of the frame that could not be sent.
    """

    default_error_code = "transport.ws_send_timeout"
    default_retryable = True


# Backward-compat alias.
WebSocketSendTimeout = WebSocketSendTimeoutError


class ConnectionClosedError(TransportError):
    """
    The underlying WebSocket / TCP connection was closed unexpectedly.

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``close_code``      – WebSocket close code (RFC 6455).
    ``close_reason``    – human-readable close reason string.
    """

    default_error_code = "transport.connection_closed"
    default_retryable = True


class ReconnectExhaustedError(TransportError):
    """
    All reconnection attempts have been exhausted.

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``attempts``        – total number of reconnection attempts made.
    ``last_error``      – repr of the last underlying transport error.
    """

    default_error_code = "transport.reconnect_exhausted"
    default_retryable = False


# ── Protocol ─────────────────────────────────────────────────────────────────


class ProtocolError(ExecTunnelError):
    """
    Raised when an unexpected or malformed frame is received.

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``frame_type``  – the frame type identifier that was received.
    ``expected``    – the frame type(s) that were expected.
    """

    default_error_code = "protocol.error"
    default_retryable = False


class UnexpectedFrameError(ProtocolError):
    """
    A frame arrived that is valid but not expected in the current state.

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``state``       – current protocol state machine state.
    ``frame_type``  – the frame type that arrived.
    """

    default_error_code = "protocol.unexpected_frame"
    default_retryable = False


class FrameDecodingError(ProtocolError):
    """
    A frame could not be decoded (bad msgpack / JSON / schema mismatch).

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``raw_bytes``   – hex-encoded raw payload (truncated for safety).
    ``codec``       – codec in use (e.g. ``"msgpack"``, ``"json"``).
    """

    default_error_code = "protocol.frame_decoding_error"
    default_retryable = False


# ── Execution ─────────────────────────────────────────────────────────────────


class ExecutionError(ExecTunnelError):
    """
    Base class for errors that occur during remote code execution.

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``session_id``  – the execution session identifier.
    """

    default_error_code = "execution.error"
    default_retryable = False


class RemoteProcessError(ExecutionError):
    """
    The remote process exited with a non-zero exit code.

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``exit_code``   – integer exit code returned by the remote process.
    ``stderr``      – last N bytes of stderr (truncated).
    ``command``     – the command that was executed.
    """

    default_error_code = "execution.remote_process_error"
    default_retryable = False


class ExecutionTimeoutError(ExecutionError):
    """
    The remote execution did not complete within the allowed time budget.

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``timeout_s``   – the timeout that was exceeded (seconds).
    ``session_id``  – the execution session identifier.
    """

    default_error_code = "execution.timeout"
    default_retryable = True


# ── Auth / Security ───────────────────────────────────────────────────────────


class AuthenticationError(ExecTunnelError):
    """
    Credentials were missing, expired, or rejected by the remote host.

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``host``        – remote host that rejected the credentials.
    ``auth_method`` – the authentication method that was attempted.
    """

    default_error_code = "auth.authentication_failed"
    default_retryable = False


class AuthorizationError(ExecTunnelError):
    """
    The authenticated principal lacks permission for the requested operation.

    Extra ``details`` keys (all optional)
    ──────────────────────────────────────
    ``principal``   – the identity that was authenticated.
    ``action``      – the action that was denied.
    ``resource``    – the resource the action was attempted on.
    """

    default_error_code = "auth.authorization_failed"
    default_retryable = False
