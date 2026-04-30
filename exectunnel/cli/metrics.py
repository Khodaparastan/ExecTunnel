"""Health monitor — collects and exposes live tunnel and pod statistics."""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Protocol, runtime_checkable

from exectunnel.observability import MetricEvent, MetricKind

__all__ = ["ConnectionStat", "HealthMonitor", "PodSpec", "TunnelHealth"]

logger = logging.getLogger(__name__)

_MAX_RECENT_CONNS: int = 64


@runtime_checkable
class PodSpec(Protocol):
    """Minimal interface for pod metadata."""

    name: str
    namespace: str
    node: str | None
    ip: str | None
    phase: str | None
    conditions: list[dict[str, str]]


@dataclass(slots=True)
class ConnectionStat:
    """Snapshot of a single SOCKS5-proxied connection for dashboard display."""

    conn_id: str
    host: str
    port: int
    state: str
    bytes_up: int = 0
    bytes_down: int = 0
    drop_count: int = 0
    opened_at: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class HistogramStat:
    """Running histogram summary for one metric."""

    count: int = 0
    total: float = 0.0
    min: float | None = None
    max: float | None = None

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.min = value if self.min is None else min(self.min, value)
        self.max = value if self.max is None else max(self.max, value)

    @property
    def avg(self) -> float | None:
        if self.count == 0:
            return None
        return self.total / self.count


@dataclass(slots=True)
class TunnelHealth:
    """Point-in-time snapshot of tunnel health."""

    connected: bool = False
    ws_url: str = ""
    reconnect_count: int = 0
    last_reconnect_at: float = 0.0

    bootstrap_ok: bool = False
    agent_version: str = "unknown"

    socks_host: str = "127.0.0.1"
    socks_port: int = 1080
    socks_ok: bool = False

    frames_sent: int = 0
    frames_recv: int = 0
    frames_dropped: int = 0
    bytes_up_total: int = 0
    bytes_down_total: int = 0

    tcp_open: int = 0
    tcp_pending: int = 0
    tcp_total: int = 0
    tcp_failed: int = 0

    udp_open: int = 0
    udp_total: int = 0

    ack_ok: int = 0
    ack_timeout: int = 0
    ack_failed: int = 0

    dns_enabled: bool = False
    dns_queries: int = 0
    dns_ok: int = 0
    dns_dropped: int = 0

    send_queue_depth: int = 0
    send_queue_cap: int = 0

    session_start: float = field(default_factory=time.monotonic)
    uptime_secs: float = 0.0

    pod_name: str = ""
    pod_namespace: str = ""
    pod_node: str = ""
    pod_ip: str = ""
    pod_phase: str = ""
    pod_conditions: list[dict[str, str]] = field(default_factory=list)

    socks5_accepted: int = 0
    socks5_rejected: int = 0
    socks5_active: int = 0
    socks5_handshakes_ok: int = 0
    socks5_handshakes_timeout: int = 0
    socks5_handshakes_error: int = 0
    socks5_cmd_connect: int = 0
    socks5_cmd_udp: int = 0
    socks5_udp_relays_active: int = 0
    socks5_udp_datagrams: int = 0
    socks5_udp_dropped: int = 0

    session_connect_attempts: int = 0
    session_connect_ok: int = 0
    session_reconnects: int = 0
    reconnect_delay_avg: float | None = None
    reconnect_delay_max: float | None = None

    session_serve_started: int = 0
    session_serve_stopped: int = 0

    cleanup_tcp: int = 0
    cleanup_pending: int = 0
    cleanup_udp: int = 0

    frames_decode_errors: int = 0
    frames_orphaned: int = 0
    frames_noise: int = 0
    frames_outbound_timeout: int = 0
    frames_outbound_ws_closed: int = 0

    tcp_upstream_started: int = 0
    tcp_downstream_started: int = 0
    tcp_completed: int = 0
    tcp_errors: int = 0

    udp_flows_opened: int = 0
    udp_flows_closed: int = 0
    udp_datagrams_sent: int = 0
    udp_datagrams_accepted: int = 0
    udp_datagrams_dropped: int = 0

    bootstrap_duration: float | None = None

    request_tasks: int = 0

    recent_conns: list[ConnectionStat] = field(default_factory=list)

    def to_report_dict(self) -> dict[str, object]:
        """Return a flat dict of key metrics suitable for JSON serialisation."""
        return {
            "connected": self.connected,
            "bootstrap_ok": self.bootstrap_ok,
            "socks_ok": self.socks_ok,
            "uptime_secs": round(self.uptime_secs, 1),
            "frames_sent": self.frames_sent,
            "frames_recv": self.frames_recv,
            "frames_dropped": self.frames_dropped,
            "bytes_up": self.bytes_up_total,
            "bytes_down": self.bytes_down_total,
            "frames_decode_errors": self.frames_decode_errors,
            "frames_orphaned": self.frames_orphaned,
            "frames_noise": self.frames_noise,
            "tcp_open": self.tcp_open,
            "tcp_pending": self.tcp_pending,
            "tcp_total": self.tcp_total,
            "tcp_failed": self.tcp_failed,
            "tcp_completed": self.tcp_completed,
            "tcp_errors": self.tcp_errors,
            "udp_open": self.udp_open,
            "udp_total": self.udp_total,
            "udp_flows_opened": self.udp_flows_opened,
            "udp_flows_closed": self.udp_flows_closed,
            "udp_datagrams_sent": self.udp_datagrams_sent,
            "udp_datagrams_accepted": self.udp_datagrams_accepted,
            "udp_datagrams_dropped": self.udp_datagrams_dropped,
            "ack_ok": self.ack_ok,
            "ack_timeout": self.ack_timeout,
            "ack_failed": self.ack_failed,
            "dns_enabled": self.dns_enabled,
            "dns_queries": self.dns_queries,
            "dns_ok": self.dns_ok,
            "dns_dropped": self.dns_dropped,
            "socks5_accepted": self.socks5_accepted,
            "socks5_rejected": self.socks5_rejected,
            "socks5_active": self.socks5_active,
            "socks5_handshakes_ok": self.socks5_handshakes_ok,
            "socks5_handshakes_error": self.socks5_handshakes_error,
            "socks5_cmd_connect": self.socks5_cmd_connect,
            "socks5_cmd_udp": self.socks5_cmd_udp,
            "socks5_udp_relays_active": self.socks5_udp_relays_active,
            "socks5_udp_datagrams": self.socks5_udp_datagrams,
            "socks5_udp_dropped": self.socks5_udp_dropped,
            "session_connect_attempts": self.session_connect_attempts,
            "session_connect_ok": self.session_connect_ok,
            "session_reconnects": self.session_reconnects,
            "reconnect_delay_avg": self.reconnect_delay_avg,
            "reconnect_delay_max": self.reconnect_delay_max,
            "session_serve_started": self.session_serve_started,
            "session_serve_stopped": self.session_serve_stopped,
            "cleanup_tcp": self.cleanup_tcp,
            "cleanup_pending": self.cleanup_pending,
            "cleanup_udp": self.cleanup_udp,
            "bootstrap_duration": self.bootstrap_duration,
            "send_queue_depth": self.send_queue_depth,
            "send_queue_cap": self.send_queue_cap,
            "request_tasks": self.request_tasks,
        }


