from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest

from zira_dashboard._schema import SCHEMA_DDL
from zira_dashboard.payroll_work_entry_rules import Decision
import zira_dashboard.payroll_work_entry_store as store


def correction_decision() -> Decision:
    return Decision(
        kind="correct",
        employee_id=19,
        employee_name="Isidro Moctezuma Aviles",
        work_date=date(2026, 7, 24),
        reason_codes=(),
        action="duration_update",
        work_entry_id=8502,
        attendance_id=3811,
        before_duration=3.621388889,
        after_duration=3.121355556,
        attendance_regular=3.121355556,
        attendance_overtime=5.3092,
        work_regular=3.621388889,
        work_overtime=5.309166667,
    )


def test_schema_defines_append_only_audit_and_singleton_monitor():
    assert "CREATE TABLE IF NOT EXISTS payroll_work_entry_corrections" in SCHEMA_DDL
    assert (
        "action TEXT NOT NULL CHECK (action IN ('duration_update', "
        "'delete_zero_regular'))" in SCHEMA_DDL
    )
    assert "CREATE INDEX IF NOT EXISTS payroll_work_entry_corrections_entry_idx" in SCHEMA_DDL
    assert "CREATE TABLE IF NOT EXISTS payroll_work_entry_guard_monitor" in SCHEMA_DDL
    assert "DEFAULT 1 CHECK (id = 1)" in SCHEMA_DDL
    assert "reported_issue_keys   TEXT[] NOT NULL DEFAULT '{}'" in SCHEMA_DDL
    assert "updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()" in SCHEMA_DDL


def test_append_correction_inserts_every_audit_value_in_column_order(monkeypatch):
    execute = MagicMock()
    monkeypatch.setattr(store.db, "execute", execute)
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)

    store.append_correction(correction_decision(), "duration reread matched", now)

    sql, params = execute.call_args.args
    normalized_sql = " ".join(sql.split())
    assert (
        "INSERT INTO payroll_work_entry_corrections "
        "(odoo_work_entry_id, action, employee_odoo_id, employee_name, work_date, "
        "before_duration, after_duration, attendance_regular, attendance_overtime, "
        "work_regular_before, work_overtime, verification_detail, corrected_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        in normalized_sql
    )
    assert params == (
        8502,
        "duration_update",
        19,
        "Isidro Moctezuma Aviles",
        date(2026, 7, 24),
        3.621388889,
        3.121355556,
        3.121355556,
        5.3092,
        3.621388889,
        5.309166667,
        "duration reread matched",
        now,
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"kind": "review"}, "correct"),
        ({"action": None}, "action"),
        ({"work_entry_id": None}, "Work Entry id"),
        ({"after_duration": None}, "after duration"),
    ],
)
def test_append_correction_rejects_incomplete_or_non_correction_decisions(
    monkeypatch, changes, message
):
    execute = MagicMock()
    monkeypatch.setattr(store.db, "execute", execute)
    decision = replace(correction_decision(), **changes)

    with pytest.raises(ValueError, match=message):
        store.append_correction(
            decision,
            "must not be stored",
            datetime(2026, 8, 3, 20, 0, tzinfo=UTC),
        )

    execute.assert_not_called()


def test_append_correction_accepts_zero_after_duration_for_verified_deletion(
    monkeypatch,
):
    execute = MagicMock()
    monkeypatch.setattr(store.db, "execute", execute)
    decision = replace(
        correction_decision(), action="delete_zero_regular", after_duration=0
    )

    store.append_correction(
        decision,
        "entry reread confirmed deletion",
        datetime(2026, 8, 3, 20, 0, tzinfo=UTC),
    )

    assert execute.call_args.args[1][6] == 0


def test_load_monitor_state_uses_singleton_and_defaults_when_absent(monkeypatch):
    query = MagicMock(return_value=[])
    monkeypatch.setattr(store.db, "query", query)

    assert store.load_monitor_state() == {
        "odoo_task_id": None,
        "reported_issue_keys": [],
    }
    assert "WHERE id = 1" in query.call_args.args[0]


def test_load_monitor_state_returns_the_persisted_values(monkeypatch):
    monkeypatch.setattr(
        store.db,
        "query",
        lambda *_: [{"odoo_task_id": 44, "reported_issue_keys": None}],
    )

    assert store.load_monitor_state() == {
        "odoo_task_id": 44,
        "reported_issue_keys": [],
    }


def test_save_monitor_state_upserts_sorted_deduplicated_issue_keys(monkeypatch):
    execute = MagicMock()
    monkeypatch.setattr(store.db, "execute", execute)
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)

    store.save_monitor_state(
        44,
        ["9:2026-07-24:z", "9:2026-07-24:a", "9:2026-07-24:z"],
        now,
    )

    sql, params = execute.call_args.args
    normalized_sql = " ".join(sql.split())
    assert (
        "INSERT INTO payroll_work_entry_guard_monitor "
        "(id, odoo_task_id, reported_issue_keys, updated_at) VALUES (1, %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET "
        "odoo_task_id = EXCLUDED.odoo_task_id, "
        "reported_issue_keys = EXCLUDED.reported_issue_keys, "
        "updated_at = EXCLUDED.updated_at"
        in normalized_sql
    )
    assert params == (44, ["9:2026-07-24:a", "9:2026-07-24:z"], now)
