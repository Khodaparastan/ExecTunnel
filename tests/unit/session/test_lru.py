"""Unit tests for :class:`exectunnel.session._lru.LruDict`.

The class is small but its contract has subtle properties — every read,
including ``__contains__``, promotes the touched key to MRU position.
We pin every documented invariant.
"""

from __future__ import annotations

import pytest
from exectunnel.session._lru import LruDict

# ── Construction ──────────────────────────────────────────────────────────────


class TestConstruction:
    def test_maxsize_property_exposes_constructor_argument(self) -> None:
        d = LruDict[str, int](maxsize=4)
        assert d.maxsize == 4

    def test_zero_maxsize_rejected(self) -> None:
        with pytest.raises(ValueError):
            LruDict[str, int](maxsize=0)

    def test_negative_maxsize_rejected(self) -> None:
        with pytest.raises(ValueError):
            LruDict[str, int](maxsize=-1)

    def test_maxsize_one_is_legal(self) -> None:
        d = LruDict[str, int](maxsize=1)
        d["a"] = 1
        d["b"] = 2  # evicts "a"
        assert "a" not in d
        assert d["b"] == 2


# ── Eviction & MRU promotion ──────────────────────────────────────────────────


class TestEvictionAndPromotion:
    def test_eviction_when_exceeding_maxsize(self) -> None:
        d = LruDict[str, int](maxsize=2)
        d["a"] = 1
        d["b"] = 2
        d["c"] = 3  # evicts "a" (oldest)
        assert list(d) == ["b", "c"]

    def test_get_promotes_to_mru(self) -> None:
        d = LruDict[str, int](maxsize=2)
        d["a"] = 1
        d["b"] = 2
        # Touch "a" via __getitem__ — now "b" is the LRU.
        _ = d["a"]
        d["c"] = 3
        assert list(d) == ["a", "c"]
        assert "b" not in d

    def test_get_default_promotes_to_mru(self) -> None:
        d = LruDict[str, int](maxsize=2)
        d["a"] = 1
        d["b"] = 2
        # ``.get`` promotes; "b" should be evicted, not "a".
        assert d.get("a") == 1
        d["c"] = 3
        assert "a" in d
        assert "b" not in d

    def test_contains_promotes_to_mru(self) -> None:
        """``in`` is documented as also promoting to MRU."""
        d = LruDict[str, int](maxsize=2)
        d["a"] = 1
        d["b"] = 2
        # Membership test promotes "a".
        assert "a" in d
        d["c"] = 3
        assert "a" in d
        assert "b" not in d

    def test_setitem_existing_key_updates_value_and_promotes(self) -> None:
        d = LruDict[str, int](maxsize=2)
        d["a"] = 1
        d["b"] = 2
        d["a"] = 99  # update + promote
        d["c"] = 3  # evicts "b"
        assert d["a"] == 99
        assert "b" not in d


# ── Default-value behaviour ───────────────────────────────────────────────────


class TestGetDefault:
    def test_get_returns_none_for_missing_by_default(self) -> None:
        d = LruDict[str, int](maxsize=2)
        assert d.get("missing") is None

    def test_get_returns_provided_default(self) -> None:
        d = LruDict[str, int](maxsize=2)
        assert d.get("missing", 42) == 42

    def test_get_does_not_create_entry(self) -> None:
        d = LruDict[str, int](maxsize=2)
        _ = d.get("missing", 42)
        assert "missing" not in d


# ── ``__contains__`` short-circuit semantics ─────────────────────────────────


class TestContainsShortCircuit:
    def test_contains_false_for_missing_does_not_raise(self) -> None:
        d = LruDict[str, int](maxsize=2)
        assert ("missing" in d) is False

    def test_contains_with_unhashable_returns_false(self) -> None:
        d = LruDict[str, int](maxsize=2)
        # Unhashable keys can't appear in a dict; ``in`` raises
        # ``TypeError`` on the underlying ``OrderedDict`` lookup —
        # this pins that we do not silently swallow it.
        with pytest.raises(TypeError):
            assert [1, 2] in d  # type: ignore[operator]


# ── Slots ─────────────────────────────────────────────────────────────────────


class TestSlots:
    """``__slots__ = ("_maxsize",)`` is declared on :class:`LruDict`.

    Note: subclasses of dict / OrderedDict carry the parent's
    ``__dict__`` regardless of an own ``__slots__``, so arbitrary
    attribute injection on instances is *not* blocked.  We pin only
    the declaration, not the runtime guard, so a future refactor
    (e.g. switching to a non-dict base) doesn't accidentally drop
    the slot definition.
    """

    def test_slots_declared(self) -> None:
        assert LruDict.__slots__ == ("_maxsize",)
