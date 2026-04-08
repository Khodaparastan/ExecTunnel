"""Configuration dataclasses and factory functions."""

from __future__ import annotations

import ipaddress
import logging
import os
import ssl
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlparse

from exectunnel.config.defaults import (
    ACK_TIMEOUT_RECONNECT_THRESHOLD,
    ACK_TIMEOUT_WARN_EVERY,
    ACK_TIMEOUT_WINDOW_SECS,
    BOOTSTRAP_AGENT_PATH,
    BOOTSTRAP_GITHUB_AGENT_URL,
    BOOTSTRAP_SYNTAX_OK_SENTINEL,
    CONN_ACK_TIMEOUT_SECS,
    CONNECT_MAX_PENDING,
    CONNECT_MAX_PENDING_PER_HOST,
    DNS_LOCAL_PORT,
    DNS_MAX_INFLIGHT,
    PRE_ACK_BUFFER_CAP_BYTES,
    READY_TIMEOUT_SECS,
    SOCKS_DEFAULT_HOST,
    SOCKS_DEFAULT_PORT,
    WS_PING_INTERVAL_SECS,
    WS_RECONNECT_BASE_DELAY_SECS,
    WS_RECONNECT_MAX_DELAY_SECS,
    WS_RECONNECT_MAX_RETRIES,
    WS_SEND_QUEUE_CAP,
    WS_SEND_TIMEOUT_SECS,
)
from exectunnel.config.env import parse_bool_env, parse_float_env, parse_int_env
from exectunnel.config.exclusions import get_default_exclusion_networks

logger = logging.getLogger("exectunnel")


@dataclass(slots=True, frozen=True)
class BridgeConfig:
    """Runtime tunables for the WebSocket bridge."""

    ping_interval: float = WS_PING_INTERVAL_SECS
    # Maximum time (seconds) to wait for a single ws.send() call.
    send_timeout: float = WS_SEND_TIMEOUT_SECS
    # Maximum number of frames queued for the outbound send loop.
    send_queue_cap: int = WS_SEND_QUEUE_CAP
    reconnect_max_retries: int = WS_RECONNECT_MAX_RETRIES
    reconnect_base_delay: float = WS_RECONNECT_BASE_DELAY_SECS
    reconnect_max_delay: float = WS_RECONNECT_MAX_DELAY_SECS
    dns_max_inflight: int = DNS_MAX_INFLIGHT


@dataclass(slots=True, frozen=True)
class AppConfig:
    """Top-level config passed to all commands."""

    wss_url: str
    insecure: bool
    bridge: BridgeConfig
    version: str = "1.0"
    ws_headers: dict[str, str] = field(default_factory=dict)

    def ssl_context(self) -> ssl.SSLContext | None:
        """Return an SSL context for wss:// URLs, or None for plain ws://."""
        if self.wss_url.startswith("ws://"):
            return None
        return create_ssl_context(self.insecure)


@dataclass(slots=True, frozen=True)
class TunnelConfig:
    """Configuration for the SOCKS5 tunnel command."""

    socks_host: str = SOCKS_DEFAULT_HOST
    socks_port: int = SOCKS_DEFAULT_PORT
    # Optional upstream DNS IP forwarded through the tunnel (e.g. "10.96.0.10").
    dns_upstream: str | None = None
    dns_local_port: int = DNS_LOCAL_PORT
    # How long to wait for the remote agent to emit AGENT_READY.
    ready_timeout: float = READY_TIMEOUT_SECS
    # How long to wait for the remote agent to ACK a CONN_OPEN (per connection).
    conn_ack_timeout: float = CONN_ACK_TIMEOUT_SECS
    # CIDRs that bypass the tunnel and connect directly.
    exclude: list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None = field(
        default_factory=get_default_exclusion_networks
    )
    # ACK-timeout tunables (overridable via env).
    ack_timeout_warn_every: int = ACK_TIMEOUT_WARN_EVERY
    ack_timeout_window_secs: float = ACK_TIMEOUT_WINDOW_SECS
    ack_timeout_reconnect_threshold: int = ACK_TIMEOUT_RECONNECT_THRESHOLD
    # Connect-hardening tunables (overridable via env).
    connect_max_pending: int = CONNECT_MAX_PENDING
    connect_max_pending_per_host: int = CONNECT_MAX_PENDING_PER_HOST
    # Pre-ACK send buffer cap in bytes (overridable via env).
    pre_ack_buffer_cap_bytes: int = PRE_ACK_BUFFER_CAP_BYTES
    connect_pace_interval_secs: int = 3
    # How the agent payload is delivered to the remote pod.
    # "upload"  — encode and upload agent.py via printf chunks (default).
    # "github"  — fetch agent.py from a raw GitHub URL using curl/wget.
    bootstrap_delivery: str = "upload"
    # Raw URL used when bootstrap_delivery="github".
    github_agent_url: str = BOOTSTRAP_GITHUB_AGENT_URL
    # If True and the agent file already exists on the pod, skip delivery
    # (regardless of bootstrap_delivery mode) and go straight to exec.
    # If False and the agent file exists, remove it and re-deliver.
    bootstrap_skip_if_present: bool = False
    # If True, run `python3 -m py_compile` on the agent before exec'ing it.
    # When skip_if_present=True and the syntax-OK sentinel file is present
    # on the pod, the syntax check is also skipped (cached result).
    bootstrap_syntax_check: bool = True
    # Path of the agent script inside the pod.
    bootstrap_agent_path: str = BOOTSTRAP_AGENT_PATH
    # Path of the syntax-OK sentinel file inside the pod.
    bootstrap_syntax_ok_sentinel: str = BOOTSTRAP_SYNTAX_OK_SENTINEL