_METRIC_TO_COUNTER: dict[str, str] = {
    "session.frames.outbound": "frames.sent",
    "session.keepalive.sent": "frames.sent",
    "session.frames.inbound": "frames.recv",
    "session.frames.outbound.drop": "frames.dropped",
    "session.frames.decode_error": "frames.decode_error",
    "session.frames.payload_decode_error": "frames.decode_error",
    "session.frames.orphaned": "frames.orphaned",
    "session.frames.noise": "frames.noise",
    "session.frames.outbound.timeout": "frames.outbound.timeout",
    "session.frames.outbound.ws_closed": "frames.outbound.ws_closed",
    "session.frames.no_conn_id": "frames.orphaned",
    "session.frames.outbound.bytes": "bytes.up",
    "session.frames.bytes.in": "bytes.down",
    "tunnel.conn_ack.ok": "ack.ok",
    "tunnel.conn_ack.failed": "ack.failed",
    "tunnel.conn_ack.timeout": "ack.timeout",
    "tunnel.conn.completed": "tcp.completed",
    "tunnel.conn.error": "tcp.error",
    "tcp.connection.upstream.started": "tcp.upstream.started",
    "tcp.connection.downstream.started": "tcp.downstream.started",
    "tcp.connection.cleanup": "tcp.cleanup",
    "udp.flow.opened": "udp.flow.opened",
    "udp.flow.closed": "udp.flow.closed",
    "udp.flow.closed_remote": "udp.flow.closed",
    "udp.flow.datagram.submitted": "udp.flow.datagram.submitted",
    "udp.flow.datagram.accepted": "udp.flow.datagram.accepted",
    "udp.flow.inbound_queue.drop": "udp.flow.datagram.dropped",
    "udp.flow.feed_after_close.drop": "udp.flow.datagram.dropped",
    "dns.forwarder.started": "dns.started",
    "dns.query.received": "dns.query.received",
    "dns.query.ok": "dns.query.ok",
    "dns.query.timeout": "dns.query.timeout",
    "dns.query.drop": "dns.query.drop",
    "dns.query.error": "dns.query.error",
    "bootstrap.ok": "bootstrap.ok",
    "bootstrap.started": "bootstrap.started",
    "socks5.connections.accepted": "socks5.accepted",
    "socks5.connections.rejected": "socks5.rejected",
    "socks5.handshakes.ok": "socks5.handshakes.ok",
    "socks5.handshakes.timeout": "socks5.handshakes.timeout",
    "socks5.handshakes.error": "socks5.handshakes.error",
    "socks5.commands.connect": "socks5.cmd.connect",
    "socks5.commands.udp_associate": "socks5.cmd.udp",
    "socks5.commands.udp_associate_rejected": "socks5.rejected",
    "socks5.commands.bind_rejected": "socks5.rejected",
    "socks5.udp.datagrams_accepted": "socks5.udp.datagrams",
    "socks5.udp.datagrams_dropped": "socks5.udp.dropped",
    "socks5.udp.foreign_client_drops": "socks5.udp.dropped",
    "socks5.udp.reply_dropped": "socks5.udp.dropped",
    "session.connect.attempt": "session.connect.attempt",
    "session.connect.ok": "session.connect.ok",
    "session.reconnect": "session.reconnect",
    "session.serve.started": "session.serve.started",
    "session.serve.stopped": "session.serve.stopped",
    "session.cleanup.tcp": "session.cleanup.tcp",
    "session.cleanup.pending": "session.cleanup.pending",
    "session.cleanup.udp": "session.cleanup.udp",
}

