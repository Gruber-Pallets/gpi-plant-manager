import os
from datetime import date

import pytest


def test_flatten_attribution_empty():
    from zira_dashboard.precompute import flatten_attribution
    out = flatten_attribution(date(2026, 5, 1), {}, name_to_emp_id={})
    assert out == []


def test_flatten_attribution_solo_operator():
    from zira_dashboard.precompute import flatten_attribution
    attribution = {
        "Christian": {
            "Repair 1": {
                "units": 80.0, "downtime": 12.0, "hours": 8.0, "days_worked": 1,
            }
        }
    }
    out = flatten_attribution(
        date(2026, 5, 1), attribution, name_to_emp_id={"Christian": "E123"}
    )
    assert out == [{
        "day": date(2026, 5, 1),
        "emp_id": "E123",
        "name": "Christian",
        "wc_name": "Repair 1",
        "units": 80.0,
        "downtime": 12.0,
        "hours": 8.0,
        "days_worked": 1.0,
    }]


def test_flatten_skips_zero_units():
    from zira_dashboard.precompute import flatten_attribution
    attribution = {"Bob": {"Repair 1": {"units": 0.0, "downtime": 0.0, "hours": 0.0, "days_worked": 0}}}
    out = flatten_attribution(date(2026, 5, 1), attribution, name_to_emp_id={"Bob": "E1"})
    assert out == []


def test_flatten_skips_unknown_name():
    from zira_dashboard.precompute import flatten_attribution
    attribution = {"Ghost": {"Repair 1": {"units": 50.0, "downtime": 0.0, "hours": 4.0, "days_worked": 1}}}
    out = flatten_attribution(date(2026, 5, 1), attribution, name_to_emp_id={})
    assert out == []


pytestmark_pg = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="Postgres tests need a live DATABASE_URL",
)


@pytestmark_pg
def test_upsert_inserts_rows():
    from zira_dashboard import db
    from zira_dashboard.precompute import upsert_production_daily
    db.init_pool(); db.bootstrap_schema()
    db.execute("DELETE FROM production_daily WHERE day = %s", (date(2099, 1, 1),))

    rows = [
        {"day": date(2099, 1, 1), "emp_id": "E1", "name": "A", "wc_name": "WC1",
         "units": 10.0, "downtime": 1.0, "hours": 4.0, "days_worked": 1.0},
    ]
    upsert_production_daily(rows)

    got = db.query(
        "SELECT emp_id, name, wc_name, units, hours FROM production_daily "
        "WHERE day = %s ORDER BY emp_id, wc_name",
        (date(2099, 1, 1),),
    )
    assert len(got) == 1
    assert got[0]["emp_id"] == "E1"
    assert float(got[0]["units"]) == 10.0

    db.execute("DELETE FROM production_daily WHERE day = %s", (date(2099, 1, 1),))


@pytestmark_pg
def test_upsert_overwrites_on_pk_conflict():
    from zira_dashboard import db
    from zira_dashboard.precompute import upsert_production_daily
    db.init_pool(); db.bootstrap_schema()
    db.execute("DELETE FROM production_daily WHERE day = %s", (date(2099, 1, 2),))

    upsert_production_daily([{
        "day": date(2099, 1, 2), "emp_id": "E1", "name": "A", "wc_name": "WC1",
        "units": 10.0, "downtime": 1.0, "hours": 4.0, "days_worked": 1.0,
    }])
    upsert_production_daily([{
        "day": date(2099, 1, 2), "emp_id": "E1", "name": "A", "wc_name": "WC1",
        "units": 99.0, "downtime": 9.0, "hours": 9.0, "days_worked": 1.0,
    }])

    got = db.query(
        "SELECT units FROM production_daily WHERE day = %s",
        (date(2099, 1, 2),),
    )
    assert len(got) == 1
    assert float(got[0]["units"]) == 99.0

    db.execute("DELETE FROM production_daily WHERE day = %s", (date(2099, 1, 2),))


