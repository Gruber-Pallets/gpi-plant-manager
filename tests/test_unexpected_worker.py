from datetime import date

from zira_dashboard import unexpected_worker


TODAY = date(2026, 7, 17)


def test_approved_full_day_leave_finds_only_approved_full_day_leave(monkeypatch):
    expected = {
        "id": 42,
        "odoo_leave_id": 314,
        "person_odoo_id": 7,
        "date_from": TODAY,
        "date_to": TODAY,
    }
    calls = []

    def fake_query(sql, params):
        calls.append((sql, params))
        return [expected]

    monkeypatch.setattr(unexpected_worker.db, "query", fake_query)

    assert unexpected_worker.approved_full_day_leave(7, TODAY) == expected
    assert calls[0][1] == (7, TODAY, TODAY)
    assert "state = 'validate'" in calls[0][0]
    assert "shape = 'full_day'" in calls[0][0]


def test_approved_full_day_leave_returns_none_when_no_match(monkeypatch):
    monkeypatch.setattr(unexpected_worker.db, "query", lambda sql, params: [])

    assert unexpected_worker.approved_full_day_leave(7, TODAY) is None


def test_record_reuses_the_event_for_the_same_worker_and_day(monkeypatch):
    event = {
        "id": 9,
        "day": TODAY,
        "person_odoo_id": 7,
        "time_off_request_id": 42,
        "odoo_leave_id": 314,
        "clock_in_wc": "Repair 1",
    }
    calls = []

    def fake_query(sql, params):
        calls.append((sql, params))
        return [event]

    monkeypatch.setattr(unexpected_worker.db, "query", fake_query)

    assert unexpected_worker.record(
        day=TODAY,
        person_odoo_id=7,
        leave={"id": 42, "odoo_leave_id": 314},
        clock_in_wc="Repair 1",
    ) == event
    assert "ON CONFLICT (day, person_odoo_id) DO NOTHING" in calls[0][0]
    assert calls[0][1] == (TODAY, 7, 42, 314, "Repair 1")


def test_record_reads_existing_event_after_conflicting_insert(monkeypatch):
    """A concurrent insert must still return the event that won the race."""
    existing_event = {
        "id": 9,
        "day": TODAY,
        "person_odoo_id": 7,
        "time_off_request_id": 42,
        "odoo_leave_id": 314,
        "clock_in_wc": "Repair 1",
    }
    calls = []

    def fake_query(sql, params):
        calls.append((sql, params))
        return [] if sql.startswith("INSERT") else [existing_event]

    monkeypatch.setattr(unexpected_worker.db, "query", fake_query)

    assert unexpected_worker.record(
        day=TODAY,
        person_odoo_id=7,
        leave={"id": 42, "odoo_leave_id": 314},
        clock_in_wc="Repair 1",
    ) == existing_event
    assert len(calls) == 2
    assert calls[0][0].startswith("INSERT")
    assert calls[1][0].startswith("SELECT")
    assert calls[1][1] == (TODAY, 7)


def test_open_events_resolves_worker_placed_in_published_schedule(monkeypatch):
    calls = []
    open_event = {
        "id": 9,
        "day": TODAY,
        "person_odoo_id": 7,
        "person_name": "Maria Delgado",
        "clock_in_wc": "Repair 1",
    }

    def fake_execute(sql, params):
        calls.append(("execute", sql, params))

    def fake_query(sql, params):
        calls.append(("query", sql, params))
        if "FROM unexpected_worker_events" in sql and "person_name" not in sql:
            return [{"id": 9, "person_odoo_id": 7}]
        if "FROM schedules" in sql:
            return [{"person_odoo_id": 7}]
        return [open_event]

    monkeypatch.setattr(unexpected_worker.db, "execute", fake_execute)
    monkeypatch.setattr(unexpected_worker.db, "query", fake_query)

    assert unexpected_worker.open_events(TODAY) == [open_event]
    assert calls[0][0] == "query"
    assert "resolved_at IS NULL" in calls[0][1]
    assert calls[0][2] == (TODAY,)
    assert calls[1][0] == "query"
    assert "s.published = TRUE" in calls[1][1]
    assert calls[2][0] == "execute"
    assert calls[2][2] == ([9],)
    assert "EXISTS" in calls[2][1]
    assert "s.day = uwe.day" in calls[2][1]
    assert "pe.odoo_id = uwe.person_odoo_id" in calls[2][1]
    assert "s.published = TRUE" in calls[2][1]
    assert calls[3][0] == "query"
    assert "resolved_at IS NULL" in calls[3][1]


def test_open_events_keeps_event_open_for_unpublished_same_worker_placement(monkeypatch):
    calls = []
    open_event = {"id": 9, "day": TODAY, "person_odoo_id": 7}

    def fake_query(sql, params):
        calls.append(("query", sql, params))
        if "FROM unexpected_worker_events" in sql and "person_name" not in sql:
            return [{"id": 9, "person_odoo_id": 7}]
        if "FROM schedules" in sql:
            return []
        return [open_event]

    monkeypatch.setattr(unexpected_worker.db, "query", fake_query)
    monkeypatch.setattr(
        unexpected_worker.db,
        "execute",
        lambda sql, params: calls.append(("execute", sql, params)),
    )

    assert unexpected_worker.open_events(TODAY) == [open_event]
    assert not [call for call in calls if call[0] == "execute"]


def test_open_events_keeps_event_open_for_published_different_worker_placement(monkeypatch):
    calls = []
    open_event = {"id": 9, "day": TODAY, "person_odoo_id": 7}

    def fake_query(sql, params):
        calls.append(("query", sql, params))
        if "FROM unexpected_worker_events" in sql and "person_name" not in sql:
            return [{"id": 9, "person_odoo_id": 7}]
        if "FROM schedules" in sql:
            return [{"person_odoo_id": 8}]
        return [open_event]

    monkeypatch.setattr(unexpected_worker.db, "query", fake_query)
    monkeypatch.setattr(
        unexpected_worker.db,
        "execute",
        lambda sql, params: calls.append(("execute", sql, params)),
    )

    assert unexpected_worker.open_events(TODAY) == [open_event]
    assert not [call for call in calls if call[0] == "execute"]


def test_unpublished_placement_leaves_unexpected_event_open():
    """A draft schedule assignment must not clear the event."""
    assert not unexpected_worker.placement_resolves_unexpected_event(
        event_person_odoo_id=7,
        assigned_person_odoo_id=7,
        schedule_published=False,
    )


def test_different_workers_placement_leaves_unexpected_event_open():
    """Only an assignment for the event worker can clear the event."""
    assert not unexpected_worker.placement_resolves_unexpected_event(
        event_person_odoo_id=7,
        assigned_person_odoo_id=8,
        schedule_published=True,
    )