_CONN_LIFECYCLE_METRICS: frozenset[str] = frozenset({
    "tunnel.conn_open",
    "tunnel.conn_ack.ok",
    "tunnel.conn_ack.failed",
    "tunnel.conn_ack.timeout",
    "tcp.connection.upstream.bytes",
    "tcp.connection.downstream.bytes",
    "tunnel.conn.completed",
    "tunnel.conn.error",
})

_GAUGE_KEYS: frozenset[str] = frozenset({
    "socks5.udp.relays_active",
    "socks5.connections.active",
    "session.registry.tcp",
    "session.registry.pending_connects",
    "session.registry.udp",
    "session.request_tasks",
    "session.send.queue.data",
    "session.send.queue.ctrl",
    "dns.forwarder.inflight",
    "session.active.tcp_connections",
    "session.active.udp_flows",
})

_HISTOGRAM_KEYS: frozenset[str] = frozenset({
    "session.reconnect.delay_sec",
    "bootstrap.duration_seconds",
    "socks5.handshake_duration_sec",
    "tcp.connection.upstream.duration_sec",
    "tcp.connection.downstream.duration_sec",
    "tcp.connection.upstream.bytes_per_connection",
    "tcp.connection.downstream.bytes_per_connection",
})


def _coerce_int(raw: object, *, default: int = 0) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return max(0, value)


