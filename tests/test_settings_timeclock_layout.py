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
from datetime import datetime, time, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

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


@contextmanager
def _settings_cursor():
    yield MagicMock()


def test_attendance_location_settings_section_has_health_and_policy_contract():
    html = Path("src/zira_dashboard/templates/settings.html").read_text()

    assert "Work-center attendance" in html
    assert 'action="/settings/attendance-location"' in html
    assert 'name="rollout_mode"' in html
    assert 'name="department_requires_work_center"' in html
    assert "Mirror freshness" in html
    assert "Last full sweep" in html
    assert "Attendance baseline" in html
    assert "Mirror sync status" in html
    assert "attendance_location.baseline_completed_at" in html
    assert "attendance_location.last_error" in html
    assert "attendance_location.open_rows_not_refreshed" in html
    for field in (
        "open_rows_not_refreshed",
        "last_sweep_deletion_count",
        "conflict_minutes_today",
        "unmapped_minutes_today",
        "missing_minutes_today",
        "oldest_unassigned_age_seconds",
        "shadow_changed_worker_units",
        "correction_retries_today",
        "correction_verification_failures_today",
    ):
        assert f"attendance_location.readiness.{field}" in html


def test_off_context_exposes_source_health_without_live_build_writes_or_pii(monkeypatch):
    from zira_dashboard import attendance_location_policy as policy
    from zira_dashboard.routes import settings

    baseline_completed_at = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
    sync_error = "Odoo request timed out"
    queries: list[str] = []

    monkeypatch.setattr(
        policy,
        "get_rollout_config",
        lambda: policy.RolloutConfig(mode="off", cutover_at=None, live_gate=None),
    )
    monkeypatch.setattr(policy, "live_is_active", lambda: False)
    monkeypatch.setattr(settings.app_settings, "get_setting", lambda _key: None)

    def query(sql, *_args, **_kwargs):
        queries.append(sql)
        if "FROM departments" in sql:
            return []
        return [
            {
                "baseline_completed_at": baseline_completed_at,
                "mirror_freshness": baseline_completed_at,
                "last_full_sweep_completed_at": baseline_completed_at,
                "last_error": sync_error,
                "open_rows_not_refreshed": 3,
            }
        ]

    def forbidden(*_args, **_kwargs):
        pytest.fail("Off-mode Settings attempted an expensive build or write")

    monkeypatch.setattr(settings.db, "query", query)
    monkeypatch.setattr(settings.db, "execute", forbidden)
    monkeypatch.setattr(settings.app_settings, "set_setting", forbidden)
    monkeypatch.setattr(settings.attendance_readiness, "build_report", forbidden)
    monkeypatch.setattr(settings.odoo_client, "execute", forbidden)

    context = settings._attendance_location_context()

    assert len(queries) == 2
    health_sql = queries[1].lower()
    assert "select count(*)" in health_sql
    assert "odoo_attendance_mirror" in health_sql
    assert "employee_id" not in health_sql
    assert "employee_name" not in health_sql
    assert context["mode"] == "off"
    assert context["baseline_completed_at"] == baseline_completed_at
    assert context["last_error"] == sync_error
    assert context["open_rows_not_refreshed"] == 3
    assert context["readiness"] is None


def test_active_live_settings_ui_disables_off_and_explains_shadow_rollback(
    monkeypatch,
):
    from zira_dashboard import attendance_location_policy as policy
    from zira_dashboard.routes import settings

    monkeypatch.setattr(
        policy,
        "get_rollout_config",
        lambda: policy.RolloutConfig(mode="live", cutover_at=None, live_gate=None),
    )
    monkeypatch.setattr(policy, "live_is_active", lambda: True)
    monkeypatch.setattr(settings.db, "query", lambda *_args, **_kwargs: [])

    context = settings._attendance_location_context()
    html = Path("src/zira_dashboard/templates/settings.html").read_text()

    assert context["live_active"] is True
    assert (
        'value="off" {% if attendance_location.mode == \'off\' %}selected{% endif %} '
        "{% if attendance_location.live_active %}disabled{% endif %}"
    ) in html
    assert (
        'value="live" {% if attendance_location.mode == \'live\' %}selected{% endif %} '
        "{% if attendance_location.live_active %}disabled{% endif %}"
    ) in html
    assert "choose Shadow and set a future workday boundary" in html


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


