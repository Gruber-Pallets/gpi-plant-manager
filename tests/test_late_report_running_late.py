import json
from datetime import UTC, date, datetime, time
from unittest.mock import MagicMock

import pytest

from zira_dashboard import shift_config
from zira_dashboard.routes import late_report as late_report_routes


def test_running_late_rejects_time_that_is_not_after_now(monkeypatch):
    monkeypatch.setattr(late_report_routes, "plant_today", lambda: date(2026, 7, 13))
    monkeypatch.setattr(
        late_report_routes, "plant_now",
        lambda: datetime(2026, 7, 13, 8, 30, tzinfo=shift_config.SITE_TZ),
    )

    response = late_report_routes._running_late_sync({
        "emp_id": "7", "name": "Jesus Galindo", "expected_time": "08:30"
    })

    assert response.status_code == 400
    assert "later than now" in json.loads(response.body)["error"]


def test_running_late_saves_utc_time_and_busts_caches(monkeypatch):
    save = MagicMock()
    bust = MagicMock()
    monkeypatch.setattr(late_report_routes, "plant_today", lambda: date(2026, 7, 13))
    monkeypatch.setattr(
        late_report_routes, "plant_now",
        lambda: datetime(2026, 7, 13, 8, 30, tzinfo=shift_config.SITE_TZ),
    )
    monkeypatch.setattr(late_report_routes.late_report, "set_expected_arrival", save)
    monkeypatch.setattr(late_report_routes, "_bust_caches", bust)

    response = late_report_routes._running_late_sync({
        "emp_id": "7", "name": "Jesus Galindo", "expected_time": "09:15"
    })

    assert response.status_code == 200
    assert save.call_args.args[:3] == (date(2026, 7, 13), "7", "Jesus Galindo")
    assert save.call_args.args[3] == datetime(
        2026, 7, 13, 9, 15, tzinfo=shift_config.SITE_TZ
    ).astimezone(UTC)
    bust.assert_called_once()


@pytest.mark.parametrize("expected_time", ["0830", "08:30:45", "08:30+01:00"])
def test_running_late_rejects_non_hh_mm_time_without_persisting(monkeypatch, expected_time):
    save = MagicMock()
    bust = MagicMock()
    monkeypatch.setattr(late_report_routes, "plant_today", lambda: date(2026, 7, 13))
    monkeypatch.setattr(
        late_report_routes, "plant_now",
        lambda: datetime(2026, 7, 13, 8, 30, tzinfo=shift_config.SITE_TZ),
    )
    monkeypatch.setattr(late_report_routes.late_report, "set_expected_arrival", save)
    monkeypatch.setattr(late_report_routes, "_bust_caches", bust)

    response = late_report_routes._running_late_sync({
        "emp_id": "7", "name": "Jesus Galindo", "expected_time": expected_time,
    })

    assert response.status_code == 400
    assert json.loads(response.body)["error"] == "expected_time must be HH:MM"
    save.assert_not_called()
    bust.assert_not_called()


