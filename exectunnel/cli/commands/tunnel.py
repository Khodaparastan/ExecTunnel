"""exectunnel tunnel — SOCKS5 proxy tunnel through a Kubernetes pod."""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from exectunnel import __version__
from exectunnel.defaults import Defaults
from exectunnel.exceptions import ConfigurationError
from exectunnel.observability.logging import configure_logging
from exectunnel.session import (
    SessionConfig,
    TunnelConfig,
    get_default_exclusion_networks,
)

from .._config import build_session_config, build_tunnel_config
from ..runner import run_session
from ..ui import BANNER, THEME, BootstrapSpinner, Icons

__all__ = ["tunnel"]

console = Console(theme=THEME, highlight=False)
_VALID_LOG_LEVELS = frozenset({"debug", "info", "warning", "error"})


def _normalize_log_level(value: str) -> str:
    level = value.lower().strip()
    if level == "warn":
        level = "warning"
    return level


# ── Argument validators ──────────────────────────────────────────────────────


def _dns_upstream_callback(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if not value or any(c in value for c in " \t\n;|&`$"):
        raise typer.BadParameter(
            f"Invalid DNS upstream {value!r}: must be an IP address or hostname"
        )
    return value


def _parse_excludes(
    extra: list[str] | None,
    no_default: bool,
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    if not no_default:
        nets.extend(get_default_exclusion_networks())
    for cidr in extra or []:
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError as exc:
            raise typer.BadParameter(f"Invalid CIDR {cidr!r}: {exc}") from exc
    return nets


# ── Command ──────────────────────────────────────────────────────────────────


def tunnel(
    # Core
    socks_port: Annotated[
        int,
        typer.Option("--socks-port", "-p", help="Local SOCKS5 port.", min=1, max=65535),
    ] = 1080,
    socks_host: Annotated[
        str,
        typer.Option("--socks-host", help="Local SOCKS5 bind address."),
    ] = "127.0.0.1",
    # DNS
    dns: Annotated[
        str | None,
        typer.Option(
            "--dns", help="Upstream DNS server to forward through the tunnel."
        ),
    ] = None,
    dns_port: Annotated[
        int,
        typer.Option(
            "--dns-port", help="Local DNS forwarder listen port.", min=1, max=65535
        ),
    ] = Defaults.DNS_LOCAL_PORT,
    dns_upstream_port: Annotated[
        int,
        typer.Option(
            "--dns-upstream-port", help="Upstream DNS server port.", min=1, max=65535
        ),
    ] = Defaults.DNS_UPSTREAM_PORT,
    dns_query_timeout: Annotated[
        float,
        typer.Option("--dns-query-timeout", help="DNS query timeout (seconds)."),
    ] = Defaults.DNS_QUERY_TIMEOUT_SECS,
    # Excludes
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Extra CIDR to bypass the tunnel (repeatable)."),
    ] = None,
    no_default_exclude: Annotated[
        bool,
        typer.Option(
            "--no-default-exclude", help="Clear default RFC1918 + loopback exclusions."
        ),
    ] = False,
    # Bootstrap
    bootstrap_delivery: Annotated[
        str,
        typer.Option(
            "--bootstrap-delivery", help="Agent delivery mode: upload | fetch."
        ),
    ] = "upload",
    fetch_agent_url: Annotated[
        str,
        typer.Option("--fetch-agent-url", help="Raw URL for fetch delivery mode."),
    ] = Defaults.BOOTSTRAP_FETCH_AGENT_URL,
    bootstrap_skip_if_present: Annotated[
        bool,
        typer.Option(
            "--bootstrap-skip-if-present/--no-bootstrap-skip-if-present",
            help="Reuse existing agent on pod vs always re-deliver.",
        ),
    ] = False,
    bootstrap_syntax_check: Annotated[
        bool,
        typer.Option(
            "--bootstrap-syntax-check/--no-bootstrap-syntax-check",
            help="Run py_compile on the agent before exec.",
        ),
    ] = True,
    bootstrap_agent_path: Annotated[
        str,
        typer.Option(
            "--bootstrap-agent-path", help="Agent script path inside the pod."
        ),
    ] = Defaults.BOOTSTRAP_AGENT_PATH,
    bootstrap_syntax_ok_sentinel: Annotated[
        str,
        typer.Option("--bootstrap-syntax-ok-sentinel", help="Syntax-OK sentinel path."),
    ] = Defaults.BOOTSTRAP_SYNTAX_OK_SENTINEL,
    bootstrap_use_go_agent: Annotated[
        bool,
        typer.Option("--bootstrap-use-go-agent", help="Use pre-built Go agent binary."),
    ] = False,
    bootstrap_go_agent_path: Annotated[
        str,
        typer.Option("--bootstrap-go-agent-path", help="Go agent binary path on pod."),
    ] = Defaults.BOOTSTRAP_GO_AGENT_PATH,
    # Timeouts & tunables
    ready_timeout: Annotated[
        float,
        typer.Option("--ready-timeout", help="Seconds to wait for AGENT_READY."),
    ] = Defaults.READY_TIMEOUT_SECS,
    conn_ack_timeout: Annotated[
        float,
        typer.Option("--conn-ack-timeout", help="Seconds to wait for CONN_ACK."),
    ] = Defaults.CONN_ACK_TIMEOUT_SECS,
    connect_pace_interval: Annotated[
        float,
        typer.Option(
            "--connect-pace-interval",
            help="Min seconds between CONN_OPENs; 0 disables.",
        ),
    ] = Defaults.CONNECT_PACE_INTERVAL_SECS,
    # SOCKS tunables
    socks_handshake_timeout: Annotated[
        float,
        typer.Option(
            "--socks-handshake-timeout", help="Max seconds for SOCKS5 handshake."
        ),
    ] = Defaults.HANDSHAKE_TIMEOUT_SECS,
    socks_request_queue_cap: Annotated[
        int,
        typer.Option(
            "--socks-request-queue-cap", help="Completed-handshake queue capacity."
        ),
    ] = Defaults.SOCKS_REQUEST_QUEUE_CAP,
    socks_queue_put_timeout: Annotated[
        float,
        typer.Option(
            "--socks-queue-put-timeout", help="Max seconds to enqueue a handshake."
        ),
    ] = Defaults.SOCKS_QUEUE_PUT_TIMEOUT_SECS,
    # UDP tunables
    udp_relay_queue_cap: Annotated[
        int,
        typer.Option(
            "--udp-relay-queue-cap", help="Inbound datagram queue per UDP session."
        ),
    ] = Defaults.UDP_RELAY_QUEUE_CAP,
    udp_drop_warn_every: Annotated[
        int,
        typer.Option(
            "--udp-drop-warn-every", help="Warn every N UDP queue-full drops."
        ),
    ] = Defaults.UDP_WARN_EVERY,
    udp_pump_poll_timeout: Annotated[
        float,
        typer.Option(
            "--udp-pump-poll-timeout", help="UDP pump loop poll interval (seconds)."
        ),
    ] = Defaults.UDP_PUMP_POLL_TIMEOUT_SECS,
    udp_direct_recv_timeout: Annotated[
        float,
        typer.Option(
            "--udp-direct-recv-timeout", help="Recv timeout for direct UDP datagrams."
        ),
    ] = Defaults.UDP_DIRECT_RECV_TIMEOUT_SECS,
    # UI
    no_dashboard: Annotated[
        bool,
        typer.Option("--no-dashboard", help="Disable the live dashboard."),
    ] = False,
    log_level: Annotated[
        str,
        typer.Option(
            "--log-level",
            "-l",
            help="Logging verbosity: debug | info | warning | error.",
            envvar="EXECTUNNEL_LOG_LEVEL",
        ),
    ] = "info",
    show_logs: Annotated[
        bool,
        typer.Option(
            "--show-logs/--no-show-logs",
            help="Show live log tail in the dashboard.",
        ),
    ] = False,
    insecure: Annotated[
        bool,
        typer.Option("--insecure", help="Skip TLS certificate verification."),
    ] = False,
) -> None:
    """Run a local SOCKS5 proxy that tunnels connections through a pod via WebSocket exec.

    \b
    Set ALL_PROXY=socks5://127.0.0.1:1080 to route your shell traffic.
    RFC1918 + loopback CIDRs are bypassed by default unless --no-default-exclude is set.
    """
    normalized_log_level = _normalize_log_level(log_level)
    if normalized_log_level not in _VALID_LOG_LEVELS:
        raise typer.BadParameter(
            f"Invalid log level {log_level!r}. "
            f"Choose from: {', '.join(sorted(_VALID_LOG_LEVELS))}",
            param_hint="'--log-level'",
        )


    configure_logging(normalized_log_level)

    _print_banner()

    dns_upstream = _dns_upstream_callback(dns)
    exclude_nets = _parse_excludes(exclude, no_default_exclude)


    try:
        session_cfg = build_session_config(insecure=insecure)
    except ConfigurationError as exc:
        _print_config_error(exc)
        raise typer.Exit(1)

    if insecure:
        console.print(f"[et.warn]{Icons.WARN} TLS verification disabled.[/et.warn]")

    try:
        tun_cfg = build_tunnel_config(
            socks_host=socks_host,
            socks_port=socks_port,
            dns_upstream=dns_upstream,
            dns_local_port=dns_port,
            ready_timeout=ready_timeout,
            conn_ack_timeout=conn_ack_timeout,
            exclude=exclude_nets,
            bootstrap_delivery=bootstrap_delivery,
            fetch_agent_url=fetch_agent_url,
            bootstrap_skip_if_present=bootstrap_skip_if_present,
            bootstrap_syntax_check=bootstrap_syntax_check,
            bootstrap_agent_path=bootstrap_agent_path,
            bootstrap_syntax_ok_sentinel=bootstrap_syntax_ok_sentinel,
            bootstrap_use_go_agent=bootstrap_use_go_agent,
            bootstrap_go_agent_path=bootstrap_go_agent_path,
            connect_pace_interval_secs=connect_pace_interval,
            socks_handshake_timeout=socks_handshake_timeout,
            socks_request_queue_cap=socks_request_queue_cap,
            socks_queue_put_timeout=socks_queue_put_timeout,
            udp_relay_queue_cap=udp_relay_queue_cap,
            udp_drop_warn_every=udp_drop_warn_every,
            udp_pump_poll_timeout=udp_pump_poll_timeout,
            udp_direct_recv_timeout=udp_direct_recv_timeout,
            dns_upstream_port=dns_upstream_port,
            dns_query_timeout=dns_query_timeout,
        )
    except ConfigurationError as exc:
        _print_config_error(exc)
        raise typer.Exit(1)

    _print_tunnel_summary(tun_cfg, session_cfg.wss_url)

    try:
        exit_code = asyncio.run(
            _tunnel_async(
                session_cfg=session_cfg,
                tun_cfg=tun_cfg,
                no_dashboard=no_dashboard,
                show_logs=show_logs,
            )
        )
    except KeyboardInterrupt:
        console.print(f"\n[et.warn]{Icons.WARN} Interrupted.[/et.warn]")
        exit_code = 0

    raise typer.Exit(exit_code)


async def _tunnel_async(
    *,
    session_cfg: SessionConfig,
    tun_cfg: TunnelConfig,
    no_dashboard: bool,
    show_logs: bool = False,
) -> int:
    console.print()
    async with BootstrapSpinner(console) as spinner:
        spinner.start_phase("stty")
        return await run_session(
            session_cfg=session_cfg,
            tun_cfg=tun_cfg,
            ws_url=session_cfg.wss_url,
            pod_spec=None,
            console=console,
            no_dashboard=no_dashboard,
            show_logs=show_logs,
            spinner=spinner,
        )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _print_banner() -> None:

    console.print(BANNER.format(version=__version__))


def _print_config_error(exc: ConfigurationError) -> None:
    console.print(
        f"[et.error]{Icons.CROSS} [{exc.error_code}] {exc.message}[/et.error]"
    )
    if exc.hint:
        console.print(f"  [et.muted]{Icons.BULLET} {exc.hint}[/et.muted]")


def _print_tunnel_summary(tun_cfg: TunnelConfig, wss_url: str) -> None:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="et.label", min_width=14)
    t.add_column(style="et.value")

    url_display = wss_url[:80] + "…" if len(wss_url) > 83 else wss_url
    t.add_row("WSS URL", url_display)
    t.add_row("SOCKS5", f"{tun_cfg.socks_host}:{tun_cfg.socks_port}")
    t.add_row(
        "DNS",
        f"{tun_cfg.dns_upstream}:{tun_cfg.dns_local_port}"
        if tun_cfg.dns_upstream
        else Text("disabled", style="et.muted"),
    )
    exclude_count = len(tun_cfg.exclude) if tun_cfg.exclude else 0
    t.add_row("Excludes", str(exclude_count))
    t.add_row("Bootstrap", tun_cfg.bootstrap_delivery)

    console.print(f"\n[et.brand]{Icons.TUNNEL} Tunnel Configuration[/et.brand]")
    console.print(t)