def test_attendance_location_save_runs_fresh_live_readiness_off_event_loop(monkeypatch):
    from zira_dashboard.routes import settings

    monkeypatch.setattr(settings.auth, "request_is_super_admin", lambda _request: True)
    cutover = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(
        settings.attendance_readiness,
        "parse_local_cutover",
        lambda raw: cutover,
    )
    calls = []
    monkeypatch.setattr(
        settings.attendance_readiness,
        "schedule_live_cutover",
        lambda value, *, now_utc: calls.append((value, now_utc)),
    )

    response = asyncio.run(
        settings.settings_save_attendance_location(
            _FormRequest({"rollout_mode": "live", "cutover_at": "2026-09-02T07:00"})
        )
    )

    assert response.status_code == 200
    assert calls and calls[0][0] == cutover
    assert calls[0][1].tzinfo is timezone.utc


def test_attendance_location_route_schedules_active_live_rollback(monkeypatch):
    from zira_dashboard import attendance_location_policy as policy
    from zira_dashboard.routes import settings

    activated_at = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)
    rollback_at = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
    stored = {
        "value": {
            "mode": "live",
            "cutover_at": activated_at.isoformat(),
            "live_gate": {
                "checked_at": (activated_at - timedelta(minutes=1)).isoformat(),
                "report_digest": "b617a1c0" * 8,
                "activated_at": activated_at.isoformat(),
            },
        }
    }
    monkeypatch.setattr(settings.auth, "request_is_super_admin", lambda _request: True)
    monkeypatch.setattr(settings.db, "cursor", _settings_cursor)
    monkeypatch.setattr(policy.shift_config, "shift_start_for", lambda _day: time(7, 0))
    monkeypatch.setattr(policy.shift_config, "is_workday", lambda _day: True)
    monkeypatch.setattr(
        policy.shift_config,
        "snapshot_for",
        lambda _day, **_kwargs: SimpleNamespace(
            shift_start=time(7, 0),
            is_workday=True,
        ),
    )
    monkeypatch.setattr(policy.app_settings, "get_setting", lambda _key: stored["value"])
    monkeypatch.setattr(
        policy.app_settings,
        "set_setting",
        lambda _key, value, *, cur=None: stored.update(value=value),
    )
    monkeypatch.setattr(policy, "_utc_now", lambda: activated_at + timedelta(hours=1))

    response = asyncio.run(
        settings.settings_save_attendance_location(
            _FormRequest(
                {
                    "rollout_mode": "shadow",
                    "cutover_at": "2026-09-02T07:00",
                }
            )
        )
    )

    assert response.status_code == 200
    assert policy.live_is_active(now_utc=rollback_at - timedelta(seconds=1)) is True
    assert policy.live_is_active(now_utc=rollback_at) is True


@pytest.mark.parametrize(
    ("handler_name", "store_name"),
    [
        ("settings_save_schedule", "schedule_store"),
        ("settings_save_saturday_schedule", "saturday_schedule_store"),
    ],
)
def test_schedule_mutation_rejects_pending_attendance_boundary(
    monkeypatch,
    handler_name,
    store_name,
):
    from zira_dashboard.routes import settings

    store = getattr(settings, store_name, None)
    if store is None:
        from zira_dashboard import saturday_schedule_store

        store = saturday_schedule_store
    monkeypatch.setattr(settings.db, "cursor", _settings_cursor)
    default_schedule = getattr(store, "DEFAULT_SCHEDULE", None) or store.DEFAULT
    monkeypatch.setattr(store, "current", lambda: default_schedule)
    monkeypatch.setattr(
        settings.attendance_location_policy,
        "require_no_pending_boundary_cur",
        lambda _cur: (_ for _ in ()).throw(
            ValueError("attendance_rollout_boundary_pending")
        ),
    )
    monkeypatch.setattr(
        store,
        "save",
        lambda *_args, **_kwargs: pytest.fail("pending boundary allowed schedule write"),
    )

    response = asyncio.run(
        getattr(settings, handler_name)(
            _FormRequest(
                {
                    "shift_start": "07:00",
                    "shift_end": "15:30",
                    "weekday_0": "on",
                }
            )
        )
    )

    assert response.status_code == 409


