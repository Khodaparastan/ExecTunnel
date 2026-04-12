"""Configuration dataclasses and factory functions."""

from __future__ import annotations

import ipaddress
import logging
import os
import ssl
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlparse

from .defaults import Defaults
from .env import parse_bool_env, parse_float_env, parse_int_env
from .exclusions import get_default_exclusion_networks

logger = logging.getLogger("exectunnel")


@dataclass(slots=True, frozen=True)
class BridgeConfig:
    """Runtime tunables for the WebSocket bridge."""

    ping_interval: float = Defaults.WS_PING_INTERVAL_SECS
    # Maximum time (seconds) to wait for a single ws.send() call.
    send_timeout: float = Defaults.WS_SEND_TIMEOUT_SECS
    # Maximum number of frames queued for the outbound send loop.
    send_queue_cap: int = Defaults.WS_SEND_QUEUE_CAP
    reconnect_max_retries: int = Defaults.WS_RECONNECT_MAX_RETRIES
    reconnect_base_delay: float = Defaults.WS_RECONNECT_BASE_DELAY_SECS
    reconnect_max_delay: float = Defaults.WS_RECONNECT_MAX_DELAY_SECS
    dns_max_inflight: int = Defaults.DNS_MAX_INFLIGHT


@dataclass(slots=True, frozen=True)
class AppConfig:
    """Top-level config passed to all commands."""

    wss_url: str
    insecure: bool
    bridge: BridgeConfig
    version: str = "1.0"
    ws_headers: dict[str, str] = field(default_factory=dict)
    # Optional pre-built SSL context (e.g. from kubeconfig credentials).
    # When set, ssl_context() returns this instead of building a new one.
    _ssl_context: ssl.SSLContext | None = field(default=None, compare=False)

    def ssl_context(self) -> ssl.SSLContext | None:
        """Return an SSL context for wss:// URLs, or None for plain ws://."""
        if self._ssl_context is not None:
            return self._ssl_context
        if self.wss_url.startswith("ws://"):
            return None
        return create_ssl_context(self.insecure)

    @classmethod
    def from_env(
        cls,
        base: "AppConfig | None" = None,
        *,
        wss_url: str | None = None,
        ws_headers: dict[str, str] | None = None,
        ssl_context: Any | None = None,
        reconnect_max_retries: int | None = None,
    ) -> "AppConfig":
        """Build an AppConfig, optionally overriding fields from a base config.

        When *base* is provided (e.g. from :func:`get_app_config`), its values
        are used as defaults and any keyword arguments override them.
        When *base* is ``None``, *wss_url* is required.

        Args:
            base:                 Optional base config to inherit defaults from.
            wss_url:              WebSocket URL override.
            ws_headers:           HTTP headers to send during the WS handshake.
            ssl_context:          Pre-built SSL context (e.g. from kubeconfig).
            reconnect_max_retries: Override for max reconnect attempts.
        """
        if base is None and wss_url is None:
            raise ValueError("Either 'base' or 'wss_url' must be provided.")

        resolved_url: str = wss_url if wss_url is not None else base.wss_url  # type: ignore[union-attr]
        resolved_insecure: bool = base.insecure if base is not None else False
        resolved_headers: dict[str, str] = (
            ws_headers if ws_headers is not None
            else (base.ws_headers if base is not None else {})
        )
        resolved_bridge: BridgeConfig = base.bridge if base is not None else BridgeConfig()
        if reconnect_max_retries is not None:
            resolved_bridge = BridgeConfig(
                ping_interval=resolved_bridge.ping_interval,
                send_timeout=resolved_bridge.send_timeout,
                send_queue_cap=resolved_bridge.send_queue_cap,
                reconnect_max_retries=reconnect_max_retries,
                reconnect_base_delay=resolved_bridge.reconnect_base_delay,
                reconnect_max_delay=resolved_bridge.reconnect_max_delay,
                dns_max_inflight=resolved_bridge.dns_max_inflight,
            )
        return cls(
            wss_url=resolved_url,
            insecure=resolved_insecure,
            bridge=resolved_bridge,
            ws_headers=resolved_headers,
            _ssl_context=ssl_context,
        )


