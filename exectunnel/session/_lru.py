"""Bounded LRU dict used by TunnelSession for per-host rate-limit structures."""

import collections


class LruDict[KT, VT](collections.OrderedDict):
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
        value: VT = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def __setitem__(self, key: KT, value: VT) -> None:  # type: ignore[override]
        super().__setitem__(key, value)
        self.move_to_end(key)
        if len(self) > self._maxsize:
            self.popitem(last=False)

    def get(self, key: KT, default: VT | None = None) -> VT | None:  # type: ignore[override]
        """Return the value for *key* if present, promoting it in LRU order.

        Unlike ``dict.get`` (which bypasses ``__getitem__`` in CPython),
        this override explicitly promotes the entry so that recently-accessed
        items are not evicted.
        """
        try:
            return self[key]
        except KeyError:
            return default
