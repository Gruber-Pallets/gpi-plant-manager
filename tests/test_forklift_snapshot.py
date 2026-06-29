from datetime import date

from zira_dashboard import forklift_snapshot

# 1782484200000 = 2026-06-26 09:30 America/Chicago (within the snapshot day)
# 1782565200000 = 2026-06-27 08:00 America/Chicago (the NEXT plant-local day)
DAY = date(2026, 6, 26)


def test_snapshot_today_fetches_aggregates_and_stores(monkeypatch):
    captured = {}
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_completions",
                        lambda since=0: [
                            {"id": "c1", "workstationName": "Prosaw #4",
                             "completedBy": "fk-1", "createdAt": 1782484200000,
                             "responseMs": 120000, "handlingMs": 300000},
                            # next plant-local day -> must be filtered out of today
                            {"id": "c2", "workstationName": "Junior #3",
                             "completedBy": "fk-2", "createdAt": 1782565200000,
                             "responseMs": 60000, "handlingMs": 90000},
                        ])
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_drivers",
                        lambda: [{"id": "fk-1", "name": "Trent", "isOverloadResponder": False},
                                 {"id": "fk-2", "name": "Louie", "isOverloadResponder": True}])
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_calls_daily",
                        lambda row: captured.setdefault("calls", row))
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_driver_daily",
                        lambda rows: captured.setdefault("drivers", rows) or len(rows))
    saved = {}
    monkeypatch.setattr(forklift_snapshot.app_settings, "set_setting",
                        lambda k, v: saved.update({k: v}))

    out = forklift_snapshot.snapshot_today(client=None, day=DAY)

    # only the same-day call counts; the next-day call is filtered out
    assert captured["calls"]["total_calls"] == 1
    assert captured["calls"]["by_station"] == {"Prosaw #4": 1}
    assert captured["calls"]["by_hour"]["9"]["calls"] == 1
    assert [d["driver_id"] for d in captured["drivers"]] == ["fk-1"]
    assert captured["drivers"][0]["name"] == "Trent"
    assert saved["forklift_overload_responders"] == ["Louie"]
    assert out["day"] == "2026-06-26"
    assert out["calls"] == 1


def test_snapshot_today_empty_day_writes_zero_row(monkeypatch):
    captured = {}
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_completions",
                        lambda since=0: [])
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_drivers", lambda: [])
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_calls_daily",
                        lambda row: captured.setdefault("calls", row))
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_driver_daily",
                        lambda rows: len(rows))
    monkeypatch.setattr(forklift_snapshot.app_settings, "set_setting", lambda k, v: None)

    out = forklift_snapshot.snapshot_today(client=None, day=DAY)

    assert captured["calls"]["total_calls"] == 0
    assert captured["calls"]["day"] == DAY
    assert out["calls"] == 0


def test_snapshot_today_requests_since_day_start(monkeypatch):
    seen = {}
    def record_since(since=0):
        seen["since"] = since
        return []

    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_completions", record_since)
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_drivers", lambda: [])
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_calls_daily", lambda row: None)
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_driver_daily", lambda rows: 0)
    monkeypatch.setattr(forklift_snapshot.app_settings, "set_setting", lambda k, v: None)

    forklift_snapshot.snapshot_today(client=None, day=DAY)
    # 2026-06-26 00:00 America/Chicago == 1782450000000 ms
    assert seen["since"] == 1782450000000
