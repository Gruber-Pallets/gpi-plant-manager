"""Tests for excluded_minutes flowing through flatten_attribution and
upsert_production_daily."""
from datetime import date

from zira_dashboard.precompute import flatten_attribution


def test_flatten_attribution_carries_excluded_minutes():
    day = date(2026, 7, 8)
    attribution = {
        "Juan": {"Dismantler 2": {"units": 50.0, "downtime": 10.0, "hours": 8.0,
                                   "days_worked": 1, "excluded_minutes": 30.0}},
    }
    rows = flatten_attribution(day, attribution, {"Juan": "emp-1"})
    assert rows[0]["excluded_minutes"] == 30.0


def test_flatten_attribution_defaults_excluded_minutes_when_absent():
    day = date(2026, 7, 8)
    attribution = {
        "Juan": {"Dismantler 2": {"units": 50.0, "downtime": 10.0, "hours": 8.0,
                                   "days_worked": 1}},
    }
    rows = flatten_attribution(day, attribution, {"Juan": "emp-1"})
    assert rows[0]["excluded_minutes"] == 0.0


def test_daily_records_in_range_returns_excluded_minutes(monkeypatch):
    from zira_dashboard import db, precompute
    monkeypatch.setattr(db, "query", lambda sql, params: [
        {"day": date(2026, 7, 8), "person": "Juan", "wc": "Dismantler 2",
         "units": 50.0, "downtime": 10.0, "hours": 8.0, "excluded_minutes": 30.0},
    ])
    rows = precompute.daily_records_in_range(date(2026, 7, 8), date(2026, 7, 8))
    assert rows[0]["excluded_minutes"] == 30.0


def test_normalized_daily_records_in_range_includes_zero_units(monkeypatch):
    from zira_dashboard import db, precompute
    monkeypatch.setattr(db, "query", lambda sql, params: [
        {"day": date(2026, 7, 8), "person": "Juan", "wc": "Dismantler 2",
         "units": 0.0, "downtime": 10.0, "hours": 8.0, "excluded_minutes": 30.0},
    ])
    rows = precompute.normalized_daily_records_in_range(date(2026, 7, 8), date(2026, 7, 8))
    assert rows[0]["units"] == 0.0
    assert rows[0]["hours"] == 8.0


def test_normalized_daily_records_in_range_includes_employee_identity(monkeypatch):
    from zira_dashboard import db, precompute

    captured = {}

    def query(sql, params):
        captured["sql"] = sql
        return [{
            "day": date(2026, 7, 8), "emp_id": "canonical-501", "person": "Jesus G.",
            "wc": "Dismantler 2", "units": 0.0, "downtime": 10.0,
            "hours": 8.0, "excluded_minutes": 30.0,
        }]

    monkeypatch.setattr(db, "query", query)
    rows = precompute.normalized_daily_records_in_range(
        date(2026, 7, 8), date(2026, 7, 8)
    )
    assert rows[0]["emp_id"] == "canonical-501"
    assert "LEFT JOIN production_identity_aliases pia" in captured["sql"]
    assert "COALESCE(pia.canonical_emp_id, pd.emp_id) AS emp_id" in captured["sql"]
