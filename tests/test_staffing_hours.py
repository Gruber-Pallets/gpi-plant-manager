from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from zira_dashboard import staffing_hours as hours


TODAY = date(2026, 8, 27)
START = date(2026, 8, 16)
END = date(2026, 8, 17)
NOW = datetime(2026, 8, 18, 2, tzinfo=UTC)


@pytest.fixture(autouse=True)
def default_pay_period(monkeypatch):
    monkeypatch.setattr(hours.app_settings, "get_setting", lambda _key: None)


def _no_batches(_start, _end):
    return []


def test_this_pay_period_uses_the_august_16_biweekly_anchor():
    result = hours.resolve_hours_range(
        "this_pay_period", None, None, TODAY, _no_batches
    )

    assert (result.start, result.end) == (date(2026, 8, 16), date(2026, 8, 29))
    assert result.verification == "anchor"


def test_odoo_payroll_batch_override_is_visible_not_silent():
    result = hours.resolve_hours_range(
        "this_pay_period", None, None, TODAY,
        lambda _start, _end: [
            hours.PayrollBatch("Run", date(2026, 8, 15), date(2026, 8, 28))
        ],
    )

    assert (result.start, result.end, result.verification) == (
        date(2026, 8, 15), date(2026, 8, 28), "odoo_override"
    )
    assert result.notice


@pytest.mark.parametrize(
    ("preset", "start", "end"),
    [
        ("this_week", date(2026, 8, 24), date(2026, 8, 30)),
        ("last_week", date(2026, 8, 17), date(2026, 8, 23)),
        ("this_month", date(2026, 8, 1), date(2026, 8, 31)),
        ("last_month", date(2026, 7, 1), date(2026, 7, 31)),
        ("custom", date(2026, 8, 3), date(2026, 8, 7)),
    ],
)
def test_non_pay_period_presets_use_expected_calendar_bounds(preset, start, end):
    calls = []
    result = hours.resolve_hours_range(
        preset,
        "2026-08-03" if preset == "custom" else None,
        "2026-08-07" if preset == "custom" else None,
        TODAY,
        lambda *args: calls.append(args),
    )

    assert (result.start, result.end, result.error) == (start, end, None)
    assert calls == []


def test_last_pay_period_uses_the_previous_anchor_cycle():
    result = hours.resolve_hours_range(
        "last_pay_period", None, None, TODAY, _no_batches
    )

    assert (result.start, result.end) == (date(2026, 8, 2), date(2026, 8, 15))


@pytest.mark.parametrize(
    ("start_raw", "end_raw", "message"),
    [
        (None, "2026-08-20", "start"),
        ("2026-08-21", None, "end"),
        ("bad-date", "2026-08-20", "valid"),
        ("2026-08-21", "2026-08-20", "before"),
    ],
)
def test_custom_range_validation_returns_an_explicit_error(start_raw, end_raw, message):
    result = hours.resolve_hours_range(
        "custom", start_raw, end_raw, TODAY, _no_batches
    )

    assert result.error is not None
    assert message in result.error.lower()


def test_exact_odoo_batch_is_reported_as_verified():
    result = hours.resolve_hours_range(
        "this_pay_period", None, None, TODAY,
        lambda _start, _end: [
            {"name": "Run", "start": date(2026, 8, 16), "end": date(2026, 8, 29)}
        ],
    )

    assert (result.verification, result.notice, result.error) == (
        "odoo_verified", None, None
    )


def test_no_odoo_batch_uses_the_anchor_with_a_visible_notice():
    result = hours.resolve_hours_range(
        "this_pay_period", None, None, TODAY, _no_batches
    )

    assert result.verification == "anchor"
    assert result.notice is not None
    assert result.error is None


def test_unavailable_odoo_batches_fall_back_with_a_visible_notice():
    result = hours.resolve_hours_range(
        "this_pay_period", None, None, TODAY,
        lambda _start, _end: (_ for _ in ()).throw(RuntimeError("no access")),
    )

    assert result.verification == "unverified"
    assert result.notice is not None
    assert result.error is None


def test_conflicting_odoo_batch_ranges_return_an_explicit_error():
    result = hours.resolve_hours_range(
        "this_pay_period", None, None, TODAY,
        lambda _start, _end: [
            {"name": "Run A", "start": date(2026, 8, 14), "end": date(2026, 8, 27)},
            {"name": "Run B", "start": date(2026, 8, 15), "end": date(2026, 8, 28)},
        ],
    )

    assert result.error is not None
    assert "multiple" in result.error.lower()


