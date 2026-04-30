"""Display utilities — logging, live status panel, unified dashboard, spinner."""

from ._dashboard import DashboardMode, TunnelSlot, UnifiedDashboard
from ._formatting import (
    LOG_LEVEL_STYLES,
    ack_rate_str,
    err_style,
    fmt_bytes,
    fmt_uptime,
    label_col,
    status_dot,
    truncate_url,
)
from ._logging import configure_logging, get_stderr_console
from ._panel import LivePanel, TunnelStatus, TunnelStatusRegistry
from ._spinner import PHASE_NAMES, BootstrapSpinner
from ._theme import BANNER, THEME, Icons

__all__ = [
    # Dashboard
    "UnifiedDashboard",
    "DashboardMode",
    "TunnelSlot",
    "LivePanel",
    "TunnelStatus",
    "TunnelStatusRegistry",
    # Spinner
    "BootstrapSpinner",
    "PHASE_NAMES",
    # Formatting
    "fmt_bytes",
    "fmt_uptime",
    "ack_rate_str",
    "err_style",
    "status_dot",
    "truncate_url",
    "label_col",
    "LOG_LEVEL_STYLES",
    # Theme
    "Icons",
    "BANNER",
    "THEME",
    # Logging
    "configure_logging",
    "get_stderr_console",
]
