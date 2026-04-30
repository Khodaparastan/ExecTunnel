"""Tests for ``exectunnel.transport._types``."""

from __future__ import annotations

from exectunnel.transport._types import TransportHandler, WsSendCallable


class TestWsSendCallable:
    def test_is_runtime_checkable(self):
        assert hasattr(WsSendCallable, "__protocol_attrs__")

    def test_async_function_matches_protocol(self):
        async def send(
            frame: str, *, must_queue: bool = False, control: bool = False
        ) -> None:
            pass

        assert isinstance(send, WsSendCallable)

    def test_callable_object_matches_protocol(self):
        class Sender:
            async def __call__(
                self, frame: str, *, must_queue: bool = False, control: bool = False
            ) -> None:
                pass

        assert isinstance(Sender(), WsSendCallable)

    def test_non_callable_fails_check(self):
        assert not isinstance("str", WsSendCallable)
        assert not isinstance(None, WsSendCallable)
        assert not isinstance(42, WsSendCallable)


class TestTransportHandler:
    def test_is_not_runtime_checkable(self):
        assert not getattr(TransportHandler, "_is_runtime_protocol", False)

    def test_declares_is_closed(self):
        assert "is_closed" in dir(TransportHandler)

    def test_declares_drop_count(self):
        assert "drop_count" in dir(TransportHandler)

    def test_declares_on_remote_closed(self):
        assert "on_remote_closed" in dir(TransportHandler)
