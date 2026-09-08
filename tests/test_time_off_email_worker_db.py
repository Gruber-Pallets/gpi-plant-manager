"""Concurrency and crash-recovery contracts against disposable PostgreSQL."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest

from zira_dashboard import db, time_off_email as email

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")


def test_two_workers_queue_one_mail_and_reconcile_sent(monkeypatch):
    db.init_pool()
    db.bootstrap_schema()
    day = email._today() + timedelta(days=30)
    rid = db.query(
        "INSERT INTO time_off_requests (person_odoo_id, shape, holiday_status_id, "
        "date_from, date_to, state, local_record) "
        "VALUES (990039, 'full_day', 1, %s, %s, 'confirm', TRUE) RETURNING id",
        (day, day),
    )[0]["id"]
    entered, release = Event(), Event()
    mails = []
    monkeypatch.setenv("TIME_OFF_APPROVAL_EMAIL_ENABLED", "1")

    def rpc(model, method, *args, **kwargs):
        if (model, method) == ("mail.mail", "search_read"):
            entered.set()
            assert release.wait(10)
            return mails.copy()
        if model == "hr.employee":
            return [{"id": 990039, "active": True, "private_email": "test@example.invalid"}]
        if method == "default_get":
            return {"email_from": "sender@example.invalid"}
        if method == "create":
            # This separate connection sees intent committed before remote I/O.
            assert db.query("SELECT state FROM time_off_approval_email WHERE request_id = %s", (rid,))[0]["state"] == "sending"
            mails.append({"id": 390038, "state": "outgoing", "message_id": args[0]["message_id"]})
            return 390038
        raise AssertionError((model, method))

    monkeypatch.setattr(email.odoo_client, "execute", rpc)
    monkeypatch.setattr(email, "_create_mail", lambda payload: rpc("mail.mail", "create", payload))
    try:
        db.execute("UPDATE time_off_requests SET state = 'validate' WHERE id = %s", (rid,))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(email.run_once)
            assert entered.wait(5)
            try:
                assert pool.submit(email.run_once).result(timeout=5) == 0
            finally:
                release.set()
            assert first.result(timeout=5) == 1
        assert len(mails) == 1
        assert db.query("SELECT state FROM time_off_approval_email WHERE request_id = %s", (rid,))[0]["state"] == "queued"
        mails[0]["state"] = "sent"
        db.execute("UPDATE time_off_approval_email SET next_attempt_at = now() WHERE request_id = %s", (rid,))
        assert email.run_once() == 1
        assert db.query("SELECT state FROM time_off_approval_email WHERE request_id = %s", (rid,))[0]["state"] == "sent"
        assert len(mails) == 1
    finally:
        release.set()
        db.execute("DELETE FROM time_off_approval_email WHERE request_id = %s", (rid,))
        db.execute("DELETE FROM time_off_requests WHERE id = %s", (rid,))
