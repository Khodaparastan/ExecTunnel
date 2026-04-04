"""SOCKS5 request — one completed SOCKS5 handshake.

:class:`Socks5Request` is produced by :class:`~exectunnel.proxy.server.Socks5Server`
after a successful SOCKS5 negotiation and handed to the session layer for data
relay.  It owns the client :class:`asyncio.StreamWriter` and any associated
:class:`~exectunnel.proxy.udp_relay.UdpRelay`.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from exectunnel.exceptions import ProtocolError
from exectunnel.protocol import Cmd, Reply
from exectunnel.proxy._wire import build_socks5_reply
from exectunnel.proxy.udp_relay import UdpRelay

__all__: list[str] = ["Socks5Request"]


@dataclass(eq=False)
class Socks5Request:
    """One completed SOCKS5 handshake, ready for data relay.

    The handler **must** call :meth:`send_reply_success` **or**
    :meth:`send_reply_error` exactly once before transferring data.

    Usable as an async context manager that guarantees cleanup even if the
    handler raises before sending a reply::

        async with request:
            await request.send_reply_success(bind_port=request.udp_relay.local_port)
            # … relay data …

    Attributes:
        cmd:       The SOCKS5 command (:class:`~exectunnel.protocol.Cmd`).
        host:      Destination hostname or IP address string.
        port:      Destination port number in ``[1, 65535]``.
        reader:    asyncio stream reader for the SOCKS5 client connection.
        writer:    asyncio stream writer for the SOCKS5 client connection.
        udp_relay: Bound :class:`~exectunnel.proxy.udp_relay.UdpRelay` for
                   ``UDP_ASSOCIATE`` requests; ``None`` for ``CONNECT``.

    Note:
        ``eq=False`` prevents the auto-generated ``__eq__`` / ``__hash__``
        from comparing :class:`asyncio.StreamReader` / :class:`asyncio.StreamWriter`
        instances, which are not safely comparable by value.
    """

    cmd: Cmd
    host: str
    port: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    udp_relay: UdpRelay | None = field(default=None)

    # Tracks whether a reply has been sent so double-reply bugs are caught at
    # runtime rather than silently corrupting the SOCKS5 stream.
    _replied: bool = field(default=False, init=False, repr=False)

    # ── Async context manager ────────────────────────────────────────────────

    async def __aenter__(self) -> Socks5Request:
        return self

    async def __aexit__(self, *_: object) -> None:
        """Guarantee writer and relay are closed on exit, regardless of outcome."""
        await self.close()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the writer and any associated UDP relay.

        Idempotent — safe to call multiple times.  ``OSError`` is suppressed
        because the connection may already be half-closed by the time this is
        called.
        """
        if self.udp_relay is not None:
            self.udp_relay.close()

        if self.writer.is_closing():
            with contextlib.suppress(OSError):
                await self.writer.wait_closed()
            return

        with contextlib.suppress(OSError):
            self.writer.close()
            await self.writer.wait_closed()

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def is_connect(self) -> bool:
        """``True`` when the SOCKS5 command is ``CONNECT``."""
        return self.cmd == Cmd.CONNECT

    @property
    def is_udp_associate(self) -> bool:
        """``True`` when the SOCKS5 command is ``UDP_ASSOCIATE``."""
        return self.cmd == Cmd.UDP_ASSOCIATE

    @property
    def replied(self) -> bool:
        """``True`` once a reply has been sent to the SOCKS5 client."""
        return self._replied

    # ── Internal reply guard ─────────────────────────────────────────────────

    def _assert_not_replied(self) -> None:
        """Raise :class:`~exectunnel.exceptions.ProtocolError` on double-reply.

        Raises:
            ProtocolError: If a reply has already been sent for this request.
        """
        if self._replied:
            raise ProtocolError(
                f"A SOCKS5 reply has already been sent for "
                f"{self.host}:{self.port} — cannot send a second reply.",
                details={
                    "frame_type": "SOCKS5_REPLY",
                    "expected": "exactly one reply per request",
                },
                hint=(
                    "Ensure the request handler calls send_reply_success() or "
                    "send_reply_error() exactly once."
                ),
            )
        self._replied = True

    # ── Synchronous reply helpers (buffer only — caller must drain) ──────────

    def reply_success(
        self,
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
    ) -> None:
        """Queue a ``SUCCESS`` reply into the writer buffer.

        The writer-closing check is performed **before** the double-reply guard
        so that the caller can still call :meth:`reply_error` if this method
        raises due to a closed writer.

        The caller is responsible for ``await writer.drain()`` after this
        method returns.

        Args:
            bind_host: The ``BND.ADDR`` to advertise.  Defaults to
                       ``"127.0.0.1"``.
            bind_port: The ``BND.PORT`` to advertise.  Defaults to ``0``.
                       For ``UDP_ASSOCIATE`` this **must** be
                       ``request.udp_relay.local_port``.

        Raises:
            ProtocolError:
                * Writer transport is already closing (checked before the
                  double-reply guard so the caller can still send an error).
                * Double-reply guard fired.
            ConfigurationError:
                *bind_host* or *bind_port* are invalid (propagated from
                :func:`~exectunnel.proxy._wire.build_socks5_reply`).
        """
        if self.writer.is_closing():
            raise ProtocolError(
                f"Cannot send SUCCESS reply for {self.host}:{self.port} — "
                "the writer transport is already closing.",
                details={
                    "frame_type": "SOCKS5_REPLY",
                    "expected": "open writer transport",
                },
                hint=(
                    "The SOCKS5 client disconnected before the reply could be sent. "
                    "This is usually benign."
                ),
            )
        self._assert_not_replied()
        self.writer.write(build_socks5_reply(Reply.SUCCESS, bind_host, bind_port))

    def reply_error(self, reply: Reply = Reply.GENERAL_FAILURE) -> None:
        """Queue an error reply into the writer buffer.

        The caller is responsible for ``await writer.drain()`` after this
        method returns.

        Unlike :meth:`reply_success`, this method does **not** guard on
        ``writer.is_closing()`` — error replies must be attempted even on a
        half-closed connection so the client receives a well-formed SOCKS5
        rejection rather than a bare TCP RST.

        Args:
            reply: The :class:`~exectunnel.protocol.Reply` code to send.
                   Defaults to :attr:`~exectunnel.protocol.Reply.GENERAL_FAILURE`.

        Raises:
            ProtocolError:
                Double-reply guard fired.
            ConfigurationError:
                *reply* is not a valid :class:`~exectunnel.protocol.Reply`
                member (propagated from
                :func:`~exectunnel.proxy._wire.build_socks5_reply`).
        """
        self._assert_not_replied()
        # Build the packet first — may raise ConfigurationError before any write.
        packet = build_socks5_reply(reply)
        # Suppress only OSError — RuntimeError (e.g. closed event loop) must
        # propagate so the caller is aware of the abnormal condition.
        with contextlib.suppress(OSError):
            self.writer.write(packet)

    # ── Async reply helpers (write + drain + optional close) ─────────────────

    async def send_reply_success(
        self,
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
    ) -> None:
        """Write and flush a ``SUCCESS`` reply to the SOCKS5 client.

        Does **not** close the writer — the caller owns the connection after a
        successful reply and is responsible for data relay and teardown.

        For ``UDP_ASSOCIATE`` requests, pass
        ``bind_port=request.udp_relay.local_port`` so the client knows which
        port to send datagrams to (RFC 1928 §7).

        Args:
            bind_host: The ``BND.ADDR`` to advertise.  Defaults to
                       ``"127.0.0.1"``.
            bind_port: The ``BND.PORT`` to advertise.  Defaults to ``0``.

        Raises:
            ProtocolError:
                Writer already closing or double-reply guard (propagated from
                :meth:`reply_success`).
            ConfigurationError:
                Invalid *bind_host* / *bind_port* (propagated from
                :func:`~exectunnel.proxy._wire.build_socks5_reply`).
            OSError:
                If the underlying socket write or drain fails.
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

        If :meth:`reply_error` raises (double-reply or invalid reply code),
        the writer is still closed via :meth:`close` to prevent resource leaks.

        Args:
            reply: The :class:`~exectunnel.protocol.Reply` code to send.
                   Defaults to :attr:`~exectunnel.protocol.Reply.GENERAL_FAILURE`.

        Raises:
            ProtocolError:
                Double-reply guard fired (propagated from :meth:`reply_error`).
            ConfigurationError:
                Invalid *reply* code (propagated from
                :func:`~exectunnel.proxy._wire.build_socks5_reply`).
                This is a caller bug and is **not** suppressed.
        """
        try:
            self.reply_error(reply)
        finally:
            # Always close — even if reply_error() raised, the writer must not
            # be left open.  Drain is best-effort; OSError is suppressed.
            with contextlib.suppress(OSError):
                await self.writer.drain()
            await self.close()
