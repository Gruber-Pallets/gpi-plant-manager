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


def test_bootstrap_creates_tv_dashboard_templates_table():
    db.init_pool()
    db.bootstrap_schema()
    rows = db.query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'tv_dashboard_templates'"
    )
    assert len(rows) == 1, "tv_dashboard_templates table missing"
    cols = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'tv_dashboard_templates'"
    )
    names = {r["column_name"] for r in cols}
    expected = {"id", "name", "layout_json", "theme", "created_at", "updated_at"}
    assert expected.issubset(names), f"missing columns: {expected - names}"


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


def test_bootstrap_creates_widget_workshop_tables():
    db.init_pool()
    db.bootstrap_schema()
    rows = db.query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name IN "
        "('widget_definitions', 'custom_dashboards', 'dashboard_widgets')"
    )
    names = {r["table_name"] for r in rows}
    assert names == {"widget_definitions", "custom_dashboards", "dashboard_widgets"}, \
        f"missing tables: {names}"


def test_widget_definitions_columns():
    db.init_pool()
    db.bootstrap_schema()
    cols = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'widget_definitions'"
    )
    names = {r["column_name"] for r in cols}
    expected = {"id", "name", "type", "visual_json", "default_data_json", "created_at", "updated_at"}
    assert expected.issubset(names), f"missing columns: {expected - names}"


def test_custom_dashboards_columns_and_slug_unique():
    db.init_pool()
    db.bootstrap_schema()
    cols = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'custom_dashboards'"
    )
    names = {r["column_name"] for r in cols}
    expected = {"id", "name", "slug", "scope_kind", "scope_value", "theme",
                "sort_order", "created_at", "updated_at"}
    assert expected.issubset(names), f"missing columns: {expected - names}"
    idx_rows = db.query(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'custom_dashboards'"
    )
    assert any("slug" in r["indexdef"] and "UNIQUE" in r["indexdef"].upper() for r in idx_rows), \
        "custom_dashboards.slug must be UNIQUE"


def test_dashboard_widgets_columns_and_fks():
    db.init_pool()
    db.bootstrap_schema()
    cols = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'dashboard_widgets'"
    )
    names = {r["column_name"] for r in cols}
    expected = {"id", "dashboard_id", "widget_def_id", "x", "y", "w", "h",
                "data_overrides_json", "sort_order", "created_at"}
    assert expected.issubset(names), f"missing columns: {expected - names}"
    fks = db.query(
        "SELECT constraint_name, delete_rule "
        "FROM information_schema.referential_constraints "
        "WHERE constraint_schema = 'public' "
        "  AND constraint_name LIKE 'dashboard_widgets%'"
    )
    rules = {f["delete_rule"] for f in fks}
    assert "CASCADE" in rules, "dashboard_widgets.dashboard_id should ON DELETE CASCADE"
    assert "RESTRICT" in rules, "dashboard_widgets.widget_def_id should ON DELETE RESTRICT"


def test_tv_displays_kind_allows_custom():
    db.init_pool()
    db.bootstrap_schema()
    db.execute("DELETE FROM tv_displays WHERE slug = 'p3-kind-test'")
    db.execute(
        "INSERT INTO tv_displays (name, slug, kind, custom_dashboard_id, theme) "
        "VALUES ('p3 kind test', 'p3-kind-test', 'custom', NULL, 'dark')"
    )
    rows = db.query("SELECT kind FROM tv_displays WHERE slug = 'p3-kind-test'")
    assert rows and rows[0]["kind"] == "custom"
    db.execute("DELETE FROM tv_displays WHERE slug = 'p3-kind-test'")


def test_tv_displays_has_custom_dashboard_id_column():
    db.init_pool()
    db.bootstrap_schema()
    cols = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'tv_displays'"
    )
    names = {r["column_name"] for r in cols}
    assert "custom_dashboard_id" in names


def test_tv_displays_custom_dashboard_id_fk_on_delete_set_null():
    """Deleting a custom dashboard should NULL the FK on any tv_displays row
    that references it, not cascade-delete the row."""
    db.init_pool()
    db.bootstrap_schema()
    db.execute("DELETE FROM tv_displays WHERE slug = 'p3-fk-test'")
    db.execute("DELETE FROM custom_dashboards WHERE slug = 'p3-fk-dash'")
    rows = db.query(
        "INSERT INTO custom_dashboards (name, slug, scope_kind, scope_value) "
        "VALUES ('p3 fk dash', 'p3-fk-dash', 'wc', 'Repair 1') RETURNING id"
    )
    dash_id = rows[0]["id"]
    db.execute(
        "INSERT INTO tv_displays (name, slug, kind, custom_dashboard_id, theme) "
        "VALUES ('p3 fk test', 'p3-fk-test', 'custom', %s, 'dark')",
        (dash_id,),
    )
    db.execute("DELETE FROM custom_dashboards WHERE id = %s", (dash_id,))
    rows = db.query("SELECT custom_dashboard_id FROM tv_displays WHERE slug = 'p3-fk-test'")
    assert rows and rows[0]["custom_dashboard_id"] is None
    db.execute("DELETE FROM tv_displays WHERE slug = 'p3-fk-test'")
