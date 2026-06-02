from datetime import date
from zira_dashboard import scheduler_time_off as sto


def _fake_db(monkeypatch, rows):
    monkeypatch.setattr(sto.db, "query", lambda sql, params=None: rows)
    # time_off_entries_for_day also queries cleared_partials_by_name (via
    # late_report). With the broad db.query stub above, that query would
    # return `rows` too — misreading the time-off names as "cleared" and
    # filtering the partial entries out. Default to "nothing cleared"; the
    # clear-specific test re-stubs this afterward (monkeypatch last wins).
    from zira_dashboard import late_report
    monkeypatch.setattr(late_report, "cleared_partial_names_for_day", lambda day: set())


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
    assert "request_id" not in out[0]


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
