from contextlib import contextmanager

from zira_dashboard import db, odoo_sync


def test_roster_writer_acquires_the_celebration_source_transaction_lock(monkeypatch):
    commands = []

    class FakeCursor:
        def execute(self, sql, params=None):
            commands.append((sql, params))

    @contextmanager
    def fake_cursor():
        yield FakeCursor()

    monkeypatch.setattr(db, "cursor", fake_cursor)
    monkeypatch.setattr(odoo_sync, "_read_last_sync", lambda: None)
    monkeypatch.setattr(odoo_sync, "_write_last_sync", lambda _value: None)
    monkeypatch.setattr(odoo_sync, "_set_roster_sync_alert", lambda _value: None)
    monkeypatch.setattr(odoo_sync, "refresh_work_schedule_hours", lambda: None)
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employees",
        lambda: [{"id": 7, "name": "Lock Test", "active": True}],
    )
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employee_statuses",
        lambda: [{"id": 7, "active": True}],
    )
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skills_for", lambda _ids: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_spanish_skill_level_ids", lambda: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_columns_with_types", lambda: [])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_level_buckets", lambda: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_departments", lambda: [])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_work_schedules", lambda: [])

    assert odoo_sync.sync(force=True).ok is True
    assert commands[0] == (
        "SELECT pg_advisory_xact_lock(%s::bigint)",
        (7_243_094_217,),
    )
