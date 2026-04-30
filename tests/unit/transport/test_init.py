"""Tests for the ``exectunnel.transport`` public API surface."""

from __future__ import annotations

from exectunnel import transport


class TestPublicAll:
    def test_all_contains_ws_send_callable(self):
        assert "WsSendCallable" in transport.__all__

    def test_all_contains_transport_handler(self):
        assert "TransportHandler" in transport.__all__

    def test_all_contains_tcp_connection(self):
        assert "TcpConnection" in transport.__all__

    def test_all_contains_udp_flow(self):
        assert "UdpFlow" in transport.__all__

    def test_all_does_not_contain_tcp_registry(self):
        assert "TcpRegistry" not in transport.__all__

    def test_all_does_not_contain_udp_registry(self):
        assert "UdpRegistry" not in transport.__all__

    def test_no_private_modules_in_all(self):
        for name in transport.__all__:
            assert not name.startswith("_"), f"Private name {name!r} found in __all__"


class TestImportability:
    def test_ws_send_callable_importable(self):
        from exectunnel.transport import WsSendCallable

        assert WsSendCallable is not None

    def test_transport_handler_importable(self):
        from exectunnel.transport import TransportHandler

        assert TransportHandler is not None

    def test_tcp_connection_importable(self):
        from exectunnel.transport import TcpConnection

        assert TcpConnection is not None

    def test_udp_flow_importable(self):
        from exectunnel.transport import UdpFlow

        assert UdpFlow is not None

    def test_tcp_registry_importable_as_alias(self):
        from exectunnel.transport import TcpRegistry

        assert TcpRegistry is not None

    def test_udp_registry_importable_as_alias(self):
        from exectunnel.transport import UdpRegistry

        assert UdpRegistry is not None


class TestTypeIdentity:
    def test_tcp_connection_is_concrete_class(self):
        from exectunnel.transport import TcpConnection

        assert isinstance(TcpConnection, type)

    def test_udp_flow_is_concrete_class(self):
        from exectunnel.transport import UdpFlow

        assert isinstance(UdpFlow, type)

    def test_tcp_registry_is_type_alias(self):
        import typing

        from exectunnel.transport import TcpRegistry

        assert isinstance(TcpRegistry, typing.TypeAliasType)

    def test_udp_registry_is_type_alias(self):
        import typing

        from exectunnel.transport import UdpRegistry

        assert isinstance(UdpRegistry, typing.TypeAliasType)

    def test_tcp_connection_satisfies_transport_handler_structurally(self):
        from exectunnel.transport import TcpConnection

        assert hasattr(TcpConnection, "is_closed")
        assert hasattr(TcpConnection, "drop_count")
        assert hasattr(TcpConnection, "on_remote_closed")

    def test_udp_flow_satisfies_transport_handler_structurally(self):
        from exectunnel.transport import UdpFlow

        assert hasattr(UdpFlow, "is_closed")
        assert hasattr(UdpFlow, "drop_count")
        assert hasattr(UdpFlow, "on_remote_closed")
