import asyncio

from zira_dashboard import anniversary_pto_reminders
from zira_dashboard import app as app_module


def test_anniversary_pto_warmer_runs_every_six_hours():
    entry = next(
        warmer
        for warmer in app_module._WARMERS
        if warmer[1] is app_module._tick_anniversary_pto_reminders
    )
    assert entry == (
        "anniversary PTO reminders",
        app_module._tick_anniversary_pto_reminders,
        21600,
    )


def test_anniversary_pto_tick_runs_off_event_loop(monkeypatch):
    seen = []

    async def fake_to_thread(func, *args):
        seen.append((func, args))

    monkeypatch.setattr(app_module.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(anniversary_pto_reminders, "run", lambda: 0)

    asyncio.run(app_module._tick_anniversary_pto_reminders())

    assert seen == [(anniversary_pto_reminders.run, ())]
