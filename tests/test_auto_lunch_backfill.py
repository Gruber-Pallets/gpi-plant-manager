from datetime import datetime

from zira_dashboard import shift_config
from zira_dashboard.auto_lunch import Window
from zira_dashboard.auto_lunch_backfill import (
    Repair,
    apply_repair,
    plan_return_only_repairs,
    persist_repair,
    plan_repairs,
)


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 18, hour, minute, tzinfo=shift_config.SITE_TZ)


def _interval(check_in: datetime, check_out: datetime) -> dict:
    return {
        "id": 41,
        "employee_odoo_id": 7,
        "check_in": check_in,
        "check_out": check_out,
        "wc_name": "Repair 1",
    }


def test_plan_repairs_splits_interval_covering_lunch():
    window = Window(_dt(11), _dt(11, 30))

    repairs = plan_repairs([_interval(_dt(7), _dt(15, 30))], {7: window}, set())

    assert repairs == [Repair(
        attendance_id=41,
        person_odoo_id=7,
        out_at=_dt(11),
        in_at=_dt(11, 30),
        wc_name="Repair 1",
        create_return=True,
        return_end_at=_dt(15, 30),
    )]
    assert repairs[0].return_end_at == _dt(15, 30)


def test_plan_repairs_accepts_normalized_odoo_timestamps():
    window = Window(_dt(11), _dt(11, 30))
    interval = _interval("2026-08-18T12:00:00+00:00", "2026-08-18T20:30:00+00:00")

    repairs = plan_repairs([interval], {7: window}, set())

    assert repairs[0].out_at == _dt(11)
    assert repairs[0].create_return is True


def test_plan_repairs_current_open_interval_after_lunch():
    window = Window(_dt(11), _dt(11, 30))
    interval = _interval(_dt(7), None)

    repairs = plan_repairs([interval], {7: window}, set(), as_of=_dt(15))

    assert repairs[0].attendance_id == 41
    assert repairs[0].create_return is True


def test_plan_repairs_keeps_open_interval_when_lunch_has_not_finished():
    window = Window(_dt(15), _dt(15, 30))
    interval = _interval(_dt(7), None)

    repairs = plan_repairs([interval], {7: window}, set(), as_of=_dt(15, 15))

    assert repairs == []


def test_plan_return_only_repairs_recovers_a_closed_lunch_out():
    window = Window(_dt(11), _dt(11, 30))
    interval = _interval(_dt(7), _dt(11))

    repairs = plan_return_only_repairs(
        [interval], {7: window}, set(), {7: _dt(15, 30)}
    )

    assert repairs == [Repair(
        attendance_id=41,
        person_odoo_id=7,
        out_at=_dt(11),
        in_at=_dt(11, 30),
        wc_name="Repair 1",
        create_return=True,
        return_end_at=_dt(15, 30),
        needs_clock_out=False,
    )]


def test_apply_repair_closes_then_returns_then_persists_audit():
    events = []
    repair = Repair(41, 7, _dt(11), _dt(11, 30), "Repair 1", True)

    apply_repair(
        repair,
        close=lambda *args: events.append(("close", args)),
        clock_in=lambda *args: events.append(("clock_in", args)) or 92,
        persist=lambda *args: events.append(("persist", args)),
    )

    assert events == [
        ("close", (41, _dt(11))),
        ("clock_in", (7, "Repair 1", _dt(11, 30))),
        ("persist", (repair, 92)),
    ]


def test_persist_repair_writes_pair_then_terminal_run():
    events = []
    repair = Repair(41, 7, _dt(11), _dt(11, 30), "Repair 1", True)

    persist_repair(
        repair,
        kind="scheduled",
        returned_attendance_id=92,
        write_punch=lambda *args: events.append(("punch", args)) or len(events),
        write_run=lambda *args: events.append(("run", args)),
    )

    assert events == [
        ("punch", (7, "clock_out", None, _dt(11), 41)),
        ("punch", (7, "clock_in", "Repair 1", _dt(11, 30), 92)),
        ("run", (7, _dt(11).date(), "scheduled", _dt(11), _dt(11, 30), "Repair 1", 1, 2)),
    ]
