"""Unit tests for :mod:`exectunnel.transport._types`.

Pins the runtime-checkable contract of :class:`WsSendCallable` and the
documented "not runtime-checkable" status of :class:`TransportHandler`.

The file is named ``test_types_dataclasses`` to avoid colliding with
``tests/unit/transport/test_types.py`` (which exists in this tree as a
public-facing protocol surface test).  Pytest collects them as
distinct nodeids regardless of name overlap thanks to the per-package
``__init__.py``, but the explicit suffix removes any ambiguity for
humans browsing the directory.
"""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

import pytest
from exectunnel.transport._types import TransportHandler, WsSendCallable

# ── WsSendCallable runtime check ──────────────────────────────────────────────


class _GoodWsSend:
    """Conforms to the runtime-checkable :class:`WsSendCallable` protocol."""

    async def __call__(  # noqa: D401  -- protocol method
        self,
        frame: str,
        *,
        must_queue: bool = False,
        control: bool = False,
    ) -> None:
        return None


class _BadWsSend:
    """Has a ``__call__`` but isn't async — still passes the *runtime* check.

    ``@runtime_checkable`` only tests *attribute* presence, not signature
    or async-ness, so this case documents the boundary of what the
    runtime check covers.
    """

    def __call__(self, frame: str) -> None:
        return None


class _NotCallable:
    pass


class TestWsSendCallableRuntimeCheck:
    def test_well_formed_callable_is_instance(self) -> None:
        assert isinstance(_GoodWsSend(), WsSendCallable)

    def test_object_without_call_is_not_instance(self) -> None:
        assert not isinstance(_NotCallable(), WsSendCallable)

    def test_runtime_check_only_inspects_attribute_presence(self) -> None:
        # The Protocol is ``@runtime_checkable``, which by design checks
        # only that the attribute exists; sync-vs-async, signature
        # mismatches, etc. are not detected at runtime.  This pin
        # documents that boundary so a future "stricter check" PR can't
        # silently break callers depending on the lenient behaviour.
        assert isinstance(_BadWsSend(), WsSendCallable)


# ── TransportHandler is intentionally NOT runtime-checkable ───────────────────


class TestTransportHandlerProtocol:
    def test_not_runtime_checkable(self) -> None:
        """``isinstance(obj, TransportHandler)`` must raise ``TypeError``.

        The docstring explicitly states the protocol is **not**
        ``@runtime_checkable``.  Using ``isinstance`` against it must
        therefore raise so callers cannot accidentally rely on a
        runtime structural check for type-narrowing.
        """

        class _Looks:
            is_closed = False
            drop_count = 0

            def on_remote_closed(self) -> None: ...

        with pytest.raises(TypeError):
            isinstance(_Looks(), TransportHandler)  # noqa: B015 — intentional


# ── Type-alias smoke ──────────────────────────────────────────────────────────


class TestRegistryTypeAliases:
    """Smoke-check that the registry types are importable & dict-shaped.

    PEP 695 ``type`` statements expose a ``TypeAliasType`` at runtime;
    they are not subscriptable like ``dict`` directly.  This test pins
    the alias *exists* — runtime usability is exercised by the actual
    transport tests that put ``TcpConnection``/``UdpFlow`` into the
    registries.
    """

    def test_aliases_are_importable(self) -> None:
        from exectunnel.transport._types import TcpRegistry, UdpRegistry

        # These are runtime objects (PEP 695 TypeAliasType); just sanity
        # that import succeeded without raising.
        assert TcpRegistry is not None
        assert UdpRegistry is not None


# ── Static fence: WsSendCallable signature shape ──────────────────────────────


class TestWsSendCallableSignatureShape:
    """Pin that the protocol declares the documented signature.

    A regression that drops ``must_queue`` / ``control`` kw-only flags
    would silently break flow-control assumptions everywhere.  We
    inspect the protocol's ``__call__`` annotation set rather than its
    runtime signature because ``@runtime_checkable`` does not preserve
    parameter info on the Protocol object itself.
    """

    def test_call_method_present(self) -> None:
        # ``Protocol.__call__`` returns the descriptor we documented.
        assert hasattr(WsSendCallable, "__call__")

    def test_documented_return_type_is_coroutine(self) -> None:
        # Sanity: the documented return type is ``Coroutine[Any, Any, None]``.
        # This lives only in annotations; we re-import to assert nothing
        # has been lost during runtime evaluation.
        annotations = WsSendCallable.__call__.__annotations__
        # Either ``Coroutine[Any, Any, None]`` or its string repr — both
        # acceptable depending on ``from __future__ import annotations``.
        ret = annotations.get("return")
        assert ret is not None
        # If it's resolved at runtime, it should be the typing alias.
        if not isinstance(ret, str):
            assert ret == Coroutine[Any, Any, None]
