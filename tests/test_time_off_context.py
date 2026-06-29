from datetime import date, timedelta

from zira_dashboard import time_off_context as ctx


def test_department_for_person_returns_distinct_departments(monkeypatch):
    monkeypatch.setattr(ctx.db, "query",
                        lambda sql, params: [{"department": "Recycled"}, {"department": "New"}])
    assert ctx.department_for_person(7) == {"Recycled", "New"}


def test_coverage_for_uses_department_scope_when_known(monkeypatch):
    monkeypatch.setattr(ctx, "department_for_person", lambda pid: {"Recycled"})
    seen = {}
    def fake_query(sql, params):
        seen["sql"] = sql
        seen["params"] = params
        return [{"n": 2}]
    monkeypatch.setattr(ctx.db, "query", fake_query)

    result = ctx.coverage_for(7, date(2026, 6, 30), date(2026, 7, 2))

    assert result == {"count": 2, "scope": "department"}
    assert "ANY(%s)" in seen["sql"]
    assert seen["params"][0] == 7  # exclude requester


def test_coverage_for_falls_back_to_plant_when_no_department(monkeypatch):
    monkeypatch.setattr(ctx, "department_for_person", lambda pid: set())
    monkeypatch.setattr(ctx.db, "query", lambda sql, params: [{"n": 5}])

    result = ctx.coverage_for(7, date(2026, 6, 30), date(2026, 7, 2))

    assert result == {"count": 5, "scope": "plant"}


def test_balance_for_returns_remaining_and_unit(monkeypatch):
    monkeypatch.setattr(ctx.db, "query",
                        lambda sql, params: [{"available": 24.0, "unit": "hours"}])
    assert ctx.balance_for(7, 3) == {"remaining": 24.0, "unit": "hours"}


def test_balance_for_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr(ctx.db, "query", lambda sql, params: [])
    assert ctx.balance_for(7, 3) is None


def test_request_amount_hours_and_days():
    assert ctx.request_amount(
        {"hour_from": 8.0, "hour_to": 12.0, "date_from": date(2026, 7, 3),
         "date_to": date(2026, 7, 3)}) == (4.0, "hours")
    assert ctx.request_amount(
        {"hour_from": None, "hour_to": None, "date_from": date(2026, 6, 30),
         "date_to": date(2026, 7, 2)}) == (3.0, "days")


def _leave(pid, name, df, dt, *, depts=(), shape="full_day", hf=None, ht=None):
    return {"person_odoo_id": pid, "name": name, "date_from": df, "date_to": dt,
            "depts": set(depts), "shape": shape, "hour_from": hf, "hour_to": ht}


def test_coverage_breakdown_peak_is_worst_single_day():
    approved = [
        _leave(2, "Juan", date(2026, 7, 6), date(2026, 7, 7), depts={"Recycling"}),
        _leave(3, "Sam", date(2026, 7, 7), date(2026, 7, 7), depts={"Recycling"}),
        _leave(4, "Dana", date(2026, 7, 6), date(2026, 7, 7), depts={"Shipping"}),
    ]
    result = ctx.coverage_breakdown(
        approved, [], {}, {"Recycling"},
        date(2026, 7, 6), date(2026, 7, 8), requester_odoo_id=1)

    assert result["peak_count"] == 3            # Jul 7 has Juan, Sam, Dana
    assert result["peak_date"] == date(2026, 7, 7)
    assert result["peak_dept_count"] == 2       # Juan + Sam in Recycling
    assert result["scope"] == "department"
    assert result["dept_label"] == "Recycling"
    assert result["severity"] == "warn"
    # only days with someone off appear (Jul 8 had nobody -> skipped)
    assert [d["date"] for d in result["by_day"]] == [date(2026, 7, 6), date(2026, 7, 7)]


def test_coverage_breakdown_excludes_requester_and_dedupes_per_person():
    approved = [_leave(1, "Me", date(2026, 7, 6), date(2026, 7, 6))]      # requester
    pending = [_leave(2, "Juan", date(2026, 7, 6), date(2026, 7, 6))]
    approved += [_leave(2, "Juan", date(2026, 7, 6), date(2026, 7, 6))]   # same person, approved
    result = ctx.coverage_breakdown(
        approved, pending, {}, set(),
        date(2026, 7, 6), date(2026, 7, 6), requester_odoo_id=1)

    assert result["peak_count"] == 1                       # requester excluded, Juan counted once
    assert result["by_day"][0]["people"][0]["pending"] is False  # approved wins over pending


