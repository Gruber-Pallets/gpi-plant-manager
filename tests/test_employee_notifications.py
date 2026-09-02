from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pytest

from zira_dashboard import employee_notifications as en


@pytest.fixture
def fake_db(monkeypatch):
    captured: dict = {"queries": [], "executes": []}

    def fake_query(sql, params=None):
        captured["queries"].append((sql, params))
        return captured.get("query_result", [])

    def fake_execute(sql, params=None):
        captured["executes"].append((sql, params))

    monkeypatch.setattr(en.db, "query", fake_query)
    monkeypatch.setattr(en.db, "execute", fake_execute)
    return captured


def test_notifications_enabled_default_on(monkeypatch):
    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    assert en.notifications_enabled() is True


def test_notifications_enabled_off_when_zero(monkeypatch):
    monkeypatch.setenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", "0")
    assert en.notifications_enabled() is False


def test_create_inserts_with_on_conflict_do_nothing(fake_db):
    req = {
        "id": 7,
        "person_odoo_id": 5,
        "odoo_leave_id": 88,
        "date_from": date(2026, 7, 1),
        "date_to": date(2026, 7, 3),
    }
    en.create_time_off_notification(5, "time_off_approved", req)

    assert len(fake_db["executes"]) == 1
    sql, params = fake_db["executes"][0]
    assert "INSERT INTO employee_notifications" in sql
    assert "ON CONFLICT (time_off_request_id, kind) DO NOTHING" in sql
    assert params[0] == 5
    assert params[1] == "time_off_approved"
    assert 7 in params and 88 in params


def test_create_saturday_cancelled_is_idempotent(fake_db):
    day = date(2026, 7, 25)

    en.create_saturday_cancelled(5, day)

    sql, params = fake_db["executes"][0]
    assert "saturday_day" in sql
    assert "ON CONFLICT" in sql
    assert params[:3] == (5, "saturday_work_cancelled", day)


def test_create_holiday_cancelled_keeps_dedupe_fields_and_stores_named_copy(fake_db):
    day = date(2026, 11, 27)

    en.create_saturday_cancelled(
        5,
        day,
        day_kind="holiday",
        event_name="Black Friday",
    )

    sql, params = fake_db["executes"][0]
    assert "ON CONFLICT (person_odoo_id, saturday_day, kind)" in sql
    assert params[:3] == (5, "saturday_work_cancelled", day)
    assert params[3:] == (
        "Holiday work cancelled",
        "Black Friday work was cancelled. Do not report to work.",
    )


def test_render_messages_distinct_per_kind():
    req = {"date_from": date(2026, 7, 1), "date_to": date(2026, 7, 1)}
    approved_title, approved_body = en._render("time_off_approved", req)
    denied_title, denied_body = en._render("time_off_denied", req)
    cancelled_title, cancelled_body = en._render("time_off_cancelled", req)
    assert "approved" in approved_body.lower()
    assert "denied" in denied_body.lower()
    assert "cancelled" in cancelled_body.lower()
    assert "–" not in approved_body


def test_render_multi_day_shows_span():
    req = {"date_from": date(2026, 7, 1), "date_to": date(2026, 7, 3)}
    _, body = en._render("time_off_approved", req)
    assert "–" in body  # "Jul 1 – Jul 3"


def test_has_unacknowledged_true_when_rows(fake_db):
    fake_db["query_result"] = [{"?column?": 1}]
    assert en.has_unacknowledged(5) is True
    sql, params = fake_db["queries"][0]
    assert "acknowledged_at IS NULL" in sql
    assert params == (5,)


def test_has_unacknowledged_false_when_empty(fake_db):
    fake_db["query_result"] = []
    assert en.has_unacknowledged(5) is False


def test_list_unacknowledged_filters_by_person_and_unacked(fake_db):
    fake_db["query_result"] = [{"id": 1, "title": "t", "body": "b"}]
    out = en.list_unacknowledged(5)
    assert out == [{"id": 1, "title": "t", "body": "b"}]
    sql, params = fake_db["queries"][0]
    assert "acknowledged_at IS NULL" in sql
    assert "ORDER BY created_at" in sql
    assert "saturday_day" in sql
    assert params == (5,)


def test_list_unacknowledged_records_first_presentation(fake_db):
    fake_db["query_result"] = [{"id": 9, "kind": "anniversary_pto_reminder"}]

    assert en.list_unacknowledged(5)[0]["id"] == 9

    sql, params = fake_db["executes"][0]
    assert "presented_at = COALESCE(presented_at, now())" in sql
    assert "person_odoo_id = %s" in sql
    assert params == ([9], 5)