def test_late_payload_emits_running_late_and_suppresses_no_punch_action(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_routes
    from zira_dashboard import attendance, staffing

    expected = datetime(2026, 7, 13, 14, 15, tzinfo=UTC)
    staffing_routes._LATE_REPORT_CACHE["value"] = None
    staffing_routes._LATE_REPORT_CACHE["expires_at"] = 0.0
    monkeypatch.setattr(staffing_routes, "plant_today", lambda: date(2026, 7, 13))
    monkeypatch.setattr(
        staffing_routes, "plant_now",
        lambda: datetime(2026, 7, 13, 9, 0, tzinfo=shift_config.SITE_TZ),
    )
    monkeypatch.setattr(shift_config, "shift_start_for", lambda day: time(7, 0))
    monkeypatch.setattr(
        staffing, "load_schedule", lambda day: type("Schedule", (), {
            "assignments": {"Repair 1": ["Jesus Galindo"]}
        })(),
    )
    monkeypatch.setattr(staffing_routes, "_safe_attendance", lambda *args: {
        "by_id": {"7": {"status": "no_punch"}},
        "scheduled_ids": ["7"],
        "name_to_id": {"Jesus Galindo": "7"},
    })
    monkeypatch.setattr(
        staffing, "load_roster", lambda: [type("Person", (), {
            "name": "Jesus Galindo", "wage_type": "hourly", "is_flexible": False
        })()],
    )
    monkeypatch.setattr(attendance, "person_id_to_name", lambda names: {"7": "Jesus Galindo"})
    monkeypatch.setattr(staffing_routes.late_report, "absent_emp_ids_for_day", lambda day: set())
    monkeypatch.setattr(staffing_routes.late_report, "late_arrivals_for_day", lambda day: set())
    monkeypatch.setattr(
        staffing_routes.late_report,
        "active_expected_arrivals",
        lambda day: [{"emp_id": "7", "name": "Jesus Galindo", "expected_at_utc": expected}],
    )
    monkeypatch.setattr(
        staffing_routes.late_report,
        "expected_arrivals_for_day",
        lambda day: [{"emp_id": "7", "name": "Jesus Galindo", "expected_at_utc": expected}],
    )
    monkeypatch.setattr(staffing_routes.late_report, "active_snoozes", lambda day: [])

    payload = staffing_routes.late_report_payload(force=True)

    assert payload["scheduled_late"] == []
    assert payload["running_late"][0]["name"] == "Jesus Galindo"
    assert payload["running_late"][0]["expected_label"] == "9:15 AM"
    assert payload["count"] == 0


def _late_payload_for_expected_arrival(
    monkeypatch, *, attendance_status, active_expected_arrivals, all_expected_arrivals
):
    from zira_dashboard import attendance, staffing
    from zira_dashboard.routes import staffing as staffing_routes

    day = date(2026, 7, 13)
    clear = MagicMock()
    staffing_routes._LATE_REPORT_CACHE["value"] = None
    staffing_routes._LATE_REPORT_CACHE["expires_at"] = 0.0
    monkeypatch.setattr(staffing_routes, "plant_today", lambda: day)
    monkeypatch.setattr(
        staffing_routes, "plant_now",
        lambda: datetime(2026, 7, 13, 9, 0, tzinfo=shift_config.SITE_TZ),
    )
    monkeypatch.setattr(shift_config, "shift_start_for", lambda day: time(7, 0))
    monkeypatch.setattr(
        staffing, "load_schedule",
        lambda day: type("Schedule", (), {"assignments": {"Repair 1": ["Jesus Galindo"]}})(),
    )
    monkeypatch.setattr(staffing_routes, "_safe_attendance", lambda *args: {
        "by_id": {"7": {"status": attendance_status, "minutes_late": 30}},
        "scheduled_ids": ["7"],
        "name_to_id": {"Jesus Galindo": "7"},
    })
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [type("Person", (), {
            "name": "Jesus Galindo", "wage_type": "hourly", "is_flexible": False
        })()],
    )
    monkeypatch.setattr(attendance, "person_id_to_name", lambda names: {"7": "Jesus Galindo"})
    monkeypatch.setattr(staffing_routes.late_report, "absent_emp_ids_for_day", lambda day: set())
    monkeypatch.setattr(staffing_routes.late_report, "late_arrivals_for_day", lambda day: set())
    monkeypatch.setattr(
        staffing_routes.late_report,
        "active_expected_arrivals",
        lambda day: active_expected_arrivals,
    )
    monkeypatch.setattr(
        staffing_routes.late_report,
        "expected_arrivals_for_day",
        lambda day: all_expected_arrivals,
        raising=False,
    )
    monkeypatch.setattr(staffing_routes.late_report, "active_snoozes", lambda day: [])
    monkeypatch.setattr(staffing_routes.late_report, "clear_expected_arrival", clear)

    return day, clear, staffing_routes.late_report_payload(force=True)


def test_late_payload_clears_active_expected_arrival_and_emits_late_reason(monkeypatch):
    expected = datetime(2026, 7, 13, 14, 15, tzinfo=UTC)

    day, clear, payload = _late_payload_for_expected_arrival(
        monkeypatch,
        attendance_status="late",
        active_expected_arrivals=[{
            "emp_id": "7", "name": "Jesus Galindo", "expected_at_utc": expected,
        }],
        all_expected_arrivals=[{
            "emp_id": "7", "name": "Jesus Galindo", "expected_at_utc": expected,
        }],
    )

    clear.assert_called_once_with(day, "7")
    assert [row["emp_id"] for row in payload["needs_reason"]] == ["7"]
    assert payload["running_late"] == []
    assert payload["count"] == 1


def test_late_payload_clears_expired_expected_arrival_after_punch(monkeypatch):
    expired = datetime(2026, 7, 13, 13, 15, tzinfo=UTC)

    day, clear, payload = _late_payload_for_expected_arrival(
        monkeypatch,
        attendance_status="late",
        active_expected_arrivals=[],
        all_expected_arrivals=[{
            "emp_id": "7", "name": "Jesus Galindo", "expected_at_utc": expired,
        }],
    )

    clear.assert_called_once_with(day, "7")
    assert [row["emp_id"] for row in payload["needs_reason"]] == ["7"]
