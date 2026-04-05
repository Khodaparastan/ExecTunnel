"""exectunnel connect — establish tunnel via kubectl pod discovery."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from exectunnel.config.settings import AppConfig, TunnelConfig
from exectunnel.exceptions import ConfigurationError, ExecTunnelError
from rich import box
from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from .._kubectl import KubectlDiscovery, PodSpec
from .._session_runner import run_session
from .._spinner import BootstrapSpinner
from .._theme import BANNER, THEME, Icons

__all__ = ["connect"]

console = Console(theme=THEME, highlight=False)


def connect(
    pod: Annotated[
        str | None,
        typer.Argument(help="Pod name. Omit to select interactively."),
    ] = None,
    namespace: Annotated[
        str | None,
        typer.Option("-n", "--namespace", help="Kubernetes namespace."),
    ] = None,
    container: Annotated[
        str | None,
        typer.Option("-c", "--container", help="Container name within the pod."),
    ] = None,
    context: Annotated[
        str | None,
        typer.Option("--context", help="kubeconfig context name."),
    ] = None,
    kubeconfig: Annotated[
        str | None,
        typer.Option("--kubeconfig", help="Path to kubeconfig file."),
    ] = None,
    label: Annotated[
        str | None,
        typer.Option(
            "-l", "--selector",
            help="Label selector to filter pods (e.g. app=myapp).",
        ),
    ] = None,
    socks_port: Annotated[
        int,
        typer.Option(
            "--socks-port", "-p",
            help="Local SOCKS5 port.",
            min=1,
            max=65535,
        ),
    ] = 1080,
    socks_host: Annotated[
        str,
        typer.Option("--socks-host", help="Local SOCKS5 bind address."),
    ] = "127.0.0.1",
    ready_timeout: Annotated[
        float,
        typer.Option("--ready-timeout", help="Seconds to wait for AGENT_READY."),
    ] = 30.0,
    reconnect_retries: Annotated[
        int,
        typer.Option(
            "--reconnect-retries",
            help="Max reconnect attempts (0 = unlimited).",
        ),
    ] = 5,
    no_dashboard: Annotated[
        bool,
        typer.Option("--no-dashboard", help="Disable the live dashboard."),
    ] = False,
    shell: Annotated[
        str,
        typer.Option("--shell", help="Shell command to exec into the pod."),
    ] = "/bin/sh",
    insecure: Annotated[
        bool,
        typer.Option(
            "--insecure",
            help="Skip TLS certificate verification (not recommended).",
        ),
    ] = False,
) -> None:
    """Connect through a Kubernetes pod using kubectl exec credentials."""
    _print_banner()

    try:
        exit_code = asyncio.run(
            _connect_async(
                pod_name=pod,
                namespace=namespace,
                container=container,
                context=context,
                kubeconfig=kubeconfig,
                label=label,
                socks_port=socks_port,
                socks_host=socks_host,
                ready_timeout=ready_timeout,
                reconnect_retries=reconnect_retries,
                no_dashboard=no_dashboard,
                shell=shell,
                insecure=insecure,
            )
        )
    except KeyboardInterrupt:
        console.print(f"\n[et.warn]{Icons.WARN} Interrupted.[/et.warn]")
        exit_code = 0

    raise typer.Exit(exit_code)


async def _connect_async(
    *,
    pod_name: str | None,
    namespace: str | None,
    container: str | None,
    context: str | None,
    kubeconfig: str | None,
    label: str | None,
    socks_port: int,
    socks_host: str,
    ready_timeout: float,
    reconnect_retries: int,
    no_dashboard: bool,
    shell: str,
    insecure: bool,
) -> int:
    discovery = KubectlDiscovery(kubeconfig=kubeconfig, context=context)

    # ── Load and display context ──────────────────────────────────────────────
    try:
        ctx = discovery.load_context()
    except ConfigurationError as exc:
        console.print(
            f"[et.error]{Icons.CROSS} kubeconfig error "
            f"[{exc.error_code}]: {exc.message}[/et.error]"
        )
        if exc.hint:
            console.print(f"  [et.muted]{Icons.BULLET} {exc.hint}[/et.muted]")
        return 1

    _print_context_summary(ctx, namespace or ctx.namespace)

    # ── Pod selection ─────────────────────────────────────────────────────────
    try:
        pod_spec = await _resolve_pod(
            discovery=discovery,
            pod_name=pod_name,
            namespace=namespace or ctx.namespace,
            label=label,
            container=container,
        )
    except (ConfigurationError, ExecTunnelError) as exc:
        console.print(
            f"[et.error]{Icons.CROSS} Pod resolution failed: "
            f"{exc.message}[/et.error]"
        )
        return 1
    except Exception as exc:
        console.print(
            f"[et.error]{Icons.CROSS} Unexpected error: {exc}[/et.error]"
        )
        console.print_exception(show_locals=False)
        return 1

    if pod_spec is None:
        return 1

    _print_pod_summary(pod_spec)

    # ── Validate pod health ───────────────────────────────────────────────────
    if pod_spec.phase and pod_spec.phase != "Running":
        console.print(
            f"[et.warn]{Icons.WARN} Pod phase is {pod_spec.phase!r} — "
            f"exec may fail.[/et.warn]"
        )
        if not Confirm.ask("  Continue anyway?", default=False, console=console):
            return 0

    # ── Build exec URL ────────────────────────────────────────────────────────
    try:
        ws_url, ws_headers = await discovery.build_exec_url(
            pod=pod_spec.name,
            namespace=pod_spec.namespace,
            container=pod_spec.container or container,
            command=[shell],
        )
    except (ConfigurationError, ExecTunnelError) as exc:
        console.print(
            f"[et.error]{Icons.CROSS} Failed to build exec URL: "
            f"{exc.message}[/et.error]"
        )
        return 1

    ssl_ctx = (
        _insecure_ssl_context() if insecure
        else discovery.make_ssl_context()
    )

    console.print(
        f"\n[et.muted]{Icons.ARROW_RIGHT} WebSocket URL: "
        f"{ws_url[:80]}{'…' if len(ws_url) > 80 else ''}[/et.muted]"
    )

    # ── Build config objects ──────────────────────────────────────────────────
    app_cfg = AppConfig.from_env(
        wss_url=ws_url,
        ws_headers=ws_headers,
        ssl_context=ssl_ctx,
        reconnect_max_retries=reconnect_retries,
    )
    tun_cfg = TunnelConfig(
        socks_host=socks_host,
        socks_port=socks_port,
        ready_timeout=ready_timeout,
    )

    # ── Bootstrap with spinner then dashboard ─────────────────────────────────
    console.print()
    async with BootstrapSpinner(console) as spinner:
        spinner.start_phase("stty")
        return await run_session(
            app_cfg=app_cfg,
            tun_cfg=tun_cfg,
            ws_url=ws_url,
            pod_spec=pod_spec,
            kubectl_ctx=ctx.name,
            console=console,
            no_dashboard=no_dashboard,
            spinner=spinner,
        )


async def _resolve_pod(
    *,
    discovery: KubectlDiscovery,
    pod_name: str | None,
    namespace: str,
    label: str | None,
    container: str | None,
) -> PodSpec | None:
    if pod_name:
        with console.status(
            f"[et.muted]Fetching pod {pod_name!r}…[/et.muted]"
        ):
            pod = await discovery.get_pod(pod_name, namespace=namespace)
        if container:
            pod = PodSpec(
                name=pod.name,
                namespace=pod.namespace,
                container=container,
                node=pod.node,
                phase=pod.phase,
                ip=pod.ip,
                labels=pod.labels,
                conditions=pod.conditions,
            )
        return pod

    with console.status(
        f"[et.muted]{Icons.PULSE} Listing pods in namespace "
        f"{namespace!r}…[/et.muted]"
    ):
        pods = await discovery.list_pods(
            namespace=namespace,
            label_selector=label,
        )

    if not pods:
        console.print(
            f"[et.warn]{Icons.WARN} No pods found in namespace {namespace!r}"
            + (f" with selector {label!r}" if label else "")
            + ".[/et.warn]"
        )
        return None

    _print_pod_table(pods)

    running = [p for p in pods if p.phase == "Running"]
    choices = running if running else pods

    if len(choices) == 1:
        console.print(
            f"[et.muted]Auto-selecting the only "
            f"{'running ' if running else ''}pod.[/et.muted]"
        )
        return choices[0]

    console.print()
    idx_str = typer.prompt(f"  Select pod [1-{len(pods)}]", default="1")
    try:
        idx = int(idx_str) - 1
        if not 0 <= idx < len(pods):
            raise ValueError
    except ValueError:
        console.print(f"[et.error]{Icons.CROSS} Invalid selection.[/et.error]")
        return None

    selected = pods[idx]
    if container:
        selected = PodSpec(
            name=selected.name,
            namespace=selected.namespace,
            container=container,
            node=selected.node,
            phase=selected.phase,
            ip=selected.ip,
            labels=selected.labels,
            conditions=selected.conditions,
        )
    return selected


# ── Display helpers ───────────────────────────────────────────────────────────


def _print_banner() -> None:
    from exectunnel import __version__
    console.print(BANNER.format(version=__version__))


def _print_context_summary(ctx: object, namespace: str) -> None:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="et.label", min_width=14)
    t.add_column(style="et.value")
    t.add_row("Context",   getattr(ctx, "name", "—"))
    t.add_row("Cluster",   getattr(ctx, "cluster_name", "—"))
    t.add_row("Server",    getattr(ctx, "server", "—"))
    t.add_row("Namespace", namespace)
    t.add_row("User",      getattr(ctx, "user", "—"))
    console.print(f"\n[et.brand]{Icons.POD} Kubernetes Context[/et.brand]")
    console.print(t)


def _print_pod_table(pods: list[PodSpec]) -> None:
    table = Table(
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="et.label",
        border_style="et.border",
        expand=False,
    )
    table.add_column("#",         width=4,  justify="right")
    table.add_column("Name",      ratio=3,  style="et.value")
    table.add_column("Namespace", ratio=2)
    table.add_column("Phase",     width=12)
    table.add_column("Node",      ratio=2,  style="et.muted")
    table.add_column("IP",        width=16, style="et.muted")
    table.add_column("Container", ratio=2,  style="et.muted")

    phase_styles = {
        "Running":   "et.stat.good",
        "Pending":   "et.stat.warn",
        "Failed":    "et.stat.bad",
        "Succeeded": "et.muted",
    }

    for i, pod in enumerate(pods, 1):
        style = phase_styles.get(pod.phase or "", "et.muted")
        table.add_row(
            str(i),
            pod.name,
            pod.namespace,
            f"[{style}]{pod.phase or '—'}[/{style}]",
            pod.node or "—",
            pod.ip or "—",
            pod.container or "—",
        )

    console.print(f"\n[et.brand]{Icons.POD} Available Pods[/et.brand]")
    console.print(table)


def _print_pod_summary(pod: PodSpec) -> None:
    from rich.text import Text

    t = Table.grid(padding=(0, 2))
    t.add_column(style="et.label", min_width=12)
    t.add_column(style="et.value")
    t.add_column(style="et.label", min_width=12)
    t.add_column(style="et.value")

    phase_style = {
        "Running": "et.stat.good",
        "Pending": "et.stat.warn",
        "Failed":  "et.stat.bad",
    }.get(pod.phase or "", "et.muted")

    t.add_row("Pod",       pod.name,             "Namespace", pod.namespace)
    t.add_row("Container", pod.container or "—", "Node",      pod.node or "—")
    t.add_row(
        "Phase",
        Text(pod.phase or "—", style=phase_style),
        "IP",
        pod.ip or "—",
    )

    console.print(f"\n[et.brand]{Icons.CHECK} Selected Pod[/et.brand]")
    console.print(t)


def _insecure_ssl_context() -> object:
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False  # type: ignore[union-attr]
    ctx.verify_mode = ssl.CERT_NONE  # type: ignore[union-attr]
    return ctx
