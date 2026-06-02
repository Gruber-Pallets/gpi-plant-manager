"""Characterization + refactor tests for the Timeclock settings panel.

Task 1's two contract tests lock the field names and form endpoints that the
sub-tabs/autosave refactor must NOT change — they are the contract between the
settings UI and the punch -> Odoo hr.attendance sync path. They pass against the
PRE-refactor template and must stay green through every task. The remaining tests
are added by later tasks (each fails before its task, passes after).

Postgres-backed, same gate as the sibling settings tests.
"""
import os

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard import db, work_schedule_store, odoo_client

pytestmark = pytest.mark.skipif(
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
                   "/settings/rounding", "/settings/auto_lunch"):
        assert f'action="{action}"' in html, action


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
        for action in ("/settings/work_schedule_rounding",
                       "/settings/work_schedule_rounding/add",
                       "/settings/work_schedule_rounding/remove"):
            assert f'action="{action}"' in html, action
    finally:
        _drop_override()


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


def test_rules_tab_orders_autolunch_after_per_schedule():
    r = client.get("/settings?section=timeclock")
    assert r.status_code == 200
    html = r.text
    assert "Per-schedule rounding" in html
    assert "Auto-Lunch" in html
    assert html.index("Per-schedule rounding") < html.index("Auto-Lunch"), \
        "Auto-Lunch should sit below the rounding block"
