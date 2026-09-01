import os
from datetime import UTC, date, datetime

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
        "excluded_minutes": 0.0,
    }]


def test_flatten_skips_zero_units_without_qualified_time():
    from zira_dashboard.precompute import flatten_attribution
    attribution = {"Bob": {"Repair 1": {"units": 0.0, "downtime": 0.0, "hours": 0.0, "days_worked": 0}}}
    out = flatten_attribution(date(2026, 5, 1), attribution, name_to_emp_id={"Bob": "E1"})
    assert out == []


def test_flatten_keeps_zero_units_with_qualified_time():
    from zira_dashboard.precompute import flatten_attribution
    attribution = {
        "Bob": {
            "Repair 1": {
                "units": 0.0, "downtime": 0.0, "hours": 7.0, "days_worked": 1,
            }
        }
    }
    out = flatten_attribution(date(2026, 5, 1), attribution, name_to_emp_id={"Bob": "E1"})
    assert len(out) == 1
    assert out[0]["units"] == 0.0
    assert out[0]["hours"] == 7.0


def test_flatten_keeps_unknown_name_using_name():
    # A person missing from the name->id map is NOT dropped — they fall back
    # to using their name as the emp_id key, so production is never lost.
    from zira_dashboard.precompute import flatten_attribution
    attribution = {"Ghost": {"Repair 1": {"units": 50.0, "downtime": 0.0, "hours": 4.0, "days_worked": 1}}}
    out = flatten_attribution(date(2026, 5, 1), attribution, name_to_emp_id={})
    assert len(out) == 1
    assert out[0]["emp_id"] == "Ghost"
    assert out[0]["name"] == "Ghost"


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


@pytestmark_pg
def test_precompute_day_replaces_stale_fallback_identity(monkeypatch):
    from zira_dashboard import db, precompute

    test_day = date(2099, 1, 3)
    db.init_pool(); db.bootstrap_schema()
    db.execute("DELETE FROM production_daily WHERE day = %s", (test_day,))

    attribution = {
        "Alice": {
            "WC1": {
                "units": 20.0,
                "downtime": 1.0,
                "hours": 4.0,
                "days_worked": 1.0,
            }
        }
    }
    identity = {}
    monkeypatch.setattr(
        "zira_dashboard.production_history.attribution_for",
        lambda day, client: attribution,
    )
    monkeypatch.setattr(
        "zira_dashboard.attendance.name_to_person_id",
        lambda: dict(identity),
    )

    try:
        precompute.precompute_day(test_day, client=None)
        identity["Alice"] = "E1"
        precompute.precompute_day(test_day, client=None)

        got = db.query(
            "SELECT emp_id, name, wc_name, units FROM production_daily "
            "WHERE day = %s ORDER BY emp_id",
            (test_day,),
        )
        assert [dict(row) for row in got] == [{
            "emp_id": "E1",
            "name": "Alice",
            "wc_name": "WC1",
            "units": 20,
        }]
    finally:
        db.execute("DELETE FROM production_daily WHERE day = %s", (test_day,))


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

    def fake_store(prepared):
        calls["upsert"].extend(prepared.rows)
        calls["replace_days"] = (prepared.day,)
        calls["expected_match_state"] = prepared.expected_match_state
        return len(prepared.rows)

    monkeypatch.setattr(
        "zira_dashboard.production_history.attribution_for", fake_attribution
    )
    monkeypatch.setattr(
        "zira_dashboard.attendance.name_to_person_id", fake_name_map
    )
    monkeypatch.setattr(precompute, "store_prepared_day", fake_store)

    result = precompute.precompute_day(date(2026, 5, 1), client=None)

    assert result == {"day": "2026-05-01", "rows_written": 2}
    assert calls["attribution"] == 1
    assert calls["replace_days"] == (date(2026, 5, 1),)
    assert calls["expected_match_state"] == "legacy"
    assert {r["name"] for r in calls["upsert"]} == {"Alice", "Bob"}


def test_prepare_day_carries_strict_source_fingerprint(monkeypatch):
    from zira_dashboard import precompute, production_history

    attribution = production_history.AttributionResult(
        {
            (101, "Alice"): {
                "WC1": {
                    "units": 1.0,
                    "downtime": 0.0,
                    "hours": 1.0,
                    "days_worked": 1.0,
                }
            }
        },
        is_strict=True,
        source_fingerprint="exact-strict-source",
        request_fingerprint="exact-local-and-meter-source",
    )
    monkeypatch.setattr(
        production_history,
        "attribution_for",
        lambda _day, _client: attribution,
    )
    monkeypatch.setattr(
        "zira_dashboard.attendance.name_to_person_id",
        lambda: {},
    )

    prepared = precompute.prepare_day(date(2026, 5, 2), client=None)

    assert prepared.expected_match_state == "strict"
    assert prepared.source_fingerprint == "exact-strict-source"
    assert prepared.request_fingerprint == "exact-local-and-meter-source"