# Module-level default bridge config
CONFIG: Final[BridgeConfig] = BridgeConfig()

# Module-level default tunnel config.
TUNNEL_CONFIG: Final[TunnelConfig] = TunnelConfig()


def get_wss_url() -> str:
    """Return EXECTUNNEL_WSS_URL (or legacy WSS_URL) from the environment or raise ConfigurationError."""
    from exectunnel.exceptions import (
        ConfigurationError,
    )

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


def get_app_config() -> AppConfig:
    """Build AppConfig from environment. Raises ConfigurationError if WSS URL is missing."""
    reconnect_base_delay = parse_float_env(
        "WSS_RECONNECT_BASE_DELAY",
        CONFIG.reconnect_base_delay,
        min_value=0.1,
    )
    reconnect_max_delay = parse_float_env(
        "WSS_RECONNECT_MAX_DELAY",
        CONFIG.reconnect_max_delay,
        min_value=0.1,
    )
    if reconnect_max_delay < reconnect_base_delay:
        logger.warning(
            "WSS_RECONNECT_MAX_DELAY %.3f < WSS_RECONNECT_BASE_DELAY %.3f; adjusting max delay",
            reconnect_max_delay,
            reconnect_base_delay,
        )
        reconnect_max_delay = reconnect_base_delay
    return AppConfig(
        wss_url=get_wss_url(),
        insecure=parse_bool_env("WSS_INSECURE"),
        bridge=BridgeConfig(
            ping_interval=parse_int_env(
                "WSS_PING_INTERVAL", CONFIG.ping_interval, min_value=1
            ),
            send_timeout=parse_float_env(
                "WSS_SEND_TIMEOUT", CONFIG.send_timeout, min_value=0.1
            ),
            send_queue_cap=parse_int_env(
                "WSS_SEND_QUEUE_CAP", CONFIG.send_queue_cap, min_value=1
            ),
            reconnect_max_retries=parse_int_env(
                "WSS_RECONNECT_MAX_RETRIES", CONFIG.reconnect_max_retries, min_value=0
            ),
            reconnect_base_delay=reconnect_base_delay,
            reconnect_max_delay=reconnect_max_delay,
        ),
    )


def get_tunnel_config(
    app_cfg: AppConfig,
    *,
    socks_host: str = TUNNEL_CONFIG.socks_host,
    socks_port: int = TUNNEL_CONFIG.socks_port,
    dns_upstream: str | None = TUNNEL_CONFIG.dns_upstream,
    dns_local_port: int = TUNNEL_CONFIG.dns_local_port,
    ready_timeout: float = TUNNEL_CONFIG.ready_timeout,
    conn_ack_timeout: float = TUNNEL_CONFIG.conn_ack_timeout,
    exclude: list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None = None,
    bootstrap_delivery: str = TUNNEL_CONFIG.bootstrap_delivery,
    github_agent_url: str = TUNNEL_CONFIG.github_agent_url,
    bootstrap_skip_if_present: bool = TUNNEL_CONFIG.bootstrap_skip_if_present,
    bootstrap_syntax_check: bool = TUNNEL_CONFIG.bootstrap_syntax_check,
    bootstrap_agent_path: str = TUNNEL_CONFIG.bootstrap_agent_path,
    bootstrap_syntax_ok_sentinel: str = TUNNEL_CONFIG.bootstrap_syntax_ok_sentinel,
) -> TunnelConfig:
    """Build TunnelConfig, merging CLI args with environment overrides."""
    return TunnelConfig(
        socks_host=socks_host,
        socks_port=socks_port,
        dns_upstream=dns_upstream,
        dns_local_port=dns_local_port,
        ready_timeout=ready_timeout,
        conn_ack_timeout=conn_ack_timeout,
        exclude=exclude if exclude is not None else TUNNEL_CONFIG.exclude,
        ack_timeout_warn_every=parse_int_env(
            "EXECTUNNEL_ACK_TIMEOUT_WARN_EVERY",
            TUNNEL_CONFIG.ack_timeout_warn_every,
            min_value=1,
        ),
        ack_timeout_window_secs=parse_float_env(
            "EXECTUNNEL_ACK_TIMEOUT_WINDOW_SECS",
            TUNNEL_CONFIG.ack_timeout_window_secs,
            min_value=1.0,
        ),
        ack_timeout_reconnect_threshold=parse_int_env(
            "EXECTUNNEL_ACK_TIMEOUT_RECONNECT_THRESHOLD",
            TUNNEL_CONFIG.ack_timeout_reconnect_threshold,
            min_value=1,
        ),
        connect_max_pending=parse_int_env(
            "EXECTUNNEL_CONNECT_MAX_PENDING",
            TUNNEL_CONFIG.connect_max_pending,
            min_value=1,
        ),
        connect_max_pending_per_host=parse_int_env(
            "EXECTUNNEL_CONNECT_MAX_PENDING_PER_HOST",
            TUNNEL_CONFIG.connect_max_pending_per_host,
            min_value=1,
        ),
        pre_ack_buffer_cap_bytes=parse_int_env(
            "EXECTUNNEL_PRE_ACK_BUFFER_CAP_BYTES",
            TUNNEL_CONFIG.pre_ack_buffer_cap_bytes,
            min_value=1024,
        ),
        bootstrap_delivery=os.getenv("EXECTUNNEL_BOOTSTRAP_DELIVERY", bootstrap_delivery),
        github_agent_url=os.getenv("EXECTUNNEL_GITHUB_AGENT_URL", github_agent_url),
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
    )


def create_ssl_context(insecure: bool) -> ssl.SSLContext:
    """Create and return an SSL context, optionally with verification disabled."""
    ctx = ssl.create_default_context()
    if insecure:
        logger.warning("TLS verification disabled (WSS_INSECURE=1)")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx
