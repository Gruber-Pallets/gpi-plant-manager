from datetime import date

import pytest
from zira_dashboard import scheduler_time_off as sto


def _fake_db(monkeypatch, rows):
    rows = [{
        "request_id": index,
        "date_from": date(2026, 6, 1),
        "date_to": date(2026, 6, 1),
        "odoo_leave_id": 1,
        "local_record": False,
        **row,
    } for index, row in enumerate(rows, start=1)]
    monkeypatch.setattr(sto.db, "query", lambda sql, params=None: rows)
    # time_off_entries_for_day also queries cleared_partials_by_name (via
    # late_report). With the broad db.query stub above, that query would
    # return `rows` too — misreading the time-off names as "cleared" and
    # filtering the partial entries out. Default to "nothing cleared"; the
    # clear-specific test re-stubs this afterward (monkeypatch last wins).
    from zira_dashboard import late_report
    monkeypatch.setattr(late_report, "cleared_partial_names_for_day", lambda day: set())
    # Likewise, time_off_entries_for_day queries manual_absences (via
    # late_report.absences_for_day); the broad stub would read the time-off rows
    # back as "absent". Default to none; the absent-specific test overrides.
    monkeypatch.setattr(late_report, "absences_for_day", lambda day: [])
    monkeypatch.setattr(sto, "_absence_pto_states", lambda day, emp_ids: {})


def test_scheduler_entry_exposes_editor_metadata_without_note(monkeypatch):
    monkeypatch.setattr(sto, "_cleared_partial_names", lambda _day: set())
    monkeypatch.setattr(sto, "_rows_for_day", lambda _day: [{
        "request_id": 91, "name": "Jose Luis", "shape": "midday_gap",
        "hour_from": 9.0, "hour_to": 11.0, "state": "validate",
        "date_from": date(2026, 7, 17), "date_to": date(2026, 7, 17),
        "odoo_leave_id": 701, "local_record": False, "note": "private",
        "pay_type": "Vacation",
    }])
    entry = sto.time_off_entries_for_day(date(2026, 7, 17))[0]
    assert entry["request_id"] == 91
    assert entry["date_from"] == "2026-07-17"
    assert entry["hour_to"] == 11.0
    assert entry["editable"] is True
    assert "note" not in entry


def test_full_day_entry_is_not_partial(monkeypatch):
    _fake_db(monkeypatch, [{
        "name": "Adrian Aragon", "shape": "full_day",
        "hour_from": None, "hour_to": None, "state": "validate",
        "pay_type": "Paid Time Off",
    }])
    out = sto.time_off_entries_for_day(date(2026, 6, 1))
    assert out[0]["name"] == "Adrian Aragon"
    assert out[0]["hours"] is None
    assert out[0]["pending"] is False
    assert out[0]["pay_type"] == "Paid Time Off"
    assert out[0]["request_id"] == 1


def test_late_arrival_is_partial_with_time_range(monkeypatch):
    _fake_db(monkeypatch, [{
        "name": "Pascual Moreno", "shape": "late_arrival",
        "hour_from": 6.0, "hour_to": 9.0, "state": "validate",
        "pay_type": "Unpaid Time Off",
    }])
    out = sto.time_off_entries_for_day(date(2026, 6, 1))
    assert out[0]["hours"] == 3.0
    assert out[0]["time_range"] == "6:00am–9:00am"
    assert out[0]["pending"] is False


def test_pending_state_flagged(monkeypatch):
    _fake_db(monkeypatch, [{
        "name": "Juan Delgado", "shape": "full_day",
        "hour_from": None, "hour_to": None, "state": "confirm",
        "pay_type": "Paid Time Off",
    }])
    out = sto.time_off_entries_for_day(date(2026, 6, 1))
    assert out[0]["pending"] is True


def test_full_day_off_names_only_full(monkeypatch):
    _fake_db(monkeypatch, [
        {"name": "Full Person", "shape": "full_day", "hour_from": None,
         "hour_to": None, "state": "validate", "pay_type": "PTO"},
        {"name": "Partial Person", "shape": "early_leave", "hour_from": 12.0,
         "hour_to": 14.5, "state": "validate", "pay_type": "PTO"},
    ])
    full = sto.full_day_off_names(date(2026, 6, 1))
    assert full == {"Full Person"}


