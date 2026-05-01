"""Tests for exectunnel.tcp_relay."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from exectunnel.exceptions import ProtocolError
from exectunnel.protocol import Cmd, Reply
from exectunnel.proxy._wire import build_socks5_reply
from exectunnel.proxy.tcp_relay import TCPRelay
from exectunnel.proxy.udp_relay import UDPRelay


def _make_request(
    cmd: Cmd = Cmd.CONNECT,
    host: str = "example.com",
    port: int = 80,
    writer: asyncio.StreamWriter | None = None,
    reader: asyncio.StreamReader | None = None,
    udp_relay: UDPRelay | None = None,
) -> TCPRelay:
    if writer is None:
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        writer.is_closing.return_value = False
    if reader is None:
        reader = MagicMock(spec=asyncio.StreamReader)
    return TCPRelay(
        cmd=cmd,
        host=host,
        port=port,
        reader=reader,
        writer=writer,
        udp_relay=udp_relay,
    )


class TestTCPRelayProperties:
    def test_is_connect_true_for_connect(self):
        req = _make_request(cmd=Cmd.CONNECT)
        assert req.is_connect is True
        assert req.is_udp_associate is False

    def test_is_udp_associate_true_for_udp_associate(self):
        req = _make_request(cmd=Cmd.UDP_ASSOCIATE)
        assert req.is_udp_associate is True
        assert req.is_connect is False

    def test_replied_false_initially(self):
        req = _make_request()
        assert req.replied is False

    async def test_replied_true_after_success_reply(self):
        """``replied`` flips to ``True`` after a successful reply.

        Originally written as a synchronous test that drove the
        coroutine via ``asyncio.get_event_loop().run_until_complete``;
        that pattern emits ``DeprecationWarning: There is no current
        event loop`` on Python 3.12+, which the project's strict
        ``filterwarnings = ["error"]`` upgrades to a hard failure.
        Converted to ``async def`` so pytest-asyncio supplies the
        running loop.
        """
        req = _make_request()
        await req.send_reply_success()
        assert req.replied is True

    def test_host_and_port_stored(self):
        req = _make_request(host="redis.default.svc", port=6379)
        assert req.host == "redis.default.svc"
        assert req.port == 6379


class TestTCPRelaySendReplySuccess:
    async def test_writes_success_packet(self, mock_writer):
        req = _make_request(writer=mock_writer)
        await req.send_reply_success(bind_host="127.0.0.1", bind_port=1080)
        expected = build_socks5_reply(Reply.SUCCESS, "127.0.0.1", 1080)
        mock_writer.write.assert_called_once_with(expected)

    async def test_drains_after_write(self, mock_writer):
        req = _make_request(writer=mock_writer)
        await req.send_reply_success()
        mock_writer.drain.assert_awaited_once()

    async def test_marks_replied(self, mock_writer):
        req = _make_request(writer=mock_writer)
        await req.send_reply_success()
        assert req.replied is True

    async def test_writer_closing_raises_before_marking_replied(self, mock_writer):
        mock_writer.is_closing.return_value = True
        req = _make_request(writer=mock_writer)
        with pytest.raises(ProtocolError):
            await req.send_reply_success()
        assert req.replied is False

    async def test_double_reply_raises_protocol_error(self, mock_writer):
        req = _make_request(writer=mock_writer)
        await req.send_reply_success()
        with pytest.raises(ProtocolError):
            await req.send_reply_success()

    async def test_default_bind_host_is_loopback(self, mock_writer):
        req = _make_request(writer=mock_writer)
        await req.send_reply_success()
        written_bytes = mock_writer.write.call_args[0][0]
        expected = build_socks5_reply(Reply.SUCCESS, "127.0.0.1", 0)
        assert written_bytes == expected


class TestTCPRelaySendReplyError:
    async def test_writes_general_failure_by_default(self, mock_writer):
        req = _make_request(writer=mock_writer)
        await req.send_reply_error()
        expected = build_socks5_reply(Reply.GENERAL_FAILURE)
        mock_writer.write.assert_called_once_with(expected)

    async def test_writes_custom_reply_code(self, mock_writer):
        req = _make_request(writer=mock_writer)
        await req.send_reply_error(Reply.HOST_UNREACHABLE)
        expected = build_socks5_reply(Reply.HOST_UNREACHABLE)
        mock_writer.write.assert_called_once_with(expected)

    async def test_drains_in_finally(self, mock_writer):
        req = _make_request(writer=mock_writer)
        await req.send_reply_error()
        mock_writer.drain.assert_awaited()

    async def test_closes_writer_in_finally(self, mock_writer):
        req = _make_request(writer=mock_writer)
        await req.send_reply_error()
        mock_writer.close.assert_called()

    async def test_marks_replied(self, mock_writer):
        req = _make_request(writer=mock_writer)
        await req.send_reply_error()
        assert req.replied is True

    async def test_double_reply_skips_write_but_still_closes(self, mock_writer):
        req = _make_request(writer=mock_writer)
        await req.send_reply_success()
        mock_writer.write.reset_mock()
        mock_writer.close.reset_mock()
        with pytest.raises(ProtocolError):
            await req.send_reply_error()
        mock_writer.write.assert_not_called()
        mock_writer.close.assert_called()

    async def test_os_error_on_write_suppressed(self, mock_writer):
        mock_writer.write.side_effect = OSError("broken pipe")
        req = _make_request(writer=mock_writer)
        await req.send_reply_error()

    async def test_closes_udp_relay_in_finally(self, mock_writer):
        relay = MagicMock(spec=UDPRelay)
        req = _make_request(writer=mock_writer, cmd=Cmd.UDP_ASSOCIATE, udp_relay=relay)
        await req.send_reply_error()
        relay.close.assert_called_once()


class TestTCPRelayClose:
    async def test_closes_writer(self, mock_writer):
        req = _make_request(writer=mock_writer)
        await req.close()
        mock_writer.close.assert_called()
        mock_writer.wait_closed.assert_awaited()

    async def test_idempotent_on_os_error(self, mock_writer):
        mock_writer.close.side_effect = OSError("already closed")
        req = _make_request(writer=mock_writer)
        await req.close()

    async def test_suppresses_runtime_error(self, mock_writer):
        mock_writer.wait_closed.side_effect = RuntimeError("event loop closed")
        req = _make_request(writer=mock_writer)
        await req.close()

    async def test_closes_udp_relay(self, mock_writer):
        relay = MagicMock(spec=UDPRelay)
        req = _make_request(writer=mock_writer, cmd=Cmd.UDP_ASSOCIATE, udp_relay=relay)
        await req.close()
        relay.close.assert_called_once()

    async def test_no_relay_close_when_none(self, mock_writer):
        req = _make_request(writer=mock_writer, udp_relay=None)
        await req.close()

    async def test_context_manager_calls_close(self, mock_writer):
        req = _make_request(writer=mock_writer)
        async with req:
            pass
        mock_writer.close.assert_called()

    async def test_context_manager_calls_close_on_exception(self, mock_writer):
        req = _make_request(writer=mock_writer)
        with pytest.raises(ValueError):
            async with req:
                raise ValueError("test error")
        mock_writer.close.assert_called()
