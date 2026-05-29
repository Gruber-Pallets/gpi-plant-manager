from zira_dashboard import _http_cache


def test_today_response_cache_ttl_is_60s():
    # The staffing-page warmer re-renders today's hot pages every 45s; the
    # today response-cache TTL must sit above that cadence so the cache
    # never goes cold between ticks.
    assert _http_cache._RESPONSE_CACHE_TODAY._ttl == 60.0