def test_entries_have_keys_the_template_reads(monkeypatch):
    _fake_db(monkeypatch, [{
        "name": "X", "shape": "midday_gap", "hour_from": 10.0,
        "hour_to": 12.0, "state": "validate", "pay_type": "PTO",
    }])
    e = sto.time_off_entries_for_day(date(2026, 6, 1))[0]
    for key in ("name", "hours", "pay_type", "time_range", "timing_label",
                "derived", "manual_absent", "pending"):
        assert key in e
    assert e["hours"] == 2.0
    assert e["time_range"] == "10:00am–12:00pm"


def test_timing_label_is_type_free_per_shape(monkeypatch):
    """The scheduler/Slack/print row shows a privacy-safe timing label, never
    the leave type. Full-day rows show no timing text (name only); the three
    partial shapes read 'arrives'/'leaves'/'gone' + time."""
    _fake_db(monkeypatch, [
        {"name": "Full", "shape": "full_day", "hour_from": None,
         "hour_to": None, "state": "validate", "pay_type": "Paid Time Off"},
        {"name": "Late", "shape": "late_arrival", "hour_from": 6.0,
         "hour_to": 7.5, "state": "validate", "pay_type": "Birthday Pay"},
        {"name": "Early", "shape": "early_leave", "hour_from": 14.0,
         "hour_to": 15.0, "state": "validate", "pay_type": "Unpaid Time Off"},
        {"name": "Gap", "shape": "midday_gap", "hour_from": 10.0,
         "hour_to": 12.0, "state": "validate", "pay_type": "Sick"},
    ])
    labels = {e["name"]: e["timing_label"]
              for e in sto.time_off_entries_for_day(date(2026, 6, 1))}
    assert labels["Full"] == ""
    assert labels["Late"] == "arrives 7:30am"
    assert labels["Early"] == "leaves 2:00pm"
    assert labels["Gap"] == "gone 10:00am–12:00pm"
    # Crucially, no leave-type name leaks into any label.
    for lt in ("Paid Time Off", "Birthday Pay", "Unpaid Time Off", "Sick"):
        assert all(lt not in v for v in labels.values())


def test_cleared_partial_is_filtered_out(monkeypatch):
    """A partial a supervisor cleared for the day (× 'actually worked') is
    dropped; a non-cleared partial stays; a full-day absence is never affected
    by a partial clear even if the name happens to be in the cleared set."""
    _fake_db(monkeypatch, [
        {"name": "Cleared P", "shape": "late_arrival", "hour_from": 6.0,
         "hour_to": 9.0, "state": "validate", "pay_type": "PTO"},
        {"name": "Kept P", "shape": "early_leave", "hour_from": 12.0,
         "hour_to": 14.5, "state": "validate", "pay_type": "PTO"},
        {"name": "Full P", "shape": "full_day", "hour_from": None,
         "hour_to": None, "state": "validate", "pay_type": "PTO"},
    ])
    import zira_dashboard.late_report as lr
    monkeypatch.setattr(
        lr, "cleared_partial_names_for_day",
        lambda day: {"Cleared P", "Full P"},
    )
    names = [e["name"] for e in sto.time_off_entries_for_day(date(2026, 6, 1))]
    assert "Cleared P" not in names   # partial + cleared -> dropped
    assert "Kept P" in names          # partial, not cleared -> kept
    assert "Full P" in names          # full-day -> unaffected by partial clear


def test_declared_absent_becomes_full_day_absent_entry(monkeypatch):
    # Bob is on Odoo full-day PTO; Ana has an approved partial; Carl is only
    # declared-absent. Declaring Ana absent must override her partial.
    _fake_db(monkeypatch, [
        {"name": "Bob", "shape": "full_day", "hour_from": None, "hour_to": None,
         "state": "validate", "pay_type": "Paid Time Off"},
        {"name": "Ana", "shape": "late_arrival", "hour_from": 6.0, "hour_to": 9.0,
         "state": "validate", "pay_type": "Unpaid Time Off"},
    ])
    import zira_dashboard.late_report as lr
    monkeypatch.setattr(
        lr,
        "absences_for_day",
        lambda day: [
            {"emp_id": "44", "name": "Ana"},
            {"emp_id": "45", "name": "Carl"},
        ],
    )

    out = {e["name"]: e for e in sto.time_off_entries_for_day(date(2026, 6, 1))}

    # Bob unchanged (not absent)
    assert out["Bob"]["pay_type"] == "Paid Time Off"
    assert out["Bob"]["manual_absent"] is False
    # Ana: her partial is replaced by a full-day Absent entry
    assert out["Ana"]["hours"] is None
    assert out["Ana"]["pay_type"] == "Absent"
    assert out["Ana"]["timing_label"] == "Absent"
    assert out["Ana"]["manual_absent"] is True
    # Carl: declared absent, not in the Odoo feed -> new Absent entry
    assert out["Carl"]["manual_absent"] is True
    assert out["Carl"]["hours"] is None


