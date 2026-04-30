"""Typer application — global callback and subcommand registration."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import typer

from ._context import (
    _CLI_DEFAULT_LOG_FORMAT,
    _CLI_DEFAULT_LOG_LEVEL,
    AppContext,
)
from ._display import configure_logging

__all__ = ["app"]

app = typer.Typer(
    name="exectunnel",
    help=(
        "SOCKS5 tunnel over Kubernetes exec/WebSocket channels.\n\n"
        "Run [bold]exectunnel run[/bold] to start all enabled tunnels from "
        "your config file, or [bold]exectunnel run-single WSS_URL[/bold] for "
        "a zero-config single tunnel."
    ),
    rich_markup_mode="rich",
    no_args_is_help=True,
    add_completion=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_VALID_LEVELS: frozenset[str] = frozenset({"debug", "info", "warning", "error"})
_VALID_FORMATS: frozenset[str] = frozenset({"text", "json"})


def _validate_choice(value: str, valid: frozenset[str], *, param_hint: str) -> None:
    if value not in valid:
        raise typer.BadParameter(
            f"Invalid value {value!r}. Choose from: {', '.join(sorted(valid))}",
            param_hint=param_hint,
        )


def _is_internal_worker_invocation(ctx: typer.Context) -> bool:
    """Return ``True`` for the hidden supervisor-spawned worker command."""
    return ctx.invoked_subcommand == "_worker"


@app.callback()
def _global_callback(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help=(
                "Path to the config file (.toml, .yaml, or .yml). "
                "Defaults to one of:\n"
                "[dim]~/.config/exectunnel/config.toml[/dim]\n"
                "[dim]~/.config/exectunnel/config.yaml[/dim]\n"
                "[dim]~/.config/exectunnel/config.yml[/dim]"
            ),
            exists=False,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    log_level: Annotated[
        Literal["debug", "info", "warning", "error"],
        typer.Option(
            "--log-level",
            help="Log verbosity: debug | info | warning | error.",
            metavar="LEVEL",
        ),
    ] = _CLI_DEFAULT_LOG_LEVEL,
    log_format: Annotated[
        Literal["text", "json"],
        typer.Option(
            "--log-format",
            help="Log output format: text (Rich) | json (NDJSON).",
            metavar="FORMAT",
        ),
    ] = _CLI_DEFAULT_LOG_FORMAT,
    log_file: Annotated[
        Path | None,
        typer.Option(
            "--log-file",
            help="Write logs to this file in addition to stderr.",
            metavar="PATH",
        ),
    ] = None,
) -> None:
    """ExecTunnel — SOCKS5 tunnel over Kubernetes exec/WebSocket channels."""
    _validate_choice(log_level, _VALID_LEVELS, param_hint="--log-level")
    _validate_choice(log_format, _VALID_FORMATS, param_hint="--log-format")

    log_level_from_cli = log_level != _CLI_DEFAULT_LOG_LEVEL
    log_format_from_cli = log_format != _CLI_DEFAULT_LOG_FORMAT
    log_file_from_cli = log_file is not None

    # Internal worker subprocesses must not go through normal app-level config
    # loading and CLI logging setup. The worker installs its own stdout IPC log
    # bridge and should stay isolated from user-facing stderr/file handlers.
    if _is_internal_worker_invocation(ctx):
        ctx.obj = AppContext.minimal(
            log_level=log_level,
            log_format=log_format,
            log_file=log_file,
            log_level_from_cli=log_level_from_cli,
            log_format_from_cli=log_format_from_cli,
            log_file_from_cli=log_file_from_cli,
        )
        return

    app_ctx = AppContext.load(
        config_path=config,
        log_level=log_level,
        log_format=log_format,
        log_file=log_file,
        log_level_from_cli=log_level_from_cli,
        log_format_from_cli=log_format_from_cli,
        log_file_from_cli=log_file_from_cli,
    )

    configure_logging(app_ctx.log_level, app_ctx.log_format, app_ctx.log_file)
    ctx.obj = app_ctx


@app.command("version")
def _version(ctx: typer.Context) -> None:
    """Print the ExecTunnel version and exit."""
    from ._commands.version import version_command  # noqa: PLC0415

    version_command(ctx)


@app.command("validate")
def _validate(ctx: typer.Context) -> None:
    """Validate the config file and print a summary."""
    from ._commands.validate import validate_command  # noqa: PLC0415

    validate_command(ctx)


@app.command("list")
def _list(ctx: typer.Context) -> None:
    """List all defined tunnels with their effective resolved configuration."""
    from ._commands.list import list_command  # noqa: PLC0415

    list_command(ctx)


@app.command("run")
def _run(
    ctx: typer.Context,
    names: Annotated[
        list[str] | None,
        typer.Argument(
            help="Tunnel names to run. Omit to run all enabled tunnels.",
            metavar="NAME",
        ),
    ] = None,
    socks_port: Annotated[
        int | None,
        typer.Option("--socks-port", help="Override SOCKS5 port.", metavar="PORT"),
    ] = None,
    socks_host: Annotated[
        str | None,
        typer.Option(
            "--socks-host", help="Override SOCKS5 bind address.", metavar="HOST"
        ),
    ] = None,
    socks_allow_non_loopback: Annotated[
        bool,
        typer.Option(
            "--socks-allow-non-loopback/--socks-loopback-only",
            help="Allow binding SOCKS5 to non-loopback addresses. Protect externally.",
        ),
    ] = False,
    socks_udp_associate_enabled: Annotated[
        bool | None,
        typer.Option(
            "--udp-associate/--no-udp-associate",
            help="Enable or disable SOCKS5 UDP_ASSOCIATE.",
        ),
    ] = None,
    socks_udp_bind_host: Annotated[
        str | None,
        typer.Option(
            "--udp-bind-host", help="SOCKS5 UDP relay bind host.", metavar="HOST"
        ),
    ] = None,
    socks_udp_advertise_host: Annotated[
        str | None,
        typer.Option(
            "--udp-advertise-host",
            help="IP advertised in UDP_ASSOCIATE replies.",
            metavar="IP",
        ),
    ] = None,
    dns_upstream: Annotated[
        str | None,
        typer.Option("--dns-upstream", help="Upstream DNS server IP.", metavar="IP"),
    ] = None,
    dns_local_port: Annotated[
        int | None,
        typer.Option(
            "--dns-local-port", help="Local DNS forwarder port.", metavar="PORT"
        ),
    ] = None,
    reconnect_max_retries: Annotated[
        int | None,
        typer.Option(
            "--reconnect-max-retries", help="Max reconnect attempts.", metavar="N"
        ),
    ] = None,
    ping_interval: Annotated[
        float | None,
        typer.Option(
            "--ping-interval", help="Seconds between KEEPALIVE frames.", metavar="SECS"
        ),
    ] = None,
    udp_flow_idle_timeout: Annotated[
        float | None,
        typer.Option(
            "--udp-flow-idle-timeout",
            help="Idle timeout for tunneled UDP flows. Use 0 to disable.",
            metavar="SECS",
        ),
    ] = None,
    show_logs: Annotated[
        bool,
        typer.Option(
            "--show-logs/--no-show-logs",
            help="Show live log tail in the dashboard.",
        ),
    ] = False,
) -> None:
    """Run one, several, or all enabled tunnels from the config file."""
    from ._commands.run import run_command  # noqa: PLC0415

    run_command(
        ctx,
        names=names or [],
        socks_port=socks_port,
        socks_host=socks_host,
        socks_allow_non_loopback=socks_allow_non_loopback,
        socks_udp_associate_enabled=socks_udp_associate_enabled,
        socks_udp_bind_host=socks_udp_bind_host,
        socks_udp_advertise_host=socks_udp_advertise_host,
        dns_upstream=dns_upstream,
        dns_local_port=dns_local_port,
        reconnect_max_retries=reconnect_max_retries,
        ping_interval=ping_interval,
        udp_flow_idle_timeout=udp_flow_idle_timeout,
        show_logs=show_logs,
    )


@app.command("run-single")
def _run_single(
    ctx: typer.Context,
    wss_url: Annotated[
        str,
        typer.Argument(help="Kubernetes exec WebSocket endpoint.", metavar="WSS_URL"),
    ],
    socks_port: Annotated[
        int,
        typer.Option(
            "--socks-port", help="Local SOCKS5 listener port.", metavar="PORT"
        ),
    ] = 1080,
    socks_host: Annotated[
        str | None,
        typer.Option("--socks-host", help="SOCKS5 bind address.", metavar="HOST"),
    ] = None,
    socks_allow_non_loopback: Annotated[
        bool,
        typer.Option(
            "--socks-allow-non-loopback/--socks-loopback-only",
            help="Allow binding SOCKS5 to non-loopback addresses. Protect externally.",
        ),
    ] = False,
    socks_udp_associate_enabled: Annotated[
        bool | None,
        typer.Option(
            "--udp-associate/--no-udp-associate",
            help="Enable or disable SOCKS5 UDP_ASSOCIATE.",
        ),
    ] = None,
    socks_udp_bind_host: Annotated[
        str | None,
        typer.Option(
            "--udp-bind-host", help="SOCKS5 UDP relay bind host.", metavar="HOST"
        ),
    ] = None,
    socks_udp_advertise_host: Annotated[
        str | None,
        typer.Option(
            "--udp-advertise-host",
            help="IP advertised in UDP_ASSOCIATE replies.",
            metavar="IP",
        ),
    ] = None,
    dns_upstream: Annotated[
        str | None,
        typer.Option("--dns-upstream", help="Upstream DNS server IP.", metavar="IP"),
    ] = None,
    dns_local_port: Annotated[
        int | None,
        typer.Option(
            "--dns-local-port", help="Local DNS forwarder port.", metavar="PORT"
        ),
    ] = None,
    reconnect_max_retries: Annotated[
        int | None,
        typer.Option(
            "--reconnect-max-retries", help="Max reconnect attempts.", metavar="N"
        ),
    ] = None,
    ping_interval: Annotated[
        float | None,
        typer.Option(
            "--ping-interval", help="Seconds between KEEPALIVE frames.", metavar="SECS"
        ),
    ] = None,
    udp_flow_idle_timeout: Annotated[
        float | None,
        typer.Option(
            "--udp-flow-idle-timeout",
            help="Idle timeout for tunneled UDP flows. Use 0 to disable.",
            metavar="SECS",
        ),
    ] = None,
    send_timeout: Annotated[
        float | None,
        typer.Option(
            "--send-timeout", help="WebSocket frame send timeout.", metavar="SECS"
        ),
    ] = None,
    conn_ack_timeout: Annotated[
        float | None,
        typer.Option(
            "--conn-ack-timeout", help="Seconds to wait for CONN_ACK.", metavar="SECS"
        ),
    ] = None,
    ready_timeout: Annotated[
        float | None,
        typer.Option(
            "--ready-timeout", help="Seconds to wait for AGENT_READY.", metavar="SECS"
        ),
    ] = None,
    bootstrap_delivery: Annotated[
        Literal["fetch", "upload"] | None,
        typer.Option(
            "--bootstrap-delivery",
            help="Agent delivery mode: upload | fetch.",
            metavar="MODE",
        ),
    ] = None,
    bootstrap_fetch_url: Annotated[
        str | None,
        typer.Option(
            "--bootstrap-fetch-url", help="URL for fetch delivery.", metavar="URL"
        ),
    ] = None,
    bootstrap_skip_if_present: Annotated[
        bool,
        typer.Option(
            "--bootstrap-skip-if-present/--no-bootstrap-skip-if-present",
            help="Skip delivery if agent already exists.",
        ),
    ] = False,
    bootstrap_use_go_agent: Annotated[
        bool,
        typer.Option(
            "--bootstrap-use-go-agent/--no-bootstrap-use-go-agent",
            help="Use Go agent binary instead of agent.py.",
        ),
    ] = False,
    no_default_exclude: Annotated[
        bool,
        typer.Option(
            "--no-default-exclude/--default-exclude",
            help="Clear RFC-1918 + loopback exclusion defaults.",
        ),
    ] = False,
    ssl_no_verify: Annotated[
        bool,
        typer.Option(
            "--ssl-no-verify/--ssl-verify",
            help="Disable TLS certificate verification. INSECURE.",
        ),
    ] = False,
    ssl_ca_cert: Annotated[
        str | None,
        typer.Option(
            "--ssl-ca-cert", help="Custom CA certificate bundle (PEM).", metavar="PATH"
        ),
    ] = None,
    ssl_cert: Annotated[
        str | None,
        typer.Option("--ssl-cert", help="Client certificate (PEM).", metavar="PATH"),
    ] = None,
    ssl_key: Annotated[
        str | None,
        typer.Option("--ssl-key", help="Client private key (PEM).", metavar="PATH"),
    ] = None,
    ws_header: Annotated[
        list[str] | None,
        typer.Option(
            "--ws-header",
            help="Extra HTTP header: 'Key: Value'. Repeat for multiple.",
            metavar="KEY: VALUE",
        ),
    ] = None,
    show_logs: Annotated[
        bool,
        typer.Option(
            "--show-logs/--no-show-logs",
            help="Show live log tail in the dashboard.",
        ),
    ] = False,
) -> None:
    """Run a single tunnel without a config file (zero-config mode)."""
    from ._commands.run import run_single_command  # noqa: PLC0415

    run_single_command(
        ctx,
        wss_url=wss_url,
        socks_port=socks_port,
        socks_host=socks_host,
        socks_allow_non_loopback=socks_allow_non_loopback,
        socks_udp_associate_enabled=socks_udp_associate_enabled,
        socks_udp_bind_host=socks_udp_bind_host,
        socks_udp_advertise_host=socks_udp_advertise_host,
        dns_upstream=dns_upstream,
        dns_local_port=dns_local_port,
        reconnect_max_retries=reconnect_max_retries,
        ping_interval=ping_interval,
        send_timeout=send_timeout,
        conn_ack_timeout=conn_ack_timeout,
        ready_timeout=ready_timeout,
        udp_flow_idle_timeout=udp_flow_idle_timeout,
        bootstrap_delivery=bootstrap_delivery,
        bootstrap_fetch_url=bootstrap_fetch_url,
        bootstrap_skip_if_present=bootstrap_skip_if_present,
        bootstrap_use_go_agent=bootstrap_use_go_agent,
        no_default_exclude=no_default_exclude,
        ssl_no_verify=ssl_no_verify,
        ssl_ca_cert=ssl_ca_cert,
        ssl_cert=ssl_cert,
        ssl_key=ssl_key,
        ws_header=ws_header or [],
        show_logs=show_logs,
    )


@app.command(
    "_worker",
    hidden=True,
    help="Internal: run a single tunnel as a supervised worker subprocess.",
)
def _worker(
    ctx: typer.Context,  # noqa: ARG001
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            help="Path to the tunnel config file.",
            exists=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ],
    name: Annotated[
        str,
        typer.Option("--name", help="Tunnel entry name to run."),
    ],
) -> None:
    """Internal worker entry point — invoked by the parent supervisor."""
    from ._supervisor import run_worker  # noqa: PLC0415

    raise typer.Exit(run_worker(config_path=config, tunnel=name))
