"""Bounded LRU dict used by TunnelSession for per-host rate-limit structures."""

from __future__ import annotations

import collections
from typing import TypeVar

KT = TypeVar("KT")
VT = TypeVar("VT")


class LruDict(collections.OrderedDict[KT, VT]):
    """``OrderedDict`` that evicts the oldest entry when ``maxsize`` is exceeded.

    All read and mutation operations move the touched key to the
    most-recently-used end so the least-recently-used entry is always at the
    front.

    Thread-safety: not thread-safe — designed for single-threaded asyncio use.
    """

    __slots__ = ("_maxsize",)

    def __init__(self, maxsize: int) -> None:
        super().__init__()
        if maxsize < 1:
            raise ValueError(f"maxsize must be >= 1, got {maxsize!r}")
        self._maxsize = maxsize

    def __getitem__(self, key: KT) -> VT:
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def __setitem__(self, key: KT, value: VT) -> None:
        super().__setitem__(key, value)
        self.move_to_end(key)
        if len(self) > self._maxsize:
            self.popitem(last=False)

    def get(self, key: KT, default: VT | None = None) -> VT | None:
        """Return the value for *key* if present, promoting it in LRU order.

        Unlike ``dict.get`` in CPython, this override explicitly promotes the
        entry so recently-accessed items are not evicted.
        """
        try:
            return self[key]
        except KeyError:
            return default