@pytest.mark.parametrize(
    ("state", "label"),
    [
        (None, "Absent"),
        ("denied", "Absent"),
        ("pending", "Absent · PTO pending"),
        ("converting", "Absent · PTO pending"),
        ("approved", "Absent · PTO"),
        ("needs_review", "Absent · PTO review"),
        ("resolved_manually", "Absent · handled"),
    ],
)
def test_manual_absence_keeps_attendance_and_adds_pay_suffix(
    monkeypatch, state, label
):
    day = date(2026, 8, 20)
    monkeypatch.setattr(sto, "_rows_for_day", lambda _day: [])
    monkeypatch.setattr(sto, "_cleared_partial_names", lambda _day: set())
    from zira_dashboard import late_report

    monkeypatch.setattr(
        late_report,
        "absences_for_day",
        lambda _day: [{"emp_id": "44", "name": "Ana"}],
    )
    queries = []

    def fake_query(sql, params=None):
        queries.append((sql, params))
        return [] if state is None else [{"emp_id": "44", "state": state}]

    monkeypatch.setattr(sto.db, "query", fake_query)

    row = sto.time_off_entries_for_day(day)[0]

    assert row["manual_absent"] is True
    assert row["pay_type"] == label
    assert row["timing_label"] == label
    assert row["hours"] is None
    assert row["pending"] is False
    assert row.get("editable", False) is False
    assert len(queries) == 1
    assert "absence_pto_requests" in queries[0][0]


def test_linked_absence_states_are_fetched_once_for_all_absent_employees(monkeypatch):
    day = date(2026, 8, 20)
    monkeypatch.setattr(sto, "_rows_for_day", lambda _day: [])
    monkeypatch.setattr(sto, "_cleared_partial_names", lambda _day: set())
    from zira_dashboard import late_report

    monkeypatch.setattr(
        late_report,
        "absences_for_day",
        lambda _day: [
            {"emp_id": "44", "name": "Ana"},
            {"emp_id": "45", "name": "Bea"},
        ],
    )
    queries = []

    def fake_query(sql, params=None):
        queries.append((sql, params))
        return [
            {"emp_id": "44", "state": "approved"},
            {"emp_id": "45", "state": "pending"},
        ]

    monkeypatch.setattr(sto.db, "query", fake_query)

    rows = {row["name"]: row for row in sto.time_off_entries_for_day(day)}

    assert len(queries) == 1
    assert set(queries[0][1][1]) == {"44", "45"}
    assert rows["Ana"]["pay_type"] == "Absent · PTO"
    assert rows["Bea"]["pay_type"] == "Absent · PTO pending"
    assert len(rows) == 2


def test_linked_state_lookup_failure_keeps_plain_attendance_absence(
    monkeypatch, caplog
):
    day = date(2026, 8, 20)
    monkeypatch.setattr(sto, "_rows_for_day", lambda _day: [])
    monkeypatch.setattr(sto, "_cleared_partial_names", lambda _day: set())
    from zira_dashboard import late_report

    monkeypatch.setattr(
        late_report,
        "absences_for_day",
        lambda _day: [{"emp_id": "44", "name": "Ana"}],
    )
    monkeypatch.setattr(
        sto,
        "_absence_pto_states",
        lambda *_: (_ for _ in ()).throw(RuntimeError("linked state unavailable")),
    )

    row = sto.time_off_entries_for_day(day)[0]

    assert row == {
        "name": "Ana",
        "hours": None,
        "pay_type": "Absent",
        "time_range": "",
        "timing_label": "Absent",
        "derived": False,
        "manual_absent": True,
        "pending": False,
    }
    assert "linked absence PTO state lookup failed" in caplog.text
