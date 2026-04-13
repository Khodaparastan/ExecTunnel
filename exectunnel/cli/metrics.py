"""Health monitor — collects and exposes live tunnel and pod statistics."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from exectunnel.observability import metrics_snapshot

__all__ = ["ConnectionStat", "HealthMonitor", "PodSpec", "TunnelHealth"]

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
    state: str  # "pending" | "open" | "closing" | "closed"
    bytes_up: int = 0
    bytes_down: int = 0
    # drop_count: incremented externally when the tunnel drops frames
    # for this specific connection (reserved — not yet wired).
    drop_count: int = 0
    opened_at: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class TunnelHealth:
    """Point-in-time snapshot of tunnel health.

    Created by :meth:`HealthMonitor.snapshot` — treat as read-only.
    All list fields are shallow copies safe to iterate without locks.
    """

    # Connection
    connected: bool = False
    ws_url: str = ""
    reconnect_count: int = 0
    last_reconnect_at: float = 0.0

    # Bootstrap
    bootstrap_ok: bool = False
    bootstrap_elapsed: float = 0.0
    agent_version: str = "unknown"

    # SOCKS5 listener
    socks_host: str = "127.0.0.1"
    socks_port: int = 1080
    socks_ok: bool = False

    # Frame counters
    frames_sent: int = 0
    frames_recv: int = 0
    frames_dropped: int = 0
    bytes_up_total: int = 0
    bytes_down_total: int = 0

    # TCP connections
    tcp_open: int = 0
    tcp_pending: int = 0
    tcp_total: int = 0
    tcp_failed: int = 0

    # UDP flows
    udp_open: int = 0
    udp_total: int = 0

    # ACK health
    ack_ok: int = 0
    ack_timeout: int = 0
    ack_failed: int = 0

    # DNS
    dns_enabled: bool = False
    dns_queries: int = 0
    dns_ok: int = 0
    dns_dropped: int = 0

    # Send queue
    send_queue_depth: int = 0
    send_queue_cap: int = 0

    # Uptime
    session_start: float = field(default_factory=time.monotonic)
    uptime_secs: float = 0.0

    # Pod metadata
    pod_name: str = ""
    pod_namespace: str = ""
    pod_node: str = ""
    pod_ip: str = ""
    pod_phase: str = ""
    pod_conditions: list[dict[str, str]] = field(default_factory=list)

    # SOCKS5 proxy detail
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

    # Session connect / reconnect
    session_connect_attempts: int = 0
    session_connect_ok: int = 0
    session_reconnects: int = 0
    reconnect_delay_avg: float | None = None
    reconnect_delay_max: float | None = None

    # Session serve
    session_serve_started: int = 0
    session_serve_stopped: int = 0

    # Session cleanup (on reconnect)
    cleanup_tcp: int = 0
    cleanup_pending: int = 0
    cleanup_udp: int = 0

    # Frame errors
    frames_decode_errors: int = 0
    frames_orphaned: int = 0
    frames_noise: int = 0
    frames_outbound_timeout: int = 0
    frames_outbound_ws_closed: int = 0

    # TCP connection detail
    tcp_upstream_started: int = 0
    tcp_downstream_started: int = 0
    tcp_completed: int = 0
    tcp_errors: int = 0

    # UDP flow detail
    udp_flows_opened: int = 0
    udp_flows_closed: int = 0
    udp_datagrams_sent: int = 0
    udp_datagrams_accepted: int = 0
    udp_datagrams_dropped: int = 0

    # Bootstrap detail
    bootstrap_duration: float | None = None

    # Request tasks gauge
    request_tasks: int = 0

    # Recent connections (ring-buffer copy)
    recent_conns: list[ConnectionStat] = field(default_factory=list)

    def to_report_dict(self) -> dict[str, object]:
        """Return a flat dict of key metrics suitable for JSON serialisation."""
        return {
            "connected": self.connected,
            "bootstrap_ok": self.bootstrap_ok,
            "socks_ok": self.socks_ok,
            "uptime_secs": round(self.uptime_secs, 1),
            # Frames
            "frames_sent": self.frames_sent,
            "frames_recv": self.frames_recv,
            "frames_dropped": self.frames_dropped,
            "bytes_up": self.bytes_up_total,
            "bytes_down": self.bytes_down_total,
            # Frame errors
            "frames_decode_errors": self.frames_decode_errors,
            "frames_orphaned": self.frames_orphaned,
            "frames_noise": self.frames_noise,
            # TCP
            "tcp_open": self.tcp_open,
            "tcp_pending": self.tcp_pending,
            "tcp_total": self.tcp_total,
            "tcp_failed": self.tcp_failed,
            "tcp_completed": self.tcp_completed,
            "tcp_errors": self.tcp_errors,
            # UDP
            "udp_open": self.udp_open,
            "udp_total": self.udp_total,
            "udp_flows_opened": self.udp_flows_opened,
            "udp_flows_closed": self.udp_flows_closed,
            "udp_datagrams_sent": self.udp_datagrams_sent,
            "udp_datagrams_accepted": self.udp_datagrams_accepted,
            "udp_datagrams_dropped": self.udp_datagrams_dropped,
            # ACK
            "ack_ok": self.ack_ok,
            "ack_timeout": self.ack_timeout,
            "ack_failed": self.ack_failed,
            # DNS
            "dns_enabled": self.dns_enabled,
            "dns_queries": self.dns_queries,
            "dns_ok": self.dns_ok,
            "dns_dropped": self.dns_dropped,
            # SOCKS5
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
            # Session
            "session_connect_attempts": self.session_connect_attempts,
            "session_connect_ok": self.session_connect_ok,
            "session_reconnects": self.session_reconnects,
            "reconnect_delay_avg": self.reconnect_delay_avg,
            "reconnect_delay_max": self.reconnect_delay_max,
            "session_serve_started": self.session_serve_started,
            "session_serve_stopped": self.session_serve_stopped,
            # Cleanup
            "cleanup_tcp": self.cleanup_tcp,
            "cleanup_pending": self.cleanup_pending,
            "cleanup_udp": self.cleanup_udp,
            # Bootstrap
            "bootstrap_duration": self.bootstrap_duration,
            # Queue & tasks
            "send_queue_depth": self.send_queue_depth,
            "send_queue_cap": self.send_queue_cap,
            "request_tasks": self.request_tasks,
        }


# ── Metric name mapping ──────────────────────────────────────────────────────

_METRIC_TO_COUNTER: dict[str, str] = {
    # Frames (outbound)
    "session.frames.outbound": "frames.sent",
    "session.keepalive.sent": "frames.sent",
    # Frames (inbound)
    "session.frames.inbound": "frames.recv",
    # Drops
    "session.frames.outbound.drop": "frames.dropped",
    # Frame errors
    "session.frames.decode_error": "frames.decode_error",
    "session.frames.payload_decode_error": "frames.decode_error",
    "session.frames.orphaned": "frames.orphaned",
    "session.frames.noise": "frames.noise",
    "session.frames.outbound.timeout": "frames.outbound.timeout",
    "session.frames.outbound.ws_closed": "frames.outbound.ws_closed",
    "session.frames.no_conn_id": "frames.orphaned",
    # TCP ACK
    "tunnel.conn_ack.ok": "ack.ok",
    "tunnel.conn_ack.failed": "ack.failed",
    "tunnel.conn_ack.timeout": "ack.timeout",
    # TCP connections
    "tunnel.conn.completed": "tcp.completed",
    "tunnel.conn.error": "tcp.error",
    "tcp.connection.upstream.started": "tcp.upstream.started",
    "tcp.connection.downstream.started": "tcp.downstream.started",
    "tcp.connection.cleanup": "tcp.cleanup",
    # UDP flows
    "udp.flow.opened": "udp.flow.opened",
    "udp.flow.closed": "udp.flow.closed",
    "udp.flow.datagram.sent": "udp.flow.datagram.sent",
    "udp.flow.datagram.accepted": "udp.flow.datagram.accepted",
    "udp.flow.inbound_queue.drop": "udp.flow.datagram.dropped",
    "udp.flow.feed_after_close.drop": "udp.flow.datagram.dropped",
    # DNS
    "dns.forwarder.started": "dns.started",
    "dns.query.received": "dns.query.received",
    "dns.query.ok": "dns.query.ok",
    "dns.query.timeout": "dns.query.timeout",
    "dns.query.drop": "dns.query.drop",
    "dns.query.error": "dns.query.error",
    # Bootstrap
    "bootstrap.ok": "bootstrap.ok",
    "bootstrap.started": "bootstrap.started",
    # SOCKS5
    "socks5.connections.accepted": "socks5.accepted",
    "socks5.connections.rejected": "socks5.rejected",
    "socks5.handshakes.ok": "socks5.handshakes.ok",
    "socks5.handshakes.timeout": "socks5.handshakes.timeout",
    "socks5.handshakes.error": "socks5.handshakes.error",
    "socks5.commands.connect": "socks5.cmd.connect",
    "socks5.commands.udp_associate": "socks5.cmd.udp",
    "socks5.udp.datagrams_accepted": "socks5.udp.datagrams",
    "socks5.udp.datagrams_dropped": "socks5.udp.dropped",
    # Session lifecycle
    "session.connect.attempt": "session.connect.attempt",
    "session.connect.ok": "session.connect.ok",
    "session.reconnect": "session.reconnect",
    "session.serve.started": "session.serve.started",
    "session.serve.stopped": "session.serve.stopped",
    # Session cleanup
    "session.cleanup.tcp": "session.cleanup.tcp",
    "session.cleanup.pending": "session.cleanup.pending",
    "session.cleanup.udp": "session.cleanup.udp",
    "session.frames.outbound.bytes": "bytes.up",
    "session.frames.bytes.in": "bytes.down",
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

_GAUGE_KEYS: tuple[str, ...] = (
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
)

_HISTOGRAM_KEYS: tuple[str, ...] = (
    "session.reconnect.delay_sec",
    "bootstrap.duration_seconds",
    "socks5.handshake_duration_sec",
    "tcp.connection.upstream.duration_sec",
    "tcp.connection.downstream.duration_sec",
)

_BYTE_COUNTER_KEYS: tuple[str, ...] = (
    "session.frames.outbound.bytes",
    "session.frames.bytes.in",
)


class HealthMonitor:
    """Collects live health statistics from the running session.

    Wire to the observability layer via :meth:`on_metric` registered with
    ``register_metric_listener(monitor.on_metric)``.

    The monitor accumulates event counters internally.  :meth:`snapshot`
    builds a consistent point-in-time :class:`TunnelHealth` by merging those
    counters with a **single** fresh call to ``metrics_snapshot()`` for both
    gauge values and byte-counter totals.

    Thread / task safety
    --------------------
    ``on_metric`` may be called from any context that the observability layer
    chooses.  The counter dict and deque mutations are GIL-protected on CPython
    and therefore safe.  For other runtimes, add an ``asyncio.Lock`` guard
    around ``_counters`` and ``_recent_conns``.
    """

    __slots__ = (
        "_pod",
        "_ws_url",
        "_socks_host",
        "_socks_port",
        "_send_queue_cap",
        "_start",
        "_reconnect_count",
        "_last_reconnect_at",
        "_bootstrap_ok",
        "_counters",
        "_recent_conns",
        "_conn_index",
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
        self._send_queue_cap = send_queue_cap
        self._start = time.monotonic()
        self._reconnect_count: int = 0
        self._last_reconnect_at: float = 0.0
        self._bootstrap_ok: bool = False
        self._counters: dict[str, int] = {}
        self._recent_conns: deque[ConnectionStat] = deque(maxlen=_MAX_RECENT_CONNS)
        self._conn_index: dict[str, ConnectionStat] = {}

    # ── Mutation ──────────────────────────────────────────────────────────

    def record_reconnect(self) -> None:
        self._reconnect_count += 1
        self._last_reconnect_at = time.monotonic()

    def record_bootstrap_ok(self) -> None:
        self._bootstrap_ok = True

    def record_connection(self, stat: ConnectionStat) -> None:
        if len(self._recent_conns) == _MAX_RECENT_CONNS:
            evicted = self._recent_conns[0]
            self._conn_index.pop(evicted.conn_id, None)
        self._recent_conns.append(stat)
        self._conn_index[stat.conn_id] = stat

    def update_connection(
        self,
        conn_id: str,
        *,
        state: str | None = None,
        bytes_up: int | None = None,
        bytes_down: int | None = None,
    ) -> None:
        conn = self._conn_index.get(conn_id)
        if conn is None:
            return
        if state is not None:
            conn.state = state
        if bytes_up is not None:
            conn.bytes_up += bytes_up
        if bytes_down is not None:
            conn.bytes_down += bytes_down

    def on_metric(self, name: str, **tags: object) -> None:
        """Observability listener callback.

        Registered with ``exectunnel.observability.register_metric_listener``.

        Each invocation increments the internal counter for *name* by 1.

        .. note::
            Byte-counting metrics (``session.frames.outbound.bytes``,
            ``session.frames.bytes.in``) carry their values in the metrics
            registry, **not** through this callback.  Byte totals are read
            directly from ``metrics_snapshot()`` inside :meth:`snapshot` to
            ensure accurate cumulative values.

        .. note::
            If the observability layer passes ``value=N`` as a tag, it will
            appear in *tags* but is intentionally ignored here.  Byte values
            must be read from the registry snapshot.
        """
        canonical = _METRIC_TO_COUNTER.get(name, name)
        self._counters[canonical] = self._counters.get(canonical, 0) + 1

        if canonical == "bootstrap.ok":
            self._bootstrap_ok = True

        if canonical == "session.reconnect":
            self.record_reconnect()

        self._update_conn_from_metric(name, tags)

    def _update_conn_from_metric(
        self,
        name: str,
        tags: dict[str, object],
    ) -> None:
        """Update connection tracking tables from lifecycle metric events."""
        if name not in _CONN_LIFECYCLE_METRICS:
            return

        conn_id = str(tags.get("conn_id", ""))
        if not conn_id:
            return

        host = str(tags.get("host", ""))
        port = int(tags.get("port", 0)) if tags.get("port") else 0

        match name:
            case "tunnel.conn_open":
                if conn_id not in self._conn_index:
                    self.record_connection(
                        ConnectionStat(
                            conn_id=conn_id,
                            host=host,
                            port=port,
                            state="pending",
                        )
                    )

            case "tunnel.conn_ack.ok":
                existing = self._conn_index.get(conn_id)
                if existing is not None:
                    existing.state = "open"
                else:
                    self.record_connection(
                        ConnectionStat(
                            conn_id=conn_id,
                            host=host,
                            port=port,
                            state="open",
                        )
                    )

            case "tunnel.conn_ack.failed" | "tunnel.conn_ack.timeout":
                existing = self._conn_index.get(conn_id)
                if existing is not None:
                    existing.state = "closed"
                else:
                    self.record_connection(
                        ConnectionStat(
                            conn_id=conn_id,
                            host=host,
                            port=port,
                            state="closed",
                        )
                    )

            case "tunnel.conn.completed" | "tunnel.conn.error":
                self.update_connection(conn_id, state="closed")

            case "tcp.connection.upstream.bytes":
                val = int(tags.get("bytes", 0)) if tags.get("bytes") else 0
                if val:
                    self.update_connection(conn_id, bytes_up=val)

            case "tcp.connection.downstream.bytes":
                val = int(tags.get("bytes", 0)) if tags.get("bytes") else 0
                if val:
                    self.update_connection(conn_id, bytes_down=val)

    # ── Read ──────────────────────────────────────────────────────────────

    def _counter(self, key: str) -> int:
        return self._counters.get(key, 0)

    @staticmethod
    def _hist_field(snap: dict[str, object], metric: str, field: str) -> float | None:
        val = snap.get(f"{metric}.{field}")
        if val is None:
            return None
        return float(val)

    def snapshot(self) -> TunnelHealth:
        """Build a consistent point-in-time health snapshot.

        ``metrics_snapshot()`` is called **once** and the result is shared
        between gauge extraction and byte-counter reads to ensure a coherent
        view and avoid double I/O on the metrics registry.
        """
        now = time.monotonic()
        snap = metrics_snapshot()
        gauges: dict[str, float] = {k: float(snap.get(k, 0)) for k in _GAUGE_KEYS}

        bytes_up = int(snap.get("session.frames.outbound.bytes", 0))
        bytes_down = int(snap.get("session.frames.bytes.in", 0))

        ack_ok = self._counter("ack.ok")
        ack_failed = self._counter("ack.failed")
        ack_timeout = self._counter("ack.timeout")

        tcp_open = sum(1 for c in self._recent_conns if c.state == "open")
        tcp_pending = sum(1 for c in self._recent_conns if c.state == "pending")

        dns_drops = self._counter("dns.query.drop") + self._counter("dns.query.timeout")

        h = TunnelHealth(
            connected=True,
            ws_url=self._ws_url,
            reconnect_count=self._reconnect_count,
            last_reconnect_at=self._last_reconnect_at,
            bootstrap_ok=self._bootstrap_ok,
            socks_host=self._socks_host,
            socks_port=self._socks_port,
            socks_ok=self._bootstrap_ok,
            # Frames
            frames_sent=self._counter("frames.sent"),
            frames_recv=self._counter("frames.recv"),
            frames_dropped=self._counter("frames.dropped"),
            bytes_up_total=bytes_up,
            bytes_down_total=bytes_down,
            # Frame errors
            frames_decode_errors=self._counter("frames.decode_error"),
            frames_orphaned=self._counter("frames.orphaned"),
            frames_noise=self._counter("frames.noise"),
            frames_outbound_timeout=self._counter("frames.outbound.timeout"),
            frames_outbound_ws_closed=self._counter("frames.outbound.ws_closed"),
            # TCP — prefer ring-buffer counts; fall back to live gauge
            tcp_open=tcp_open or int(gauges.get("session.active.tcp_connections", 0)),
            tcp_pending=tcp_pending
            or int(gauges.get("session.registry.pending_connects", 0)),
            tcp_total=ack_ok + ack_failed + ack_timeout,
            tcp_failed=ack_failed,
            # TCP detail
            tcp_upstream_started=self._counter("tcp.upstream.started"),
            tcp_downstream_started=self._counter("tcp.downstream.started"),
            tcp_completed=self._counter("tcp.completed"),
            tcp_errors=self._counter("tcp.error"),
            # UDP — from gauge
            udp_open=int(gauges.get("session.active.udp_flows", 0)),
            udp_total=int(gauges.get("session.registry.udp", 0)),
            # UDP detail
            udp_flows_opened=self._counter("udp.flow.opened"),
            udp_flows_closed=self._counter("udp.flow.closed"),
            udp_datagrams_sent=self._counter("udp.flow.datagram.sent"),
            udp_datagrams_accepted=self._counter("udp.flow.datagram.accepted"),
            udp_datagrams_dropped=self._counter("udp.flow.datagram.dropped"),
            # ACK
            ack_ok=ack_ok,
            ack_timeout=ack_timeout,
            ack_failed=ack_failed,
            # DNS
            dns_enabled=self._counter("dns.started") > 0,
            dns_queries=self._counter("dns.query.received"),
            dns_ok=self._counter("dns.query.ok"),
            dns_dropped=dns_drops,
            # SOCKS5
            socks5_accepted=self._counter("socks5.accepted"),
            socks5_rejected=self._counter("socks5.rejected"),
            socks5_active=int(gauges.get("socks5.connections.active", 0)),
            socks5_handshakes_ok=self._counter("socks5.handshakes.ok"),
            socks5_handshakes_timeout=self._counter("socks5.handshakes.timeout"),
            socks5_handshakes_error=self._counter("socks5.handshakes.error"),
            socks5_cmd_connect=self._counter("socks5.cmd.connect"),
            socks5_cmd_udp=self._counter("socks5.cmd.udp"),
            socks5_udp_relays_active=int(gauges.get("socks5.udp.relays_active", 0)),
            socks5_udp_datagrams=self._counter("socks5.udp.datagrams"),
            socks5_udp_dropped=self._counter("socks5.udp.dropped"),
            # Session connect / reconnect
            session_connect_attempts=self._counter("session.connect.attempt"),
            session_connect_ok=self._counter("session.connect.ok"),
            session_reconnects=self._counter("session.reconnect"),
            reconnect_delay_avg=self._hist_field(
                snap, "session.reconnect.delay_sec", "avg"
            ),
            reconnect_delay_max=self._hist_field(
                snap, "session.reconnect.delay_sec", "max"
            ),
            # Session serve
            session_serve_started=self._counter("session.serve.started"),
            session_serve_stopped=self._counter("session.serve.stopped"),
            # Session cleanup
            cleanup_tcp=self._counter("session.cleanup.tcp"),
            cleanup_pending=self._counter("session.cleanup.pending"),
            cleanup_udp=self._counter("session.cleanup.udp"),
            # Bootstrap detail
            bootstrap_duration=self._hist_field(
                snap, "bootstrap.duration_seconds", "sum"
            ),
            # Send queue
            send_queue_depth=int(gauges.get("session.send.queue.data", 0)),
            send_queue_cap=self._send_queue_cap,
            # Request tasks
            request_tasks=int(gauges.get("session.request_tasks", 0)),
            # Uptime
            session_start=self._start,
            uptime_secs=now - self._start,
            # Recent connections — shallow copy of deque
            recent_conns=list(self._recent_conns),
        )

        if self._pod is not None:
            h.pod_name = self._pod.name
            h.pod_namespace = self._pod.namespace
            h.pod_node = self._pod.node or ""
            h.pod_ip = self._pod.ip or ""
            h.pod_phase = self._pod.phase or ""
            h.pod_conditions = self._pod.conditions

        return h
