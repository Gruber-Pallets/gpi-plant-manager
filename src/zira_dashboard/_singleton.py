"""Lock-guarded lazy singleton cache shared by the ``*_store`` modules.

Several stores keep one value (a settings row, a schedule, an overrides map)
cached in process because it's read on hot paths (kiosk punches, per-sample
shift lookups) and only changes on an explicit save. They all need the same
scaffolding: load from the DB on first read, hand back the cached value after,
let save() replace the cache with what it just wrote, and let reload() force a
fresh read. This class owns that scaffolding so each store only supplies its
loader; the stores keep their public module-level API (current/save/reload).
"""

from __future__ import annotations

from threading import RLock
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class CachedSingleton(Generic[T]):
    def __init__(self, load: Callable[[], T]) -> None:
        self._lock = RLock()
        self._load = load
        self._value: T | None = None

    def current(self) -> T:
        """The cached value; loaded on first read."""
        with self._lock:
            if self._value is None:
                self._value = self._load()
            return self._value

    def set(self, value: T) -> None:
        """Replace the cache (a save() just persisted this value)."""
        with self._lock:
            self._value = value

    def reload(self) -> T:
        """Force a fresh load, bypassing the cache."""
        with self._lock:
            self._value = self._load()
            return self._value
