"""Shared fixtures for the :mod:`exectunnel.proxy` test suite.

Most generic fixtures (``make_stream_reader``, ``free_port``,
``mock_writer``-shaped factories) live in
:mod:`tests._helpers`; this conftest only carries proxy-package-specific
glue (the ``mock_observability`` autouse patch set).

Required ``pyproject.toml`` configuration::

    [tool.pytest.ini_options]
    asyncio_mode = "auto"
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest

# Re-export commonly imported names so existing
# ``from .conftest import make_stream_reader, free_port`` imports keep
# working without per-test churn.
from tests._helpers import (  # noqa: F401  -- intentional re-export
    free_port,
    make_mock_writer,
    make_stream_reader,
)


@asynccontextmanager
async def _noop_aspan(*args: object, **kwargs: object):
    """Drop-in replacement for ``observability.aspan`` that does nothing."""
    yield None


@pytest.fixture(autouse=True)
def mock_observability():
    """Replace every observability call in the proxy package with no-ops.

    The proxy code paths exercised in this suite are pure-business-
    logic; we don't want metric/trace side-effects bleeding into other
    tests.  This is the single autouse patch list — keep it ordered
    by sub-module to make additions obvious in diffs.
    """
    with (
        patch("exectunnel.proxy.server.metrics_inc"),
        patch("exectunnel.proxy.server.metrics_gauge_inc"),
        patch("exectunnel.proxy.server.metrics_gauge_dec"),
        patch("exectunnel.proxy.server.metrics_observe"),
        patch("exectunnel.proxy.server.start_trace"),
        patch("exectunnel.proxy.server.aspan", _noop_aspan),
        patch("exectunnel.proxy.udp_relay.metrics_inc"),
        patch("exectunnel.proxy.udp_relay.metrics_gauge_inc"),
        patch("exectunnel.proxy.udp_relay.metrics_gauge_dec"),
        patch("exectunnel.proxy.udp_relay.metrics_observe"),
        patch("exectunnel.proxy.tcp_relay.metrics_inc"),
    ):
        yield


@pytest.fixture
def mock_writer() -> MagicMock:
    """Return a fresh :func:`tests._helpers.make_mock_writer`."""
    return make_mock_writer()
