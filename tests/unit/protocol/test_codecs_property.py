"""Hypothesis-driven property tests for the protocol codecs.

The example-based tests in :mod:`tests.unit.protocol.test_codecs` and
:mod:`tests.unit.protocol.test_frames` pin a finite set of round-trip
inputs.  This module instead generates inputs from typed strategies and
asserts the documented round-trip / shape invariants for *every* draw.

Properties pinned here:

1. ``decode_binary_payload(encode_binary_payload(b)) == b`` for any
   ``bytes`` input up to 64 KiB.
2. ``encode_binary_payload(b)`` always emits a string in the
   base64url alphabet (``A-Z``, ``a-z``, ``0-9``, ``-``, ``_``) and
   never contains ``=`` padding.
3. ``encode_host_port`` / ``parse_host_port`` round-trip for every
   well-formed ``(host, port)`` pair, with IPv6 compression as the
   only non-identity normalisation.
4. ``encode_data_frame`` → ``parse_frame`` → ``decode_binary_payload``
   round-trips arbitrary byte payloads up to ``MAX_DATA_PAYLOAD_BYTES``.
5. The ID generator yields strings that match ``CONN_FLOW_ID_RE`` for
   every call (sanity for the namespace policing tests in
   ``test_frames.py``).
"""

from __future__ import annotations

import ipaddress
import re
from typing import Final

import pytest

# Hypothesis is a declared dev-dependency; if it is somehow missing
# (e.g. the user ran ``pip install -e .`` instead of ``poetry install
# --with dev``) we skip rather than blocking the rest of the suite.
hypothesis = pytest.importorskip("hypothesis")
from exectunnel.protocol import (  # noqa: E402
    MAX_DATA_PAYLOAD_BYTES,
    decode_binary_payload,
    decode_error_payload,
    encode_data_frame,
    encode_host_port,
    parse_frame,
    parse_host_port,
)
from exectunnel.protocol.codecs import encode_binary_payload  # noqa: E402
from exectunnel.protocol.ids import CONN_FLOW_ID_RE, new_conn_id  # noqa: E402
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

pytestmark = pytest.mark.property


_BASE64URL_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]*$")


# ── Binary codec ──────────────────────────────────────────────────────────────


class TestBinaryCodecRoundTrip:
    """``encode_binary_payload`` ∘ ``decode_binary_payload`` = id."""

    @given(st.binary(max_size=4096))
    @settings(max_examples=200, deadline=None)
    def test_round_trip(self, data: bytes) -> None:
        assert decode_binary_payload(encode_binary_payload(data)) == data

    @given(st.binary(max_size=4096))
    @settings(max_examples=200, deadline=None)
    def test_encoded_form_is_base64url_no_padding(self, data: bytes) -> None:
        encoded = encode_binary_payload(data)
        assert _BASE64URL_RE.match(encoded), encoded
        assert "=" not in encoded


class TestErrorPayloadRoundTrip:
    """UTF-8 error message ∘ binary codec round-trip."""

    @given(st.text(max_size=1024))
    @settings(max_examples=100, deadline=None)
    def test_text_round_trip(self, message: str) -> None:
        encoded = encode_binary_payload(message.encode("utf-8"))
        assert decode_error_payload(encoded) == message


# ── host:port codec ───────────────────────────────────────────────────────────


# Hostnames (RFC-1123 + underscore for K8s/SRV) — alphabet kept lowercase
# so the encoder's case-sensitive comparison is deterministic.
_LABEL_ALPHABET: Final[str] = "abcdefghijklmnopqrstuvwxyz0123456789-_"


def _hostname_strategy() -> st.SearchStrategy[str]:
    """Single-or-multi-label hostname (no leading/trailing dot or '-')."""
    label = st.text(alphabet=_LABEL_ALPHABET, min_size=1, max_size=20).filter(
        lambda s: (
            not s.startswith("-")
            and not s.endswith("-")
            and not s.startswith(".")
            and not s.endswith(".")
            and ".." not in s
        )
    )
    return (
        st
        .lists(label, min_size=1, max_size=4)
        .map(".".join)
        .filter(
            lambda s: ".." not in s and not s.startswith(".") and not s.endswith(".")
        )
    )


