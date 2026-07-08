"""expected_by_wc's productive_minutes callable now receives wc_name too."""
from datetime import datetime, timezone

from zira_dashboard.assignment_windows import WorkSegment, expected_by_wc


def test_expected_by_wc_passes_wc_name_to_productive_minutes():
    seg = WorkSegment("Dismantler 2", "Juan",
                       datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc),
                       datetime(2026, 7, 8, 13, 0, tzinfo=timezone.utc), "punch")
    calls = []

    def productive_minutes(person, wc_name, start, end):
        calls.append((person, wc_name, start, end))
        return 60.0

    out = expected_by_wc([seg], {"Dismantler 2": 30.0}, productive_minutes)
    assert calls == [("Juan", "Dismantler 2", seg.start_utc, seg.end_utc)]
    assert out["Dismantler 2"] == 30.0