def test_history_is_person_scoped_and_newest_first(fake_db):
    en.list_history(5)

    sql, params = fake_db["queries"][0]
    assert "WHERE person_odoo_id = %s" in sql
    assert "ORDER BY created_at DESC, id DESC" in sql
    assert "anniversary_date" in sql and "presented_at" in sql
    assert params == (5,)


class _TransactionCursor:
    def __init__(self, existing=()):
        self.existing = list(existing)
        self.executes = []

    def execute(self, sql, params=None):
        self.executes.append((sql, params))

    def fetchall(self):
        return self.existing


def _install_transaction(monkeypatch, existing=()):
    cursor = _TransactionCursor(existing)

    @contextmanager
    def transaction():
        yield cursor

    monkeypatch.setattr(en.db, "cursor", transaction)
    return cursor


def test_reconcile_updates_only_unpresented_anniversary_notice(monkeypatch):
    cursor = _install_transaction(monkeypatch)
    notice = en.AnniversaryPtoNotice(5, date(2026, 10, 2), Decimal("2.5"), "days")

    en.reconcile_anniversary_pto((notice,))

    insert_sql, params = cursor.executes[-1]
    assert "ON CONFLICT (person_odoo_id, anniversary_date, kind)" in insert_sql
    assert "employee_notifications.presented_at IS NULL" in insert_sql
    assert "employee_notifications.acknowledged_at IS NULL" in insert_sql
    assert params[:4] == (5, "anniversary_pto_reminder", "Your work anniversary is coming up", "Your work anniversary is Oct 2. You have 2.5 days of unused Paid Time Off. Please plan to use your time or talk with your supervisor if you have questions.")


def test_reconcile_removes_only_stale_unpresented_anniversary_notice(monkeypatch):
    cursor = _install_transaction(
        monkeypatch,
        [{"id": 7, "person_odoo_id": 5, "anniversary_date": date(2026, 10, 2)}],
    )

    en.reconcile_anniversary_pto(())

    select_sql = cursor.executes[0][0]
    delete_sql, params = cursor.executes[1]
    assert "kind = 'anniversary_pto_reminder'" in select_sql
    assert "presented_at IS NULL" in select_sql
    assert "acknowledged_at IS NULL" in select_sql
    assert "kind = 'anniversary_pto_reminder'" in delete_sql
    assert "presented_at IS NULL" in delete_sql
    assert "acknowledged_at IS NULL" in delete_sql
    assert params == (7,)


def test_acknowledge_all_is_person_scoped(fake_db):
    en.acknowledge_all(5)
    sql, params = fake_db["executes"][0]
    assert "UPDATE employee_notifications SET acknowledged_at = now()" in sql
    assert "person_odoo_id = %s" in sql
    assert "acknowledged_at IS NULL" in sql
    assert params == (5,)


def _req(state, date_to=date(2026, 7, 3), **extra):
    base = {
        "id": 7,
        "person_odoo_id": 5,
        "odoo_leave_id": 88,
        "state": state,
        "date_from": date(2026, 7, 1),
        "date_to": date(2026, 7, 3),
    }
    base.update(extra)
    base["date_to"] = date_to
    return base


def test_notify_on_approve(fake_db, monkeypatch):
    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    en.maybe_notify_resolution(_req("confirm"), _req("validate"), today=date(2026, 6, 29))
    inserts = [e for e in fake_db["executes"] if "INSERT INTO employee_notifications" in e[0]]
    assert len(inserts) == 1
    assert "time_off_approved" in inserts[0][1]


def test_notify_on_deny_from_confirm(fake_db, monkeypatch):
    # The case the scheduler cascade misses: deny a never-approved request.
    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    en.maybe_notify_resolution(_req("confirm"), _req("refuse"), today=date(2026, 6, 29))
    inserts = [e for e in fake_db["executes"] if "INSERT INTO employee_notifications" in e[0]]
    assert len(inserts) == 1
    assert "time_off_denied" in inserts[0][1]


def test_no_notify_on_self_cancel_pushed_as_refuse(fake_db, monkeypatch):
    # Employee cancelled their own approved request -> Odoo records 'refuse'
    # from local 'draft_cancel'. Not a denial: suppress.
    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    en.maybe_notify_resolution(_req("draft_cancel"), _req("refuse"), today=date(2026, 6, 29))
    assert not [e for e in fake_db["executes"] if "INSERT INTO employee_notifications" in e[0]]


def test_no_notify_on_self_cancel_to_cancel(fake_db, monkeypatch):
    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    en.maybe_notify_resolution(_req("draft_cancel"), _req("cancel"), today=date(2026, 6, 29))
    assert not [e for e in fake_db["executes"] if "INSERT INTO employee_notifications" in e[0]]


