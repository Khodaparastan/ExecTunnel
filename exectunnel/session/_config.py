"""Session-layer configuration dataclasses.

Declares exactly what the tunnel session consumes — flat, frozen,
no environment reading, no SSL logic.
"""

from __future__ import annotations

import ipaddress
import logging
import ssl
from dataclasses import dataclass, field
from typing import Literal

from exectunnel.defaults import Defaults

from ._routing import get_default_exclusion_networks

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class SessionConfig:
    """All tunables the tunnel session needs."""

    # ── WebSocket connection ──────────────────────────────────────────
    wss_url: str
    ws_headers: dict[str, str] = field(default_factory=dict)
    ssl_context_override: ssl.SSLContext | None = field(default=None, compare=False)
    version: str = "1.0"

    # ── Reconnect / transport ─────────────────────────────────────────
    reconnect_max_retries: int = Defaults.WS_RECONNECT_MAX_RETRIES
    reconnect_base_delay: float = Defaults.WS_RECONNECT_BASE_DELAY_SECS
    reconnect_max_delay: float = Defaults.WS_RECONNECT_MAX_DELAY_SECS
    ping_interval: float = Defaults.WS_PING_INTERVAL_SECS
    send_timeout: float = Defaults.WS_SEND_TIMEOUT_SECS
    send_queue_cap: int = Defaults.WS_SEND_QUEUE_CAP

    def ssl_context(self) -> ssl.SSLContext | None:
        """Return explicit SSL context override, or None for library defaults."""
        if self.ssl_context_override is not None:
            return self.ssl_context_override
        if self.wss_url.startswith("ws://"):
            return None
        return None


@dataclass(slots=True, frozen=True)
class TunnelConfig:
    """Tunnel-specific tunables."""

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
    exclude: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(
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
    bootstrap_delivery: Literal["upload", "fetch"] = "upload"
    # Raw URL used when bootstrap_delivery="fetch".
    bootstrap_fetch_url: str = Defaults.BOOTSTRAP_FETCH_AGENT_URL
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
    # Maximum concurrent in-flight DNS queries (moved from BridgeConfig).
    dns_max_inflight: int = Defaults.DNS_MAX_INFLIGHT
    # Upstream DNS port (default: 53).
    dns_upstream_port: int = Defaults.DNS_UPSTREAM_PORT
    # End-to-end DNS query timeout through the tunnel (seconds).
    dns_query_timeout: float = Defaults.DNS_QUERY_TIMEOUT_SECS
