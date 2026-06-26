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
