"""TcpConnectionWorker and flow ID generation."""
from __future__ import annotations

import secrets


def new_conn_id() -> str:
    """Generate a unique TCP connection ID (e.g. ``c3f9a1b2c3d4e5f6``)."""
    return "c" + secrets.token_hex(8)


def new_flow_id() -> str:
    """Generate a unique UDP flow ID (e.g. ``ua1b2c3d4e5f6f7e8``)."""
    return "u" + secrets.token_hex(8)
