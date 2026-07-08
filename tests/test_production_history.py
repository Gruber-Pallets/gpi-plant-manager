from datetime import date

from zira_dashboard import production_history
from zira_dashboard.production_history import attribute_for_day


def test_attribute_for_day_empty_schedule_returns_empty():
    out = attribute_for_day(
        assignments={},
        wc_totals={},
        elapsed_minutes=480,
    )
    assert out == {}


def test_solo_operator_gets_full_credit():
    out = attribute_for_day(
        assignments={"Repair 1": ["Christian"]},
        wc_totals={"Repair 1": (80, 12)},
        elapsed_minutes=480,
    )
    assert out == {
        "Christian": {
            "Repair 1": {
                "units": 80.0,
                "downtime": 12.0,
                "hours": 8.0,
                "days_worked": 1,
                "excluded_minutes": 0.0,
            }
        }
    }


def test_two_operators_split_evenly():
    out = attribute_for_day(
        assignments={"Trim Saw 1": ["Iban", "Porfirio"]},
        wc_totals={"Trim Saw 1": (200, 6)},
        elapsed_minutes=480,
    )
    assert out["Iban"]["Trim Saw 1"]["units"] == 100.0
    assert out["Iban"]["Trim Saw 1"]["downtime"] == 3.0
    assert out["Porfirio"]["Trim Saw 1"]["units"] == 100.0
    assert out["Porfirio"]["Trim Saw 1"]["downtime"] == 3.0
    assert out["Iban"]["Trim Saw 1"]["days_worked"] == 1
    assert out["Porfirio"]["Trim Saw 1"]["days_worked"] == 1


def test_three_operators_split_evenly():
    out = attribute_for_day(
        assignments={"Hand Build #1": ["A", "B", "C"]},
        wc_totals={"Hand Build #1": (90, 9)},
        elapsed_minutes=480,
    )
    for n in ("A", "B", "C"):
        assert out[n]["Hand Build #1"]["units"] == 30.0
        assert out[n]["Hand Build #1"]["downtime"] == 3.0


from zira_dashboard.staffing import TIME_OFF_KEY


def test_time_off_excluded():
    out = attribute_for_day(
        assignments={
            "Repair 1": ["Christian"],
            TIME_OFF_KEY: ["Iban", "Lupe"],
        },
        wc_totals={"Repair 1": (80, 12)},
        elapsed_minutes=480,
    )
    assert "Christian" in out
    assert "Iban" not in out
    assert "Lupe" not in out


def test_unmetered_wc_credits_day_but_zero_units():
    # Hand Build has no meter_id, so no entry in wc_totals.
    out = attribute_for_day(
        assignments={"Hand Build #1": ["Lupe", "Carlos"]},
        wc_totals={},  # empty — no Zira data for this WC
        elapsed_minutes=480,
    )
    assert out["Lupe"]["Hand Build #1"]["units"] == 0.0
    assert out["Lupe"]["Hand Build #1"]["downtime"] == 0.0
    assert out["Lupe"]["Hand Build #1"]["days_worked"] == 1
    assert out["Carlos"]["Hand Build #1"]["days_worked"] == 1


from zira_dashboard.production_history import attribute_for_range


def test_range_sums_units_and_days():
    day1 = {
        "Christian": {"Repair 1": {"units": 80.0, "downtime": 12.0, "hours": 8.0, "days_worked": 1}},
    }
    day2 = {
        "Christian": {"Repair 1": {"units": 95.0, "downtime": 5.0,  "hours": 8.0, "days_worked": 1}},
    }
    day3 = {
        "Christian": {"Repair 4": {"units": 70.0, "downtime": 0.0, "hours": 8.0, "days_worked": 1}},
        "Adrian":    {"Repair 1": {"units": 75.0, "downtime": 8.0, "hours": 8.0, "days_worked": 1}},
    }
    out = attribute_for_range([day1, day2, day3])
    assert out["Christian"]["Repair 1"]["units"] == 175.0
    assert out["Christian"]["Repair 1"]["days_worked"] == 2
    assert out["Christian"]["Repair 4"]["days_worked"] == 1
    assert out["Adrian"]["Repair 1"]["days_worked"] == 1
    assert out["Adrian"]["Repair 1"]["units"] == 75.0


