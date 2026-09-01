import asyncio
from datetime import date, timedelta

from zira_dashboard import app as app_module
from zira_dashboard import (
    forklift_backfill,
    forklift_event_store,
    forklift_snapshot,
    forklift_store,
)


def test_forklift_tick_finalizes_yesterday_before_refreshing_today(monkeypatch):
    today = date(2026, 8, 28)
    snapshots = []
    monkeypatch.setattr(app_module, "plant_today", lambda: today)
    monkeypatch.setattr(forklift_store, "history_day_count", lambda: 14)
    monkeypatch.setattr(forklift_snapshot, "day_is_finalized", lambda day: False)
    monkeypatch.setattr(
        forklift_snapshot,
        "snapshot_today",
        lambda client, day: snapshots.append(day) or {"day": day.isoformat()},
    )
    monkeypatch.setattr(app_module, "_capture_forklift_ontime", lambda: None)

    async def no_reconstruct():
        return None

    monkeypatch.setattr(app_module, "_maybe_reconstruct_ontime", no_reconstruct)

    asyncio.run(app_module._tick_forklift())

    assert snapshots == [today - timedelta(days=1), today]


def test_empty_history_coverage_stops_repeating_full_backfill(monkeypatch):
    today = date(2026, 8, 28)
    snapshots = []
    monkeypatch.setattr(app_module, "plant_today", lambda: today)
    monkeypatch.setattr(forklift_store, "history_day_count", lambda: 1)
    monkeypatch.setattr(
        forklift_event_store,
        "completion_coverage_for_day",
        lambda day: object() if day == today else None,
    )
    monkeypatch.setattr(
        forklift_backfill,
        "backfill_history",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not repeat backfill")),
    )
    monkeypatch.setattr(forklift_snapshot, "day_is_finalized", lambda day: True)
    monkeypatch.setattr(
        forklift_snapshot,
        "snapshot_today",
        lambda client, day: snapshots.append(day) or {"day": day.isoformat()},
    )
    monkeypatch.setattr(app_module, "_capture_forklift_ontime", lambda: None)

    async def no_reconstruct():
        return None

    monkeypatch.setattr(app_module, "_maybe_reconstruct_ontime", no_reconstruct)

    asyncio.run(app_module._tick_forklift())

    assert snapshots == [today]
