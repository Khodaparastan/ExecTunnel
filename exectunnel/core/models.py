from __future__ import annotations

import asyncio
from dataclasses import dataclass

from exectunnel.connection import _ConnHandler
from exectunnel.socks5 import Socks5Request


@dataclass(slots=True)
class PendingConnect:
    host: str
    request: Socks5Request
    handler: _ConnHandler
    ack_future: asyncio.Future[str]