def test_coverage_breakdown_holiday_is_flag_not_count():
    result = ctx.coverage_breakdown(
        [], [], {date(2026, 7, 6): "Independence Day"}, set(),
        date(2026, 7, 6), date(2026, 7, 6), requester_odoo_id=1)

    assert result["peak_count"] == 0
    assert result["has_holiday"] is True
    assert result["severity"] == "clear"
    assert result["by_day"][0]["holiday"] == "Independence Day"


def test_coverage_breakdown_pending_marked_and_partial_label():
    pending = [_leave(2, "Lee", date(2026, 7, 6), date(2026, 7, 6),
                      shape="late_arrival", hf=8.0, ht=9.0)]
    result = ctx.coverage_breakdown(
        [], pending, {}, set(),
        date(2026, 7, 6), date(2026, 7, 6), requester_odoo_id=1)

    person = result["by_day"][0]["people"][0]
    assert person["pending"] is True
    assert person["label"] == "arrives 9:00am"
    assert result["severity"] == "ok"          # 1 off, no same-dept, below plant threshold


def test_coverage_breakdown_zero_is_clear():
    result = ctx.coverage_breakdown(
        [], [], {}, set(), date(2026, 7, 6), date(2026, 7, 6), requester_odoo_id=1)
    assert result == {
        "severity": "clear", "peak_count": 0, "peak_date": None,
        "peak_dept_count": 0, "scope": "plant", "dept_label": None,
        "has_holiday": False, "by_day": [], "more_days": 0,
    }


def test_coverage_breakdown_caps_long_windows():
    # 14 distinct off-days, each one person
    approved = [_leave(100 + i, f"P{i}", date(2026, 7, 1) + timedelta(days=i),
                       date(2026, 7, 1) + timedelta(days=i)) for i in range(14)]
    result = ctx.coverage_breakdown(
        approved, [], {}, set(),
        date(2026, 7, 1), date(2026, 7, 14), requester_odoo_id=1, max_days=10)

    assert len(result["by_day"]) == 10
    assert result["more_days"] == 4
    assert result["peak_count"] == 1           # peak computed over all days before the cap


def test_coverage_breakdowns_for_batches_queries(monkeypatch):
    calls = []

    def fake_query(sql, params):
        calls.append(sql)
        if "state = 'validate'" in sql:
            return [{"person_odoo_id": 2, "name": "Juan", "shape": "full_day",
                     "date_from": date(2026, 7, 6), "date_to": date(2026, 7, 6),
                     "hour_from": None, "hour_to": None}]
        if "IN ('draft'" in sql:
            return [{"person_odoo_id": 3, "name": "Lee", "shape": "full_day",
                     "date_from": date(2026, 7, 6), "date_to": date(2026, 7, 6),
                     "hour_from": None, "hour_to": None}]
        # departments lookup
        return [{"person_odoo_id": 2, "department": "Recycling"},
                {"person_odoo_id": 1, "department": "Recycling"}]

    monkeypatch.setattr(ctx.db, "query", fake_query)
    monkeypatch.setattr(ctx, "_holiday_names", lambda s, e: {})

    rows = [{"id": 55, "person_odoo_id": 1,
             "date_from": date(2026, 7, 6), "date_to": date(2026, 7, 6)}]
    out = ctx.coverage_breakdowns_for(rows)

    assert len(calls) == 3                       # approved, pending, departments
    cov = out[55]
    assert cov["peak_count"] == 2                # Juan + Lee
    assert cov["peak_dept_count"] == 1           # Juan shares requester's Recycling
    assert cov["scope"] == "department"


def test_coverage_breakdowns_for_empty_rows_does_nothing(monkeypatch):
    monkeypatch.setattr(ctx.db, "query",
                        lambda *a: (_ for _ in ()).throw(AssertionError("no query")))
    assert ctx.coverage_breakdowns_for([]) == {}


def test_holiday_names_fan_out_and_fail_soft(monkeypatch):
    from zira_dashboard import odoo_client
    monkeypatch.setattr(odoo_client, "fetch_public_holidays",
                        lambda s, e: [{"name": "July 4", "date_from": "2026-07-03",
                                       "date_to": "2026-07-03"}])
    names = ctx._holiday_names(date(2026, 7, 1), date(2026, 7, 6))
    assert names == {date(2026, 7, 3): "July 4"}

    def boom(s, e):
        raise RuntimeError("odoo down")
    monkeypatch.setattr(odoo_client, "fetch_public_holidays", boom)
    assert ctx._holiday_names(date(2026, 7, 1), date(2026, 7, 6)) == {}
