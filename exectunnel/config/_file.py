"""Root config file model and tunnel config resolution.

:class:`TunnelFile` is the top-level model that owns the full config file
structure.  It validates cross-tunnel constraints (unique names, unique ports)
and exposes :meth:`TunnelFile.resolve` which applies the full merge chain:

    Defaults class → GlobalDefaults → TunnelEntry → CLIOverrides

producing a ready-to-use ``(SessionConfig, TunnelConfig)`` pair.
"""

from __future__ import annotations

import ipaddress
import ssl
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self, TypedDict

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    IPvAnyAddress,
    PrivateAttr,
    model_validator,
)

from ._global import GlobalDefaults
from ._overrides import CLIOverrides
from ._tunnel import TunnelEntry

if TYPE_CHECKING:
    from exectunnel.defaults import Defaults

__all__ = ["TunnelFile"]


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def _first[T](*values: T | None) -> T | None:
    """Return the first non-``None`` value, or ``None`` if all are ``None``."""
    for v in values:
        if v is not None:
            return v
    return None


def _first_required[T](*values: T | None) -> T:
    """Like :func:`_first` but raises ``RuntimeError`` if every candidate is ``None``.

    Use for fields that **must** resolve to a concrete value (e.g. required
    identity fields or fields with a hardcoded ``Defaults`` fallback).
    """
    for v in values:
        if v is not None:
            return v
    raise RuntimeError(
        "All resolution layers returned None for a required field. "
        "This is a programming error — ensure a Defaults fallback is provided."
    )


def _build_ssl_context(
    ssl_no_verify: bool,
    ssl_ca_cert: Path | None,
    ssl_cert: Path | None,
    ssl_key: Path | None,
) -> ssl.SSLContext | None:
    """Build an :class:`ssl.SSLContext` from resolved SSL parameters.

    Returns ``None`` if no SSL customisation is required (websockets library
    defaults apply).
    """
    if not (ssl_no_verify or ssl_ca_cert or ssl_cert):
        return None

    ctx = ssl.create_default_context()

    if ssl_no_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif ssl_ca_cert:
        ctx.load_verify_locations(str(ssl_ca_cert))

    if ssl_cert and ssl_key:
        ctx.load_cert_chain(str(ssl_cert), str(ssl_key))

    return ctx


# ---------------------------------------------------------------------------
# TypedDicts for resolved domain groups — type-safe dict replacements
# ---------------------------------------------------------------------------


class _ResolvedWebSocket(TypedDict):
    wss_url: str
    reconnect_max_retries: int
    reconnect_base_delay: float
    reconnect_max_delay: float
    ping_interval: float
    send_timeout: float
    send_queue_cap: int


class _ResolvedSocks(TypedDict):
    socks_host: str
    socks_port: int
    socks_allow_non_loopback: bool
    socks_handshake_timeout: float
    socks_request_queue_cap: int
    socks_queue_put_timeout: float
    socks_udp_associate_enabled: bool
    socks_udp_bind_host: str
    socks_udp_advertise_host: str | None


class _ResolvedBootstrap(TypedDict):
    bootstrap_delivery: Literal["upload", "fetch"]
    bootstrap_fetch_url: str | None
    bootstrap_skip_if_present: bool
    bootstrap_syntax_check: bool
    bootstrap_agent_path: str
    bootstrap_syntax_ok_sentinel: str
    bootstrap_use_go_agent: bool
    bootstrap_go_agent_path: str


class _ResolvedTransport(TypedDict):
    ready_timeout: float
    conn_ack_timeout: float
    ack_timeout_warn_every: int
    ack_timeout_window_secs: float
    ack_timeout_reconnect_threshold: int
    connect_max_pending: int
    connect_max_pending_per_host: int
    pre_ack_buffer_cap_bytes: int
    connect_pace_interval_secs: float


