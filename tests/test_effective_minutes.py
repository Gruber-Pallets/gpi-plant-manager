from datetime import datetime, date, timezone
from zira_dashboard import staffing, shift_config, attendance


def test_effective_minutes_subtracts_partial_off(monkeypatch):
    day = date(2026, 6, 1)
    start = datetime(2026, 6, 1, 13, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)  # 120-min window
    monkeypatch.setattr(shift_config, "breaks_for", lambda d: [])
    # Ana off 13:30-14:00 UTC -> 30 min overlap; Bob has no partial off.
    monkeypatch.setattr(attendance, "partial_off_intervals", lambda d: {
        "Ana": [(datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc),
                 datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc))],
    })
    assert staffing.effective_minutes_worked("Ana", day, start, end) == 90
    assert staffing.effective_minutes_worked("Bob", day, start, end) == 120
