"""Bounded LRU dictionary for per-host rate-limit and pacing structures.

Designed for single-threaded asyncio use — not thread-safe.
"""

from __future__ import annotations

import collections
from typing import overload

from ._types import KT, VT, DefaultT


class LruDict(collections.OrderedDict[KT, VT]):
    """``OrderedDict`` that evicts the oldest entry when ``maxsize`` is exceeded.

    All read and mutation operations promote the touched key to the
    most-recently-used (MRU) end so the least-recently-used (LRU) entry is
    always at the front and is the next candidate for eviction.

    Note:
        ``key in lru_dict`` (``__contains__``) **does** promote the key to
        MRU position.  This differs from plain ``dict`` but is consistent with
        the intent of an LRU cache: any access — including membership tests —
        counts as a use.

    Thread-safety:
        Not thread-safe.  Designed exclusively for single-threaded asyncio use.

    Args:
        maxsize: Maximum number of entries to retain.  Must be >= 1.

    Raises:
        ValueError: If *maxsize* is less than 1.
    """

    __slots__ = ("_maxsize",)

    def __init__(self, maxsize: int) -> None:
        super().__init__()
        if maxsize < 1:
            raise ValueError(f"maxsize must be >= 1, got {maxsize!r}")
        self._maxsize = maxsize

    @property
    def maxsize(self) -> int:
        """Maximum number of entries retained before LRU eviction."""
        return self._maxsize

    def __getitem__(self, key: KT) -> VT:
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def __setitem__(self, key: KT, value: VT) -> None:
        super().__setitem__(key, value)
        self.move_to_end(key)
        if len(self) > self._maxsize:
            self.popitem(last=False)

    def __contains__(self, key: object) -> bool:
        """Return ``True`` if *key* is present, promoting it to MRU position."""
        if super().__contains__(key):
            # key is guaranteed to be KT here since it was stored as such.
            self.move_to_end(key)
            return True
        return False

    @overload
    def get(self, key: KT, /) -> VT | None: ...
    @overload
    def get(self, key: KT, default: VT, /) -> VT: ...
    @overload
    def get(self, key: KT, default: DefaultT, /) -> VT | DefaultT: ...

    def get(
        self,
        key: KT,
        default: VT | DefaultT | None = None,
        /,
    ) -> VT | DefaultT | None:
        """Return the value for *key* if present, promoting it in LRU order.

        Unlike the standard ``dict.get``, this override promotes the accessed
        entry to the MRU position so recently-read items are not evicted.

        Args:
            key:     The key to look up.
            default: Value returned when *key* is absent.  Defaults to ``None``.

        Returns:
            The stored value for *key*, or *default* if the key is absent.
        """
        try:
            return self[key]
        except KeyError:
            return default
