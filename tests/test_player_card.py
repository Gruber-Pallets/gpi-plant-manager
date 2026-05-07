from datetime import date
from unittest.mock import patch

from fastapi.testclient import TestClient

from zira_dashboard.app import app


def _attr(units: float, downtime: float = 0.0):
    return {"units": units, "downtime": downtime, "hours": 8.0, "days_worked": 1}


def test_player_card_renders_per_day_breakdown_table():
    """The player card surfaces a per-day-per-WC table below the per-WC
    summary, with each Date cell hyperlinked to the recycling dashboard
    for that day. Days are sorted newest-first."""
    fake = [
        (date(2026, 4, 27), {"Carlos": {"Repair-1": _attr(95)}}),
        (date(2026, 4, 28), {"Carlos": {"Repair-1": _attr(80), "Repair-2": _attr(70)}}),
        (date(2026, 4, 29), {"Other": {"Repair-1": _attr(50)}}),
    ]
    with patch("zira_dashboard.production_history.attribution_per_day", return_value=fake), \
         patch("zira_dashboard.production_history.attribution_range",
               return_value={"Carlos": {"Repair-1": {"units": 175.0, "downtime": 0.0,
                                                     "hours": 16.0, "days_worked": 2},
                                        "Repair-2": {"units": 70.0, "downtime": 0.0,
                                                     "hours": 8.0, "days_worked": 1}}}), \
         patch("zira_dashboard.staffing.load_roster", return_value=[]), \
         patch("zira_dashboard.work_centers_store.registered_groups", return_value=[]), \
         patch("zira_dashboard.late_report.absences_history_for_name", return_value=[]), \
         patch("zira_dashboard.late_report.late_arrivals_history_for_name", return_value=[]):
        client = TestClient(app)
        html = client.get("/staffing/people/Carlos?start=2026-04-27&end=2026-04-29").text

    # Per-day breakdown header is present.
    assert "Per-day breakdown" in html
    # Date hyperlinks point at the recycling dashboard for that day.
    assert 'href="/recycling?start=2026-04-28&end=2026-04-28"' in html
    assert 'href="/recycling?start=2026-04-27&end=2026-04-27"' in html
    # Newest first — anchor on the per-day-row href so we don't accidentally
    # match the date input fields at the top of the page (which carry the same
    # YYYY-MM-DD strings via value="..."  attributes).
    assert (
        html.index('href="/recycling?start=2026-04-28&end=2026-04-28"')
        < html.index('href="/recycling?start=2026-04-27&end=2026-04-27"')
    )
    # Carlos's entries appear, "Other" does not.
    assert "Repair-1" in html and "Repair-2" in html


def test_player_card_renders_attendance_section_with_reasons():
    """The player card shows an Attendance section with absent/late
    rows and reasons when history exists in the range."""
    from datetime import date
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from zira_dashboard.app import app

    abs_rows = [{"day": date(2026, 5, 6), "reason": "sick"}]
    late_rows = [{"day": date(2026, 5, 7), "reason": "car issues"}]

    with patch("zira_dashboard.production_history.attribution_per_day", return_value=[]), \
         patch("zira_dashboard.production_history.attribution_range", return_value={}), \
         patch("zira_dashboard.staffing.load_roster", return_value=[]), \
         patch("zira_dashboard.work_centers_store.registered_groups", return_value=[]), \
         patch("zira_dashboard.late_report.absences_history_for_name", return_value=abs_rows), \
         patch("zira_dashboard.late_report.late_arrivals_history_for_name", return_value=late_rows):
        client = TestClient(app)
        html = client.get("/staffing/people/Carlos?start=2026-05-01&end=2026-05-07").text

    assert "Attendance" in html
    assert "Days Absent" in html
    assert "Days Late" in html
    assert "sick" in html
    assert "car issues" in html
    # Date hyperlinks point to the recycling day-view.
    assert 'href="/recycling?start=2026-05-06&end=2026-05-06"' in html
    assert 'href="/recycling?start=2026-05-07&end=2026-05-07"' in html


def test_player_card_attendance_section_hidden_when_empty():
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from zira_dashboard.app import app

    with patch("zira_dashboard.production_history.attribution_per_day", return_value=[]), \
         patch("zira_dashboard.production_history.attribution_range", return_value={}), \
         patch("zira_dashboard.staffing.load_roster", return_value=[]), \
         patch("zira_dashboard.work_centers_store.registered_groups", return_value=[]), \
         patch("zira_dashboard.late_report.absences_history_for_name", return_value=[]), \
         patch("zira_dashboard.late_report.late_arrivals_history_for_name", return_value=[]):
        client = TestClient(app)
        html = client.get("/staffing/people/Carlos?start=2026-05-01&end=2026-05-07").text

    # No section header.
    assert ">Attendance<" not in html
