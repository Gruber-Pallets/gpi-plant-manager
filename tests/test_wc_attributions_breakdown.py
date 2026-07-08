"""Pure-logic + DB tests for the breakdown exclusion extension to
wc_attributions.py. Mirrors tests/test_wc_attributions_testing.py's style."""
from datetime import date, datetime, timezone

from zira_dashboard import wc_attributions


def test_breakdown_source_excluded_from_people_by_wc():
    rows = [
        {"id": 1, "wc_name": "Dismantler 2", "person_name": "Juan",
         "start_utc": None, "end_utc": None, "source": "manual"},
        {"id": 2, "wc_name": "Dismantler 2", "person_name": "Benjamin",
         "start_utc": None, "end_utc": None, "source": wc_attributions.BREAKDOWN_SOURCE},
    ]
    out = wc_attributions.people_by_wc("2026-07-08", rows=rows)
    assert out["Dismantler 2"] == ["Juan"]


def test_breakdown_source_excluded_from_creditable_for_day(monkeypatch):
    rows = [
        {"id": 1, "wc_name": "Dismantler 2", "person_name": "Juan",
         "start_utc": None, "end_utc": None, "source": "manual"},
        {"id": 2, "wc_name": "Dismantler 2", "person_name": "Benjamin",
         "start_utc": None, "end_utc": None, "source": wc_attributions.BREAKDOWN_SOURCE},
    ]
    monkeypatch.setattr(wc_attributions, "for_day", lambda day: rows)
    out = wc_attributions.creditable_for_day("2026-07-08")
    assert [r["person_name"] for r in out] == ["Juan"]


def test_breakdown_windows_for_day_groups_by_person_and_wc():
    s1 = datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc)
    e1 = datetime(2026, 7, 8, 13, 30, tzinfo=timezone.utc)
    rows = [
        {"id": 1, "wc_name": "Dismantler 2", "person_name": "Juan",
         "start_utc": s1, "end_utc": e1, "source": wc_attributions.BREAKDOWN_SOURCE},
        {"id": 2, "wc_name": "Dismantler 2", "person_name": "Juan",
         "start_utc": s1, "end_utc": None, "source": "manual"},
    ]
    out = wc_attributions.breakdown_windows_for_day("2026-07-08", rows=rows)
    assert out == {("Juan", "Dismantler 2"): [(s1, e1)]}


def test_add_breakdown_and_cap_and_reopen(monkeypatch):
    from zira_dashboard import db
    calls = {}

    def fake_query(sql, params):
        calls["insert"] = (sql, params)
        return [{"id": 5}]

    monkeypatch.setattr(db, "query", fake_query)
    day = date(2026, 7, 8)
    start = datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc)
    row_id = wc_attributions.add_breakdown(day, "Dismantler 2", "Juan", start, breakdown_id=42)
    assert row_id == 5
    sql, params = calls["insert"]
    assert "source" in sql.lower()
    assert params == (day, "Dismantler 2", "Juan", start, None, wc_attributions.BREAKDOWN_SOURCE, 42)

    def fake_execute(sql, params):
        calls["cap"] = (sql, params)

    monkeypatch.setattr(db, "execute", fake_execute)
    end = datetime(2026, 7, 8, 13, 30, tzinfo=timezone.utc)
    wc_attributions.cap_breakdown(5, end)
    assert calls["cap"][1] == (end, 5, wc_attributions.BREAKDOWN_SOURCE)

    wc_attributions.reopen_breakdown(5)
    assert calls["cap"][1] == (5, wc_attributions.BREAKDOWN_SOURCE)  # last _execute call was reopen


def test_open_breakdown_row(monkeypatch):
    from zira_dashboard import db
    day = date(2026, 7, 8)
    start = datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(db, "query", lambda sql, params: [{"id": 7, "start_utc": start}])
    row = wc_attributions.open_breakdown_row(day, "Dismantler 2", "Juan")
    assert row == {"id": 7, "start_utc": start}

    monkeypatch.setattr(db, "query", lambda sql, params: [])
    assert wc_attributions.open_breakdown_row(day, "Dismantler 2", "Juan") is None


def test_delete_breakdown_rows_for_incident(monkeypatch):
    from zira_dashboard import db
    calls = {}
    monkeypatch.setattr(db, "execute", lambda sql, params: calls.setdefault("args", params))
    wc_attributions.delete_breakdown_rows_for_incident(42)
    assert calls["args"] == (42, wc_attributions.BREAKDOWN_SOURCE)
