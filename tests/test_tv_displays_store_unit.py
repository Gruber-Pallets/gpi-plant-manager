from __future__ import annotations


def test_seed_defaults_backfills_recycling_leaderboard_when_rows_already_exist(monkeypatch):
    from zira_dashboard import app_settings, db, tv_displays_store

    calls: list[tuple[str, tuple | None]] = []
    marker: dict[str, object | None] = {"value": None}

    def fake_get_setting(key):
        assert key == "tv_displays:seed_recycling_leaderboard_v1"
        return marker["value"]

    def fake_set_setting(key, value):
        assert key == "tv_displays:seed_recycling_leaderboard_v1"
        marker["value"] = value

    def fake_query(sql, params=None):
        calls.append((sql, params))
        if "SELECT 1 FROM tv_displays LIMIT 1" in sql:
            return [{"exists": 1}]
        if "WHERE kind = %s" in sql:
            return []
        if "SELECT COALESCE(MAX(sort_order), -1)" in sql:
            return [{"sort_order": 10}]
        if "SELECT id FROM tv_displays WHERE slug = %s" in sql:
            return []
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(app_settings, "get_setting", fake_get_setting)
    monkeypatch.setattr(app_settings, "set_setting", fake_set_setting)
    monkeypatch.setattr(db, "query", fake_query)
    monkeypatch.setattr(db, "execute", lambda sql, params=None: calls.append((sql, params)))

    tv_displays_store.seed_defaults_if_empty()

    inserted = [
        params for sql, params in calls
        if "INSERT INTO tv_displays" in sql and params is not None
    ]
    assert inserted == [
        (
            "Recycling-leaderboard",
            "recycling-leaderboard",
            "vs_recycling_leaderboard",
            None,
            "dark",
            11,
        )
    ]
    assert marker["value"] == {"done": True}
