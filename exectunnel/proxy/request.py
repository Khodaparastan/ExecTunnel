"""SOCKS5 request data class."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from exectunnel.exceptions import ProtocolError
from exectunnel.protocol.enums import Cmd, Reply
from exectunnel.proxy._codec import _build_reply
from exectunnel.proxy.relay import UdpRelay


@dataclass
class Socks5Request:
    """
    Represents one completed SOCKS5 handshake.

    The handler must call :meth:`send_reply_success` **or**
    :meth:`send_reply_error` exactly once before transferring data.
    The synchronous :meth:`reply_success` / :meth:`reply_error` helpers write
    to the transport buffer only — the caller is responsible for draining.
    """

    cmd: Cmd
    host: str
    port: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    udp_relay: UdpRelay | None = field(default=None)

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def is_connect(self) -> bool:
        """``True`` when the SOCKS5 command is ``CONNECT``."""
        return self.cmd == Cmd.CONNECT

    @property
    def is_udp(self) -> bool:
        """``True`` when the SOCKS5 command is ``UDP_ASSOCIATE``."""
        return self.cmd == Cmd.UDP_ASSOCIATE

    # ── Synchronous reply helpers (buffer only — caller must drain) ───────────

    def reply_success(
        self,
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
    ) -> None:
        """Queue a ``SUCCESS`` reply into the writer buffer.

        The caller is responsible for calling ``await writer.drain()``
        after this method returns.

        Raises
        ------
        ConfigurationError
            If *bind_host* or *bind_port* are invalid (propagated from
            :func:`_build_reply`).
        ProtocolError
            If the writer transport has already been closed, indicating
            the reply is being sent to a dead connection.
        """
        if self.writer.is_closing():
            raise ProtocolError(
                f"Cannot send SUCCESS reply for {self.host}:{self.port} — "
                "the writer transport is already closing.",
                error_code="protocol.socks5_reply_on_closed_writer",
                details={
                    "host": self.host,
                    "port": self.port,
                    "reply": "SUCCESS",
                },
                hint=(
                    "The SOCKS5 client disconnected before the reply could be sent. "
                    "This is usually benign."
                ),
            )
        # ConfigurationError from _build_reply (bad bind_host / bind_port)
        # propagates as-is — it is a caller bug, not a client error.
        self.writer.write(_build_reply(Reply.SUCCESS, bind_host, bind_port))

    def reply_error(self, reply: Reply = Reply.GENERAL_FAILURE) -> None:
        """Queue an error reply into the writer buffer.

        The caller is responsible for calling ``await writer.drain()``
        after this method returns.

        Raises
        ------
        ConfigurationError
            If *reply* is not a valid :class:`Reply` member (propagated from
            :func:`_build_reply`).
        """
        # We intentionally do NOT guard on writer.is_closing() here: error
        # replies must be attempted even on a half-closed connection so that
        # the client receives a well-formed SOCKS5 rejection rather than a
        # bare TCP RST.  The subsequent drain() / close() calls in
        # send_reply_error() already suppress OSError.
        self.writer.write(_build_reply(reply))

    # ── Async reply helpers (write + drain) ───────────────────────────────────

    async def send_reply_success(
        self,
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
    ) -> None:
        """Write and flush a ``SUCCESS`` reply to the SOCKS5 client.

        Raises
        ------
        ConfigurationError
            If *bind_host* or *bind_port* are invalid (propagated from
            :meth:`reply_success` → :func:`_build_reply`).
        ProtocolError
            If the writer transport is already closing when the reply is
            attempted (propagated from :meth:`reply_success`).
        OSError
            If the underlying socket write or drain fails.  Callers that
            want to suppress connection-reset errors should wrap this call
            in ``contextlib.suppress(OSError)``.
        """
        self.reply_success(bind_host, bind_port)
        await self.writer.drain()

    async def send_reply_error(
        self,
        reply: Reply = Reply.GENERAL_FAILURE,
    ) -> None:
        """Write and flush an error reply, then close the writer and any UDP relay.

        Errors during drain or close are suppressed — the connection is being
        torn down regardless and a secondary ``OSError`` must not shadow the
        original failure that caused the error reply.

        Raises
        ------
        ConfigurationError
            If *reply* is not a valid :class:`Reply` member (propagated from
            :meth:`reply_error` → :func:`_build_reply`).  This is a caller
            bug and is not suppressed.
        """
        # _build_reply / reply_error may raise ConfigurationError — let it
        # propagate so the caller knows it passed a bad Reply value.
        self.reply_error(reply)

        with contextlib.suppress(OSError):
            await self.writer.drain()
        with contextlib.suppress(OSError):
            self.writer.close()
            await self.writer.wait_closed()

        if self.udp_relay is not None:
            self.udp_relay.close()
