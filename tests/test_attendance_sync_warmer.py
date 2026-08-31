from __future__ import annotations

import asyncio

from zira_dashboard import app as app_module, attendance_sync


def test_exactly_one_attendance_mirror_warmer_runs_every_30_seconds():
    matches = [item for item in app_module._WARMERS if item[1] is app_module._tick_attendance_mirror]

    assert matches == [
        ("attendance mirror", app_module._tick_attendance_mirror, 30)
    ]
    assert any(name == "live_cache" for name, _tick, _interval in app_module._WARMERS)


def test_attendance_mirror_tick_runs_blocking_sync_off_event_loop(monkeypatch):
    calls = []
    monkeypatch.setattr(attendance_sync, "tick", lambda: calls.append("tick"))

    asyncio.run(app_module._tick_attendance_mirror())

    assert calls == ["tick"]