def test_exact_odoo_batch_does_not_hide_two_other_ambiguous_ranges():
    result = hours.resolve_hours_range(
        "this_pay_period", None, None, TODAY,
        lambda _start, _end: [
            {"name": "Exact", "start": date(2026, 8, 16), "end": date(2026, 8, 29)},
            {"name": "Run A", "start": date(2026, 8, 14), "end": date(2026, 8, 27)},
            {"name": "Run B", "start": date(2026, 8, 15), "end": date(2026, 8, 28)},
        ],
    )

    assert result.error is not None
    assert "multiple" in result.error.lower()


def test_duplicate_odoo_batch_ranges_are_not_ambiguous():
    result = hours.resolve_hours_range(
        "this_pay_period", None, None, TODAY,
        lambda _start, _end: [
            {"name": "Run A", "start": date(2026, 8, 15), "end": date(2026, 8, 28)},
            {"name": "Run B", "start": date(2026, 8, 15), "end": date(2026, 8, 28)},
        ],
    )

    assert (result.start, result.end, result.verification, result.error) == (
        date(2026, 8, 15), date(2026, 8, 28), "odoo_override", None
    )


def test_current_config_falls_back_to_default_when_the_saved_value_is_invalid(monkeypatch):
    monkeypatch.setattr(
        hours.app_settings, "get_setting",
        lambda _key: {"anchor": "not-a-date", "cycle_days": 0},
    )

    assert hours.current_pay_period_config() == hours.PayPeriodConfig(
        date(2026, 8, 16), 14
    )


def test_save_pay_period_config_validates_and_persists_the_typed_value(monkeypatch):
    saved = []
    monkeypatch.setattr(
        hours.app_settings, "set_setting", lambda key, value: saved.append((key, value))
    )

    config = hours.save_pay_period_config("2026-09-01", "15")

    assert config == hours.PayPeriodConfig(date(2026, 9, 1), 15)
    assert saved == [
        (hours.PAY_PERIOD_SETTING, {"anchor": "2026-09-01", "cycle_days": 15})
    ]


@pytest.mark.parametrize("cycle_raw", ["0", "32", "2.5", "not-a-number"])
def test_save_pay_period_config_rejects_an_invalid_cycle_length(cycle_raw):
    with pytest.raises(ValueError):
        hours.save_pay_period_config("2026-08-16", cycle_raw)


def _person(name, employee_id, *, active=True):
    return SimpleNamespace(name=name, employee_id=employee_id, active=active)


def _attendance(employee_id, check_in, check_out, *, conflict=False):
    return {
        "employee_odoo_id": employee_id,
        "check_in": check_in,
        "check_out": check_out,
        "conflict": conflict,
    }


def _entry(
    employee_id,
    day,
    code,
    duration,
    *,
    active=True,
    numeric_data_valid=True,
    conflict=False,
):
    return {
        "employee_id": employee_id,
        "date": day,
        "type_code": code,
        "duration": duration,
        "active": active,
        "numeric_data_valid": numeric_data_valid,
        "conflict": conflict,
    }


def test_clocked_report_splits_an_open_shift_at_midnight():
    report = hours.build_hours_report(
        source="clocked", roster=[_person("Ana", 7)],
        start=date(2026, 8, 16), end=date(2026, 8, 17),
        now=datetime(2026, 8, 18, 2, tzinfo=UTC),
        attendances=[_attendance(7, "2026-08-17T04:00:00+00:00", None)],
        work_entries=[], departments={7: "Recycled"},
    )

    assert report.rows[0].daily == (
        (date(2026, 8, 16), 1.0), (date(2026, 8, 17), 21.0)
    )
    assert report.rows[0].needs_attention is True
    assert report.rows[0].records[0].label == "11:00 PM – open"
    assert report.rows[0].records[0].is_open is True


def test_clocked_report_clips_closed_intervals_to_the_local_report_bounds():
    report = hours.build_hours_report(
        source="clocked", roster=[_person("Ana", 7)],
        start=date(2026, 8, 17), end=date(2026, 8, 17), now=NOW,
        attendances=[_attendance(7, "2026-08-16T17:00:00+00:00", "2026-08-18T17:00:00+00:00")],
        work_entries=[], departments={},
    )

    assert report.rows[0].daily == ((date(2026, 8, 17), 24.0),)
    assert report.rows[0].total_hours == 24.0


def test_clocked_report_uses_elapsed_time_across_a_daylight_saving_boundary():
    report = hours.build_hours_report(
        source="clocked", roster=[_person("Ana", 7)],
        start=date(2026, 11, 1), end=date(2026, 11, 1),
        now=datetime(2026, 11, 3, tzinfo=UTC),
        attendances=[_attendance(7, "2026-11-01T05:00:00+00:00", "2026-11-02T06:00:00+00:00")],
        work_entries=[], departments={},
    )

    assert report.rows[0].daily == ((date(2026, 11, 1), 25.0),)


