"""time_off_local_backfill — replays locally-recorded absences
(time_off_requests.local_record) into Odoo once Odoo will accept them.

A local record exists because Odoo rejected validation (no working hours on
the day: a holiday record or a calendar gap). When the Odoo data is later
corrected, the reconciler drafts/confirms/approves the refused copy and
hands ownership back to the poller. Attempts are prediction-gated and
backoff-limited so a doomed replay never leaves the Odoo copy churning or
pending, and adoption is guarded so a cancel/deny racing the replay wins.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from zira_dashboard import time_off_local_backfill as bf


@pytest.fixture
def fake_db(monkeypatch):
    captured: dict = {"queries": [], "executes": []}

    def fake_query(sql, params=None):
        captured["queries"].append((sql, params))
        # Guarded UPDATE ... RETURNING (adoption) gets its own knob so a
        # test can simulate the row being settled mid-replay.
        if sql.startswith("UPDATE"):
            return captured.get("update_result", [{"id": 80}])
        return captured.get("query_result", [])

    def fake_execute(sql, params=None):
        captured["executes"].append((sql, params))

    monkeypatch.setattr(bf.db, "query", fake_query)
    monkeypatch.setattr(bf.db, "execute", fake_execute)
    return captured


def _row(**over):
    row = {
        "id": 80, "person_odoo_id": 13, "shape": "full_day",
        "holiday_status_id": 78,
        "date_from": date(2026, 7, 3), "date_to": date(2026, 7, 3),
        "hour_from": None, "hour_to": None, "note": None,
        "odoo_leave_id": 112, "backfill_attempts": 0,
    }
    row.update(over)
    return row


def _wire_odoo(monkeypatch, *, employees=None, cal_hours=None, holidays=None,
               leave_state="refuse"):
    """Stub the odoo_client surface the reconciler reads. Returns the dict
    of action mocks (draft/approve/refuse) for assertions."""
    oc = bf.odoo_client
    monkeypatch.setattr(oc, "fetch_employees", lambda: employees if employees is not None else [
        {"id": 13, "name": "Gerardo Garcia", "resource_calendar_id": [1, "Plant 40 hours/week"]},
    ])
    monkeypatch.setattr(oc, "fetch_calendar_hours", lambda ids: cal_hours if cal_hours is not None else {
        1: {"0": ["07:00", "15:30"], "1": ["07:00", "15:30"], "2": ["07:00", "15:30"],
            "3": ["07:00", "15:30"], "4": ["07:00", "15:30"]},
    })
    monkeypatch.setattr(oc, "fetch_public_holidays", lambda s, e: holidays if holidays is not None else [])
    monkeypatch.setattr(oc, "fetch_leave_state", lambda lid: leave_state)
    mocks = {
        "draft": MagicMock(), "approve": MagicMock(return_value="validate"),
        "refuse": MagicMock(),
    }
    monkeypatch.setattr(oc, "draft_leave", mocks["draft"])
    monkeypatch.setattr(oc, "approve_leave", mocks["approve"])
    monkeypatch.setattr(oc, "refuse_leave", mocks["refuse"])
    return mocks


def test_run_once_no_rows_makes_no_odoo_calls(monkeypatch, fake_db):
    fetch = MagicMock()
    monkeypatch.setattr(bf.odoo_client, "fetch_employees", fetch)

    assert bf.run_once() == 0
    fetch.assert_not_called()


def test_candidate_select_is_guarded_and_capped(fake_db):
    """The SELECT itself carries the load-bearing filters: only approved
    local records (a cancelled/denied local record must never be replayed
    into a validated Odoo leave), only rows whose backoff window elapsed,
    capped at _ATTEMPT_LIMIT."""
    bf.run_once()
    sql, params = fake_db["queries"][0]
    assert "local_record" in sql
    assert "state = 'validate'" in sql
    assert "backfill_next_at" in sql
    assert params == (bf._ATTEMPT_LIMIT,)


def test_company_wide_holiday_blocks_and_schedules_recheck(monkeypatch, fake_db):
    fake_db["query_result"] = [_row()]
    mocks = _wire_odoo(monkeypatch, holidays=[
        # The exact prod shape: global '4th of July' covering 2026-07-03
        # (UTC datetimes for a Central midnight-to-midnight day).
        {"id": 1, "name": "4th of July", "calendar_id": False,
         "date_from": "2026-07-03 05:00:00", "date_to": "2026-07-04 04:59:59"},
    ])

    assert bf.run_once() == 0
    mocks["draft"].assert_not_called()
    mocks["approve"].assert_not_called()
    mocks["refuse"].assert_not_called()
    # Prediction-skips rotate out of the LIMIT window so permanently-local
    # rows can't starve newer replayable rows.
    skips = [e for e in fake_db["executes"] if "backfill_next_at" in e[0]]
    assert len(skips) == 1


def test_holiday_scoped_to_another_calendar_does_not_block(monkeypatch, fake_db):
    """The flagship remediation: HR scopes the holiday record to the office
    calendar (plant actually worked). A plant-calendar employee's absence
    must then replay."""
    fake_db["query_result"] = [_row()]
    mocks = _wire_odoo(monkeypatch, holidays=[
        {"id": 1, "name": "4th of July", "calendar_id": [3, "Flexible"],
         "date_from": "2026-07-03 05:00:00", "date_to": "2026-07-04 04:59:59"},
    ])

    assert bf.run_once() == 1
    mocks["approve"].assert_called_once_with(112)


def test_holiday_scoped_to_the_employees_calendar_blocks(monkeypatch, fake_db):
    fake_db["query_result"] = [_row()]
    mocks = _wire_odoo(monkeypatch, holidays=[
        {"id": 1, "name": "4th of July", "calendar_id": [1, "Plant 40 hours/week"],
         "date_from": "2026-07-03 05:00:00", "date_to": "2026-07-04 04:59:59"},
    ])

    assert bf.run_once() == 0
    mocks["approve"].assert_not_called()


def test_holiday_blocks_only_its_plant_local_dates(monkeypatch, fake_db):
    """A CT midnight-to-midnight holiday is stored as 05:00 UTC -> 04:59:59
    UTC next day. Only the plant-local date is blocked — an absence on the
    DAY AFTER the holiday must stay replayable (holiday records are
    permanent, so over-blocking would freeze such rows forever)."""
    fake_db["query_result"] = [_row(date_from=date(2026, 7, 6),
                                    date_to=date(2026, 7, 6))]  # Monday
    mocks = _wire_odoo(monkeypatch, holidays=[
        # Sunday 2026-07-05 CT, stored in UTC spilling into 07-06.
        {"id": 2, "name": "Observed", "calendar_id": False,
         "date_from": "2026-07-05 05:00:00", "date_to": "2026-07-06 04:59:59"},
    ])

    assert bf.run_once() == 1
    mocks["approve"].assert_called_once()


def test_malformed_holiday_row_is_skipped_not_fatal(monkeypatch, fake_db):
    # Odoo returns False for null datetimes; the tick must not crash.
    fake_db["query_result"] = [_row()]
    mocks = _wire_odoo(monkeypatch, holidays=[
        {"id": 3, "name": "broken", "calendar_id": False,
         "date_from": False, "date_to": False},
    ])

    assert bf.run_once() == 1
    mocks["approve"].assert_called_once()


def test_uncovered_weekday_blocks_the_attempt(monkeypatch, fake_db):
    # Saturday absence, Mon-Fri calendar: Odoo can never hold it.
    fake_db["query_result"] = [_row(date_from=date(2026, 7, 11),
                                    date_to=date(2026, 7, 11))]
    mocks = _wire_odoo(monkeypatch)

    assert bf.run_once() == 0
    mocks["draft"].assert_not_called()
    mocks["approve"].assert_not_called()


def test_archived_employee_is_skipped_not_fatal(monkeypatch, fake_db):
    fake_db["query_result"] = [_row(person_odoo_id=999)]
    mocks = _wire_odoo(monkeypatch)

    assert bf.run_once() == 0
    mocks["approve"].assert_not_called()


def test_multi_day_span_needs_only_one_working_day(monkeypatch, fake_db):
    # Fri..Sun span: Friday works -> Odoo computes >0 days -> attempt runs.
    fake_db["query_result"] = [_row(date_from=date(2026, 7, 10),
                                    date_to=date(2026, 7, 12))]
    mocks = _wire_odoo(monkeypatch)

    assert bf.run_once() == 1
    mocks["approve"].assert_called_once()


def test_replays_refused_leave_and_hands_ownership_back(monkeypatch, fake_db):
    fake_db["query_result"] = [_row()]
    mocks = _wire_odoo(monkeypatch, leave_state="refuse")
    unsuppress = MagicMock()
    monkeypatch.setattr(bf.employee_notifications, "unsuppress_resolution",
                        unsuppress)
    suppress = MagicMock()
    monkeypatch.setattr(bf.employee_notifications, "suppress_resolution",
                        suppress)

    assert bf.run_once() == 1

    # refuse -> draft -> (confirm+approve via approve_leave)
    mocks["draft"].assert_called_once_with(112)
    mocks["approve"].assert_called_once_with(112)
    # Adoption is a guarded UPDATE: only an untouched approved local record
    # hands ownership back (WHERE local_record AND state='validate').
    adopts = [q for q in fake_db["queries"]
              if q[0].startswith("UPDATE") and "local_record = FALSE" in q[0]]
    assert len(adopts) == 1
    assert "AND local_record" in adopts[0][0]
    assert "state = 'validate'" in adopts[0][0]
    assert "RETURNING" in adopts[0][0]
    assert "synced_to_odoo = TRUE" in adopts[0][0]
    # Denied-popup suppression retired; a pre-acked approved suppression is
    # armed so a poll tick that caught a stale mid-replay state can't fire
    # a spurious 'approved' popup afterwards.
    unsuppress.assert_called_once_with(80, "time_off_denied")
    suppress.assert_called_once()
    assert suppress.call_args[1].get("kind") or suppress.call_args[0][-1] == "time_off_approved"


def test_settled_mid_replay_row_is_not_adopted_and_odoo_is_resettled(
        monkeypatch, fake_db):
    """Employee cancel / manager deny racing the replay must win: the
    guarded adoption matches no row, so the replayed Odoo approval is
    rolled back to refused and the local settle stands."""
    fake_db["query_result"] = [_row()]
    fake_db["update_result"] = []  # guarded adopt found the row settled
    mocks = _wire_odoo(monkeypatch, leave_state="refuse")
    unsuppress = MagicMock()
    monkeypatch.setattr(bf.employee_notifications, "unsuppress_resolution",
                        unsuppress)
    monkeypatch.setattr(bf.employee_notifications, "suppress_resolution",
                        MagicMock())

    assert bf.run_once() == 0
    mocks["refuse"].assert_called_once_with(112)
    unsuppress.assert_not_called()


def test_failed_replay_resettles_and_backs_off(monkeypatch, fake_db):
    import xmlrpc.client
    fake_db["query_result"] = [_row(backfill_attempts=2)]
    mocks = _wire_odoo(monkeypatch)
    mocks["approve"].side_effect = xmlrpc.client.Fault(
        2, "The following employees are not supposed to work during that period:\n X")

    assert bf.run_once() == 0

    # The Odoo copy must not linger pending: re-refused. And the row backs
    # off exponentially so a persistently-rejected leave can't churn
    # Odoo's workflow every hour forever.
    mocks["refuse"].assert_called_once_with(112)
    backoffs = [e for e in fake_db["executes"]
                if "backfill_attempts" in e[0] and "backfill_next_at" in e[0]]
    assert len(backoffs) == 1
    assert not [q for q in fake_db["queries"]
                if "local_record = FALSE" in q[0]]


def test_two_step_validation_result_counts_as_failure(monkeypatch, fake_db):
    fake_db["query_result"] = [_row()]
    mocks = _wire_odoo(monkeypatch)
    mocks["approve"].return_value = "validate1"

    assert bf.run_once() == 0
    mocks["refuse"].assert_called_once_with(112)


def test_deleted_odoo_copy_stays_local_with_long_backoff(monkeypatch, fake_db):
    """No recreate path: if HR deleted the refused copy they touched it
    deliberately — the absence stays app-only (long recheck, no RPC churn,
    no orphan draft leaves)."""
    fake_db["query_result"] = [_row()]
    mocks = _wire_odoo(monkeypatch, leave_state=None)

    assert bf.run_once() == 0
    mocks["draft"].assert_not_called()
    mocks["approve"].assert_not_called()
    mocks["refuse"].assert_not_called()
    assert [e for e in fake_db["executes"] if "backfill_next_at" in e[0]]


def test_cancelled_odoo_copy_is_reset_via_draft(monkeypatch, fake_db):
    fake_db["query_result"] = [_row()]
    mocks = _wire_odoo(monkeypatch, leave_state="cancel")
    monkeypatch.setattr(bf.employee_notifications, "unsuppress_resolution",
                        MagicMock())
    monkeypatch.setattr(bf.employee_notifications, "suppress_resolution",
                        MagicMock())

    assert bf.run_once() == 1
    mocks["draft"].assert_called_once_with(112)


def test_adopts_leave_hr_already_validated(monkeypatch, fake_db):
    # HR fixed the data and re-approved the leave directly in Odoo — the
    # reconciler adopts it without touching the workflow.
    fake_db["query_result"] = [_row()]
    mocks = _wire_odoo(monkeypatch, leave_state="validate")
    monkeypatch.setattr(bf.employee_notifications, "unsuppress_resolution",
                        MagicMock())
    monkeypatch.setattr(bf.employee_notifications, "suppress_resolution",
                        MagicMock())

    assert bf.run_once() == 1
    mocks["draft"].assert_not_called()
    mocks["approve"].assert_not_called()


def test_hr_validated_adopt_guard_failure_never_refuses(monkeypatch, fake_db):
    """If the row was settled locally while adopting an HR-validated leave,
    we must NOT refuse the leave HR just approved — log and stand down."""
    fake_db["query_result"] = [_row()]
    fake_db["update_result"] = []
    mocks = _wire_odoo(monkeypatch, leave_state="validate")
    monkeypatch.setattr(bf.employee_notifications, "unsuppress_resolution",
                        MagicMock())
    monkeypatch.setattr(bf.employee_notifications, "suppress_resolution",
                        MagicMock())

    assert bf.run_once() == 0
    mocks["refuse"].assert_not_called()


def test_backfill_warmer_registered():
    from zira_dashboard import app as app_module
    entry = next(
        (e for e in app_module._WARMERS
         if e[0] == "time-off local backfill"), None)
    assert entry is not None, "backfill warmer not registered in _WARMERS"
    assert entry[2] >= 3600  # hourly at most — replays are deliberate, not hot
