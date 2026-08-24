import json
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from unittest.mock import MagicMock

from zira_dashboard import attendance, shift_config, staffing
from zira_dashboard.routes import late_report as late_report_routes
from zira_dashboard.routes import staffing as staffing_routes


FIXED_DAY = date(2026, 7, 13)


def _late_payload(
    monkeypatch,
    *,
    status="late",
    minutes_late=6,
    arrived_after_grace=True,
    absent_ids=None,
    snoozes=None,
):
    staffing_routes._LATE_REPORT_CACHE["value"] = None
    staffing_routes._LATE_REPORT_CACHE["expires_at"] = 0.0
    monkeypatch.setattr(staffing_routes, "plant_today", lambda: FIXED_DAY)
    monkeypatch.setattr(
        staffing_routes,
        "plant_now",
        lambda: datetime(2026, 7, 13, 9, 0, tzinfo=shift_config.SITE_TZ),
    )
    monkeypatch.setattr(shift_config, "shift_start_for", lambda day: time(7, 0))
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda day: SimpleNamespace(assignments={"Repair 1": ["Jesus Galindo"]}),
    )
    monkeypatch.setattr(
        staffing_routes,
        "_safe_attendance",
        lambda *args: {
            "by_id": {
                "7": {
                    "status": status,
                    "minutes_late": minutes_late,
                    "arrived_after_grace": arrived_after_grace,
                }
            },
            "scheduled_ids": ["7"],
            "name_to_id": {"Jesus Galindo": "7"},
        },
    )
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [
            SimpleNamespace(
                name="Jesus Galindo", wage_type="hourly", is_flexible=False
            )
        ],
    )
    monkeypatch.setattr(
        attendance, "person_id_to_name", lambda names: {"7": "Jesus Galindo"}
    )
    monkeypatch.setattr(
        staffing_routes.late_report,
        "absent_emp_ids_for_day",
        lambda day: absent_ids or set(),
    )
    monkeypatch.setattr(
        staffing_routes.late_report, "late_arrivals_for_day", lambda day: set()
    )
    monkeypatch.setattr(
        staffing_routes.late_report, "active_snoozes", lambda day: snoozes or []
    )

    return staffing_routes.late_report_payload(force=True)


def test_late_payload_records_scheduled_punch_over_five_minutes(monkeypatch):
    record = MagicMock()
    monkeypatch.setattr(staffing_routes.late_report, "record_late_arrival", record)

    payload = _late_payload(monkeypatch, status="late", minutes_late=6)

    record.assert_called_once_with(FIXED_DAY, "7", "Jesus Galindo", 6)
    assert payload["scheduled_late"] == []
    assert "needs_reason" not in payload
    assert "running_late" not in payload


def test_late_payload_records_scheduled_punch_just_after_five_minutes(monkeypatch):
    record = MagicMock()
    monkeypatch.setattr(staffing_routes.late_report, "record_late_arrival", record)

    _late_payload(
        monkeypatch,
        status="late",
        minutes_late=5,
        arrived_after_grace=True,
    )

    record.assert_called_once_with(FIXED_DAY, "7", "Jesus Galindo", 5)


def test_late_payload_records_closed_punch_just_after_five_minutes(monkeypatch):
    record = MagicMock()
    monkeypatch.setattr(staffing_routes.late_report, "record_late_arrival", record)

    _late_payload(
        monkeypatch,
        status="clocked_out",
        minutes_late=5,
        arrived_after_grace=True,
    )

    record.assert_called_once_with(FIXED_DAY, "7", "Jesus Galindo", 5)


def test_late_payload_does_not_record_punch_exactly_at_five_minutes(monkeypatch):
    record = MagicMock()
    monkeypatch.setattr(staffing_routes.late_report, "record_late_arrival", record)

    _late_payload(
        monkeypatch,
        status="on_time",
        minutes_late=5,
        arrived_after_grace=False,
    )

    record.assert_not_called()


def test_record_late_arrival_keeps_a_positive_five_minute_display(monkeypatch):
    execute = MagicMock()
    monkeypatch.setattr(staffing_routes.late_report.db, "execute", execute)

    staffing_routes.late_report.record_late_arrival(
        FIXED_DAY, "7", "Jesus Galindo", 5
    )

    execute.assert_called_once()
    assert execute.call_args.args[1] == (FIXED_DAY, "7", "Jesus Galindo", 5)


def test_record_late_arrival_rejects_nonpositive_minutes(monkeypatch):
    execute = MagicMock()
    monkeypatch.setattr(staffing_routes.late_report.db, "execute", execute)

    staffing_routes.late_report.record_late_arrival(
        FIXED_DAY, "7", "Jesus Galindo", 0
    )

    execute.assert_not_called()


def test_late_payload_does_not_record_absent_employee_punch(monkeypatch):
    record = MagicMock()
    monkeypatch.setattr(staffing_routes.late_report, "record_late_arrival", record)

    _late_payload(
        monkeypatch,
        status="late",
        minutes_late=6,
        arrived_after_grace=True,
        absent_ids={"7"},
    )

    record.assert_not_called()


def test_late_payload_clears_active_snooze_when_employee_has_punched(monkeypatch):
    clear_snooze = MagicMock()
    monkeypatch.setattr(staffing_routes.late_report, "clear_snooze", clear_snooze)
    monkeypatch.setattr(staffing_routes.late_report, "record_late_arrival", MagicMock())

    _late_payload(
        monkeypatch,
        status="late",
        arrived_after_grace=True,
        snoozes=[
            {
                "emp_id": "7",
                "name": "Jesus Galindo",
                "until_utc": datetime(2026, 7, 13, 16, 0, tzinfo=UTC),
            }
        ],
    )

    clear_snooze.assert_called_once_with(FIXED_DAY, "7")


def test_running_late_snoozes_for_an_hour_and_busts_caches(monkeypatch):
    snooze = MagicMock()
    bust = MagicMock()
    monkeypatch.setattr(late_report_routes, "plant_today", lambda: FIXED_DAY)
    monkeypatch.setattr(late_report_routes.late_report, "snooze", snooze)
    monkeypatch.setattr(late_report_routes, "_bust_caches", bust)

    response = late_report_routes._running_late_sync(
        {"emp_id": "7", "name": "Jesus Galindo"}
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {"ok": True, "minutes": 60}
    snooze.assert_called_once_with(FIXED_DAY, "7", "Jesus Galindo", 60)
    bust.assert_called_once()
