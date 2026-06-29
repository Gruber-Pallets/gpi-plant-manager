from zira_dashboard import forklift_backfill

# 1782484200000 = 2026-06-26 09:30 America/Chicago
# 1782565200000 = 2026-06-27 08:00 America/Chicago
COMPLETIONS = [
    {"id": "c1", "workstationName": "Prosaw #4", "completedBy": "fk-1",
     "createdAt": 1782484200000, "responseMs": 120000, "handlingMs": 300000},
    {"id": "c2", "workstationName": "Junior #3", "completedBy": "fk-2",
     "createdAt": 1782565200000, "responseMs": 60000, "handlingMs": 90000},
]
DRIVERS = [
    {"id": "fk-1", "name": "Trent", "isOverloadResponder": True},
    {"id": "fk-2", "name": "Louie", "isOverloadResponder": False},
]


def test_backfill_history_aggregates_and_upserts(monkeypatch):
    captured = {"calls": [], "drivers": None, "settings": {}}
    monkeypatch.setattr(forklift_backfill.forklift_client, "fetch_completions",
                        lambda since=0: COMPLETIONS)
    monkeypatch.setattr(forklift_backfill.forklift_client, "fetch_drivers",
                        lambda: DRIVERS)
    monkeypatch.setattr(forklift_backfill.forklift_store, "upsert_calls_daily",
                        lambda row: captured["calls"].append(row))
    monkeypatch.setattr(forklift_backfill.forklift_store, "upsert_driver_daily",
                        lambda rows: captured.update(drivers=rows) or len(rows))
    monkeypatch.setattr(forklift_backfill.app_settings, "set_setting",
                        lambda k, v: captured["settings"].update({k: v}))

    out = forklift_backfill.backfill_history()

    assert out == {"days": 2, "drivers": 2, "calls": 2}
    # one calls row per local day
    assert {r["day"].isoformat() for r in captured["calls"]} == {"2026-06-26", "2026-06-27"}
    # overload responder names persisted, like snapshot_today
    assert captured["settings"]["forklift_overload_responders"] == ["Trent"]
    # driver name resolved from id->name
    names = {r["driver_id"]: r["name"] for r in captured["drivers"]}
    assert names == {"fk-1": "Trent", "fk-2": "Louie"}


def test_backfill_history_passes_since(monkeypatch):
    seen = {}

    def record_since(since=0):
        seen["since"] = since
        return []

    monkeypatch.setattr(forklift_backfill.forklift_client, "fetch_completions", record_since)
    monkeypatch.setattr(forklift_backfill.forklift_client, "fetch_drivers", lambda: [])
    monkeypatch.setattr(forklift_backfill.forklift_store, "upsert_calls_daily", lambda row: None)
    monkeypatch.setattr(forklift_backfill.forklift_store, "upsert_driver_daily", lambda rows: 0)
    monkeypatch.setattr(forklift_backfill.app_settings, "set_setting", lambda k, v: None)

    forklift_backfill.backfill_history(since=12345)
    assert seen["since"] == 12345


def test_backfill_history_swallows_errors(monkeypatch):
    def boom(since=0):
        raise forklift_backfill.forklift_client.ForkliftError("no key")

    monkeypatch.setattr(forklift_backfill.forklift_client, "fetch_completions", boom)

    out = forklift_backfill.backfill_history()
    assert out["days"] == 0 and out["calls"] == 0 and "error" in out


def test_diff_cumulative_days_clamps_and_subtracts():
    # cum_by_day[d] = {driver_id: {"on_time":.., "late":.., "on_call_ms":.., "available_ms":..}}
    cum = {
        "2026-04-01": {"d1": {"on_time": 100, "late": 10, "on_call_ms": 5000, "available_ms": 9000}},
        "2026-04-02": {"d1": {"on_time": 82,  "late": 8,  "on_call_ms": 4300, "available_ms": 7000}},
    }
    # day 2026-04-01 = cum(04-01) - cum(04-02)
    rows = forklift_backfill.diff_day("2026-04-01", "2026-04-02", cum)
    r = rows[0]
    assert r["on_time"] == 18 and r["late"] == 2
    assert r["on_call_ms"] == 700 and r["available_ms"] == 2000
    assert round(r["utilization_pct"], 1) == 35.0  # 700/2000


def test_diff_day_clamps_negative_to_zero():
    cum = {
        "2026-04-01": {"d1": {"on_time": 5, "late": 0, "on_call_ms": 0, "available_ms": 0}},
        "2026-04-02": {"d1": {"on_time": 9, "late": 0, "on_call_ms": 0, "available_ms": 0}},
    }
    rows = forklift_backfill.diff_day("2026-04-01", "2026-04-02", cum)
    assert rows[0]["on_time"] == 0  # clamp, never negative
