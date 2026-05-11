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
