from __future__ import annotations

import asyncio
import collections
from dataclasses import dataclass
from typing import Generic, TypeVar

_KT = TypeVar("_KT")
_VT = TypeVar("_VT")


@dataclass(slots=True)
class PendingConnectState:
    """Tracks one in-flight CONN_OPEN that has not yet received CONN_ACK."""

    host: str
    ack_future: asyncio.Future[str]


class _LruDict(collections.OrderedDict, Generic[_KT, _VT]):
    """OrderedDict that evicts the oldest entry when ``maxsize`` is exceeded."""

    def __init__(self, maxsize: int) -> None:
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key: _KT, value: _VT) -> None:  # type: ignore[override]
        super().__setitem__(key, value)
        self.move_to_end(key)
        if len(self) > self._maxsize:
            self.popitem(last=False)
