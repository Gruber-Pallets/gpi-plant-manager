import os
import pytest

from zira_dashboard import db


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="No DATABASE_URL — Postgres tests need a live database",
)


@pytest.fixture(autouse=True)
def reset_pool():
    db.shutdown_pool()
    yield
    db.shutdown_pool()


def test_init_pool_idempotent():
    db.init_pool()
    db.init_pool()


def test_query_and_execute_round_trip():
    db.init_pool()
    db.execute("CREATE TEMP TABLE _t (id INT, name TEXT)")
    db.execute("INSERT INTO _t VALUES (1, 'alpha'), (2, 'beta')")
    rows = db.query("SELECT id, name FROM _t ORDER BY id")
    assert rows == [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]


def test_cursor_rolls_back_on_exception():
    db.init_pool()
    db.execute("CREATE TEMP TABLE _t (id INT)")
    with pytest.raises(RuntimeError, match="boom"):
        with db.cursor() as cur:
            cur.execute("INSERT INTO _t VALUES (1)")
            raise RuntimeError("boom")
    rows = db.query("SELECT id FROM _t")
    assert rows == []


def test_bootstrap_schema_idempotent():
    db.init_pool()
    db.bootstrap_schema()
    db.bootstrap_schema()
    rows = db.query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name IN "
        "('people', 'skills', 'person_skills', 'work_centers', "
        "'schedules', 'app_settings', 'sync_outbox')"
    )
    names = {r["table_name"] for r in rows}
    for expected in ("people", "skills", "person_skills", "work_centers",
                     "schedules", "app_settings", "sync_outbox"):
        assert expected in names, f"missing {expected}"


def test_bootstrap_creates_precompute_tables():
    db.init_pool()
    db.bootstrap_schema()
    rows = db.query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name IN "
        "('production_daily','today_attendance_cache',"
        "'today_timeoff_cache','today_production_cache')"
    )
    names = {r["table_name"] for r in rows}
    assert names == {
        "production_daily",
        "today_attendance_cache",
        "today_timeoff_cache",
        "today_production_cache",
    }


def test_production_daily_pk_and_indexes():
    db.init_pool()
    db.bootstrap_schema()
    # PK columns
    pk_rows = db.query(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'production_daily'::regclass AND i.indisprimary "
        "ORDER BY a.attname"
    )
    assert {r["attname"] for r in pk_rows} == {"day", "emp_id", "wc_name"}
    # Both secondary indexes exist
    idx_rows = db.query(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'production_daily'"
    )
    idx_names = {r["indexname"] for r in idx_rows}
    assert "idx_production_daily_name_day" in idx_names
    assert "idx_production_daily_wc_day" in idx_names
    # Verify the columns each secondary index covers — guards against
    # rename-but-broken regressions (right name, wrong columns).
    cols_by_idx = db.query(
        "SELECT i.relname AS idx, a.attname AS col, "
        "       array_position(ix.indkey::int[], a.attnum) AS pos "
        "FROM pg_class t "
        "JOIN pg_index ix ON ix.indrelid = t.oid "
        "JOIN pg_class i ON i.oid = ix.indexrelid "
        "JOIN pg_attribute a ON a.attrelid = t.oid "
        "  AND a.attnum = ANY(ix.indkey) "
        "WHERE t.relname = 'production_daily' AND NOT ix.indisprimary "
        "ORDER BY i.relname, pos"
    )
    by_idx: dict = {}
    for r in cols_by_idx:
        by_idx.setdefault(r["idx"], []).append(r["col"])
    assert by_idx["idx_production_daily_name_day"] == ["name", "day"]
    assert by_idx["idx_production_daily_wc_day"] == ["wc_name", "day"]


def test_bootstrap_creates_tv_displays_table():
    db.init_pool()
    db.bootstrap_schema()
    rows = db.query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'tv_displays'"
    )
    assert len(rows) == 1, "tv_displays table missing"
    cols = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'tv_displays'"
    )
    names = {r["column_name"] for r in cols}
    expected = {"id", "name", "slug", "kind", "wc_name", "theme", "sort_order", "created_at", "updated_at"}
    assert expected.issubset(names), f"missing columns: {expected - names}"
    # Slug must be UNIQUE
    idx_rows = db.query(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'tv_displays'"
    )
    assert any("slug" in r["indexdef"] and "UNIQUE" in r["indexdef"].upper() for r in idx_rows), \
        "tv_displays.slug must be UNIQUE"


