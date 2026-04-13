"""CLI-layer configuration: env-reading factories and SSL.

This module owns construction of session-layer configs from environment
variables and CLI arguments.  The session layer declares only what it
consumes; this layer handles all env parsing, SSL context creation,
and merging of CLI args with env overrides.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import ssl
from typing import Final
from urllib.parse import urlparse

from exectunnel.exceptions import ConfigurationError
from exectunnel.session import (
    SessionConfig,
    TunnelConfig,
    get_default_exclusion_networks,
)

from .utils import parse_bool_env, parse_float_env, parse_int_env

logger = logging.getLogger(__name__)

_SESSION_DEFAULTS: Final[SessionConfig] = SessionConfig(wss_url="ws://placeholder")
_TUNNEL_DEFAULTS: Final[TunnelConfig] = TunnelConfig()

__all__ = [
    "build_session_config",
    "build_tunnel_config",
    "create_ssl_context",
    "get_wss_url",
]


# ── SSL helper ───────────────────────────────────────────────────────────────


def create_ssl_context(insecure: bool) -> ssl.SSLContext:
    """Create and return an SSL context, optionally with verification disabled."""
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    if insecure:
        logger.warning("TLS verification disabled (WSS_INSECURE=1)")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ── WSS URL ──────────────────────────────────────────────────────────────────


def get_wss_url() -> str:
    """Return EXECTUNNEL_WSS_URL (or legacy WSS_URL) from the environment or raise."""
    url = os.getenv("EXECTUNNEL_WSS_URL") or os.getenv("WSS_URL")
    if not url:
        raise ConfigurationError(
            "Environment variable 'EXECTUNNEL_WSS_URL' (or legacy 'WSS_URL') must be set."
        )
    normalized = url.strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"ws", "wss"}:
        raise ConfigurationError("EXECTUNNEL_WSS_URL must use ws:// or wss:// scheme.")
    if not parsed.netloc:
        raise ConfigurationError(
            "EXECTUNNEL_WSS_URL must include a host (and optional port)."
        )
    return normalized


# ── SessionConfig factory ────────────────────────────────────────────────────


def build_session_config(
    *,
    wss_url: str | None = None,
    ws_headers: dict[str, str] | None = None,
    ssl_context: ssl.SSLContext | None = None,
    insecure: bool = False,
    reconnect_max_retries: int | None = None,
) -> SessionConfig:
    """Build a SessionConfig from env + overrides.

    Args:
        wss_url:              WebSocket URL override.  If ``None``, reads from env.
        ws_headers:           HTTP headers to send during the WS handshake.
        ssl_context:          Pre-built SSL context (e.g. from kubeconfig).
        insecure:             Skip TLS certificate verification.
        reconnect_max_retries: Override for max reconnect attempts.
    """
    url = wss_url or get_wss_url()
    resolved_insecure = insecure or parse_bool_env("WSS_INSECURE")

    ssl_ctx = ssl_context
    if ssl_ctx is None and url.startswith("wss://"):
        ssl_ctx = create_ssl_context(resolved_insecure)

    reconnect_base_delay = parse_float_env(
        "EXECTUNNEL_RECONNECT_BASE_DELAY",
        _SESSION_DEFAULTS.reconnect_base_delay,
        min_value=0.1,
    )
    reconnect_max_delay = parse_float_env(
        "EXECTUNNEL_RECONNECT_MAX_DELAY",
        _SESSION_DEFAULTS.reconnect_max_delay,
        min_value=0.1,
    )
    if reconnect_max_delay < reconnect_base_delay:
        logger.warning(
            "EXECTUNNEL_RECONNECT_MAX_DELAY %.3f < EXECTUNNEL_RECONNECT_BASE_DELAY %.3f; "
            "adjusting max delay",
            reconnect_max_delay,
            reconnect_base_delay,
        )
        reconnect_max_delay = reconnect_base_delay

    return SessionConfig(
        wss_url=url,
        ws_headers=ws_headers or {},
        ssl_context_override=ssl_ctx,
        ping_interval=parse_float_env(
            "EXECTUNNEL_PING_INTERVAL",
            _SESSION_DEFAULTS.ping_interval,
            min_value=1,
        ),
        send_timeout=parse_float_env(
            "EXECTUNNEL_SEND_TIMEOUT",
            _SESSION_DEFAULTS.send_timeout,
            min_value=0.1,
        ),
        send_queue_cap=parse_int_env(
            "EXECTUNNEL_SEND_QUEUE_CAP",
            _SESSION_DEFAULTS.send_queue_cap,
            min_value=1,
        ),
        reconnect_max_retries=reconnect_max_retries
        if reconnect_max_retries is not None
        else parse_int_env(
            "EXECTUNNEL_RECONNECT_MAX_RETRIES",
            _SESSION_DEFAULTS.reconnect_max_retries,
            min_value=0,
        ),
        reconnect_base_delay=reconnect_base_delay,
        reconnect_max_delay=reconnect_max_delay,
    )


# ── TunnelConfig factory ────────────────────────────────────────────────────


def build_tunnel_config(
    *,
    socks_host: str = _TUNNEL_DEFAULTS.socks_host,
    socks_port: int = _TUNNEL_DEFAULTS.socks_port,
    dns_upstream: str | None = _TUNNEL_DEFAULTS.dns_upstream,
    dns_local_port: int = _TUNNEL_DEFAULTS.dns_local_port,
    ready_timeout: float = _TUNNEL_DEFAULTS.ready_timeout,
    conn_ack_timeout: float = _TUNNEL_DEFAULTS.conn_ack_timeout,
    exclude: list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None = None,
    bootstrap_delivery: str = _TUNNEL_DEFAULTS.bootstrap_delivery,
    fetch_agent_url: str = _TUNNEL_DEFAULTS.bootstrap_fetch_url,
    bootstrap_skip_if_present: bool = _TUNNEL_DEFAULTS.bootstrap_skip_if_present,
    bootstrap_syntax_check: bool = _TUNNEL_DEFAULTS.bootstrap_syntax_check,
    bootstrap_agent_path: str = _TUNNEL_DEFAULTS.bootstrap_agent_path,
    bootstrap_syntax_ok_sentinel: str = _TUNNEL_DEFAULTS.bootstrap_syntax_ok_sentinel,
    bootstrap_use_go_agent: bool = _TUNNEL_DEFAULTS.bootstrap_use_go_agent,
    bootstrap_go_agent_path: str = _TUNNEL_DEFAULTS.bootstrap_go_agent_path,
    connect_pace_interval_secs: float = _TUNNEL_DEFAULTS.connect_pace_interval_secs,
    socks_handshake_timeout: float = _TUNNEL_DEFAULTS.socks_handshake_timeout,
    socks_request_queue_cap: int = _TUNNEL_DEFAULTS.socks_request_queue_cap,
    socks_queue_put_timeout: float = _TUNNEL_DEFAULTS.socks_queue_put_timeout,
    udp_relay_queue_cap: int = _TUNNEL_DEFAULTS.udp_relay_queue_cap,
    udp_drop_warn_every: int = _TUNNEL_DEFAULTS.udp_drop_warn_every,
    udp_pump_poll_timeout: float = _TUNNEL_DEFAULTS.udp_pump_poll_timeout,
    udp_direct_recv_timeout: float = _TUNNEL_DEFAULTS.udp_direct_recv_timeout,
    dns_upstream_port: int = _TUNNEL_DEFAULTS.dns_upstream_port,
    dns_query_timeout: float = _TUNNEL_DEFAULTS.dns_query_timeout,
) -> TunnelConfig:
    """Build TunnelConfig, merging CLI args with environment overrides."""
    return TunnelConfig(
        socks_host=socks_host,
        socks_port=socks_port,
        dns_upstream=dns_upstream,
        dns_local_port=dns_local_port,
        ready_timeout=ready_timeout,
        conn_ack_timeout=conn_ack_timeout,
        exclude=exclude if exclude is not None else get_default_exclusion_networks(),
        ack_timeout_warn_every=parse_int_env(
            "EXECTUNNEL_ACK_TIMEOUT_WARN_EVERY",
            _TUNNEL_DEFAULTS.ack_timeout_warn_every,
            min_value=1,
        ),
        ack_timeout_window_secs=parse_float_env(
            "EXECTUNNEL_ACK_TIMEOUT_WINDOW_SECS",
            _TUNNEL_DEFAULTS.ack_timeout_window_secs,
            min_value=1.0,
        ),
        ack_timeout_reconnect_threshold=parse_int_env(
            "EXECTUNNEL_ACK_TIMEOUT_RECONNECT_THRESHOLD",
            _TUNNEL_DEFAULTS.ack_timeout_reconnect_threshold,
            min_value=1,
        ),
        connect_max_pending=parse_int_env(
            "EXECTUNNEL_CONNECT_MAX_PENDING",
            _TUNNEL_DEFAULTS.connect_max_pending,
            min_value=1,
        ),
        connect_max_pending_per_host=parse_int_env(
            "EXECTUNNEL_CONNECT_MAX_PENDING_PER_HOST",
            _TUNNEL_DEFAULTS.connect_max_pending_per_host,
            min_value=1,
        ),
        pre_ack_buffer_cap_bytes=parse_int_env(
            "EXECTUNNEL_PRE_ACK_BUFFER_CAP_BYTES",
            _TUNNEL_DEFAULTS.pre_ack_buffer_cap_bytes,
            min_value=1024,
        ),
        bootstrap_delivery=os.getenv(
            "EXECTUNNEL_BOOTSTRAP_DELIVERY", bootstrap_delivery
        ),
        bootstrap_fetch_url=os.getenv("EXECTUNNEL_FETCH_AGENT_URL", fetch_agent_url),
        bootstrap_skip_if_present=parse_bool_env(
            "EXECTUNNEL_BOOTSTRAP_SKIP_IF_PRESENT",
            default=bootstrap_skip_if_present,
        ),
        bootstrap_syntax_check=parse_bool_env(
            "EXECTUNNEL_BOOTSTRAP_SYNTAX_CHECK",
            default=bootstrap_syntax_check,
        ),
        bootstrap_agent_path=os.getenv(
            "EXECTUNNEL_BOOTSTRAP_AGENT_PATH", bootstrap_agent_path
        ),
        bootstrap_syntax_ok_sentinel=os.getenv(
            "EXECTUNNEL_BOOTSTRAP_SYNTAX_OK_SENTINEL", bootstrap_syntax_ok_sentinel
        ),
        bootstrap_use_go_agent=parse_bool_env(
            "EXECTUNNEL_BOOTSTRAP_USE_GO_AGENT",
            default=bootstrap_use_go_agent,
        ),
        bootstrap_go_agent_path=os.getenv(
            "EXECTUNNEL_BOOTSTRAP_GO_AGENT_PATH", bootstrap_go_agent_path
        ),
        connect_pace_interval_secs=parse_float_env(
            "EXECTUNNEL_CONNECT_PACE_INTERVAL_SECS",
            _TUNNEL_DEFAULTS.connect_pace_interval_secs,
            min_value=0.0,
        ),
        socks_handshake_timeout=parse_float_env(
            "EXECTUNNEL_SOCKS_HANDSHAKE_TIMEOUT",
            socks_handshake_timeout,
            min_value=0.1,
        ),
        socks_request_queue_cap=parse_int_env(
            "EXECTUNNEL_SOCKS_REQUEST_QUEUE_CAP",
            socks_request_queue_cap,
            min_value=1,
        ),
        socks_queue_put_timeout=parse_float_env(
            "EXECTUNNEL_SOCKS_QUEUE_PUT_TIMEOUT",
            socks_queue_put_timeout,
            min_value=0.1,
        ),
        udp_relay_queue_cap=parse_int_env(
            "EXECTUNNEL_UDP_RELAY_QUEUE_CAP",
            udp_relay_queue_cap,
            min_value=1,
        ),
        udp_drop_warn_every=parse_int_env(
            "EXECTUNNEL_UDP_DROP_WARN_EVERY",
            udp_drop_warn_every,
            min_value=1,
        ),
        udp_pump_poll_timeout=parse_float_env(
            "EXECTUNNEL_UDP_PUMP_POLL_TIMEOUT",
            udp_pump_poll_timeout,
            min_value=0.01,
        ),
        udp_direct_recv_timeout=parse_float_env(
            "EXECTUNNEL_UDP_DIRECT_RECV_TIMEOUT",
            udp_direct_recv_timeout,
            min_value=0.1,
        ),
        dns_max_inflight=parse_int_env(
            "EXECTUNNEL_DNS_MAX_INFLIGHT",
            _TUNNEL_DEFAULTS.dns_max_inflight,
            min_value=1,
        ),
        dns_upstream_port=parse_int_env(
            "EXECTUNNEL_DNS_UPSTREAM_PORT",
            dns_upstream_port,
            min_value=1,
        ),
        dns_query_timeout=parse_float_env(
            "EXECTUNNEL_DNS_QUERY_TIMEOUT",
            dns_query_timeout,
            min_value=0.1,
        ),
    )