def test_attribution_for_today_drafts_return_empty(monkeypatch):
    """Today's drafts (published=False) don't count — supervisor may be
    mid-edit and partial assignments would skew live leaderboards."""
    from datetime import datetime, timezone
    from zira_dashboard import staffing
    from zira_dashboard.production_history import attribution_for

    today = datetime.now(timezone.utc).date()
    fake_sched = staffing.Schedule(
        day=today,
        published=False,
        assignments={"Repair 1": ["Christian"]},
    )
    monkeypatch.setattr(staffing, "load_schedule", lambda d: fake_sched)
    out = attribution_for(today, client=object())
    assert out == {}


def test_attribution_for_past_unpublished_day_uses_assignments(monkeypatch):
    """Past days use saved assignments even if never formally published —
    by the time a day is in the past, the saved draft is the closest
    record of what actually happened."""
    from zira_dashboard import staffing
    from zira_dashboard.production_history import attribution_for

    fake_sched = staffing.Schedule(
        day=date(2026, 4, 27),
        published=False,  # never clicked Publish — but units still ran
        assignments={"Repair 1": ["Christian"]},
    )
    monkeypatch.setattr(staffing, "load_schedule", lambda d: fake_sched)
    monkeypatch.setattr(production_history, "_fetch_wc_totals",
                        lambda client, day: {"Repair 1": (95, 5)})
    monkeypatch.setattr(production_history, "_elapsed_minutes_for", lambda d: 480)

    out = attribution_for(date(2026, 4, 27), client=object())
    assert out["Christian"]["Repair 1"]["units"] == 95.0
    assert out["Christian"]["Repair 1"]["downtime"] == 5.0


def test_attribution_for_uses_published_assignments(monkeypatch):
    from zira_dashboard import staffing
    from zira_dashboard.production_history import attribution_for

    fake_sched = staffing.Schedule(
        day=date(2026, 4, 27),
        published=True,
        assignments={"Trim Saw 1": ["Iban", "Porfirio"]},
    )
    monkeypatch.setattr(staffing, "load_schedule", lambda d: fake_sched)

    # Stub the per-day Zira lookup so we don't hit the real API.
    def fake_wc_totals(client, day):
        return {"Trim Saw 1": (200, 6)}
    monkeypatch.setattr(production_history, "_fetch_wc_totals", fake_wc_totals)
    monkeypatch.setattr(production_history, "_elapsed_minutes_for", lambda d: 480)

    out = attribution_for(date(2026, 4, 27), client=object())
    assert out["Iban"]["Trim Saw 1"]["units"] == 100.0
    assert out["Porfirio"]["Trim Saw 1"]["units"] == 100.0


from zira_dashboard.production_history import rank_by_category


def test_rank_by_category_filters_to_category_wcs_and_threshold():
    range_out = {
        "Christian": {"Repair 1": {"units": 480.0, "downtime": 30.0, "hours": 40.0, "days_worked": 5}},
        "Adrian":    {"Repair 1": {"units": 250.0, "downtime": 10.0, "hours": 16.0, "days_worked": 2}},  # below threshold
        "Eulogio":   {"Repair 4": {"units": 385.0, "downtime": 18.0, "hours": 40.0, "days_worked": 5}},
        "Iban":      {"Trim Saw 1": {"units": 600.0, "downtime": 12.0, "hours": 40.0, "days_worked": 5}},  # different category
    }
    expected_per_wc = {"Repair 1": 100, "Repair 4": 100}

    rows = rank_by_category(
        range_out,
        category_wcs=["Repair 1", "Repair 2", "Repair 3", "Repair 4", "Repair 5"],
        expected_units_per_day_by_wc=expected_per_wc,
        min_days=3,
    )
    names = [r["name"] for r in rows]
    assert names == ["Christian", "Eulogio"]
    assert "Adrian" not in names
    assert "Iban" not in names
    assert rows[0]["pct_of_target"] == 96.0


import os
from datetime import date as _date