class _ResolvedUdp(TypedDict):
    udp_relay_queue_cap: int
    udp_drop_warn_every: int
    udp_pump_poll_timeout: float
    udp_direct_recv_timeout: float
    udp_flow_idle_timeout: float


class _ResolvedDns(TypedDict):
    dns_upstream: str | None
    dns_local_port: int
    dns_upstream_port: int
    dns_max_inflight: int
    dns_query_timeout: float


# ---------------------------------------------------------------------------
# TunnelFile
# ---------------------------------------------------------------------------


class TunnelFile(BaseModel):
    """Root configuration file model.

    Owns the ``[global]`` section and all ``[[tunnels]]`` entries.
    Cross-tunnel validation (name uniqueness, port uniqueness) is enforced
    here rather than in individual entry models.

    Attributes:
        global_config: Process-wide defaults.  Optional — if the ``[global]``
                       section is absent from the file, an empty
                       :class:`GlobalDefaults` is used (env vars still apply).
        tunnels:       Ordered list of tunnel definitions.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    global_config: GlobalDefaults = Field(
        default_factory=GlobalDefaults,
        alias="global",
        description="Process-wide defaults applied to all tunnels.",
    )
    tunnels: list[TunnelEntry] = Field(
        default_factory=list,
        description="Ordered list of tunnel definitions.",
    )

    # ── Derived index (private, not serialised, not a model field) ────────────
    # PrivateAttr ensures Pydantic v2 never treats this as a field, and it
    # survives model_copy / frozen configs correctly.
    _tunnel_index: dict[str, TunnelEntry] = PrivateAttr(default_factory=dict)

    # ── Cross-tunnel validators ───────────────────────────────────────────────

    @model_validator(mode="after")
    def _validate_unique_names_and_build_index(self) -> Self:
        """Tunnel names must be unique; builds the O(1) lookup index."""
        seen: set[str] = set()
        duplicates: list[str] = []
        for t in self.tunnels:
            if t.name in seen:
                duplicates.append(t.name)
            seen.add(t.name)
        if duplicates:
            raise ValueError(
                f"Duplicate tunnel names found: {duplicates}. "
                "Each tunnel must have a unique name."
            )
        self._tunnel_index = {t.name: t for t in self.tunnels}
        return self

    @model_validator(mode="after")
    def _validate_unique_socks_ports(self) -> Self:
        """SOCKS5 ports must be unique across all tunnel entries."""
        seen: set[int] = set()
        duplicates: list[int] = []
        for t in self.tunnels:
            if t.socks_port in seen:
                duplicates.append(t.socks_port)
            seen.add(t.socks_port)
        if duplicates:
            raise ValueError(
                f"Duplicate socks_port values found: {duplicates}. "
                "Each tunnel must bind to a unique port."
            )
        return self

    # ── Resolution helpers (private, domain-scoped) ───────────────────────────

    def _resolve_websocket(
        self,
        entry: TunnelEntry,
        cli: CLIOverrides,
        g: GlobalDefaults,
        D: type[Defaults],
    ) -> _ResolvedWebSocket:
        return _ResolvedWebSocket(
            wss_url=str(_first_required(cli.wss_url, entry.wss_url)),
            reconnect_max_retries=_first_required(
                cli.reconnect_max_retries,
                entry.reconnect_max_retries,
                g.reconnect_max_retries,
                D.WS_RECONNECT_MAX_RETRIES,
            ),
            reconnect_base_delay=_first_required(
                cli.reconnect_base_delay,
                entry.reconnect_base_delay,
                g.reconnect_base_delay,
                D.WS_RECONNECT_BASE_DELAY_SECS,
            ),
            reconnect_max_delay=_first_required(
                cli.reconnect_max_delay,
                entry.reconnect_max_delay,
                g.reconnect_max_delay,
                D.WS_RECONNECT_MAX_DELAY_SECS,
            ),
            ping_interval=_first_required(
                cli.ping_interval,
                entry.ping_interval,
                g.ping_interval,
                D.WS_PING_INTERVAL_SECS,
            ),
            send_timeout=_first_required(
                cli.send_timeout,
                entry.send_timeout,
                g.send_timeout,
                D.WS_SEND_TIMEOUT_SECS,
            ),
            send_queue_cap=_first_required(
                entry.send_queue_cap,
                g.send_queue_cap,
                D.WS_SEND_QUEUE_CAP,
            ),
        )

    def _resolve_ssl(
        self,
        entry: TunnelEntry,
        cli: CLIOverrides,
    ) -> ssl.SSLContext | None:
        ssl_no_verify = _first(cli.ssl_no_verify, entry.ssl_no_verify) is True
        ssl_ca_cert: Path | None = _first(cli.ssl_ca_cert, entry.ssl_ca_cert)
        ssl_cert: Path | None = _first(cli.ssl_cert, entry.ssl_cert)
        ssl_key: Path | None = _first(cli.ssl_key, entry.ssl_key)
        return _build_ssl_context(ssl_no_verify, ssl_ca_cert, ssl_cert, ssl_key)

    def _resolve_headers(
        self,
        entry: TunnelEntry,
        cli: CLIOverrides,
        g: GlobalDefaults,
    ) -> dict[str, str]:
        """Merge headers: global ← tunnel ← cli (later wins, additive merge)."""
        merged: dict[str, str] = {}
        if g.ws_headers:
            merged.update(g.ws_headers)
        if entry.ws_headers:
            merged.update(entry.ws_headers)
        if cli.ws_headers:
            merged.update(cli.ws_headers)
        return merged

    def _resolve_socks(
        self,
        entry: TunnelEntry,
        cli: CLIOverrides,
        g: GlobalDefaults,
        D: type[Defaults],
    ) -> _ResolvedSocks:
        return _ResolvedSocks(
            socks_host=_first_required(
                cli.socks_host, entry.socks_host, g.socks_host, D.SOCKS_DEFAULT_HOST
            ),
            socks_port=_first_required(cli.socks_port, entry.socks_port),
            socks_allow_non_loopback=(
                _first(
                    cli.socks_allow_non_loopback,
                    entry.socks_allow_non_loopback,
                    g.socks_allow_non_loopback,
                    False,
                )
                is True
            ),
            socks_handshake_timeout=_first_required(
                cli.socks_handshake_timeout,
                entry.socks_handshake_timeout,
                g.socks_handshake_timeout,
                D.HANDSHAKE_TIMEOUT_SECS,
            ),
            socks_request_queue_cap=_first_required(
                entry.socks_request_queue_cap,
                g.socks_request_queue_cap,
                D.SOCKS_REQUEST_QUEUE_CAP,
            ),
            socks_queue_put_timeout=_first_required(
                entry.socks_queue_put_timeout,
                g.socks_queue_put_timeout,
                D.SOCKS_QUEUE_PUT_TIMEOUT_SECS,
            ),
            socks_udp_associate_enabled=(
                _first(
                    cli.socks_udp_associate_enabled,
                    entry.socks_udp_associate_enabled,
                    g.socks_udp_associate_enabled,
                    True,
                )
                is True
            ),
            socks_udp_bind_host=_first_required(
                cli.socks_udp_bind_host,
                entry.socks_udp_bind_host,
                g.socks_udp_bind_host,
                D.SOCKS_DEFAULT_HOST,
            ),
            socks_udp_advertise_host=(
                None
                if _first(
                    cli.socks_udp_advertise_host,
                    entry.socks_udp_advertise_host,
                    g.socks_udp_advertise_host,
                )
                is None
                else str(
                    _first(
                        cli.socks_udp_advertise_host,
                        entry.socks_udp_advertise_host,
                        g.socks_udp_advertise_host,
                    )
                )
            ),
        )

    def _resolve_bootstrap(
        self,
        entry: TunnelEntry,
        cli: CLIOverrides,
        g: GlobalDefaults,
        D: type[Defaults],
    ) -> _ResolvedBootstrap:
        delivery: Literal["upload", "fetch"] = _first_required(
            cli.bootstrap_delivery,
            entry.bootstrap_delivery,
            g.bootstrap_delivery,
            D.BOOTSTRAP_DELIVERY,
        )

        # Stringify AnyHttpUrl objects; keep None as None
        fetch_url: str | None = _first(
            None if cli.bootstrap_fetch_url is None else str(cli.bootstrap_fetch_url),
            None
            if entry.bootstrap_fetch_url is None
            else str(entry.bootstrap_fetch_url),
            None if g.bootstrap_fetch_url is None else str(g.bootstrap_fetch_url),
            D.BOOTSTRAP_FETCH_AGENT_URL,  # may itself be None
        )

        if delivery == "fetch" and not fetch_url:
            raise ValueError(
                "bootstrap_delivery is 'fetch' but no bootstrap_fetch_url is "
                "configured at any layer (tunnel, global, env, or defaults)."
            )

        return _ResolvedBootstrap(
            bootstrap_delivery=delivery,
            bootstrap_fetch_url=fetch_url,  # None is valid when delivery != "fetch"
            bootstrap_skip_if_present=_first(
                cli.bootstrap_skip_if_present,
                entry.bootstrap_skip_if_present,
                g.bootstrap_skip_if_present,
                False,
            )
            is True,
            bootstrap_syntax_check=_first(
                cli.bootstrap_syntax_check,
                entry.bootstrap_syntax_check,
                g.bootstrap_syntax_check,
                False,
            )
            is True,
            bootstrap_agent_path=_first_required(
                cli.bootstrap_agent_path,
                entry.bootstrap_agent_path,
                g.bootstrap_agent_path,
                D.BOOTSTRAP_AGENT_PATH,
            ),
            bootstrap_syntax_ok_sentinel=_first_required(
                entry.bootstrap_syntax_ok_sentinel,
                g.bootstrap_syntax_ok_sentinel,
                D.BOOTSTRAP_SYNTAX_OK_SENTINEL,
            ),
            bootstrap_use_go_agent=_first(
                cli.bootstrap_use_go_agent,
                entry.bootstrap_use_go_agent,
                g.bootstrap_use_go_agent,
                False,
            )
            is True,
            bootstrap_go_agent_path=_first_required(
                cli.bootstrap_go_agent_path,
                entry.bootstrap_go_agent_path,
                g.bootstrap_go_agent_path,
                D.BOOTSTRAP_GO_AGENT_PATH,
            ),
        )

    def _resolve_transport(
        self,
        entry: TunnelEntry,
        cli: CLIOverrides,
        g: GlobalDefaults,
        D: type[Defaults],
    ) -> _ResolvedTransport:
        return _ResolvedTransport(
            ready_timeout=_first_required(
                cli.ready_timeout,
                entry.ready_timeout,
                g.ready_timeout,
                D.READY_TIMEOUT_SECS,
            ),
            conn_ack_timeout=_first_required(
                cli.conn_ack_timeout,
                entry.conn_ack_timeout,
                g.conn_ack_timeout,
                D.CONN_ACK_TIMEOUT_SECS,
            ),
            ack_timeout_warn_every=_first_required(
                entry.ack_timeout_warn_every,
                g.ack_timeout_warn_every,
                D.ACK_TIMEOUT_WARN_EVERY,
            ),
            ack_timeout_window_secs=_first_required(
                entry.ack_timeout_window_secs,
                g.ack_timeout_window_secs,
                D.ACK_TIMEOUT_WINDOW_SECS,
            ),
            ack_timeout_reconnect_threshold=_first_required(
                entry.ack_timeout_reconnect_threshold,
                g.ack_timeout_reconnect_threshold,
                D.ACK_TIMEOUT_RECONNECT_THRESHOLD,
            ),
            connect_max_pending=_first_required(
                cli.connect_max_pending,
                entry.connect_max_pending,
                g.connect_max_pending,
                D.CONNECT_MAX_PENDING,
            ),
            connect_max_pending_per_host=_first_required(
                cli.connect_max_pending_per_host,
                entry.connect_max_pending_per_host,
                g.connect_max_pending_per_host,
                D.CONNECT_MAX_PENDING_PER_HOST,
            ),
            pre_ack_buffer_cap_bytes=_first_required(
                entry.pre_ack_buffer_cap_bytes,
                g.pre_ack_buffer_cap_bytes,
                D.PRE_ACK_BUFFER_CAP_BYTES,
            ),
            connect_pace_interval_secs=_first_required(
                entry.connect_pace_interval_secs,
                g.connect_pace_interval_secs,
                D.CONNECT_PACE_INTERVAL_SECS,
            ),
        )

    def _resolve_udp(
        self,
        entry: TunnelEntry,
        cli: CLIOverrides,
        g: GlobalDefaults,
        D: type[Defaults],
    ) -> _ResolvedUdp:
        return _ResolvedUdp(
            udp_relay_queue_cap=_first_required(
                entry.udp_relay_queue_cap,
                g.udp_relay_queue_cap,
                D.UDP_RELAY_QUEUE_CAP,
            ),
            udp_drop_warn_every=_first_required(
                entry.udp_drop_warn_every,
                g.udp_drop_warn_every,
                D.UDP_WARN_EVERY,
            ),
            udp_pump_poll_timeout=_first_required(
                entry.udp_pump_poll_timeout,
                g.udp_pump_poll_timeout,
                D.UDP_PUMP_POLL_TIMEOUT_SECS,
            ),
            udp_direct_recv_timeout=_first_required(
                entry.udp_direct_recv_timeout,
                g.udp_direct_recv_timeout,
                D.UDP_DIRECT_RECV_TIMEOUT_SECS,
            ),
            udp_flow_idle_timeout=_first_required(
                cli.udp_flow_idle_timeout,
                entry.udp_flow_idle_timeout,
                g.udp_flow_idle_timeout,
                120.0,
            ),
        )

    def _resolve_dns(
        self,
        entry: TunnelEntry,
        cli: CLIOverrides,
        g: GlobalDefaults,
        D: type[Defaults],
    ) -> _ResolvedDns:
        # IPvAnyAddress | None — stringify for SessionConfig (expects str | None)
        raw_upstream: IPvAnyAddress | None = _first(
            cli.dns_upstream, entry.dns_upstream, g.dns_upstream
        )
        return _ResolvedDns(
            dns_upstream=str(raw_upstream) if raw_upstream is not None else None,
            dns_local_port=_first_required(
                cli.dns_local_port,
                entry.dns_local_port,
                g.dns_local_port,
                D.DNS_LOCAL_PORT,
            ),
            dns_upstream_port=_first_required(
                cli.dns_upstream_port,
                entry.dns_upstream_port,
                g.dns_upstream_port,
                D.DNS_UPSTREAM_PORT,
            ),
            dns_max_inflight=_first_required(
                cli.dns_max_inflight,
                entry.dns_max_inflight,
                g.dns_max_inflight,
                D.DNS_MAX_INFLIGHT,
            ),
            dns_query_timeout=_first_required(
                cli.dns_query_timeout,
                entry.dns_query_timeout,
                g.dns_query_timeout,
                D.DNS_QUERY_TIMEOUT_SECS,
            ),
        )

    def _resolve_routing(
        self,
        entry: TunnelEntry,
        cli: CLIOverrides,
        g: GlobalDefaults,
    ) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        """Build the final exclusion network list."""
        # Deferred import — avoids pulling session layer at config-parse time
        from exectunnel.session._routing import (
            get_default_exclusion_networks,  # noqa: PLC0415
        )

        no_default_exclude = (
            _first(
                cli.no_default_exclude, entry.no_default_exclude, g.no_default_exclude
            )
            is True
        )

        raw_extra = _first(cli.extra_excludes, entry.extra_excludes, g.extra_excludes)
        extra: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = (
            [ipaddress.ip_network(str(n), strict=False) for n in raw_extra]
            if raw_extra is not None
            else []
        )

        base: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = (
            [] if no_default_exclude else get_default_exclusion_networks()
        )
        base.extend(extra)
        return base

    # ── Public API ────────────────────────────────────────────────────────────

    def resolve(
        self,
        entry: TunnelEntry,
        cli_overrides: CLIOverrides | None = None,
    ) -> tuple[SessionConfig, TunnelConfig]:
        """Resolve a tunnel entry into a ``(SessionConfig, TunnelConfig)`` pair.

        Applies the full merge chain (lowest → highest priority):

        1. ``Defaults`` class values (library hardcoded defaults)
        2. ``GlobalDefaults`` fields (config file ``[global]`` + env vars)
        3. ``TunnelEntry`` fields (config file ``[[tunnels]]`` entry)
        4. ``CLIOverrides`` fields (command-line flags)

        ``None`` at any layer means "not set — use the layer below".

        Args:
            entry:         The tunnel entry to resolve.
            cli_overrides: Optional CLI flag values.  ``None`` is equivalent
                           to an all-``None`` :class:`CLIOverrides` instance.

        Returns:
            A ``(SessionConfig, TunnelConfig)`` tuple ready for
            :class:`~exectunnel.session.TunnelSession`.

        Raises:
            RuntimeError: If a required field resolves to ``None`` across all
                          layers (indicates a missing ``Defaults`` fallback).
            ValueError:   If resolved ``bootstrap_delivery='fetch'`` but no
                          ``bootstrap_fetch_url`` is available.
        """
        # Deferred to avoid circular import:
        # exectunnel.defaults → session layer → config layer
        from exectunnel.defaults import Defaults  # noqa: PLC0415
        from exectunnel.session._config import (  # noqa: PLC0415
            SessionConfig,
            TunnelConfig,
        )

        cli = cli_overrides or CLIOverrides()
        g = self.global_config
        D = Defaults  # noqa: N806 — uppercase alias mirrors the class name convention

        ws = self._resolve_websocket(entry, cli, g, D)
        ssl_ctx = self._resolve_ssl(entry, cli)
        headers = self._resolve_headers(entry, cli, g)
        socks = self._resolve_socks(entry, cli, g, D)
        bootstrap = self._resolve_bootstrap(entry, cli, g, D)
        transport = self._resolve_transport(entry, cli, g, D)
        udp = self._resolve_udp(entry, cli, g, D)
        dns = self._resolve_dns(entry, cli, g, D)
        exclude = self._resolve_routing(entry, cli, g)

        # Post-resolution cross-field validation (after all layers merged)
        if ws["reconnect_base_delay"] >= ws["reconnect_max_delay"]:
            raise ValueError(
                f"Resolved reconnect_base_delay ({ws['reconnect_base_delay']}) must be "
                f"less than reconnect_max_delay ({ws['reconnect_max_delay']}). "
                "Check CLI flags, tunnel entry, global config, and defaults."
            )

        session_cfg = SessionConfig(
            wss_url=ws["wss_url"],
            ws_headers=headers,
            ssl_context_override=ssl_ctx,
            reconnect_max_retries=ws["reconnect_max_retries"],
            reconnect_base_delay=ws["reconnect_base_delay"],
            reconnect_max_delay=ws["reconnect_max_delay"],
            ping_interval=ws["ping_interval"],
            send_timeout=ws["send_timeout"],
            send_queue_cap=ws["send_queue_cap"],
        )

        tun_cfg = TunnelConfig(
            socks_host=socks["socks_host"],
            socks_port=socks["socks_port"],
            socks_allow_non_loopback=socks["socks_allow_non_loopback"],
            socks_udp_associate_enabled=socks["socks_udp_associate_enabled"],
            socks_udp_bind_host=socks["socks_udp_bind_host"],
            socks_udp_advertise_host=socks["socks_udp_advertise_host"],
            dns_upstream=dns["dns_upstream"],
            dns_local_port=dns["dns_local_port"],
            dns_upstream_port=dns["dns_upstream_port"],
            dns_max_inflight=dns["dns_max_inflight"],
            dns_query_timeout=dns["dns_query_timeout"],
            ready_timeout=transport["ready_timeout"],
            conn_ack_timeout=transport["conn_ack_timeout"],
            exclude=exclude,
            ack_timeout_warn_every=transport["ack_timeout_warn_every"],
            ack_timeout_window_secs=transport["ack_timeout_window_secs"],
            ack_timeout_reconnect_threshold=transport[
                "ack_timeout_reconnect_threshold"
            ],
            connect_max_pending=transport["connect_max_pending"],
            connect_max_pending_per_host=transport["connect_max_pending_per_host"],
            pre_ack_buffer_cap_bytes=transport["pre_ack_buffer_cap_bytes"],
            connect_pace_interval_secs=transport["connect_pace_interval_secs"],
            bootstrap_delivery=bootstrap["bootstrap_delivery"],
            bootstrap_fetch_url=bootstrap["bootstrap_fetch_url"],
            bootstrap_skip_if_present=bootstrap["bootstrap_skip_if_present"],
            bootstrap_syntax_check=bootstrap["bootstrap_syntax_check"],
            bootstrap_agent_path=bootstrap["bootstrap_agent_path"],
            bootstrap_syntax_ok_sentinel=bootstrap["bootstrap_syntax_ok_sentinel"],
            bootstrap_use_go_agent=bootstrap["bootstrap_use_go_agent"],
            bootstrap_go_agent_path=bootstrap["bootstrap_go_agent_path"],
            socks_handshake_timeout=socks["socks_handshake_timeout"],
            socks_request_queue_cap=socks["socks_request_queue_cap"],
            socks_queue_put_timeout=socks["socks_queue_put_timeout"],
            udp_relay_queue_cap=udp["udp_relay_queue_cap"],
            udp_drop_warn_every=udp["udp_drop_warn_every"],
            udp_pump_poll_timeout=udp["udp_pump_poll_timeout"],
            udp_direct_recv_timeout=udp["udp_direct_recv_timeout"],
            udp_flow_idle_timeout=udp["udp_flow_idle_timeout"],
        )

        return session_cfg, tun_cfg

    def get_tunnel(self, name: str) -> TunnelEntry:
        """Look up a tunnel entry by name (O(1)).

        Args:
            name: The tunnel name to look up.

        Returns:
            The matching :class:`TunnelEntry`.

        Raises:
            KeyError: If no tunnel with the given name exists.
        """
        try:
            return self._tunnel_index[name]
        except KeyError:
            available = list(self._tunnel_index)
            raise KeyError(
                f"No tunnel named {name!r}. Available: {available}"
            ) from None

    def enabled_tunnels(self) -> list[TunnelEntry]:
        """Return all tunnels whose effective ``enabled`` state is ``True``.

        A tunnel's effective enabled state is its own ``enabled`` field if
        set, otherwise ``GlobalDefaults.default_enabled``, otherwise ``True``.

        Returns:
            List of enabled :class:`TunnelEntry` instances.
        """
        effective_default = (
            self.global_config.default_enabled
            if self.global_config.default_enabled is not None
            else True
        )
        return [
            t
            for t in self.tunnels
            if (t.enabled if t.enabled is not None else effective_default)
        ]
