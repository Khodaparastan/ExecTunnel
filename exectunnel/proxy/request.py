"""SOCKS5 request data class."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from exectunnel.protocol.enums import Cmd, Reply
from exectunnel.proxy._codec import _build_reply
from exectunnel.proxy.relay import UdpRelay


@dataclass
class Socks5Request:
    """
    Represents one completed SOCKS5 handshake.

    The handler must call :meth:`reply_success` **or** :meth:`reply_error`
    exactly once, then ``await writer.drain()`` before transferring data.
    Both reply helpers write to the transport buffer only — the caller is
    responsible for draining.
    """

    cmd: Cmd
    host: str
    port: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    udp_relay: UdpRelay | None = field(default=None)

    @property
    def is_connect(self) -> bool:
        return self.cmd == Cmd.CONNECT

    @property
    def is_udp(self) -> bool:
        return self.cmd == Cmd.UDP_ASSOCIATE

    def reply_success(self, bind_host: str = "127.0.0.1", bind_port: int = 0) -> None:
        """Queue a SUCCESS reply into the writer buffer (caller must drain)."""
        self.writer.write(_build_reply(Reply.SUCCESS, bind_host, bind_port))

    def reply_error(self, reply: Reply = Reply.GENERAL_FAILURE) -> None:
        """Queue an error reply into the writer buffer (caller must drain)."""
        self.writer.write(_build_reply(reply))

    async def send_reply_success(
        self, bind_host: str = "127.0.0.1", bind_port: int = 0
    ) -> None:
        """Write and flush a SUCCESS reply."""
        self.reply_success(bind_host, bind_port)
        await self.writer.drain()

    async def send_reply_error(self, reply: Reply = Reply.GENERAL_FAILURE) -> None:
        """Write and flush an error reply, then close the writer."""
        self.reply_error(reply)
        with contextlib.suppress(OSError):
            await self.writer.drain()
        with contextlib.suppress(OSError):
            self.writer.close()
            await self.writer.wait_closed()