import pytest


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="Postgres test needs DATABASE_URL",
)
def test_daily_records_reads_from_production_daily():
    """daily_records must return rows from production_daily without
    calling production_history.attribution_for at all."""
    from zira_dashboard import db, precompute

    db.init_pool(); db.bootstrap_schema()
    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s",
               (_date(2099, 7, 1), _date(2099, 7, 31)))
    precompute.upsert_production_daily([
        {"day": _date(2099, 7, 1), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 10.0, "downtime": 1.0, "hours": 4.0,
         "days_worked": 1.0},
        {"day": _date(2099, 7, 2), "emp_id": "E2", "name": "Bob",
         "wc_name": "WC2", "units": 20.0, "downtime": 2.0, "hours": 8.0,
         "days_worked": 1.0},
    ])

    def poison(*a, **k):
        raise AssertionError("attribution_for should not be called")

    saved = production_history.attribution_for
    production_history.attribution_for = poison
    try:
        out = production_history.daily_records(
            _date(2099, 7, 1), _date(2099, 7, 31)
        )
    finally:
        production_history.attribution_for = saved

    by_day = {(r["day"], r["person"]): r for r in out}
    assert by_day[(_date(2099, 7, 1), "Alice")]["units"] == 10.0
    assert by_day[(_date(2099, 7, 2), "Bob")]["units"] == 20.0

    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s",
               (_date(2099, 7, 1), _date(2099, 7, 31)))


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="Postgres test needs DATABASE_URL",
)
def test_attribution_range_reads_from_production_daily():
    from zira_dashboard import db, precompute

    db.init_pool(); db.bootstrap_schema()
    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s",
               (_date(2099, 8, 1), _date(2099, 8, 31)))
    precompute.upsert_production_daily([
        {"day": _date(2099, 8, 1), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 10.0, "downtime": 1.0, "hours": 4.0,
         "days_worked": 1.0},
        {"day": _date(2099, 8, 2), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 5.0,  "downtime": 0.5, "hours": 2.0,
         "days_worked": 1.0},
    ])

    def poison(*a, **k):
        raise AssertionError("attribution_for should not be called")
    saved = production_history.attribution_for
    production_history.attribution_for = poison
    try:
        out = production_history.attribution_range(
            _date(2099, 8, 1), _date(2099, 8, 31)
        )
    finally:
        production_history.attribution_for = saved

    assert out["Alice"]["WC1"]["units"] == 15.0
    assert out["Alice"]["WC1"]["hours"] == 6.0
    assert out["Alice"]["WC1"]["days_worked"] == 2.0

    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s",
               (_date(2099, 8, 1), _date(2099, 8, 31)))


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="Postgres test needs DATABASE_URL",
)
def test_attribution_per_day_reads_from_production_daily():
    from zira_dashboard import db, precompute

    db.init_pool(); db.bootstrap_schema()
    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s",
               (_date(2099, 9, 1), _date(2099, 9, 30)))
    precompute.upsert_production_daily([
        {"day": _date(2099, 9, 1), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 10.0, "downtime": 1.0, "hours": 4.0,
         "days_worked": 1.0},
        {"day": _date(2099, 9, 2), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 5.0,  "downtime": 0.0, "hours": 2.0,
         "days_worked": 1.0},
        {"day": _date(2099, 9, 1), "emp_id": "E2", "name": "Bob",
         "wc_name": "WC2", "units": 20.0, "downtime": 0.0, "hours": 8.0,
         "days_worked": 1.0},
    ])

    def poison(*a, **k):
        raise AssertionError("attribution_for should not be called")
    saved = production_history.attribution_for
    production_history.attribution_for = poison
    try:
        out = production_history.attribution_per_day(
            _date(2099, 9, 1), _date(2099, 9, 30)
        )
    finally:
        production_history.attribution_for = saved

    # Date-ascending order is part of the contract — callers rely on it.
    days_emitted = [d for d, _ in out]
    assert days_emitted == sorted(days_emitted)

    by_day = dict(out)
    assert by_day[_date(2099, 9, 1)]["Alice"]["WC1"]["units"] == 10.0
    assert by_day[_date(2099, 9, 1)]["Bob"]["WC2"]["units"] == 20.0
    assert by_day[_date(2099, 9, 2)]["Alice"]["WC1"]["units"] == 5.0
    # Every day in range present (even empty days), so callers can
    # distinguish "checked and empty" from "didn't check".
    assert len(out) == 30

    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s",
               (_date(2099, 9, 1), _date(2099, 9, 30)))