@dataclass(slots=True, frozen=True)
class TunnelConfig:
    """Configuration for the SOCKS5 tunnel command."""

    socks_host: str = Defaults.SOCKS_DEFAULT_HOST
    socks_port: int = Defaults.SOCKS_DEFAULT_PORT
    # Optional upstream DNS IP forwarded through the tunnel (e.g. "10.96.0.10").
    dns_upstream: str | None = None
    dns_local_port: int = Defaults.DNS_LOCAL_PORT
    # How long to wait for the remote agent to emit AGENT_READY.
    ready_timeout: float = Defaults.READY_TIMEOUT_SECS
    # How long to wait for the remote agent to ACK a CONN_OPEN (per connection).
    conn_ack_timeout: float = Defaults.CONN_ACK_TIMEOUT_SECS
    # CIDRs that bypass the tunnel and connect directly.
    exclude: list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None = field(
        default_factory=get_default_exclusion_networks
    )
    # ACK-timeout tunables (overridable via env).
    ack_timeout_warn_every: int = Defaults.ACK_TIMEOUT_WARN_EVERY
    ack_timeout_window_secs: float = Defaults.ACK_TIMEOUT_WINDOW_SECS
    ack_timeout_reconnect_threshold: int = Defaults.ACK_TIMEOUT_RECONNECT_THRESHOLD
    # Connect-hardening tunables (overridable via env).
    connect_max_pending: int = Defaults.CONNECT_MAX_PENDING
    connect_max_pending_per_host: int = Defaults.CONNECT_MAX_PENDING_PER_HOST
    # Pre-ACK send buffer cap in bytes (overridable via env).
    pre_ack_buffer_cap_bytes: int = Defaults.PRE_ACK_BUFFER_CAP_BYTES
    connect_pace_interval_secs: float = Defaults.CONNECT_PACE_INTERVAL_SECS
    # How the agent payload is delivered to the remote pod.
    # "upload"  — encode and upload agent.py via printf chunks (default).
    # "fetch"  — fetch agent.py from a raw fetch URL using curl/wget.
    bootstrap_delivery: str = "upload"
    # Raw URL used when bootstrap_delivery="fetch".
    fetch_agent_url: str = Defaults.BOOTSTRAP_FETCH_AGENT_URL
    # If True and the agent file already exists on the pod, skip delivery
    # (regardless of bootstrap_delivery mode) and go straight to exec.
    # If False and the agent file exists, remove it and re-deliver.
    bootstrap_skip_if_present: bool = False
    # If True, run `python3 -m py_compile` on the agent before exec'ing it.
    # When skip_if_present=True and the syntax-OK sentinel file is present
    # on the pod, the syntax check is also skipped (cached result).
    bootstrap_syntax_check: bool = True
    # Path of the agent script inside the pod.
    bootstrap_agent_path: str = Defaults.BOOTSTRAP_AGENT_PATH
    # Path of the syntax-OK sentinel file inside the pod.
    bootstrap_syntax_ok_sentinel: str = Defaults.BOOTSTRAP_SYNTAX_OK_SENTINEL
    # If True, upload and run the pre-built Go agent binary instead of agent.py.
    # The binary must be at exectunnel/payload/go_agent/agent_linux_amd64 —
    # build it with: CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o agent_linux_amd64 .
    bootstrap_use_go_agent: bool = False
    # Path of the Go agent binary inside the pod.
    # Override via EXECTUNNEL_BOOTSTRAP_GO_AGENT_PATH if /tmp is noexec.
    bootstrap_go_agent_path: str = Defaults.BOOTSTRAP_GO_AGENT_PATH
    # ── SOCKS5 server tunables ────────────────────────────────────────────────
    # Max seconds to complete a SOCKS5 handshake before the connection is dropped.
    socks_handshake_timeout: float = Defaults.HANDSHAKE_TIMEOUT_SECS
    # asyncio.Queue capacity for completed handshakes awaiting dispatch.
    socks_request_queue_cap: int = Defaults.SOCKS_REQUEST_QUEUE_CAP
    # Max seconds to enqueue a completed handshake before dropping it.
    socks_queue_put_timeout: float = Defaults.SOCKS_QUEUE_PUT_TIMEOUT_SECS
    # ── UDP relay tunables ────────────────────────────────────────────────────
    # asyncio.Queue capacity for inbound datagrams per UDP_ASSOCIATE session.
    udp_relay_queue_cap: int = Defaults.UDP_RELAY_QUEUE_CAP
    # Log a warning every N UDP queue-full drops.
    udp_drop_warn_every: int = Defaults.UDP_WARN_EVERY
    # Polling interval for the UDP ASSOCIATE pump loop (seconds).
    udp_pump_poll_timeout: float = Defaults.UDP_PUMP_POLL_TIMEOUT_SECS
    # Timeout for receiving a UDP response on directly-connected (excluded) hosts.
    udp_direct_recv_timeout: float = Defaults.UDP_DIRECT_RECV_TIMEOUT_SECS
    # ── DNS tunables ──────────────────────────────────────────────────────────
    # Upstream DNS port (default: 53).
    dns_upstream_port: int = Defaults.DNS_UPSTREAM_PORT
    # End-to-end DNS query timeout through the tunnel (seconds).
    dns_query_timeout: float = Defaults.DNS_QUERY_TIMEOUT_SECS


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
        "EXECTUNNEL_RECONNECT_BASE_DELAY",
        CONFIG.reconnect_base_delay,
        min_value=0.1,
    )
    reconnect_max_delay = parse_float_env(
        "EXECTUNNEL_RECONNECT_MAX_DELAY",
        CONFIG.reconnect_max_delay,
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
    return AppConfig(
        wss_url=get_wss_url(),
        insecure=parse_bool_env("WSS_INSECURE"),
        bridge=BridgeConfig(
            ping_interval=parse_int_env(
                "EXECTUNNEL_PING_INTERVAL", CONFIG.ping_interval, min_value=1
            ),
            send_timeout=parse_float_env(
                "EXECTUNNEL_SEND_TIMEOUT", CONFIG.send_timeout, min_value=0.1
            ),
            send_queue_cap=parse_int_env(
                "EXECTUNNEL_SEND_QUEUE_CAP", CONFIG.send_queue_cap, min_value=1
            ),
            reconnect_max_retries=parse_int_env(
                "EXECTUNNEL_RECONNECT_MAX_RETRIES", CONFIG.reconnect_max_retries, min_value=0
            ),
            reconnect_base_delay=reconnect_base_delay,
            reconnect_max_delay=reconnect_max_delay,
            dns_max_inflight=parse_int_env(
                "EXECTUNNEL_DNS_MAX_INFLIGHT", CONFIG.dns_max_inflight, min_value=1
            ),
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
    fetch_agent_url: str = TUNNEL_CONFIG.fetch_agent_url,
    bootstrap_skip_if_present: bool = TUNNEL_CONFIG.bootstrap_skip_if_present,
    bootstrap_syntax_check: bool = TUNNEL_CONFIG.bootstrap_syntax_check,
    bootstrap_agent_path: str = TUNNEL_CONFIG.bootstrap_agent_path,
    bootstrap_syntax_ok_sentinel: str = TUNNEL_CONFIG.bootstrap_syntax_ok_sentinel,
    bootstrap_use_go_agent: bool = TUNNEL_CONFIG.bootstrap_use_go_agent,
    bootstrap_go_agent_path: str = TUNNEL_CONFIG.bootstrap_go_agent_path,
    connect_pace_interval_secs: float = TUNNEL_CONFIG.connect_pace_interval_secs,
    socks_handshake_timeout: float = TUNNEL_CONFIG.socks_handshake_timeout,
    socks_request_queue_cap: int = TUNNEL_CONFIG.socks_request_queue_cap,
    socks_queue_put_timeout: float = TUNNEL_CONFIG.socks_queue_put_timeout,
    udp_relay_queue_cap: int = TUNNEL_CONFIG.udp_relay_queue_cap,
    udp_drop_warn_every: int = TUNNEL_CONFIG.udp_drop_warn_every,
    udp_pump_poll_timeout: float = TUNNEL_CONFIG.udp_pump_poll_timeout,
    udp_direct_recv_timeout: float = TUNNEL_CONFIG.udp_direct_recv_timeout,
    dns_upstream_port: int = TUNNEL_CONFIG.dns_upstream_port,
    dns_query_timeout: float = TUNNEL_CONFIG.dns_query_timeout,
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
        bootstrap_delivery=os.getenv(
            "EXECTUNNEL_BOOTSTRAP_DELIVERY", bootstrap_delivery
        ),
        fetch_agent_url=os.getenv("EXECTUNNEL_FETCH_AGENT_URL", fetch_agent_url),
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
            TUNNEL_CONFIG.connect_pace_interval_secs,
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


def create_ssl_context(insecure: bool) -> ssl.SSLContext:
    """Create and return an SSL context, optionally with verification disabled."""
    ctx = ssl.create_default_context()
    # Force HTTP/1.1 via ALPN — WebSocket upgrade only works over HTTP/1.1.
    # Servers like stream.runflare.com return 400 if h2 is negotiated.
    ctx.set_alpn_protocols(["http/1.1"])
    if insecure:
        logger.warning("TLS verification disabled (WSS_INSECURE=1)")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx
