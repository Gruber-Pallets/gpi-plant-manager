"""Characterization + refactor tests for the Timeclock settings panel.

Task 1's two contract tests lock the field names and form endpoints that the
sub-tabs/autosave refactor must NOT change — they are the contract between the
settings UI and the punch -> Odoo hr.attendance sync path. They pass against the
PRE-refactor template and must stay green through every task. The remaining tests
are added by later tasks (each fails before its task, passes after).

The existing render tests are Postgres-backed, with the same gate as sibling
Settings tests. Attendance-location source and route contracts run everywhere.
"""
import asyncio
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard import db, work_schedule_store, odoo_client

requires_postgres = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres"
)

client = TestClient(app)
CAL_ID = 990077


def _seed_override():
    db.execute("DELETE FROM work_schedules WHERE resource_calendar_id = %s", (CAL_ID,))
    work_schedule_store.create(CAL_ID, "Contract-Test Schedule")
    work_schedule_store.reload()


def _drop_override():
    db.execute("DELETE FROM work_schedules WHERE resource_calendar_id = %s", (CAL_ID,))
    work_schedule_store.reload()


@requires_postgres
def test_timeclock_panel_preserves_core_field_contract():
    r = client.get("/settings?section=timeclock")
    assert r.status_code == 200
    html = r.text
    # Company Schedule + Saturday Default fields
    for name in ("shift_start", "shift_end", "weekday_0", "weekday_6"):
        assert f'name="{name}"' in html, name
    # Default rounding windows
    for name in ("in_before_min", "in_after_min", "out_before_min", "out_after_min"):
        assert f'name="{name}"' in html, name
    # Auto-Lunch fields
    for name in ("mode", "flex_after_hours", "flex_minutes"):
        assert f'name="{name}"' in html, name
    # Always-rendered form endpoints
    for action in ("/settings/schedule", "/settings/saturday_schedule",
                   "/settings/auto_lunch"):
        assert f'action="{action}"' in html, action


@requires_postgres
def test_timeclock_panel_preserves_per_schedule_contract(monkeypatch):
    # Seed one override (so its card + remove form render) and stub Odoo so the
    # "Add a schedule" form renders too.
    _seed_override()
    monkeypatch.setattr(
        odoo_client, "fetch_work_schedules",
        lambda: [{"id": CAL_ID + 1, "name": "Another Schedule"}],
    )
    try:
        r = client.get("/settings?section=timeclock")
        assert r.status_code == 200
        html = r.text
        assert 'name="resource_calendar_id"' in html
        # The bare /settings/work_schedule_rounding SAVE form was removed when
        # punch rounding moved from per-schedule to department-driven systems:
        # the per-schedule block is now hours-only ("Custom shift hours"), so
        # only its add/remove endpoints remain. The rounding windows now live
        # under the rounding-system + department-mapping endpoints.
        for action in ("/settings/work_schedule_rounding/add",
                       "/settings/work_schedule_rounding/remove",
                       "/settings/rounding_system/add",
                       "/settings/department_rounding"):
            assert f'action="{action}"' in html, action
    finally:
        _drop_override()


@requires_postgres
def test_timeclock_panel_renders_subtabs():
    r = client.get("/settings?section=timeclock")
    assert r.status_code == 200
    html = r.text
    for marker in ('data-tc-tab="schedules"',
                   'data-tc-tab="rules"',
                   'data-tc-tab="activity"'):
        assert marker in html, marker
    for pid in ('id="tc-tab-schedules"',
                'id="tc-tab-rules"',
                'id="tc-tab-activity"'):
        assert pid in html, pid


@requires_postgres
def test_rules_tab_orders_autolunch_after_per_schedule():
    r = client.get("/settings?section=timeclock")
    assert r.status_code == 200
    html = r.text
    # "Per-schedule rounding" was renamed "Custom shift hours" when rounding
    # became department-driven; it's the last rounding block before Auto-Lunch.
    assert "Custom shift hours" in html
    assert "Auto-Lunch" in html
    assert html.index("Custom shift hours") < html.index("Auto-Lunch"), \
        "Auto-Lunch should sit below the rounding blocks"


@requires_postgres
def test_rules_forms_have_no_explicit_save_buttons(monkeypatch):
    _seed_override()
    monkeypatch.setattr(
        odoo_client, "fetch_work_schedules",
        lambda: [{"id": CAL_ID + 1, "name": "Another Schedule"}],
    )
    try:
        r = client.get("/settings?section=timeclock")
        assert r.status_code == 200
        html = r.text
        # The old explicit Save buttons are gone (autosave replaces them).
        assert "Save Rounding" not in html
        assert "Save Auto-Lunch" not in html
        # The per-schedule window form is tagged for the autosaver.
        assert "ws-rounding-fields" in html
        # Structural action buttons remain.
        assert 'action="/settings/work_schedule_rounding/add"' in html
        assert 'action="/settings/work_schedule_rounding/remove"' in html
    finally:
        _drop_override()