def test_attendance_location_route_rejects_midday_rollback(monkeypatch):
    from zira_dashboard import attendance_location_policy as policy
    from zira_dashboard.routes import settings

    activated_at = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)
    stored = {
        "mode": "live",
        "cutover_at": activated_at.isoformat(),
        "live_gate": {
            "checked_at": (activated_at - timedelta(minutes=1)).isoformat(),
            "report_digest": "b617a1c0" * 8,
            "activated_at": activated_at.isoformat(),
        },
    }
    monkeypatch.setattr(settings.auth, "request_is_super_admin", lambda _request: True)
    monkeypatch.setattr(settings.db, "cursor", _settings_cursor)
    monkeypatch.setattr(policy.shift_config, "shift_start_for", lambda _day: time(7, 0))
    monkeypatch.setattr(policy.app_settings, "get_setting", lambda _key: stored)
    monkeypatch.setattr(
        policy.app_settings,
        "set_setting",
        lambda *_args, **_kwargs: pytest.fail("midday rollback was persisted"),
    )
    monkeypatch.setattr(policy, "_utc_now", lambda: activated_at + timedelta(hours=1))

    response = asyncio.run(
        settings.settings_save_attendance_location(
            _FormRequest(
                {
                    "rollout_mode": "shadow",
                    "cutover_at": "2026-09-02T12:00",
                }
            )
        )
    )

    assert response.status_code == 422
    assert response.body == b'{"ok":false,"error":"cutover_boundary_required"}'


def test_attendance_location_route_rejects_active_live_off_without_any_write(
    monkeypatch,
):
    from zira_dashboard import attendance_location_policy as policy
    from zira_dashboard.routes import settings

    activated_at = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)
    original = {
        "mode": "live",
        "cutover_at": activated_at.isoformat(),
        "live_gate": {
            "checked_at": (activated_at - timedelta(minutes=1)).isoformat(),
            "report_digest": "b617a1c0" * 8,
            "activated_at": activated_at.isoformat(),
        },
    }
    stored = {"value": original}
    cursor_entries = []
    department_writes = []

    @contextmanager
    def cursor():
        cursor_entries.append(True)
        yield MagicMock()

    monkeypatch.setattr(settings.auth, "request_is_super_admin", lambda _request: True)
    monkeypatch.setattr(settings.db, "cursor", cursor)
    monkeypatch.setattr(policy.app_settings, "get_setting", lambda _key: stored["value"])
    monkeypatch.setattr(
        policy.app_settings,
        "set_setting",
        lambda _key, value, *, cur=None: stored.update(value=value),
    )
    monkeypatch.setattr(policy, "_utc_now", lambda: activated_at + timedelta(hours=1))
    monkeypatch.setattr(
        settings.work_centers_store,
        "synced_departments",
        lambda: ["Assembly"],
    )
    monkeypatch.setattr(
        policy,
        "set_department_requirement",
        lambda name, required, *, cur=None: department_writes.append(
            (name, required)
        ),
    )

    response = asyncio.run(
        settings.settings_save_attendance_location(
            _FormRequest(
                {
                    "rollout_mode": "off",
                    "departments_present": "1",
                    "department_requires_work_center": ["Assembly"],
                }
            )
        )
    )

    assert response.status_code == 422
    assert response.body == b'{"ok":false,"error":"rollback_boundary_required"}'
    assert stored["value"] is original
    assert cursor_entries == [True]
    assert department_writes == []