def test_precompute_day_flattens_and_upserts(monkeypatch):
    from zira_dashboard import precompute
    calls = {"attribution": 0, "upsert": []}

    def fake_attribution(d, client):
        calls["attribution"] += 1
        return {
            "Alice": {"WC1": {"units": 50.0, "downtime": 2.0, "hours": 4.0, "days_worked": 1}},
            "Bob":   {"WC1": {"units": 50.0, "downtime": 2.0, "hours": 4.0, "days_worked": 1}},
        }

    def fake_name_map():
        return {"Alice": "E1", "Bob": "E2"}

    def fake_upsert(rows):
        calls["upsert"].extend(rows)
        return len(rows)

    monkeypatch.setattr(
        "zira_dashboard.production_history.attribution_for", fake_attribution
    )
    monkeypatch.setattr(
        "zira_dashboard.stratustime_client.name_to_emp_id_map", fake_name_map
    )
    monkeypatch.setattr(precompute, "upsert_production_daily", fake_upsert)

    result = precompute.precompute_day(date(2026, 5, 1), client=None)

    assert result == {"day": "2026-05-01", "rows_written": 2}
    assert calls["attribution"] == 1
    assert {r["name"] for r in calls["upsert"]} == {"Alice", "Bob"}


def _seed(rows):
    from zira_dashboard import db
    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s",
               (date(2099, 6, 1), date(2099, 6, 30)))
    from zira_dashboard.precompute import upsert_production_daily
    upsert_production_daily(rows)


@pytestmark_pg
def test_sum_by_range_groups_by_name():
    from zira_dashboard import db
    from zira_dashboard.precompute import sum_by_range
    db.init_pool(); db.bootstrap_schema()
    _seed([
        {"day": date(2099, 6, 1), "emp_id": "E1", "name": "Alice", "wc_name": "WC1",
         "units": 10.0, "downtime": 1.0, "hours": 4.0, "days_worked": 1.0},
        {"day": date(2099, 6, 2), "emp_id": "E1", "name": "Alice", "wc_name": "WC1",
         "units": 20.0, "downtime": 2.0, "hours": 4.0, "days_worked": 1.0},
        {"day": date(2099, 6, 1), "emp_id": "E2", "name": "Bob", "wc_name": "WC1",
         "units": 30.0, "downtime": 0.0, "hours": 8.0, "days_worked": 1.0},
    ])

    out = sum_by_range(
        start=date(2099, 6, 1), end=date(2099, 6, 30),
        wc_names=["WC1"], group_by="name",
    )
    by_name = {r["name"]: r for r in out}
    assert float(by_name["Alice"]["units"]) == 30.0
    assert float(by_name["Alice"]["days_worked"]) == 2.0
    assert float(by_name["Bob"]["units"]) == 30.0


@pytestmark_pg
def test_sum_by_name_returns_per_wc_breakdown():
    from zira_dashboard import db
    from zira_dashboard.precompute import sum_by_name
    db.init_pool(); db.bootstrap_schema()
    _seed([
        {"day": date(2099, 6, 1), "emp_id": "E1", "name": "Alice", "wc_name": "WC1",
         "units": 10.0, "downtime": 1.0, "hours": 4.0, "days_worked": 1.0},
        {"day": date(2099, 6, 1), "emp_id": "E1", "name": "Alice", "wc_name": "WC2",
         "units": 5.0,  "downtime": 0.5, "hours": 2.0, "days_worked": 1.0},
    ])

    out = sum_by_name("Alice", start=date(2099, 6, 1), end=date(2099, 6, 30))
    by_wc = {r["wc_name"]: r for r in out}
    assert float(by_wc["WC1"]["units"]) == 10.0
    assert float(by_wc["WC2"]["units"]) == 5.0


@pytestmark_pg
def test_daily_records_in_range_returns_per_row():
    from zira_dashboard import db
    from zira_dashboard.precompute import daily_records_in_range
    db.init_pool(); db.bootstrap_schema()
    _seed([
        {"day": date(2099, 6, 1), "emp_id": "E1", "name": "Alice", "wc_name": "WC1",
         "units": 10.0, "downtime": 1.0, "hours": 4.0, "days_worked": 1.0},
        {"day": date(2099, 6, 2), "emp_id": "E1", "name": "Alice", "wc_name": "WC1",
         "units": 20.0, "downtime": 2.0, "hours": 4.0, "days_worked": 1.0},
    ])

    out = daily_records_in_range(date(2099, 6, 1), date(2099, 6, 30))
    assert len(out) == 2
    out_sorted = sorted(out, key=lambda r: r["day"])
    assert out_sorted[0]["units"] == 10.0
    assert out_sorted[1]["units"] == 20.0
    assert out_sorted[0]["person"] == "Alice"