@requires_postgres
def test_helper_text_unified_to_help_class():
    r = client.get("/settings?section=timeclock")
    assert r.status_code == 200
    html = r.text
    # The ad-hoc rounding blurb class is replaced by the shared .help style.
    assert 'class="rounding-blurb"' not in html
    assert 'class="help"' in html


@requires_postgres
def test_add_redirects_to_rules_tab(monkeypatch):
    monkeypatch.setattr(odoo_client, "fetch_work_schedules",
                        lambda: [{"id": CAL_ID, "name": "Drivers"}])
    monkeypatch.setattr(odoo_client, "fetch_calendar_hours",
                        lambda ids: {CAL_ID: {"0": ["05:45", "14:30"]}})
    _drop_override()
    try:
        r = client.post("/settings/work_schedule_rounding/add",
                        data={"resource_calendar_id": str(CAL_ID)},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].endswith("#rules")
    finally:
        _drop_override()


@requires_postgres
def test_remove_redirects_to_rules_tab():
    work_schedule_store.create(CAL_ID, "Drivers")
    work_schedule_store.reload()
    try:
        r = client.post("/settings/work_schedule_rounding/remove",
                        data={"resource_calendar_id": str(CAL_ID)},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].endswith("#rules")
    finally:
        _drop_override()


class _FormValues(dict):
    def getlist(self, key):
        value = self.get(key, [])
        return value if isinstance(value, list) else [value]


class _FormRequest:
    def __init__(self, values):
        self._values = _FormValues(values)
        self.headers = {"accept": "application/json"}

    async def form(self):
        return self._values


def test_attendance_location_settings_section_has_health_and_policy_contract():
    html = Path("src/zira_dashboard/templates/settings.html").read_text()

    assert "Work-center attendance" in html
    assert 'action="/settings/attendance-location"' in html
    assert 'name="rollout_mode"' in html
    assert 'name="department_requires_work_center"' in html
    assert "Mirror freshness" in html
    assert "Last full sweep" in html


def test_attendance_location_save_is_super_admin_only(monkeypatch):
    from zira_dashboard.routes import settings

    monkeypatch.setattr(settings.auth, "request_is_super_admin", lambda _request: False)
    monkeypatch.setattr(
        settings.attendance_location_policy,
        "set_rollout_config",
        lambda *_args, **_kwargs: pytest.fail("manager changed rollout"),
    )
    monkeypatch.setattr(
        settings.attendance_location_policy,
        "set_department_requirement",
        lambda *_args, **_kwargs: pytest.fail("manager changed department policy"),
    )

    response = asyncio.run(
        settings.settings_save_attendance_location(
            _FormRequest({"rollout_mode": "shadow"})
        )
    )

    assert response.status_code == 403
    assert response.body == b'{"ok":false,"error":"super_admin_required"}'


def test_attendance_location_save_rejects_live_until_readiness_exists(monkeypatch):
    from zira_dashboard.routes import settings

    monkeypatch.setattr(settings.auth, "request_is_super_admin", lambda _request: True)
    monkeypatch.setattr(
        settings.attendance_location_policy,
        "set_rollout_config",
        lambda *_args, **_kwargs: pytest.fail("ungated live config was persisted"),
    )

    response = asyncio.run(
        settings.settings_save_attendance_location(
            _FormRequest({"rollout_mode": "live"})
        )
    )

    assert response.status_code == 422
    assert response.body == b'{"ok":false,"error":"live_readiness_required"}'


def test_attendance_location_save_updates_shadow_and_department_choices(monkeypatch):
    from zira_dashboard.routes import settings

    saved_configs = []
    saved_departments = []
    monkeypatch.setattr(settings.auth, "request_is_super_admin", lambda _request: True)
    monkeypatch.setattr(
        settings.work_centers_store,
        "synced_departments",
        lambda: ["Assembly", "Maintenance"],
    )
    monkeypatch.setattr(
        settings.attendance_location_policy,
        "set_rollout_config",
        lambda config: saved_configs.append(config),
    )
    monkeypatch.setattr(
        settings.attendance_location_policy,
        "set_department_requirement",
        lambda name, required: saved_departments.append((name, required)),
    )

    response = asyncio.run(
        settings.settings_save_attendance_location(
            _FormRequest(
                {
                    "rollout_mode": "shadow",
                    "departments_present": "1",
                    "department_requires_work_center": ["Assembly"],
                }
            )
        )
    )

    assert response.status_code == 200
    assert saved_configs == [
        settings.attendance_location_policy.RolloutConfig(
            mode="shadow", cutover_at=None, live_gate=None
        )
    ]
    assert saved_departments == [("Assembly", True), ("Maintenance", False)]