def test_tv_displays_kind_allows_recycling_leaderboard():
    db.init_pool()
    db.bootstrap_schema()
    db.execute("DELETE FROM tv_displays WHERE slug = 'db-recycling-leaderboard'")
    try:
        db.execute(
            "INSERT INTO tv_displays (name, slug, kind, wc_name, theme) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                "db Recycling-leaderboard",
                "db-recycling-leaderboard",
                "vs_recycling_leaderboard",
                None,
                "dark",
            ),
        )
        rows = db.query(
            "SELECT kind FROM tv_displays WHERE slug = 'db-recycling-leaderboard'"
        )
        assert rows == [{"kind": "vs_recycling_leaderboard"}]
    finally:
        db.execute("DELETE FROM tv_displays WHERE slug = 'db-recycling-leaderboard'")


def test_bootstrap_drops_workshop_tables():
    """The widget workshop / custom dashboards experiment was torn out
    on 2026-05-14. After bootstrap_schema, none of those tables exist."""
    db.init_pool()
    db.bootstrap_schema()
    rows = db.query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name IN "
        "('widget_definitions', 'custom_dashboards', 'dashboard_widgets', "
        " 'tv_dashboard_templates', 'pinned_dashboards')"
    )
    names = {r["table_name"] for r in rows}
    assert names == set(), f"workshop tables should be gone, got: {names}"


def test_tv_displays_custom_dashboard_id_column_dropped():
    """After teardown the FK column on tv_displays is gone too."""
    db.init_pool()
    db.bootstrap_schema()
    cols = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'tv_displays'"
    )
    names = {r["column_name"] for r in cols}
    assert "custom_dashboard_id" not in names


def test_bootstrap_drops_legacy_wc_layouts_and_customizations():
    """The earlier per-WC operator dashboard saved rows under page='wc:{slug}'.
    After the switch to a shared page='operator' key, those rows are
    orphaned — bootstrap drops them on every boot."""
    db.init_pool()
    db.bootstrap_schema()
    # Seed legacy rows the way the old code did.
    db.execute(
        "INSERT INTO widget_layouts (page, layout, updated_at) "
        "VALUES ('wc:repair-1', '[]'::jsonb, now()) "
        "ON CONFLICT (page) DO UPDATE SET layout = EXCLUDED.layout"
    )
    db.execute(
        "INSERT INTO widget_customizations (page, widget_id, customizations) "
        "VALUES ('wc:repair-1', 'kpi-units', '{}'::jsonb) "
        "ON CONFLICT (page, widget_id) DO UPDATE SET customizations = EXCLUDED.customizations"
    )
    # Re-run bootstrap; cleanup should drop both.
    db.bootstrap_schema()
    layouts = db.query("SELECT page FROM widget_layouts WHERE page LIKE 'wc:%'")
    customs = db.query("SELECT page FROM widget_customizations WHERE page LIKE 'wc:%'")
    assert layouts == [], f"legacy widget_layouts rows still present: {layouts}"
    assert customs == [], f"legacy widget_customizations rows still present: {customs}"


def test_time_off_requests_table_bootstraps():
    """Schema bootstrap must include time_off_requests with expected columns."""
    db.init_pool()
    db.bootstrap_schema()
    rows = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'time_off_requests' ORDER BY column_name"
    )
    names = {r["column_name"] for r in rows}
    expected = {
        "id", "person_odoo_id", "originating_kiosk_user", "shape",
        "holiday_status_id", "date_from", "date_to", "hour_from", "hour_to",
        "working_hours_json", "note", "state", "odoo_leave_id",
        "synced_to_odoo", "sync_error", "last_pulled_at", "last_pushed_at",
        "created_at", "updated_at",
    }
    assert expected.issubset(names), f"missing: {expected - names}"


def test_time_off_balances_table_bootstraps():
    db.init_pool()
    db.bootstrap_schema()
    rows = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'time_off_balances'"
    )
    names = {r["column_name"] for r in rows}
    assert {"person_odoo_id", "holiday_status_id", "unit",
            "allocated_total", "taken", "pending", "available",
            "available_practical", "last_pulled_at"}.issubset(names)


def test_scheduler_moves_table_bootstraps():
    db.init_pool()
    db.bootstrap_schema()
    rows = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'scheduler_moves'"
    )
    names = {r["column_name"] for r in rows}
    assert {"id", "person_odoo_id", "occurred_at", "from_bucket",
            "to_bucket", "reason", "schedule_date"}.issubset(names)
