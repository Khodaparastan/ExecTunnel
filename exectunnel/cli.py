"""
Command-line interface.  Entry point: ``exectunnel.cli:main``.

Commands
--------
tunnel
    Run a local SOCKS5 proxy that tunnels connections through the pod via
    WebSocket exec.

Environment variables
---------------------
WSS_URL
    WebSocket endpoint (required).
WSS_INSECURE
    Set to ``1`` to skip TLS verification.
WSS_PING_INTERVAL
    WebSocket ping interval in seconds (default: 30).
WSS_PING_TIMEOUT
    WebSocket ping timeout in seconds (default: 10).
WSS_SEND_TIMEOUT
    Maximum time in seconds to wait for a WebSocket send (default: 30).
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import os
import sys

from exectunnel._version import __version__
from exectunnel.core.config import (
    TUNNEL_CONFIG,
    AppConfig,
    TunnelConfig,
    get_config,
    get_tunnel_config,
)
from exectunnel.core.consts import DEFAULT_EXCLUDE_CIDRS, METRICS_REPORT_INTERVAL_SECS
from exectunnel.exceptions import BootstrapError, ConfigError
from exectunnel.observability import (
    configure_logging,
    metrics_inc,
    run_metrics_reporter,
    span,
    start_trace,
)

logger = logging.getLogger("exectunnel.cli")


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid port {value!r}") from exc
    if not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError("port must be in range 1..65535")
    return port


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid number {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be > 0")
    return parsed


def _dns_ip(value: str) -> str:
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid DNS IP {value!r}") from exc
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="exectunnel",
        description="SOCKS5 proxy tunnel over WebSocket into Kubernetes pods.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  exectunnel tunnel
  exectunnel tunnel --dns 10.96.0.10
  exectunnel tunnel --socks-port 1080 --exclude 10.10.0.0/16

environment:
  WSS_URL            WebSocket endpoint (required)
  WSS_INSECURE       set to 1 to skip TLS verification
  WSS_PING_INTERVAL  ping interval in seconds (default: 30)
  WSS_PING_TIMEOUT   ping timeout in seconds (default: 10)
  WSS_SEND_TIMEOUT   send timeout in seconds (default: 30)
  WSS_SEND_QUEUE_CAP outbound frame queue capacity (default: 512)
  WSS_RECONNECT_MAX_RETRIES   reconnect attempts after disconnect (default: 5)
  WSS_RECONNECT_BASE_DELAY    reconnect backoff base seconds (default: 1.0)
  WSS_RECONNECT_MAX_DELAY     reconnect backoff max seconds (default: 30.0)
  EXECTUNNEL_LOG_ENGINE      stdlib|structlog (default: stdlib)
  EXECTUNNEL_LOG_FORMAT      console|json (default: console)
  EXECTUNNEL_LOG_COLOR       auto|always|never (default: auto)
  EXECTUNNEL_METRICS_REPORT_INTERVAL  seconds; 0 disables reporter (default: 60)
  EXECTUNNEL_METRICS_LOG_LEVEL        debug|info (default: debug)
  EXECTUNNEL_METRICS_VERBOSE          1 to include full metrics snapshot at DEBUG
  EXECTUNNEL_ACK_TIMEOUT_RECONNECT_THRESHOLD  timeout count before forced reconnect (default: 3)
  EXECTUNNEL_ACK_TIMEOUT_WINDOW_SECS          rolling window for timeout threshold (default: 20)
  EXECTUNNEL_ACK_TIMEOUT_WARN_EVERY           warning cadence for ACK failures (default: 10)
  EXECTUNNEL_OBS_EXPORTERS            comma list: log,file,http (default: log)
  EXECTUNNEL_OBS_PLATFORM             generic|datadog|splunk|newrelic (default: generic)
  EXECTUNNEL_OBS_SERVICE              service name in exported payloads (default: exectunnel)
  EXECTUNNEL_OBS_FILE_PATH            jsonl path for file exporter
  EXECTUNNEL_OBS_HTTP_URL             endpoint for http exporter
  EXECTUNNEL_OBS_HTTP_TIMEOUT         http exporter timeout seconds (default: 5)
  EXECTUNNEL_OBS_HTTP_HEADERS         semicolon header list, e.g. Authorization=Bearer token
        """,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"exectunnel {__version__}",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default="info",
        metavar="LEVEL",
        help="logging verbosity: debug | info | warning | error (default: info)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # ── tunnel ────────────────────────────────────────────────────────────────
    tun = sub.add_parser(
        "tunnel",
        help="SOCKS5 proxy tunnel through the pod",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Run a local SOCKS5 proxy that tunnels connections through the pod.\n"
            "Set ALL_PROXY=socks5://127.0.0.1:1080 to route your shell traffic.\n\n"
            "RFC1918 + loopback CIDRs are bypassed by default:\n"
            + "\n".join(f"  {c}" for c in DEFAULT_EXCLUDE_CIDRS)
        ),
    )
    tun.add_argument(
        "--socks-port",
        type=_port,
        default=TUNNEL_CONFIG.socks_port,
        metavar="PORT",
        help=f"local SOCKS5 port (default: {TUNNEL_CONFIG.socks_port})",
    )
    tun.add_argument(
        "--socks-host",
        default=TUNNEL_CONFIG.socks_host,
        metavar="HOST",
        help=f"local SOCKS5 bind address (default: {TUNNEL_CONFIG.socks_host})",
    )
    tun.add_argument(
        "--dns",
        type=_dns_ip,
        metavar="IP",
        help="upstream DNS IP to forward queries to through the tunnel",
    )
    tun.add_argument(
        "--dns-port",
        type=_port,
        default=TUNNEL_CONFIG.dns_local_port,
        metavar="PORT",
        help=f"local DNS forwarder port (default: {TUNNEL_CONFIG.dns_local_port})",
    )
    tun.add_argument(
        "--exclude",
        metavar="CIDR",
        action="append",
        default=[],
        help="extra CIDR to bypass tunnel (repeatable); e.g. 10.10.0.0/16",
    )
    tun.add_argument(
        "--no-default-exclude",
        action="store_true",
        help="clear the default RFC1918 exclusions",
    )
    tun.add_argument(
        "--ready-timeout",
        type=_positive_float,
        default=TUNNEL_CONFIG.ready_timeout,
        metavar="SECS",
        help=f"seconds to wait for agent READY signal (default: {TUNNEL_CONFIG.ready_timeout:.0f})",
    )
    tun.add_argument(
        "--conn-ack-timeout",
        type=_positive_float,
        default=TUNNEL_CONFIG.conn_ack_timeout,
        metavar="SECS",
        help=f"seconds to wait for agent CONN_ACK per connection (default: {TUNNEL_CONFIG.conn_ack_timeout:.0f})",
    )

    return parser