def test_strict_store_rejects_changed_exact_source_before_writing(monkeypatch):
    from zira_dashboard import (
        attendance_location_policy,
        precompute,
        production_history,
    )

    events = []
    monkeypatch.setattr(
        attendance_location_policy,
        "lock_rollout_decision_cur",
        lambda _cur: events.append("rollout"),
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "match_state_for_day_cur",
        lambda _day, *, cur: "strict",
    )
    monkeypatch.setattr(
        production_history,
        "lock_strict_sources_cur",
        lambda _cur: events.append("sources"),
    )
    monkeypatch.setattr(
        production_history,
        "strict_local_source_fingerprint",
        lambda _day, *, cur: "new-source",
    )
    prepared = precompute.PreparedProductionDay(
        date(2026, 5, 2),
        (),
        date(2026, 5, 2),
        "strict",
        "old-source",
    )

    with pytest.raises(
        production_history.ProductionSourceUnavailable,
        match="source changed",
    ):
        precompute._validate_prepared_match_state_cur(object(), prepared)

    assert events == ["rollout", "sources"]


def test_direct_strict_precompute_routes_through_durable_recalc(monkeypatch):
    from zira_dashboard import attendance_mirror, precompute

    day = date(2026, 5, 3)
    prepared = precompute.PreparedProductionDay(
        day,
        (),
        day,
        "strict",
        "exact-source",
        "exact-request-source",
    )
    calls = []
    monkeypatch.setattr(precompute, "prepare_day", lambda _day, _client: prepared)
    monkeypatch.setattr(
        attendance_mirror,
        "ensure_recalc_queued",
        lambda days, reason, **kwargs: calls.append(
            (tuple(days), reason, kwargs["source_fingerprint"])
        ),
        raising=False,
    )
    monkeypatch.setattr(
        precompute,
        "store_prepared_day",
        lambda _prepared: pytest.fail("direct strict refresh wrote production"),
    )

    result = precompute.precompute_day(day, client=None)

    assert result == {"day": day.isoformat(), "rows_written": 0, "queued": True}
    assert calls == [((day,), "strict_direct_refresh", "exact-request-source")]


def test_direct_strict_queue_failure_never_resets_active_claim(monkeypatch):
    from zira_dashboard import attendance_mirror, precompute

    day = date(2026, 5, 3)
    prepared = precompute.PreparedProductionDay(
        day, (), day, "strict", "exact-source", "exact-request-source"
    )
    monkeypatch.setattr(precompute, "prepare_day", lambda _day, _client: prepared)
    monkeypatch.setattr(
        attendance_mirror,
        "ensure_recalc_queued",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("queue busy")),
    )
    monkeypatch.setattr(
        attendance_mirror,
        "enqueue_recalc",
        lambda *_args, **_kwargs: pytest.fail("active lease was reset"),
    )

    with pytest.raises(RuntimeError, match="queue busy"):
        precompute.precompute_day(day, client=None)


@pytestmark_pg
def test_exact_strict_queue_waits_for_cache_ready_without_resetting_active_request():
    from zira_dashboard import attendance_mirror, db

    day = date(2099, 2, 2)
    completed = datetime(2099, 2, 2, 22, tzinfo=UTC)
    db.init_pool()
    db.bootstrap_schema()
    db.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (day,))
    try:
        assert attendance_mirror.ensure_recalc_queued(
            (day,), "goat_finalization", source_fingerprint="exact-a"
        ) is False
        queued = db.query(
            "SELECT requested_at, completed_at, cache_ready_at, source_fingerprint "
            "FROM attendance_recalc_queue WHERE day = %s",
            (day,),
        )[0]
        assert queued["source_fingerprint"] == "exact-a"

        db.execute(
            "UPDATE attendance_recalc_queue SET completed_at = %s "
            "WHERE day = %s",
            (completed, day),
        )
        assert attendance_mirror.ensure_recalc_queued(
            (day,), "goat_finalization", source_fingerprint="exact-a"
        ) is False
        waiting_for_cache = db.query(
            "SELECT requested_at, completed_at, cache_ready_at, source_fingerprint "
            "FROM attendance_recalc_queue WHERE day = %s",
            (day,),
        )[0]
        assert waiting_for_cache == {
            **queued,
            "completed_at": completed,
        }

        db.execute(
            "UPDATE attendance_recalc_queue SET cache_ready_at = %s WHERE day = %s",
            (completed, day),
        )
        assert attendance_mirror.ensure_recalc_queued(
            (day,), "goat_finalization", source_fingerprint="exact-a"
        ) is True

        assert attendance_mirror.ensure_recalc_queued(
            (day,), "goat_finalization", source_fingerprint="exact-b"
        ) is False
        refreshed = db.query(
            "SELECT completed_at, cache_ready_at, source_fingerprint "
            "FROM attendance_recalc_queue WHERE day = %s",
            (day,),
        )[0]
        assert refreshed == {
            "completed_at": None,
            "cache_ready_at": None,
            "source_fingerprint": "exact-b",
        }
    finally:
        db.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (day,))


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


@pytestmark_pg
def test_normalized_daily_records_in_range_includes_zero_unit_qualified_days():
    from zira_dashboard import db
    from zira_dashboard.precompute import normalized_daily_records_in_range
    db.init_pool(); db.bootstrap_schema()
    _seed([
        {"day": date(2099, 6, 1), "emp_id": "E1", "name": "Alice", "wc_name": "WC1",
         "units": 0.0, "downtime": 0.0, "hours": 7.0, "days_worked": 1.0},
        {"day": date(2099, 6, 2), "emp_id": "E1", "name": "Alice", "wc_name": "WC1",
         "units": 20.0, "downtime": 2.0, "hours": 4.0, "days_worked": 1.0},
    ])

    out = normalized_daily_records_in_range(date(2099, 6, 1), date(2099, 6, 30))
    assert [r["units"] for r in sorted(out, key=lambda r: r["day"])] == [0.0, 20.0]