def test_no_notify_for_past_leave(fake_db, monkeypatch):
    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    en.maybe_notify_resolution(
        _req("confirm", date_to=date(2026, 6, 20)),
        _req("validate", date_to=date(2026, 6, 20)),
        today=date(2026, 6, 29),
    )
    assert not [e for e in fake_db["executes"] if "INSERT INTO employee_notifications" in e[0]]


def test_no_notify_for_non_resolution_transition(fake_db, monkeypatch):
    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    en.maybe_notify_resolution(_req("draft"), _req("confirm"), today=date(2026, 6, 29))
    assert not fake_db["executes"]


def test_no_notify_when_disabled(fake_db, monkeypatch):
    monkeypatch.setenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", "0")
    en.maybe_notify_resolution(_req("confirm"), _req("validate"), today=date(2026, 6, 29))
    assert not fake_db["executes"]


def test_maybe_notify_swallows_db_errors(fake_db, monkeypatch):
    # A notification failure must never propagate out of the poll loop.
    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)

    def boom(sql, params=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(en.db, "execute", boom)

    # Should not raise.
    en.maybe_notify_resolution(_req("confirm"), _req("validate"), today=date(2026, 6, 29))


# --------------------------------------------------------------------------
# suppress_resolution — pre-acknowledged insert that arms the dedupe index
# so a later poller-generated popup of the same kind is silently swallowed.
# Used by the local-record fallback (approve despite an Odoo work-schedule
# rejection): the fallback refuses the Odoo copy, and the employee must
# never see a "denied" popup for a request the app actually approved.
# --------------------------------------------------------------------------


def test_suppress_resolution_inserts_pre_acknowledged_row(fake_db):
    req = {
        "id": 71,
        "person_odoo_id": 5,
        "odoo_leave_id": 88,
        "date_from": date(2026, 7, 3),
        "date_to": date(2026, 7, 3),
    }

    en.suppress_resolution(5, req, kind="time_off_denied")

    inserts = [e for e in fake_db["executes"] if "INSERT INTO employee_notifications" in e[0]]
    assert len(inserts) == 1
    sql, params = inserts[0]
    # Born acknowledged — must never render on the kiosk interstitial.
    assert "acknowledged_at" in sql
    assert "now()" in sql
    # Same dedupe arm as regular notifications, but DO UPDATE so a
    # pre-existing unacked popup gets neutralized too.
    assert "ON CONFLICT (time_off_request_id, kind) DO UPDATE" in sql
    assert 5 in params
    assert 71 in params
    assert "time_off_denied" in params


def test_suppress_resolution_ignores_kill_switch(fake_db, monkeypatch):
    # The suppression row must be written even while popups are disabled:
    # it protects against the flag being re-enabled later.
    monkeypatch.setenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", "0")
    req = {
        "id": 72,
        "person_odoo_id": 6,
        "odoo_leave_id": 89,
        "date_from": date(2026, 7, 3),
        "date_to": date(2026, 7, 3),
    }

    en.suppress_resolution(6, req, kind="time_off_denied")

    assert any("INSERT INTO employee_notifications" in e[0] for e in fake_db["executes"])


def test_suppress_resolution_acks_a_preexisting_unacknowledged_row(fake_db):
    # A stale UNacked "denied" popup can already exist (earlier Odoo-side
    # refuse that was later reset). Suppression must neutralize it, not
    # no-op past it — otherwise the employee sees "denied" for an absence
    # the app just approved. Hence DO UPDATE, not DO NOTHING.
    req = {
        "id": 73,
        "person_odoo_id": 7,
        "odoo_leave_id": 90,
        "date_from": date(2026, 7, 3),
        "date_to": date(2026, 7, 3),
    }

    en.suppress_resolution(7, req, kind="time_off_denied")

    sql = fake_db["executes"][0][0]
    assert "ON CONFLICT (time_off_request_id, kind) DO UPDATE" in sql
    assert "COALESCE(employee_notifications.acknowledged_at, now())" in sql


def test_unsuppress_resolution_deletes_only_acknowledged_rows(fake_db):
    # Abort-path cleanup: remove the pre-acked suppression row so a future
    # genuine Odoo-side denial can still notify. Never touch a live
    # (unacknowledged) popup.
    en.unsuppress_resolution(71, kind="time_off_denied")

    deletes = [e for e in fake_db["executes"] if "DELETE FROM employee_notifications" in e[0]]
    assert len(deletes) == 1
    sql, params = deletes[0]
    assert "acknowledged_at IS NOT NULL" in sql
    assert 71 in params
    assert "time_off_denied" in params
