"""TcpConnectionWorker and flow ID generation."""
from __future__ import annotations

import secrets


def new_conn_id() -> str:
    """Generate a unique TCP connection ID (e.g. ``c3f9a1``)."""
    return "c" + secrets.token_hex(3)


def new_flow_id() -> str:
    """Generate a unique UDP flow ID (e.g. ``ua1b2c3``)."""
    return "u" + secrets.token_hex(3)
