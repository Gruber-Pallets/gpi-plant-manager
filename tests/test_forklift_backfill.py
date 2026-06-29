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
