from zira_dashboard import _cache


def test_get_or_compute_ttl_starts_after_compute(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(_cache.time, "monotonic", lambda: clock["now"])
    cache = _cache.TTLCache(ttl_seconds=0.1)

    def compute():
        clock["now"] = 100.2
        return "ready"

    assert cache.get_or_compute("key", compute) == "ready"

    clock["now"] = 100.25
    assert cache.peek("key") == "ready"


def test_peek_prunes_expired_entry(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(_cache.time, "monotonic", lambda: clock["now"])
    cache = _cache.TTLCache(ttl_seconds=1.0)

    cache.set("old", "value")
    clock["now"] = 102.0

    assert cache.peek("old") is None
    assert "old" not in cache._store


def test_invalidation_during_compute_does_not_restore_stale_value():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    for whole_cache in (False, True):
        cache = _cache.TTLCache(ttl_seconds=60)
        started, release = Event(), Event()

        def slow_read():
            started.set()
            assert release.wait(2)
            return "before-save"

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(cache.get_or_compute, "key", slow_read)
            try:
                assert started.wait(2)
                cache.invalidate(None if whole_cache else "key")
            finally:
                release.set()
            assert pending.result(timeout=2) == "before-save"
        assert cache.peek("key") is None
        assert cache.get_or_compute("key", lambda: "after-save") == "after-save"


def test_set_during_compute_preserves_newer_value():
    cache = _cache.TTLCache(ttl_seconds=60)

    def read_then_save():
        cache.set("key", "after-save")
        return "before-save"

    assert cache.get_or_compute("key", read_then_save) == "before-save"
    assert cache.peek("key") == "after-save"


def test_refresh_moves_entry_to_end_before_capacity_eviction():
    cache = _cache.TTLCache(ttl_seconds=60, max_entries=2)
    cache.set("first", 1)
    cache.set("second", 2)
    cache.set("first", 3)
    cache.set("third", 4)
    assert cache.peek("first") == 3
    assert cache.peek("second") is None
