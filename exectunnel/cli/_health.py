"""Health monitor — collects and exposes live tunnel and pod statistics."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from exectunnel.session import TunnelSession


@dataclass(slots=True)
class ConnectionStat:
    conn_id:        str
    host:           str
    port:           int
    state:          str          # "pending" | "open" | "closing" | "closed"
    bytes_up:       int = 0
    bytes_down:     int = 0
    drop_count:     int = 0
    opened_at:      float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class TunnelHealth:
    """Snapshot of tunnel health at a point in time."""

    # Connection
    connected:          bool   = False
    ws_url:             str    = ""
    reconnect_count:    int    = 0
    last_reconnect_at:  float  = 0.0

    # Bootstrap
    bootstrap_ok:       bool   = False
    bootstrap_elapsed:  float  = 0.0
    agent_version:      str    = "unknown"

    # SOCKS5
    socks_host:         str    = "127.0.0.1"
    socks_port:         int    = 1080
    socks_ok:           bool   = False

    # Traffic
    frames_sent:        int    = 0
    frames_recv:        int    = 0
    frames_dropped:     int    = 0
    bytes_up_total:     int    = 0
    bytes_down_total:   int    = 0

    # Connections
    tcp_open:           int    = 0
    tcp_pending:        int    = 0
    tcp_total:          int    = 0
    tcp_failed:         int    = 0
    udp_open:           int    = 0
    udp_total:          int    = 0

    # ACK health
    ack_ok:             int    = 0
    ack_timeout:        int    = 0
    ack_failed:         int    = 0

    # DNS
    dns_enabled:        bool   = False
    dns_queries:        int    = 0
    dns_ok:             int    = 0
    dns_dropped:        int    = 0

    # Send queue
    send_queue_depth:   int    = 0
    send_queue_cap:     int    = 0

    # Uptime
    session_start:      float  = field(default_factory=time.monotonic)
    uptime_secs:        float  = 0.0

    # Pod
    pod_name:           str    = ""
    pod_namespace:      str    = ""
    pod_node:           str    = ""
    pod_ip:             str    = ""
    pod_phase:          str    = ""
    pod_conditions:     list[dict[str, str]] = field(default_factory=list)

    # Recent connections (ring buffer)
    recent_conns:       list[ConnectionStat] = field(default_factory=list)


class HealthMonitor:
    """Collects live health statistics from the running session.

    Designed to be polled by the dashboard at a fixed interval.
    All reads are non-blocking — no locks, no I/O.

    Args:
        session:    The running ``TunnelSession`` instance.
        pod_spec:   Pod metadata resolved at startup.
        ws_url:     WebSocket URL in use.
        socks_host: SOCKS5 bind host.
        socks_port: SOCKS5 bind port.
    """

    __slots__ = (
        "_session",
        "_pod",
        "_ws_url",
        "_socks_host",
        "_socks_port",
        "_start",
        "_reconnect_count",
        "_snapshot",
        "_metrics",
    )

    def __init__(
        self,
        session: "TunnelSession",
        pod_spec: object | None,
        ws_url: str,
        socks_host: str,
        socks_port: int,
    ) -> None:
        self._session    = session
        self._pod        = pod_spec
        self._ws_url     = ws_url
        self._socks_host = socks_host
        self._socks_port = socks_port
        self._start      = time.monotonic()
        self._reconnect_count = 0
        self._snapshot   = TunnelHealth()
        self._metrics: dict[str, int] = {}

    def record_reconnect(self) -> None:
        self._reconnect_count += 1

    def ingest_metrics(self, metrics: dict[str, int]) -> None:
        """Ingest a metrics snapshot from the observability layer."""
        self._metrics = dict(metrics)

    def snapshot(self) -> TunnelHealth:
        """Return a fresh health snapshot."""
        m = self._metrics
        now = time.monotonic()

        h = TunnelHealth(
            connected=True,
            ws_url=self._ws_url,
            reconnect_count=self._reconnect_count,
            bootstrap_ok=True,
            socks_host=self._socks_host,
            socks_port=self._socks_port,
            socks_ok=True,
            frames_sent=m.get("tunnel.frames.sent", 0),
            frames_recv=m.get("tunnel.frames.received", 0),
            frames_dropped=m.get("tunnel.frames.send_drop", 0),
            tcp_open=m.get("tcp.open", 0),
            tcp_pending=m.get("pending_connects", 0),
            tcp_total=m.get("tunnel.conn_ack.ok", 0),
            tcp_failed=m.get("tunnel.conn_ack.failed", 0),
            udp_open=m.get("udp.open", 0),
            udp_total=m.get("udp.flow.opened", 0),
            ack_ok=m.get("tunnel.conn_ack.ok", 0),
            ack_timeout=m.get("connect_ack_timeout", 0),
            ack_failed=m.get("tunnel.conn_ack.failed", 0),
            dns_enabled=m.get("dns.forwarder.started", 0) > 0,
            dns_queries=m.get("dns.query.received", 0),
            dns_ok=m.get("dns.query.ok", 0),
            dns_dropped=m.get("dns.query.drop.saturated", 0)
                        + m.get("dns.query.timeout", 0),
            send_queue_depth=m.get("send_queue_depth", 0),
            send_queue_cap=m.get("send_queue_cap", 0),
            session_start=self._start,
            uptime_secs=now - self._start,
        )

        if self._pod is not None:
            pod = self._pod
            h.pod_name       = getattr(pod, "name", "")
            h.pod_namespace  = getattr(pod, "namespace", "")
            h.pod_node       = getattr(pod, "node", "") or ""
            h.pod_ip         = getattr(pod, "ip", "") or ""
            h.pod_phase      = getattr(pod, "phase", "") or ""
            h.pod_conditions = getattr(pod, "conditions", [])

        self._snapshot = h
        return h
