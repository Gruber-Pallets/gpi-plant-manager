from __future__ import annotations


def test_seed_defaults_backfills_recycling_leaderboard_when_rows_already_exist(monkeypatch):
    from zira_dashboard import app_settings, db, tv_displays_store

    calls: list[tuple[str, tuple | None]] = []
    markers = {
        "tv_displays:seed_recycling_leaderboard_v1": None,
        "tv_displays:seed_new_leaderboard_v1": {"done": True},
        "tv_displays:seed_default_work_center_displays_v1": {"done": True},
    }

    def fake_get_setting(key):
        return markers[key]

    def fake_set_setting(key, value):
        markers[key] = value

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
    assert markers["tv_displays:seed_recycling_leaderboard_v1"] == {"done": True}


def test_seed_defaults_backfills_new_leaderboard_when_rows_already_exist(monkeypatch):
    from zira_dashboard import app_settings, db, tv_displays_store

    calls: list[tuple[str, tuple | None]] = []
    markers = {
        "tv_displays:seed_recycling_leaderboard_v1": {"done": True},
        "tv_displays:seed_new_leaderboard_v1": None,
        "tv_displays:seed_default_work_center_displays_v1": {"done": True},
    }

    monkeypatch.setattr(app_settings, "get_setting", lambda key: markers[key])
    monkeypatch.setattr(
        app_settings,
        "set_setting",
        lambda key, value: markers.__setitem__(key, value),
    )

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

    monkeypatch.setattr(db, "query", fake_query)
    monkeypatch.setattr(db, "execute", lambda sql, params=None: calls.append((sql, params)))

    tv_displays_store.seed_defaults_if_empty()

    inserted = [
        params for sql, params in calls
        if "INSERT INTO tv_displays" in sql and params is not None
    ]
    assert inserted == [
        ("New-Leaderboard", "new-leaderboard", "vs_new_leaderboard", None, "dark", 11)
    ]
    assert markers["tv_displays:seed_new_leaderboard_v1"] == {"done": True}


def test_existing_registry_restores_canonical_url_despite_named_wc_display(monkeypatch):
    """A backup Repair 2 display does not block the standard Repair 2 TV URL."""
    from zira_dashboard import app_settings, db, staffing, tv_displays_store

    class _Loc:
        def __init__(self, name):
            self.name = name

    inserts = []
    settings_updates = []
    default_wc_names = [
        "Junior #2", "Repair 1", "Repair 2", "Repair 3", "Dismantler 1",
        "Dismantler 2", "Dismantler 3", "Dismantler 4",
    ]
    existing_slugs = [
        "junior-2", "repair-1", "repair-2-backup", "repair-3", "dismantler-1",
        "dismantler-2", "dismantler-3", "dismantler-4",
    ]

    def fake_query(sql, params=None):
        if sql.startswith("SELECT 1 FROM tv_displays"):
            return [{"exists": 1}]
        if "SELECT wc_name FROM tv_displays WHERE kind" in sql:
            return [{"wc_name": name} for name in default_wc_names]
        if sql.startswith("SELECT id, name, slug, kind, wc_name FROM tv_displays"):
            return [
                {"id": index, "name": slug, "slug": slug, "kind": "wc", "wc_name": slug}
                for index, slug in enumerate(existing_slugs, start=1)
            ]
        if sql.startswith("SELECT slug FROM tv_displays"):
            return [{"slug": slug} for slug in existing_slugs]
        if "SELECT COALESCE(MAX(sort_order)" in sql:
            return [{"sort_order": 4}]
        if sql.startswith("SELECT id FROM tv_displays WHERE slug"):
            return []
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(db, "query", fake_query)
    monkeypatch.setattr(db, "execute", lambda sql, params=None: inserts.append((sql, params)))
    monkeypatch.setattr(staffing, "LOCATIONS", [_Loc(name) for name in default_wc_names])
    monkeypatch.setattr(
        app_settings,
        "get_setting",
        lambda marker: {"done": True}
        if marker in {
            "tv_displays:seed_recycling_leaderboard_v1",
            "tv_displays:seed_new_leaderboard_v1",
        }
        else None,
    )
    monkeypatch.setattr(
        app_settings,
        "set_setting",
        lambda marker, value: settings_updates.append((marker, value)),
    )

    tv_displays_store.seed_defaults_if_empty()

    assert any(
        params == ("Repair 2", "repair-2", "wc", "Repair 2", "dark", 5)
        for _sql, params in inserts
    )
    assert settings_updates == [
        ("tv_displays:seed_default_work_center_displays_v1", {"done": True})
    ]


