from starlette.responses import HTMLResponse

from zira_dashboard import _http_cache


def test_today_response_cache_ttl_is_60s():
    # The staffing-page warmer re-renders today's hot pages every 45s; the
    # today response-cache TTL must sit above that cadence so the cache
    # never goes cold between ticks.
    assert _http_cache._RESPONSE_CACHE_TODAY._ttl == 60.0


def test_stable_response_cache_ttl_is_600s():
    # The skills matrix lives in the stable bucket; its warmer ticks every
    # 300s, so the TTL must sit above that cadence.
    assert _http_cache._RESPONSE_CACHE_STABLE._ttl == 600.0


def test_stable_bucket_round_trip_and_invalidation():
    key = ("test_stable_round_trip",)
    resp = HTMLResponse("<html>stable</html>")
    _http_cache.store_cached_response(key, includes_today=True, response=resp, stable=True)
    assert _http_cache.get_cached_response(key, includes_today=True, stable=True) is not None
    # The stable entry must NOT live in the today bucket...
    assert _http_cache.get_cached_response(key, includes_today=True) is None
    # ...so today-bucket invalidation (autosave etc.) leaves it warm...
    _http_cache.invalidate_today_cache()
    assert _http_cache.get_cached_response(key, includes_today=True, stable=True) is not None
    # ...while the dedicated stable invalidation clears it.
    _http_cache.invalidate_stable_cache()
    assert _http_cache.get_cached_response(key, includes_today=True, stable=True) is None


def test_invalidate_all_clears_stable_bucket():
    key = ("test_stable_all",)
    resp = HTMLResponse("<html>stable</html>")
    _http_cache.store_cached_response(key, includes_today=True, response=resp, stable=True)
    _http_cache.invalidate_all_cache()
    assert _http_cache.get_cached_response(key, includes_today=True, stable=True) is None
