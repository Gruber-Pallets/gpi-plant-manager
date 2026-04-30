"""Tiny in-process TTL cache.

Used to amortize repeated computation across requests in the same
process. Each FastAPI worker has its own cache; that's fine for our
single-instance Railway deployment. If the deploy ever scales out,
caches are still safe (independent per worker, just less effective).

Usage:
    cache = TTLCache(ttl_seconds=30)
    def expensive(key):
        return cache.get_or_compute(key, lambda: heavy_call())

For per-arg caching, use the cached() decorator.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Hashable


class TTLCache:
    """Simple thread-safe TTL cache. Last-N eviction via dict order."""

    def __init__(self, ttl_seconds: float, max_entries: int = 256):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: dict[Hashable, tuple[float, Any]] = {}
        self._lock = threading.RLock()

    def get_or_compute(self, key: Hashable, compute: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is not None:
                ts, value = entry
                if now - ts < self._ttl:
                    # Move to end (most-recently-used).
                    self._store.pop(key, None)
                    self._store[key] = (ts, value)
                    return value
        # Compute outside the lock so concurrent misses don't serialize.
        value = compute()
        with self._lock:
            self._store[key] = (now, value)
            # Evict oldest if over capacity.
            while len(self._store) > self._max:
                oldest_key = next(iter(self._store))
                self._store.pop(oldest_key, None)
        return value

    def peek(self, key: Hashable) -> Any | None:
        """Return cached value if present and unexpired; None otherwise.
        Does NOT compute on miss."""
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if now - ts >= self._ttl:
                return None
            # Move to end (most-recently-used).
            self._store.pop(key, None)
            self._store[key] = (ts, value)
            return value

    def set(self, key: Hashable, value: Any) -> None:
        """Store a value with the current timestamp (TTL window starts now)."""
        now = time.monotonic()
        with self._lock:
            self._store[key] = (now, value)
            while len(self._store) > self._max:
                oldest_key = next(iter(self._store))
                self._store.pop(oldest_key, None)

    def invalidate(self, key: Hashable | None = None) -> None:
        with self._lock:
            if key is None:
                self._store.clear()
            else:
                self._store.pop(key, None)
