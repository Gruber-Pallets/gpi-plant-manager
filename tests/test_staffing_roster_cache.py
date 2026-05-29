def test_roster_cache_ttl_is_one_hour():
    # Roster changes only on save_roster() / Odoo sync, both of which
    # invalidate the cache directly — so a short TTL just causes cold
    # misses on long-tail pages (player cards, odd date ranges). 1 hour.
    from zira_dashboard import staffing
    assert staffing._ROSTER_CACHE_TTL_SECONDS == 3600.0


def test_invalidate_roster_cache_clears_entry():
    # Invalidation must still work, so a TTL bump can't serve stale data
    # after an edit.
    from zira_dashboard import staffing
    staffing._ROSTER_CACHE = (["sentinel"], float("inf"))
    staffing._invalidate_roster_cache()
    assert staffing._ROSTER_CACHE is None