def test_clocked_report_keeps_dst_elapsed_time_when_it_clips_both_endpoints():
    report = hours.build_hours_report(
        source="clocked", roster=[_person("Ana", 7)],
        start=date(2026, 11, 1), end=date(2026, 11, 1),
        now=datetime(2026, 11, 3, tzinfo=UTC),
        attendances=[_attendance(7, "2026-11-01T04:00:00+00:00", "2026-11-02T07:00:00+00:00")],
        work_entries=[], departments={},
    )

    assert report.rows[0].daily == ((date(2026, 11, 1), 25.0),)


def test_payroll_report_separates_regular_and_overtime():
    report = hours.build_hours_report(
        source="payroll", roster=[_person("Ana", 7)], start=START, end=END,
        now=NOW, attendances=[], departments={7: "Recycled"},
        work_entries=[
            _entry(7, START, "WORK100", 38), _entry(7, END, "OVERTIME", 3)
        ],
    )

    assert (report.rows[0].regular_hours, report.rows[0].overtime_hours) == (38, 3)
    assert report.rows[0].records[0].label == "Regular"
    assert report.rows[0].records[1].label == "Overtime"


def test_payroll_report_only_includes_valid_active_regular_and_overtime_entries():
    report = hours.build_hours_report(
        source="payroll", roster=[_person("Ana", 7)], start=START, end=END,
        now=NOW, attendances=[], departments={},
        work_entries=[
            _entry(7, START, "WORK100", 8),
            _entry(7, START, "WORK100", 4, active=False),
            _entry(7, START, "PTO", 8),
            _entry(7, START, "OVERTIME", 3, numeric_data_valid=False),
            _entry(7, date(2026, 8, 18), "OVERTIME", 3),
        ],
    )

    assert report.rows[0].daily == ((START, 8.0),)
    assert report.rows[0].regular_hours == 8.0
    assert report.rows[0].overtime_hours == 0.0


def test_report_includes_zero_hour_active_people_with_an_odoo_id_and_sorts_rows():
    report = hours.build_hours_report(
        source="payroll",
        roster=[
            _person("Zulu", 3), _person("ana", 2), _person("Inactive", 4, active=False),
            _person("Missing id", None),
        ],
        start=START, end=END, now=NOW, attendances=[], departments={},
        work_entries=[_entry(2, START, "WORK100", 4)],
    )

    assert [(row.name, row.total_hours) for row in report.rows] == [
        ("ana", 4.0), ("Zulu", 0.0)
    ]


def test_report_filters_query_department_and_summary_thresholds_after_aggregation():
    roster = [_person("Ana", 7), _person("Bea", 8), _person("Carl", 9)]
    entries = [
        _entry(7, START, "WORK100", 38),
        _entry(8, START, "WORK100", 40),
    ]
    departments = {7: "Recycled", 8: "New", 9: "Recycled"}

    query = hours.build_hours_report(
        source="payroll", roster=roster, start=START, end=END, now=NOW,
        attendances=[], work_entries=entries, departments=departments, query="an",
    )
    approaching = hours.build_hours_report(
        source="payroll", roster=roster, start=START, end=END, now=NOW,
        attendances=[], work_entries=entries, departments=departments,
        attention="approaching_40",
    )
    over = hours.build_hours_report(
        source="payroll", roster=roster, start=START, end=END, now=NOW,
        attendances=[], work_entries=entries, departments=departments,
        department="New", attention="over_40",
    )

    assert [row.name for row in query.rows] == ["Ana"]
    assert query.available_departments == ("New", "Recycled")
    assert [row.name for row in approaching.rows] == ["Ana"]
    assert [(row.name, row.total_hours) for row in over.rows] == [("Bea", 40.0)]
    assert over.team_total_hours == 40.0


def test_conflicting_payroll_records_are_attention_items_even_when_they_have_no_hours():
    report = hours.build_hours_report(
        source="payroll", roster=[_person("Ana", 7)], start=START, end=END,
        now=NOW, attendances=[], departments={},
        work_entries=[
            _entry(7, START, "WORK100", 0, conflict=True),
        ],
        attention="attention",
    )

    assert len(report.rows) == 1
    assert report.rows[0].conflicting_record is True
    assert report.rows[0].needs_attention is True


@pytest.mark.parametrize("source", ["scheduled", "", "PAYROLL"])
def test_report_rejects_unknown_sources_before_aggregating(source):
    with pytest.raises(ValueError, match="source"):
        hours.build_hours_report(
            source=source, roster=[], start=START, end=END, now=NOW,
            attendances=[], work_entries=[], departments={},
        )


@pytest.mark.parametrize("attention", ["soon", "", "OVER_40"])
def test_report_rejects_unknown_attention_filters_before_aggregating(attention):
    with pytest.raises(ValueError, match="attention"):
        hours.build_hours_report(
            source="clocked", roster=[], start=START, end=END, now=NOW,
            attendances=[], work_entries=[], departments={}, attention=attention,
        )
