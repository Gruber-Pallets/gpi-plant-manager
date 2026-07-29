import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs DATABASE_URL"
)


def test_schema_creates_forklift_tables():
    from zira_dashboard import db
    db.bootstrap_schema()
    rows = db.query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name IN "
        "('forklift_calls_daily','forklift_driver_daily','forklift_name_map')"
    )
    names = {r["table_name"] for r in rows}
    assert names == {"forklift_calls_daily", "forklift_driver_daily", "forklift_name_map"}


from datetime import date

from zira_dashboard import forklift_store


def test_upsert_and_read_calls_daily_roundtrip():
    from zira_dashboard import db
    db.bootstrap_schema()
    day = date(2026, 6, 24)  # a Wednesday
    db.execute("DELETE FROM forklift_calls_daily WHERE day = %s", (day,))
    row = {"day": day, "total_calls": 400, "urgent_calls": 30,
           "overload_count": 5, "neglected_count": 2,
           "by_hour": {"9": {"calls": 70}}, "by_station": {"Prosaw #4": 120},
           "by_skill": {"sk-2": 260}}
    forklift_store.upsert_calls_daily(row)
    forklift_store.upsert_calls_daily({**row, "total_calls": 410})  # idempotent update

    got = forklift_store.calls_daily_for_weekday(2, limit=10)  # 2 == Wednesday
    mine = [r for r in got if r["day"] == day]
    assert mine and mine[0]["total_calls"] == 410
    assert mine[0]["by_hour"]["9"]["calls"] == 70


def test_name_map_overrides():
    from zira_dashboard import db
    db.bootstrap_schema()
    db.execute("DELETE FROM forklift_name_map WHERE forklift_name = %s", ("Luke",))
    db.execute(
        "INSERT INTO forklift_name_map (kind, forklift_name, plant_name) "
        "VALUES ('driver', 'Luke', 'Luke Gruber')"
    )
    assert forklift_store.name_map("driver")["Luke"] == "Luke Gruber"


def test_recent_driver_throughput_pairs_daily_calls_and_on_call_time():
    from zira_dashboard import db

    db.bootstrap_schema()
    d = date(2026, 6, 25)
    db.execute("DELETE FROM forklift_driver_daily WHERE day = %s", (d,))
    db.execute("DELETE FROM forklift_calls_daily WHERE day = %s", (d,))
    forklift_store.upsert_calls_daily({
        "day": d,
        "total_calls": 80,
        "urgent_calls": 0,
        "overload_count": 0,
        "neglected_count": 0,
        "by_hour": {},
        "by_station": {},
        "by_skill": {},
    })
    # Reconstructed driver rows may have valid time while per-driver calls are
    # missing. The daily demand table supplies the numerator.
    forklift_store.upsert_driver_daily([
        {
            "day": d,
            "driver_id": "fk-a",
            "name": "A",
            "calls": 0,
            "on_time": 70,
            "late": 10,
            "avg_ms": 0,
            "max_ms": 0,
            "utilization_pct": 90,
            "on_call_ms": 14_400_000,
            "available_ms": 16_000_000,
        },
    ])

    rate = forklift_store.recent_driver_throughput(days=3650)

    assert rate is not None and 19.0 < rate < 21.0
    db.execute("DELETE FROM forklift_driver_daily WHERE day = %s", (d,))
    db.execute("DELETE FROM forklift_calls_daily WHERE day = %s", (d,))


def test_recent_driver_throughput_none_on_thin_data():
    from zira_dashboard import db
    db.bootstrap_schema()
    db.execute("DELETE FROM forklift_driver_daily")
    assert forklift_store.recent_driver_throughput(days=1) is None


def test_upsert_driver_metrics_fills_ontime_without_clobbering_calls():
    from zira_dashboard import db
    db.bootstrap_schema()
    day = date(2026, 4, 1)
    db.execute("DELETE FROM forklift_driver_daily WHERE day = %s", (day,))
    forklift_store.upsert_driver_daily([
        {"day": day, "driver_id": "d1", "name": "Trent", "calls": 20,
         "on_time": 0, "late": 0, "avg_ms": 50000, "max_ms": 90000,
         "utilization_pct": 0, "on_call_ms": 600000, "available_ms": 0},
    ])
    forklift_store.upsert_driver_metrics([
        {"day": day, "driver_id": "d1", "on_time": 18, "late": 2,
         "on_call_ms": 700000, "available_ms": 3600000, "utilization_pct": 19.4},
    ])
    rows = forklift_store.driver_rows_for_day(day)
    row = next(r for r in rows if r["driver_id"] == "d1")
    assert row["calls"] == 20          # untouched
    assert row["avg_ms"] == 50000      # untouched
    assert row["on_time"] == 18
    assert row["late"] == 2
    assert round(float(row["utilization_pct"]), 1) == 19.4


def test_ontime_history_day_count_counts_only_days_with_ontime():
    from zira_dashboard import db
    db.bootstrap_schema()
    db.execute("DELETE FROM forklift_driver_daily")
    has_ontime = date(2026, 5, 1)
    has_late = date(2026, 5, 2)
    no_ontime = date(2026, 5, 3)
    forklift_store.upsert_driver_daily([
        # day with on_time > 0 -> counts
        {"day": has_ontime, "driver_id": "d1", "name": "A", "calls": 10,
         "on_time": 9, "late": 0, "avg_ms": 1000, "max_ms": 2000,
         "utilization_pct": 50, "on_call_ms": 600000, "available_ms": 1200000},
        # day with late > 0 (on_time 0) -> counts
        {"day": has_late, "driver_id": "d1", "name": "A", "calls": 10,
         "on_time": 0, "late": 3, "avg_ms": 1000, "max_ms": 2000,
         "utilization_pct": 50, "on_call_ms": 600000, "available_ms": 1200000},
        # day with neither -> excluded
        {"day": no_ontime, "driver_id": "d1", "name": "A", "calls": 10,
         "on_time": 0, "late": 0, "avg_ms": 1000, "max_ms": 2000,
         "utilization_pct": 50, "on_call_ms": 600000, "available_ms": 1200000},
    ])
    assert forklift_store.ontime_history_day_count() == 2
    # ontime_history_day_count() scans the whole table, so this test needs the
    # unscoped DELETE above for a clean baseline -> clean up its own rows after,
    # or they leak into other tests' wide-window queries (e.g.
    # recent_driver_throughput(days=3650)) on a shared/persistent DB.
    db.execute(
        "DELETE FROM forklift_driver_daily WHERE day IN (%s, %s, %s)",
        (has_ontime, has_late, no_ontime),
    )