def _parse_tunnel_excludes(
    args: argparse.Namespace,
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    if not args.no_default_exclude:
        from exectunnel.core.consts import default_exclusion_networks

        nets.extend(default_exclusion_networks())
    for cidr in args.exclude:
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError as exc:
            raise SystemExit(f"invalid CIDR {cidr!r}: {exc}") from exc
    return nets


async def cmd_tunnel(cfg: AppConfig, tun_cfg: TunnelConfig) -> None:
    from exectunnel.tunnel import TunnelSession

    trace_seed = os.getenv("EXECTUNNEL_TRACE_ID")
    with start_trace(trace_seed), span("cli.cmd_tunnel"):
        metrics_inc("cli.commands.started", command="tunnel")
        logger.info(
            "starting tunnel socks=%s:%d dns=%s excludes=%d",
            tun_cfg.socks_host,
            tun_cfg.socks_port,
            f"{tun_cfg.dns_upstream}:{tun_cfg.dns_local_port}"
            if tun_cfg.dns_upstream
            else "disabled",
            len(tun_cfg.exclude),
        )
        session = TunnelSession(cfg, tun_cfg)
        metrics_interval_raw = os.getenv("EXECTUNNEL_METRICS_REPORT_INTERVAL", str(METRICS_REPORT_INTERVAL_SECS))
        try:
            metrics_interval = float(metrics_interval_raw)
        except ValueError:
            logger.warning(
                "invalid EXECTUNNEL_METRICS_REPORT_INTERVAL=%r; using %.0f",
                metrics_interval_raw,
                METRICS_REPORT_INTERVAL_SECS,
            )
            metrics_interval = METRICS_REPORT_INTERVAL_SECS
        if metrics_interval < 0:
            logger.warning(
                "negative EXECTUNNEL_METRICS_REPORT_INTERVAL=%s; using 0 (disabled)",
                metrics_interval_raw,
            )
            metrics_interval = 0.0
        stop_event = asyncio.Event()
        reporter_task: asyncio.Task[None] | None = None
        if metrics_interval > 0:
            reporter_task = asyncio.create_task(
                run_metrics_reporter(metrics_interval, stop_event),
                name="metrics-reporter",
            )
        try:
            await session.run()
            metrics_inc("cli.commands.ok", command="tunnel")
        except (BootstrapError, RuntimeError) as exc:
            metrics_inc(
                "cli.commands.error", command="tunnel", error=exc.__class__.__name__
            )
            logger.error("tunnel failed: %s", exc)
            logger.debug("tunnel failure traceback", exc_info=True)
            sys.exit(1)
        except KeyboardInterrupt:
            metrics_inc("cli.commands.interrupted", command="tunnel")
            logger.info("interrupted")
        finally:
            stop_event.set()
            if reporter_task is not None:
                await reporter_task


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Configure logging before anything else so early errors are visible.
    configure_logging(args.log_level)

    try:
        cfg = get_config()
    except ConfigError as exc:
        parser.error(str(exc))

    if args.command == "tunnel":
        try:
            tun_cfg = get_tunnel_config(
                cfg,
                socks_host=args.socks_host,
                socks_port=args.socks_port,
                dns_upstream=args.dns,
                dns_local_port=args.dns_port,
                ready_timeout=args.ready_timeout,
                conn_ack_timeout=args.conn_ack_timeout,
                exclude=_parse_tunnel_excludes(args),
            )
        except ValueError as exc:
            parser.error(str(exc))
        try:
            asyncio.run(cmd_tunnel(cfg, tun_cfg))
        except KeyboardInterrupt:
            logger.info("interrupted")


if __name__ == "__main__":
    main()
