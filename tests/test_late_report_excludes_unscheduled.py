"""Regression: people who weren't on today's schedule must never be flagged
for a missing punch.

Background: the late/absence report used to fold in "unscheduled" people
(active non-reserve roster members not assigned today) and flag them when
they hadn't punched in. After the Exception Inbox rework those rows became
*urgent* inbox items, so every active employee who simply wasn't scheduled
today flooded the inbox with "Unscheduled late · No punch yet". Per product
decision (2026-06-27) the report now covers scheduled people only.
"""

from datetime import date, datetime, time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")  # routes import FastAPI; skip where it's absent

from zira_dashboard import late_report, shift_config
from zira_dashboard.routes import staffing as staffing_routes


def test_late_report_payload_excludes_unscheduled_no_punch(monkeypatch):
    d = date(2026, 6, 27)

    # Freeze the plant clock well past shift start + threshold so the report
    # is active and the no_punch checks fire.
    monkeypatch.setattr(staffing_routes, "plant_today", lambda: d)
    monkeypatch.setattr(
        staffing_routes,
        "plant_now",
        lambda: datetime.combine(d, time(12, 0), tzinfo=shift_config.SITE_TZ),
    )
    monkeypatch.setattr(shift_config, "shift_start_for", lambda day: time(6, 0))

    # Bust the 30s in-process cache so we recompute from the mocks.
    staffing_routes._LATE_REPORT_CACHE["value"] = None
    staffing_routes._LATE_REPORT_CACHE["expires_at"] = 0.0

    # Ana (1) is scheduled, Bob (2) is unscheduled. Neither has punched in.
    monkeypatch.setattr(
        staffing_routes.staffing,
        "load_schedule",
        lambda day: SimpleNamespace(assignments={"Baler": ["Ana"]}, published=True),
    )
    monkeypatch.setattr(
        staffing_routes,
        "_safe_attendance",
        lambda today, sched, t: {
            "by_id": {"1": {"status": "no_punch"}, "2": {"status": "no_punch"}},
            "by_name": {},
            "name_to_id": {"Ana": "1", "Bob": "2"},
            "scheduled_ids": ["1"],
            "unscheduled_ids": ["2"],
        },
    )
    monkeypatch.setattr(
        staffing_routes.staffing,
        "load_roster",
        lambda: [
            SimpleNamespace(name="Ana", active=True, reserve=False),
            SimpleNamespace(name="Bob", active=True, reserve=False),
        ],
    )
    monkeypatch.setattr(late_report, "report_eligible_emp_ids", lambda roster, n2i: {"1", "2"})
    monkeypatch.setattr(late_report, "absent_emp_ids_for_day", lambda day: set())
    monkeypatch.setattr(late_report, "active_snoozes", lambda day: [])
    monkeypatch.setattr(late_report, "late_arrivals_for_day", lambda day: set())
    monkeypatch.setattr(
        staffing_routes.attendance, "person_id_to_name", lambda n2i: {"1": "Ana", "2": "Bob"}
    )

    out = staffing_routes.late_report_payload()

    # The scheduled person who didn't punch in is still flagged...
    assert [r["emp_id"] for r in out["scheduled_late"]] == ["1"]
    # ...but the unscheduled person is NOT flagged at all.
    assert out["unscheduled_late"] == []
    # Badge count reflects only the scheduled person.
    assert out["count"] == 1