def _coerce_float(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _coerce_port(raw: object) -> int:
    return _coerce_int(raw, default=0)


def _copy_pod_conditions(raw: list[dict[str, str]]) -> list[dict[str, str]]:
    return [dict(item) for item in raw]


class HealthMonitor:
    """Collect live health statistics from structured observability events.

    This implementation is per-session and does not depend on any global
    metrics snapshot, which keeps multi-tunnel dashboards correctly isolated.
    """

    __slots__ = (
        "_pod",
        "_ws_url",
        "_socks_host",
        "_socks_port",
        "_send_queue_cap",
        "_start",
        "_connected",
        "_reconnect_count",
        "_last_reconnect_at",
        "_bootstrap_ok",
        "_counters",
        "_gauges",
        "_histograms",
        "_recent_conns",
        "_conn_index",
        "_lock",
    )

    def __init__(
        self,
        pod_spec: PodSpec | None,
        ws_url: str,
        socks_host: str,
        socks_port: int,
        send_queue_cap: int = 0,
    ) -> None:
        self._pod = pod_spec
        self._ws_url = ws_url
        self._socks_host = socks_host
        self._socks_port = socks_port
        self._send_queue_cap = max(0, send_queue_cap)
        self._start = time.monotonic()
        self._connected = False
        self._reconnect_count = 0
        self._last_reconnect_at = 0.0
        self._bootstrap_ok = False
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, HistogramStat] = {}
        self._recent_conns: deque[ConnectionStat] = deque(maxlen=_MAX_RECENT_CONNS)
        self._conn_index: dict[str, ConnectionStat] = {}
        self._lock = RLock()

    def record_reconnect(self) -> None:
        with self._lock:
            self._reconnect_count += 1
            self._last_reconnect_at = time.monotonic()

    def set_connected(self, connected: bool) -> None:
        with self._lock:
            self._connected = connected

    def record_bootstrap_ok(self) -> None:
        with self._lock:
            self._bootstrap_ok = True

    def record_connection(self, stat: ConnectionStat) -> None:
        with self._lock:
            self._append_connection_unlocked(replace(stat))

    def update_connection(
        self,
        conn_id: str,
        *,
        state: str | None = None,
        bytes_up: int | None = None,
        bytes_down: int | None = None,
    ) -> None:
        with self._lock:
            conn = self._conn_index.get(conn_id)
            if conn is None:
                return
            if state is not None:
                conn.state = state
            if bytes_up is not None:
                conn.bytes_up += max(0, bytes_up)
            if bytes_down is not None:
                conn.bytes_down += max(0, bytes_down)

    def _append_connection_unlocked(self, stat: ConnectionStat) -> None:
        if len(self._recent_conns) == _MAX_RECENT_CONNS:
            evicted = self._recent_conns[0]
            self._conn_index.pop(evicted.conn_id, None)

        self._recent_conns.append(stat)
        self._conn_index[stat.conn_id] = stat

    def _get_or_create_connection_unlocked(
        self,
        conn_id: str,
        *,
        host: str,
        port: int,
        state: str,
    ) -> ConnectionStat:
        existing = self._conn_index.get(conn_id)
        if existing is not None:
            if host and not existing.host:
                existing.host = host
            if port and existing.port == 0:
                existing.port = port
            existing.state = state
            return existing

        created = ConnectionStat(
            conn_id=conn_id,
            host=host,
            port=port,
            state=state,
        )
        self._append_connection_unlocked(created)
        return created

    def on_metric(self, event: MetricEvent | str, **legacy_tags: object) -> None:
        """Observability listener callback.

        Supports:
        * structured callback: ``on_metric(MetricEvent)``
        * legacy callback: ``on_metric(name, **tags)``
        """
        if isinstance(event, str):
            name = event
            value = legacy_tags.get("value", 1)
            tags = dict(legacy_tags)
            kind = MetricKind.COUNTER
        else:
            name = event.name
            value = event.value
            tags = dict(event.tags)
            kind = event.kind

        with self._lock:
            self._update_conn_from_metric_unlocked(name, value, tags)

            if kind is MetricKind.COUNTER:
                self._apply_counter_unlocked(name, value, tags)
            elif kind is MetricKind.GAUGE:
                self._apply_gauge_unlocked(name, value)
            elif kind is MetricKind.HISTOGRAM:
                self._apply_histogram_unlocked(name, value)

    def _apply_counter_unlocked(
        self,
        name: str,
        value: object,
        tags: dict[str, object],
    ) -> None:
        increment = _coerce_int(value, default=1)
        if name == "socks5.handshakes.error" and str(tags.get("reason")) == "timeout":
            canonical = "socks5.handshakes.timeout"
        else:
            canonical = _METRIC_TO_COUNTER.get(name, name)
        self._counters[canonical] = self._counters.get(canonical, 0) + increment

        if canonical == "bootstrap.ok":
            self._bootstrap_ok = True

        if canonical == "session.reconnect":
            self._reconnect_count += increment
            self._last_reconnect_at = time.monotonic()

    def _apply_gauge_unlocked(self, name: str, value: object) -> None:
        numeric = _coerce_float(value)
        if numeric is None:
            return
        if name in _GAUGE_KEYS:
            self._gauges[name] = numeric

    def _apply_histogram_unlocked(self, name: str, value: object) -> None:
        numeric = _coerce_float(value)
        if numeric is None:
            return
        if name not in _HISTOGRAM_KEYS:
            return
        hist = self._histograms.setdefault(name, HistogramStat())
        hist.observe(numeric)

    def _update_conn_from_metric_unlocked(
        self,
        name: str,
        value: object,
        tags: dict[str, object],
    ) -> None:
        if name not in _CONN_LIFECYCLE_METRICS:
            return

        conn_id = str(tags.get("conn_id", "")).strip()
        if not conn_id:
            return

        host = str(tags.get("host", "")).strip()
        port = _coerce_port(tags.get("port"))

        match name:
            case "tunnel.conn_open":
                self._get_or_create_connection_unlocked(
                    conn_id,
                    host=host,
                    port=port,
                    state="pending",
                )

            case "tunnel.conn_ack.ok":
                self._get_or_create_connection_unlocked(
                    conn_id,
                    host=host,
                    port=port,
                    state="open",
                )

            case "tunnel.conn_ack.failed" | "tunnel.conn_ack.timeout":
                self._get_or_create_connection_unlocked(
                    conn_id,
                    host=host,
                    port=port,
                    state="closed",
                )

            case "tunnel.conn.completed" | "tunnel.conn.error":
                conn = self._get_or_create_connection_unlocked(
                    conn_id,
                    host=host,
                    port=port,
                    state="closed",
                )
                conn.state = "closed"

            case "tcp.connection.upstream.bytes":
                conn = self._conn_index.get(conn_id)
                if conn is not None:
                    conn.bytes_up += _coerce_int(value, default=0)

            case "tcp.connection.downstream.bytes":
                conn = self._conn_index.get(conn_id)
                if conn is not None:
                    conn.bytes_down += _coerce_int(value, default=0)

    @staticmethod
    def _gauge_int(gauges: dict[str, float], key: str) -> int:
        return _coerce_int(gauges.get(key), default=0)

    @staticmethod
    def _hist_field(
        histograms: dict[str, HistogramStat],
        metric: str,
        field: str,
    ) -> float | None:
        hist = histograms.get(metric)
        if hist is None:
            return None

        match field:
            case "avg":
                return hist.avg
            case "max":
                return hist.max
            case "min":
                return hist.min
            case "count":
                return float(hist.count)
            case _:
                return None

    def snapshot(self) -> TunnelHealth:
        """Build a consistent point-in-time health snapshot."""
        now = time.monotonic()

        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            histograms = {
                name: replace(stat) for name, stat in self._histograms.items()
            }
            recent_conns = [replace(conn) for conn in self._recent_conns]
            connected = self._connected
            reconnect_count = self._reconnect_count
            last_reconnect_at = self._last_reconnect_at
            bootstrap_ok = self._bootstrap_ok

        ack_ok = counters.get("ack.ok", 0)
        ack_failed = counters.get("ack.failed", 0)
        ack_timeout = counters.get("ack.timeout", 0)

        tcp_open_from_conns = sum(1 for conn in recent_conns if conn.state == "open")
        tcp_pending_from_conns = sum(
            1 for conn in recent_conns if conn.state == "pending"
        )

        tcp_open = tcp_open_from_conns or self._gauge_int(
            gauges, "session.active.tcp_connections"
        )
        tcp_pending = tcp_pending_from_conns or self._gauge_int(
            gauges, "session.registry.pending_connects"
        )

        dns_drops = counters.get("dns.query.drop", 0) + counters.get(
            "dns.query.timeout", 0
        )
        cleanup_tcp = counters.get("session.cleanup.tcp", 0) + counters.get(
            "session.recv.cleanup.tcp", 0
        )
        cleanup_udp = counters.get("session.cleanup.udp", 0) + counters.get(
            "session.recv.cleanup.udp", 0
        )

        socks_listener_ok = (
            connected
            and bootstrap_ok
            and (
                self._gauge_int(gauges, "socks5.connections.active") > 0
                or counters.get("socks5.handshakes.ok", 0) > 0
                or counters.get("session.serve.started", 0) > 0
            )
        )

        pod_name = ""
        pod_namespace = ""
        pod_node = ""
        pod_ip = ""
        pod_phase = ""
        pod_conditions: list[dict[str, str]] = []

        if self._pod is not None:
            pod_name = self._pod.name
            pod_namespace = self._pod.namespace
            pod_node = self._pod.node or ""
            pod_ip = self._pod.ip or ""
            pod_phase = self._pod.phase or ""
            pod_conditions = _copy_pod_conditions(self._pod.conditions)

        send_queue_depth = self._gauge_int(
            gauges, "session.send.queue.data"
        ) + self._gauge_int(gauges, "session.send.queue.ctrl")

        return TunnelHealth(
            pod_name=pod_name,
            pod_namespace=pod_namespace,
            pod_node=pod_node,
            pod_ip=pod_ip,
            pod_phase=pod_phase,
            pod_conditions=pod_conditions,
            connected=connected,
            ws_url=self._ws_url,
            reconnect_count=reconnect_count,
            last_reconnect_at=last_reconnect_at,
            bootstrap_ok=bootstrap_ok,
            socks_host=self._socks_host,
            socks_port=self._socks_port,
            socks_ok=socks_listener_ok,
            frames_sent=counters.get("frames.sent", 0),
            frames_recv=counters.get("frames.recv", 0),
            frames_dropped=counters.get("frames.dropped", 0),
            bytes_up_total=counters.get("bytes.up", 0),
            bytes_down_total=counters.get("bytes.down", 0),
            frames_decode_errors=counters.get("frames.decode_error", 0),
            frames_orphaned=counters.get("frames.orphaned", 0),
            frames_noise=counters.get("frames.noise", 0),
            frames_outbound_timeout=counters.get("frames.outbound.timeout", 0),
            frames_outbound_ws_closed=counters.get("frames.outbound.ws_closed", 0),
            tcp_open=tcp_open,
            tcp_pending=tcp_pending,
            tcp_total=ack_ok + ack_failed + ack_timeout,
            tcp_failed=ack_failed + ack_timeout,
            tcp_upstream_started=counters.get("tcp.upstream.started", 0),
            tcp_downstream_started=counters.get("tcp.downstream.started", 0),
            tcp_completed=counters.get("tcp.completed", 0),
            tcp_errors=counters.get("tcp.error", 0),
            udp_open=self._gauge_int(gauges, "session.active.udp_flows"),
            udp_total=counters.get("udp.flow.opened", 0),
            udp_flows_opened=counters.get("udp.flow.opened", 0),
            udp_flows_closed=counters.get("udp.flow.closed", 0),
            udp_datagrams_sent=counters.get("udp.flow.datagram.submitted", 0),
            udp_datagrams_accepted=counters.get("udp.flow.datagram.accepted", 0),
            udp_datagrams_dropped=counters.get("udp.flow.datagram.dropped", 0),
            ack_ok=ack_ok,
            ack_timeout=ack_timeout,
            ack_failed=ack_failed,
            dns_enabled=counters.get("dns.started", 0) > 0,
            dns_queries=counters.get("dns.query.received", 0),
            dns_ok=counters.get("dns.query.ok", 0),
            dns_dropped=dns_drops,
            socks5_accepted=counters.get("socks5.accepted", 0),
            socks5_rejected=counters.get("socks5.rejected", 0),
            socks5_active=self._gauge_int(gauges, "socks5.connections.active"),
            socks5_handshakes_ok=counters.get("socks5.handshakes.ok", 0),
            socks5_handshakes_timeout=counters.get("socks5.handshakes.timeout", 0),
            socks5_handshakes_error=counters.get("socks5.handshakes.error", 0),
            socks5_cmd_connect=counters.get("socks5.cmd.connect", 0),
            socks5_cmd_udp=counters.get("socks5.cmd.udp", 0),
            socks5_udp_relays_active=self._gauge_int(
                gauges, "socks5.udp.relays_active"
            ),
            socks5_udp_datagrams=counters.get("socks5.udp.datagrams", 0),
            socks5_udp_dropped=counters.get("socks5.udp.dropped", 0),
            session_connect_attempts=counters.get("session.connect.attempt", 0),
            session_connect_ok=counters.get("session.connect.ok", 0),
            session_reconnects=reconnect_count,
            reconnect_delay_avg=self._hist_field(
                histograms, "session.reconnect.delay_sec", "avg"
            ),
            reconnect_delay_max=self._hist_field(
                histograms, "session.reconnect.delay_sec", "max"
            ),
            session_serve_started=counters.get("session.serve.started", 0),
            session_serve_stopped=counters.get("session.serve.stopped", 0),
            cleanup_tcp=cleanup_tcp,
            cleanup_pending=counters.get("session.cleanup.pending", 0),
            cleanup_udp=cleanup_udp,
            bootstrap_duration=self._hist_field(
                histograms, "bootstrap.duration_seconds", "avg"
            ),
            send_queue_depth=send_queue_depth,
            send_queue_cap=self._send_queue_cap,
            request_tasks=self._gauge_int(gauges, "session.request_tasks"),
            session_start=self._start,
            uptime_secs=now - self._start,
            recent_conns=recent_conns,
        )