# --- Declared-absent (person, day) production is excluded everywhere ---
# A stray meter unit crediting someone who was manager-declared Absent must
# not count as a worked day (leaderboards) or a stat (player card). Covers
# all three production_daily read paths.

def _insert_absence(db, day, emp_id, name):
    db.execute(
        "INSERT INTO manual_absences (day, emp_id, name) VALUES (%s, %s, %s) "
        "ON CONFLICT DO NOTHING",
        (day, emp_id, name),
    )


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="Postgres test needs DATABASE_URL",
)
def test_daily_records_excludes_declared_absent_days():
    from zira_dashboard import db, precompute

    db.init_pool(); db.bootstrap_schema()
    lo, hi = _date(2099, 10, 1), _date(2099, 10, 31)
    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s", (lo, hi))
    db.execute("DELETE FROM manual_absences WHERE day BETWEEN %s AND %s", (lo, hi))
    precompute.upsert_production_daily([
        # phantom 1-unit day on a date Alice was declared absent
        {"day": _date(2099, 10, 1), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 1.0, "downtime": 0.0, "hours": 0.0,
         "days_worked": 1.0},
        # a normal worked day that must still show
        {"day": _date(2099, 10, 2), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 800.0, "downtime": 1.0, "hours": 8.0,
         "days_worked": 1.0},
    ])
    _insert_absence(db, _date(2099, 10, 1), "E1", "Alice")

    out = production_history.daily_records(lo, hi)
    days = {(r["day"], r["person"]) for r in out}
    assert (_date(2099, 10, 1), "Alice") not in days  # phantom absent day dropped
    assert (_date(2099, 10, 2), "Alice") in days       # real day kept

    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s", (lo, hi))
    db.execute("DELETE FROM manual_absences WHERE day BETWEEN %s AND %s", (lo, hi))


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="Postgres test needs DATABASE_URL",
)
def test_attribution_range_excludes_declared_absent_days():
    from zira_dashboard import db, precompute

    db.init_pool(); db.bootstrap_schema()
    lo, hi = _date(2099, 11, 1), _date(2099, 11, 30)
    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s", (lo, hi))
    db.execute("DELETE FROM manual_absences WHERE day BETWEEN %s AND %s", (lo, hi))
    precompute.upsert_production_daily([
        {"day": _date(2099, 11, 1), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 1.0, "downtime": 0.0, "hours": 0.0,
         "days_worked": 1.0},
        {"day": _date(2099, 11, 2), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 800.0, "downtime": 1.0, "hours": 8.0,
         "days_worked": 1.0},
    ])
    _insert_absence(db, _date(2099, 11, 1), "E1", "Alice")

    out = production_history.attribution_range(lo, hi)
    # Only the non-absent day contributes — no phantom day inflating the totals.
    assert out["Alice"]["WC1"]["units"] == 800.0
    assert out["Alice"]["WC1"]["days_worked"] == 1.0

    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s", (lo, hi))
    db.execute("DELETE FROM manual_absences WHERE day BETWEEN %s AND %s", (lo, hi))


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="Postgres test needs DATABASE_URL",
)
def test_attribution_per_day_excludes_declared_absent_days():
    from zira_dashboard import db, precompute

    db.init_pool(); db.bootstrap_schema()
    lo, hi = _date(2099, 12, 1), _date(2099, 12, 31)
    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s", (lo, hi))
    db.execute("DELETE FROM manual_absences WHERE day BETWEEN %s AND %s", (lo, hi))
    precompute.upsert_production_daily([
        {"day": _date(2099, 12, 1), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 1.0, "downtime": 0.0, "hours": 0.0,
         "days_worked": 1.0},
        {"day": _date(2099, 12, 2), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 800.0, "downtime": 1.0, "hours": 8.0,
         "days_worked": 1.0},
    ])
    _insert_absence(db, _date(2099, 12, 1), "E1", "Alice")

    by_day = dict(production_history.attribution_per_day(lo, hi))
    assert "Alice" not in by_day[_date(2099, 12, 1)]   # absent day empty
    assert by_day[_date(2099, 12, 2)]["Alice"]["WC1"]["units"] == 800.0

    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s", (lo, hi))
    db.execute("DELETE FROM manual_absences WHERE day BETWEEN %s AND %s", (lo, hi))