def test_attendance_location_save_updates_shadow_and_department_choices(monkeypatch):
    from zira_dashboard.routes import settings

    saved_configs = []
    saved_departments = []
    monkeypatch.setattr(settings.auth, "request_is_super_admin", lambda _request: True)
    monkeypatch.setattr(settings.db, "cursor", _settings_cursor)
    monkeypatch.setattr(
        settings.work_centers_store,
        "synced_departments",
        lambda: ["Assembly", "Maintenance"],
    )
    monkeypatch.setattr(
        settings.attendance_location_policy,
        "set_rollout_config",
        lambda config, *, cur=None: saved_configs.append(config),
    )
    monkeypatch.setattr(
        settings.attendance_location_policy,
        "set_department_requirement",
        lambda name, required, *, cur=None: saved_departments.append((name, required)),
    )
    monkeypatch.setattr(
        settings.attendance_location_policy,
        "get_rollout_config_strict",
        lambda: settings.attendance_location_policy.RolloutConfig(
            mode="off", cutover_at=None, live_gate=None
        ),
    )
    monkeypatch.setattr(
        settings.attendance_location_policy,
        "live_is_active",
        lambda: False,
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


def test_attendance_location_save_rolls_back_every_write_on_department_failure(
    monkeypatch,
):
    from zira_dashboard import attendance_location_policy as policy
    from zira_dashboard.routes import settings

    class TransactionalDb:
        def __init__(self):
            self.durable = []
            self.cursor_entries = 0

        @contextmanager
        def cursor(self):
            self.cursor_entries += 1
            pending = []
            try:
                yield pending
            except Exception:
                raise
            else:
                self.durable.extend(pending)

    transactional_db = TransactionalDb()

    def persist(item, cur):
        if cur is None:
            transactional_db.durable.append(item)
        else:
            cur.append(item)

    def save_rollout(config, *, cur=None):
        persist(("rollout", config.mode), cur)

    def save_department(name, required, *, cur=None):
        if name == "Maintenance":
            raise ValueError("injected_department_failure")
        persist(("department", name, required), cur)

    monkeypatch.setattr(settings, "db", transactional_db, raising=False)
    monkeypatch.setattr(settings.auth, "request_is_super_admin", lambda _request: True)
    monkeypatch.setattr(
        settings.work_centers_store,
        "synced_departments",
        lambda: ["Assembly", "Maintenance"],
    )
    monkeypatch.setattr(
        policy,
        "get_rollout_config_strict",
        lambda: policy.RolloutConfig(mode="off", cutover_at=None, live_gate=None),
    )
    monkeypatch.setattr(policy, "lock_rollout_decision_cur", lambda _cur: None)
    monkeypatch.setattr(policy, "set_rollout_config", save_rollout)
    monkeypatch.setattr(policy, "set_department_requirement", save_department)
    monkeypatch.setattr(
        settings.attendance_readiness,
        "clear_shadow_evidence_cur",
        lambda cur: persist(("clear_shadow_evidence",), cur),
    )
    monkeypatch.setattr(
        settings.attendance_readiness,
        "clear_cutover_blocked_cur",
        lambda cur: persist(("clear_cutover_blocked",), cur),
    )
    monkeypatch.setattr(
        settings.attendance_readiness,
        "_record_rollout_audit_cur",
        lambda cur, **_kwargs: persist(("rollout_audit",), cur),
    )

    response = asyncio.run(
        settings.settings_save_attendance_location(
            _FormRequest(
                {
                    "rollout_mode": "off",
                    "departments_present": "1",
                    "department_requires_work_center": ["Assembly"],
                }
            )
        )
    )

    assert response.status_code == 422
    assert response.body == b'{"ok":false,"error":"injected_department_failure"}'
    assert transactional_db.cursor_entries == 1
    assert transactional_db.durable == []


def test_non_live_save_rejects_a_rollout_that_changed_before_the_fence(monkeypatch):
    from zira_dashboard import attendance_location_policy as policy
    from zira_dashboard.routes import settings

    checked_at = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    expected = policy.RolloutConfig(
        "live",
        checked_at,
        policy.LiveGate(checked_at - timedelta(minutes=1), "scheduled", None),
    )
    activated = policy.RolloutConfig(
        "live",
        checked_at,
        policy.LiveGate(checked_at, "boundary", checked_at),
    )
    lock_calls = []
    monkeypatch.setattr(settings.db, "cursor", _settings_cursor)
    monkeypatch.setattr(
        policy,
        "lock_rollout_decision_cur",
        lambda cur: lock_calls.append(cur),
    )
    monkeypatch.setattr(policy, "get_rollout_config_strict", lambda: activated)
    monkeypatch.setattr(
        policy,
        "set_rollout_config",
        lambda *_args, **_kwargs: pytest.fail("stale request overwrote activation"),
    )

    with pytest.raises(ValueError, match="rollout_save_superseded"):
        settings._save_non_live_attendance_location(
            mode="off",
            cutover_at=None,
            selected_departments=set(),
            departments=(),
            expected_config=expected,
        )

    assert len(lock_calls) == 1


def test_cancel_pending_live_schedule_appends_atomic_audit(monkeypatch):
    from zira_dashboard import attendance_location_policy as policy
    from zira_dashboard.routes import settings

    checked_at = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    pending = policy.RolloutConfig(
        "live",
        checked_at + timedelta(days=1),
        policy.LiveGate(checked_at, "scheduled", None),
    )
    cursor = MagicMock()
    audits = []

    @contextmanager
    def cursor_context():
        yield cursor

    monkeypatch.setattr(settings.db, "cursor", cursor_context)
    monkeypatch.setattr(policy, "lock_rollout_decision_cur", lambda _cur: None)
    monkeypatch.setattr(policy, "get_rollout_config_strict", lambda: pending)
    monkeypatch.setattr(policy, "_utc_now", lambda: checked_at)
    monkeypatch.setattr(policy, "set_rollout_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        settings.attendance_readiness,
        "_record_rollout_audit_cur",
        lambda cur, **kwargs: audits.append((cur, kwargs)),
    )

    settings._save_non_live_attendance_location(
        mode="shadow",
        cutover_at=None,
        selected_departments=set(),
        departments=(),
        expected_config=pending,
    )

    assert audits == [
        (
            cursor,
            {
                "event_kind": "live_cancelled",
                "rollout_mode": "shadow",
                "cutover_at": pending.cutover_at,
                "checked_at": checked_at,
                "report_fingerprint": "scheduled",
            },
        )
    ]


def test_entering_shadow_starts_a_new_observation_epoch_and_clears_old_proof(
    monkeypatch,
):
    from zira_dashboard import attendance_location_policy as policy
    from zira_dashboard.routes import settings

    previous = policy.RolloutConfig("off", None, None)
    setting_writes = []
    cursor = MagicMock()

    @contextmanager
    def cursor_context():
        yield cursor

    monkeypatch.setattr(settings.db, "cursor", cursor_context)
    monkeypatch.setattr(policy, "lock_rollout_decision_cur", lambda _cur: None)
    monkeypatch.setattr(policy, "get_rollout_config_strict", lambda: previous)
    monkeypatch.setattr(policy, "_utc_now", lambda: datetime(2026, 9, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(policy, "set_rollout_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        settings.attendance_readiness,
        "start_shadow_epoch_cur",
        lambda cur, *, entered_at: setting_writes.append((cur, entered_at)),
    )

    settings._save_non_live_attendance_location(
        mode="shadow",
        cutover_at=None,
        selected_departments=set(),
        departments=(),
        expected_config=previous,
    )

    assert setting_writes == [(cursor, datetime(2026, 9, 1, tzinfo=timezone.utc))]


def test_explicit_off_clears_shadow_proof_and_cutover_blocker(monkeypatch):
    from zira_dashboard import attendance_location_policy as policy
    from zira_dashboard.routes import settings

    previous = policy.RolloutConfig("shadow", None, None)
    cursor = MagicMock()
    cleared = []

    @contextmanager
    def cursor_context():
        yield cursor

    monkeypatch.setattr(settings.db, "cursor", cursor_context)
    monkeypatch.setattr(policy, "lock_rollout_decision_cur", lambda _cur: None)
    monkeypatch.setattr(policy, "get_rollout_config_strict", lambda: previous)
    monkeypatch.setattr(
        policy,
        "_utc_now",
        lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(policy, "set_rollout_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        settings.attendance_readiness,
        "clear_shadow_evidence_cur",
        lambda cur: cleared.append(("shadow", cur)),
    )
    monkeypatch.setattr(
        settings.attendance_readiness,
        "clear_cutover_blocked_cur",
        lambda cur: cleared.append(("cutover", cur)),
        raising=False,
    )
    monkeypatch.setattr(
        settings.attendance_readiness,
        "_record_rollout_audit_cur",
        lambda *_args, **_kwargs: None,
    )

    settings._save_non_live_attendance_location(
        mode="off",
        cutover_at=None,
        selected_departments=set(),
        departments=(),
        expected_config=previous,
    )

    assert cleared == [("shadow", cursor), ("cutover", cursor)]