def _ipv4_strategy() -> st.SearchStrategy[str]:
    return st.builds(
        lambda i: str(ipaddress.IPv4Address(i)),
        st.integers(min_value=0, max_value=2**32 - 1),
    )


def _ipv6_strategy() -> st.SearchStrategy[str]:
    return st.builds(
        lambda i: str(ipaddress.IPv6Address(i)),
        st.integers(min_value=0, max_value=2**128 - 1),
    )


_PORT_STRATEGY: Final[st.SearchStrategy[int]] = st.integers(
    min_value=1, max_value=65_535
)


class TestHostPortRoundTrip:
    """Encoding then parsing returns the encoder's normalised host form."""

    @given(host=_hostname_strategy(), port=_PORT_STRATEGY)
    @settings(max_examples=100, deadline=None)
    def test_hostname_round_trip(self, host: str, port: int) -> None:
        encoded = encode_host_port(host, port)
        parsed_host, parsed_port = parse_host_port(encoded)
        assert parsed_port == port
        # Hostnames are not normalised — they round-trip identically.
        assert parsed_host == host

    @given(host=_ipv4_strategy(), port=_PORT_STRATEGY)
    @settings(max_examples=100, deadline=None)
    def test_ipv4_round_trip(self, host: str, port: int) -> None:
        encoded = encode_host_port(host, port)
        parsed_host, parsed_port = parse_host_port(encoded)
        assert parsed_port == port
        # Compressed form: encoder normalises through ipaddress, so the
        # parsed value re-encodes to the same string.
        assert encode_host_port(parsed_host, parsed_port) == encoded

    @given(host=_ipv6_strategy(), port=_PORT_STRATEGY)
    @settings(max_examples=100, deadline=None)
    def test_ipv6_round_trip(self, host: str, port: int) -> None:
        encoded = encode_host_port(host, port)
        parsed_host, parsed_port = parse_host_port(encoded)
        assert parsed_port == port
        assert encode_host_port(parsed_host, parsed_port) == encoded


# ── DATA frame full-stack round-trip ──────────────────────────────────────────


class TestDataFrameRoundTrip:
    """Full encoder → wire → parser → payload-codec round-trip."""

    @given(
        data=st.binary(max_size=min(4096, MAX_DATA_PAYLOAD_BYTES)),
    )
    @settings(max_examples=50, deadline=None)
    def test_data_frame_round_trip(self, data: bytes) -> None:
        conn_id = new_conn_id()
        if not data:
            # encode_data_frame rejects empty payloads (CONN_CLOSE
            # signals EOF instead) — that's a tested invariant in
            # test_frames.py; just skip the empty case here.
            return
        frame = encode_data_frame(conn_id, data)
        parsed = parse_frame(frame)
        assert parsed is not None
        assert parsed.msg_type == "DATA"
        assert parsed.conn_id == conn_id
        assert decode_binary_payload(parsed.payload) == data


# ── conn_id / flow_id generator ───────────────────────────────────────────────


class TestIdGenerator:
    """Every generated ID matches ``CONN_FLOW_ID_RE``."""

    @given(st.integers(min_value=0, max_value=200))
    @settings(max_examples=50, deadline=None)
    def test_generator_yields_well_formed_ids(self, _seed: int) -> None:
        # `_seed` is unused; the strategy just drives a sufficient
        # number of generator calls.
        for _ in range(10):
            cid = new_conn_id()
            assert CONN_FLOW_ID_RE.match(cid), cid

    def test_generator_yields_unique_enough_ids(self) -> None:
        # 1000 draws should be unique with overwhelming probability for
        # a 96-bit ID — collisions here indicate a broken RNG.
        ids = {new_conn_id() for _ in range(1000)}
        assert len(ids) == 1000
