from __future__ import annotations

import ast
import logging
import re
from pathlib import Path


def _clears_whole_tv_registry(sql: str) -> bool:
    normalized = " ".join(sql.lower().split()).strip().rstrip(";").strip()
    if re.fullmatch(r"delete from (?:public\.)?tv_displays", normalized):
        return True
    return bool(
        re.fullmatch(
            r"truncate(?: table)?(?: only)? (?:public\.)?tv_displays"
            r"(?: restart identity)?(?: (?:cascade|restrict))?",
            normalized,
        )
    )


def test_whole_registry_delete_detector_handles_sql_variants():
    destructive_sql = [
        "DELETE" + " FROM tv_displays",
        " delete\nfrom " + "public.tv_displays ; ",
        "TRUNCATE" + " tv_displays",
        "truncate table " + "public.tv_displays restart identity cascade",
    ]

    assert all(_clears_whole_tv_registry(sql) for sql in destructive_sql)
    assert not _clears_whole_tv_registry(
        "DELETE FROM tv_displays WHERE slug LIKE 'safe-test-%'"
    )


def test_postgres_tv_display_tests_never_delete_the_whole_registry():
    """No test may erase the shared TV registry, regardless of SQL spelling."""
    violations = []
    for test_path in sorted(Path(__file__).parent.rglob("*.py")):
        tree = ast.parse(test_path.read_text(), filename=str(test_path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _clears_whole_tv_registry(node.value)
            ):
                violations.append(f"{test_path.name}:{node.lineno}")

    assert violations == []


def test_seed_defaults_backfills_recycling_leaderboard_when_rows_already_exist(monkeypatch):
    from zira_dashboard import app_settings, db, tv_displays_store

    calls: list[tuple[str, tuple | None]] = []
    markers = {
        "tv_displays:seed_recycling_leaderboard_v1": None,
        "tv_displays:seed_new_leaderboard_v1": {"done": True},
        "tv_displays:seed_default_work_center_displays_v2": {"done": True},
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
        "tv_displays:seed_default_work_center_displays_v2": {"done": True},
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
        ("tv_displays:seed_default_work_center_displays_v2", {"done": True})
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

    assert markers["tv_displays:seed_default_work_center_displays_v2"] == {"done": True}
    del rows["repair-2"]

    tv_displays_store.seed_defaults_if_empty()

    assert "repair-2" not in rows


def test_fresh_seed_skips_unavailable_work_centers_without_marking_backfill_done(
    monkeypatch,
    caplog,
):
    from zira_dashboard import app_settings, db, staffing, tv_displays_store

    class _Loc:
        def __init__(self, name):
            self.name = name

    rows = {}
    markers = {}

    def fake_query(sql, params=None):
        if sql.startswith("SELECT 1 FROM tv_displays"):
            return []
        if sql.startswith("SELECT id FROM tv_displays WHERE slug"):
            return [{"id": 1}] if params[0] in rows else []
        raise AssertionError(f"unexpected query: {sql}")

    def fake_execute(sql, params=None):
        if "INSERT INTO tv_displays" in sql:
            rows[params[1]] = params

    monkeypatch.setattr(db, "query", fake_query)
    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(staffing, "LOCATIONS", [_Loc("Repair 1")])
    monkeypatch.setattr(app_settings, "get_setting", markers.get)
    monkeypatch.setattr(app_settings, "set_setting", markers.__setitem__)

    with caplog.at_level(logging.WARNING):
        tv_displays_store.seed_defaults_if_empty()

    assert set(rows) == {
        "recycling",
        "new",
        "recycling-leaderboard",
        "new-leaderboard",
        "repair-1",
    }
    assert "tv_displays:seed_default_work_center_displays_v2" not in markers
    assert "tv_displays seed skipping Dismantler 4" in caplog.text


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


def test_corrective_v2_backfill_runs_after_v1_marker(monkeypatch):
    """The repair must run again after the shared-DB test erased seeded displays."""
    from zira_dashboard import app_settings, db, staffing, tv_displays_store

    class _Loc:
        def __init__(self, name):
            self.name = name

    default_wc_names = [
        "Junior #2", "Repair 1", "Repair 2", "Repair 3", "Dismantler 1",
        "Dismantler 2", "Dismantler 3", "Dismantler 4",
    ]
    existing_rows = [
        {
            "id": 5,
            "name": "Repair 1",
            "slug": "repair-1",
            "kind": "wc",
            "wc_name": "Repair 1",
        }
    ]
    markers = {
        "tv_displays:seed_default_work_center_displays_v1": {"done": True},
        "tv_displays:seed_default_work_center_displays_v2": None,
        "tv_displays:seed_recycling_leaderboard_v1": {"done": True},
        "tv_displays:seed_new_leaderboard_v1": {"done": True},
    }
    inserts = []
    next_id = 6

    def fake_query(sql, params=None):
        if sql.startswith("SELECT 1 FROM tv_displays"):
            return [{"exists": 1}]
        if sql.startswith("SELECT id, name, slug, kind, wc_name FROM tv_displays"):
            return existing_rows
        if "SELECT COALESCE(MAX(sort_order)" in sql:
            return [{"sort_order": 5}]
        if sql.startswith("SELECT id FROM tv_displays WHERE slug"):
            return []
        raise AssertionError(f"unexpected query: {sql}")

    def fake_execute(sql, params=None):
        nonlocal next_id
        inserts.append((sql, params))
        if "INSERT INTO tv_displays" not in sql:
            return
        name, slug, kind, wc_name, _theme, _sort_order = params
        existing_rows.append(
            {
                "id": next_id,
                "name": name,
                "slug": slug,
                "kind": kind,
                "wc_name": wc_name,
            }
        )
        next_id += 1

    monkeypatch.setattr(db, "query", fake_query)
    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(staffing, "LOCATIONS", [_Loc(name) for name in default_wc_names])
    monkeypatch.setattr(app_settings, "get_setting", markers.get)
    monkeypatch.setattr(app_settings, "set_setting", markers.__setitem__)

    tv_displays_store.seed_defaults_if_empty()

    inserted_slugs = {
        params[1]
        for sql, params in inserts
        if "INSERT INTO tv_displays" in sql
    }
    assert inserted_slugs == {
        "junior-2",
        "repair-2",
        "repair-3",
        "dismantler-1",
        "dismantler-2",
        "dismantler-3",
        "dismantler-4",
    }
    assert markers["tv_displays:seed_default_work_center_displays_v2"] == {"done": True}

    existing_rows[:] = [row for row in existing_rows if row["slug"] != "repair-2"]
    inserts.clear()

    tv_displays_store.seed_defaults_if_empty()

    assert inserts == []
    assert not any(row["slug"] == "repair-2" for row in existing_rows)
