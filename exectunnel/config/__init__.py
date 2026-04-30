"""Public surface of the ``exectunnel.config`` package.

Typical usage
-------------
Load a config file and resolve a tunnel::

    from pathlib import Path
    from exectunnel.config import load_config_file, TunnelFile, CLIOverrides

    raw = load_config_file(Path("~/.config/exectunnel/config.toml").expanduser())
    tunnel_file = TunnelFile.model_validate(raw)

    entry = tunnel_file.get_tunnel("prod-gateway")
    session_cfg, tun_cfg = tunnel_file.resolve(entry)

Zero-config (wss_url on CLI only)::

    from exectunnel.config import make_single_tunnel_file, CLIOverrides

    tunnel_file = make_single_tunnel_file(wss_url="wss://...", socks_port=1080)
    entry = tunnel_file.tunnels[0]
    session_cfg, tun_cfg = tunnel_file.resolve(
        entry,
        cli_overrides=CLIOverrides(dns_upstream="10.96.0.10"),
    )
"""

from __future__ import annotations

from ._file import TunnelFile
from ._global import GlobalDefaults
from ._loader import ConfigFileError, load_config_file
from ._mixin import TunnelOverrideMixin
from ._overrides import CLIOverrides
from ._tunnel import TunnelEntry
from ._types import NonNegFloat, NonNegInt, PortInt, PosFloat, PosInt

__all__ = [
    "CLIOverrides",
    "ConfigFileError",
    "GlobalDefaults",
    "NonNegFloat",
    "NonNegInt",
    "PortInt",
    "PosFloat",
    "PosInt",
    "TunnelEntry",
    "TunnelFile",
    "TunnelOverrideMixin",
    "load_config_file",
    "make_single_tunnel_file",
]


def make_single_tunnel_file(
    wss_url: str,
    socks_port: int = 1080,
    name: str = "default",
) -> TunnelFile:
    """Construct a synthetic :class:`TunnelFile` for the zero-config path.

    Used when the user passes a ``wss_url`` directly on the CLI without a
    config file.  All tunables resolve from env vars and ``Defaults`` class
    values.  CLI flags are applied via :meth:`TunnelFile.resolve`.

    Args:
        wss_url:    The Kubernetes exec WebSocket endpoint.
        socks_port: Local SOCKS5 listener port.  Defaults to ``1080``.
        name:       Tunnel name used in logs.  Defaults to ``"default"``.

    Returns:
        A :class:`TunnelFile` containing a single :class:`TunnelEntry`.

    Raises:
        :exc:`pydantic.ValidationError`: If ``wss_url`` or ``socks_port``
            fail validation.
    """
    # Pass raw values — TunnelEntry's field validators handle URL parsing
    # and port range checks through the normal Pydantic pipeline.
    # Do NOT call AnyWebsocketUrl(wss_url) directly; that bypasses validation.
    entry = TunnelEntry.model_validate({
        "name": name,
        "wss_url": wss_url,
        "socks_port": socks_port,
    })
    return TunnelFile(tunnels=[entry])
