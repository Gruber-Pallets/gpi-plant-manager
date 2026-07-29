import asyncio
from datetime import UTC

from zira_dashboard import app as app_module
from zira_dashboard import goat_watch


def test_goat_notification_warmer_is_registered_each_minute():
    entry = next(warmer for warmer in app_module._WARMERS if warmer[1] is app_module._tick_goat_notifications)
    assert entry == ("GOAT notifications", app_module._tick_goat_notifications, 60)


def test_tick_runs_the_sync_worker_off_the_event_loop(monkeypatch):
    seen = []

    async def fake_to_thread(func, *args):
        seen.append((func, args))

    monkeypatch.setattr(app_module.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(app_module, "_zira_client", lambda: "zira-client")
    monkeypatch.setattr("zira_dashboard.goat_notifications.run_due", lambda now_utc, client: None)

    asyncio.run(app_module._tick_goat_notifications())

    assert seen[0][1][1] == "zira-client"
    assert seen[0][1][0].tzinfo is UTC


def test_dashboard_finalization_delegates_to_notification_worker(monkeypatch):
    seen = []
    monkeypatch.setattr("zira_dashboard.goat_notifications.run_due", lambda now_utc, client: seen.append((now_utc, client)))
    monkeypatch.setattr(goat_watch, "_zira_client", lambda: "zira-client")

    goat_watch.maybe_finalize_today(None)

    assert seen[0][1] == "zira-client"