def test_fresh_seed_does_not_restore_a_deleted_default_display(monkeypatch):
    """A fresh install records the upgrade marker before any admin can delete a TV."""
    from zira_dashboard import app_settings, db, staffing, tv_displays_store

    class _Loc:
        def __init__(self, name):
            self.name = name

    default_wc_names = [
        "Junior #2", "Repair 1", "Repair 2", "Repair 3", "Dismantler 1",
        "Dismantler 2", "Dismantler 3", "Dismantler 4",
    ]
    rows = {}
    markers = {}

    def fake_query(sql, params=None):
        if sql.startswith("SELECT 1 FROM tv_displays"):
            return [{"exists": 1}] if rows else []
        if sql.startswith("SELECT id FROM tv_displays WHERE slug"):
            return [{"id": 1}] if params[0] in rows else []
        if sql.startswith("SELECT slug FROM tv_displays"):
            return [{"slug": slug} for slug in rows]
        if "SELECT wc_name FROM tv_displays WHERE kind" in sql:
            return [{"wc_name": params[3]} for params in rows.values() if params[2] == "wc"]
        if "SELECT COALESCE(MAX(sort_order)" in sql:
            return [{"sort_order": max((params[5] for params in rows.values()), default=-1)}]
        if "WHERE kind = %s" in sql:
            return [{"exists": 1}]
        raise AssertionError(f"unexpected query: {sql}")

    def fake_execute(sql, params=None):
        if "INSERT INTO tv_displays" in sql:
            rows[params[1]] = params

    monkeypatch.setattr(db, "query", fake_query)
    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(staffing, "LOCATIONS", [_Loc(name) for name in default_wc_names])
    monkeypatch.setattr(app_settings, "get_setting", markers.get)
    monkeypatch.setattr(app_settings, "set_setting", markers.__setitem__)

    tv_displays_store.seed_defaults_if_empty()

    assert markers["tv_displays:seed_default_work_center_displays_v1"] == {"done": True}
    del rows["repair-2"]

    tv_displays_store.seed_defaults_if_empty()

    assert "repair-2" not in rows


def test_existing_registry_migrates_only_the_legacy_junior_default(monkeypatch):
    """The old Junior 2 default keeps working after the WC was renamed Junior #2."""
    from zira_dashboard import app_settings, db, staffing, tv_displays_store

    class _Loc:
        def __init__(self, name):
            self.name = name

    default_wc_names = [
        "Junior #2", "Repair 1", "Repair 2", "Repair 3", "Dismantler 1",
        "Dismantler 2", "Dismantler 3", "Dismantler 4",
    ]
    rows = [
        {"id": 1, "name": "Junior 2", "slug": "junior-2", "kind": "wc", "wc_name": "Junior 2"},
        *[
            {"id": index, "name": name, "slug": name.lower().replace(" ", "-"), "kind": "wc", "wc_name": name}
            for index, name in enumerate(default_wc_names[1:], start=2)
        ],
    ]
    updates = []
    markers = {
        "tv_displays:seed_recycling_leaderboard_v1": {"done": True},
        "tv_displays:seed_new_leaderboard_v1": {"done": True},
    }

    def fake_query(sql, params=None):
        if sql.startswith("SELECT 1 FROM tv_displays"):
            return [{"exists": 1}]
        if sql.startswith("SELECT slug FROM tv_displays"):
            return [{"slug": row["slug"]} for row in rows]
        if sql.startswith("SELECT id, name, slug, kind, wc_name FROM tv_displays"):
            return rows
        if "SELECT COALESCE(MAX(sort_order)" in sql:
            return [{"sort_order": 10}]
        if sql.startswith("SELECT id FROM tv_displays WHERE slug"):
            return []
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(db, "query", fake_query)
    monkeypatch.setattr(db, "execute", lambda sql, params=None: updates.append((sql, params)))
    monkeypatch.setattr(staffing, "LOCATIONS", [_Loc(name) for name in default_wc_names])
    monkeypatch.setattr(app_settings, "get_setting", markers.get)
    monkeypatch.setattr(app_settings, "set_setting", markers.__setitem__)

    tv_displays_store.seed_defaults_if_empty()

    assert updates == [
        (
            "UPDATE tv_displays SET name = %s, wc_name = %s, updated_at = now() "
            "WHERE id = %s AND name = %s AND slug = %s AND kind = %s AND wc_name = %s",
            ("Junior #2", "Junior #2", 1, "Junior 2", "junior-2", "wc", "Junior 2"),
        )
    ]
