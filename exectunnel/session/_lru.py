"""Bounded LRU dict used by TunnelSession for per-host rate-limit structures."""

import collections


class LruDict[KT, VT](collections.OrderedDict):
    """``OrderedDict`` that evicts the oldest entry when ``maxsize`` is exceeded.

    All mutation operations move the touched key to the most-recently-used
    end so the least-recently-used entry is always at the front.

    Thread-safety: not thread-safe — designed for single-threaded asyncio use.
    """

    __slots__ = ("_maxsize",)

    def __init__(self, maxsize: int) -> None:
        super().__init__()
        if maxsize < 1:
            raise ValueError(f"maxsize must be >= 1, got {maxsize!r}")
        self._maxsize = maxsize

    def __setitem__(self, key: KT, value: VT) -> None:  # type: ignore[override]
        super().__setitem__(key, value)
        self.move_to_end(key)
        if len(self) > self._maxsize:
            self.popitem(last=False)
