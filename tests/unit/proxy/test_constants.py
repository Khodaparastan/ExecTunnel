"""Tests for exectunnel.proxy._constants."""

from exectunnel.proxy._constants import (
    DEFAULT_DROP_WARN_INTERVAL,
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_QUEUE_PUT_TIMEOUT,
    DEFAULT_REQUEST_QUEUE_CAPACITY,
    DEFAULT_UDP_QUEUE_CAPACITY,
    MAX_UDP_PAYLOAD_BYTES,
)


def test_max_udp_payload_bytes_value():
    assert MAX_UDP_PAYLOAD_BYTES == 65_507


def test_max_udp_payload_bytes_derivation():
    assert MAX_UDP_PAYLOAD_BYTES == 65_535 - 20 - 8


def test_default_handshake_timeout_positive():
    assert DEFAULT_HANDSHAKE_TIMEOUT == 30.0
    assert DEFAULT_HANDSHAKE_TIMEOUT > 0


def test_default_request_queue_capacity_at_least_one():
    assert DEFAULT_REQUEST_QUEUE_CAPACITY == 256
    assert DEFAULT_REQUEST_QUEUE_CAPACITY >= 1


def test_default_udp_queue_capacity_at_least_one():
    assert DEFAULT_UDP_QUEUE_CAPACITY == 2_048
    assert DEFAULT_UDP_QUEUE_CAPACITY >= 1


def test_default_drop_warn_interval_at_least_one():
    assert DEFAULT_DROP_WARN_INTERVAL == 1_000
    assert DEFAULT_DROP_WARN_INTERVAL >= 1


def test_default_queue_put_timeout_positive():
    assert DEFAULT_QUEUE_PUT_TIMEOUT == 5.0
    assert DEFAULT_QUEUE_PUT_TIMEOUT > 0


def test_default_host_is_loopback():
    assert DEFAULT_HOST == "127.0.0.1"


def test_default_port_in_valid_range():
    assert DEFAULT_PORT == 1080
    assert 1 <= DEFAULT_PORT <= 65_535
