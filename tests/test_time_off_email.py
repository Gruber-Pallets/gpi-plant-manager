from datetime import date
from unittest.mock import Mock
import http.client
import xmlrpc.client

import pytest

from zira_dashboard import time_off_email as email


@pytest.fixture
def row():
    return dict(request_id=38, person_odoo_id=7, delivery_key="a-test-key",
                date_from=date(2030, 10, 1), date_to=date(2030, 10, 2),
                shape="full_day", hour_from=None, hour_to=None, state="pending",
                odoo_mail_id=None, attempts=0)


@pytest.mark.parametrize(("policy", "expected"), [
    ("personal_then_work", "personal@example.com"),
    ("personal", "personal@example.com"), ("work", "work@example.com"),
])
def test_recipient_policy(policy, expected):
    person = {"private_email": "Personal@example.com", "work_email": "work@example.com"}
    assert email.recipient(person, policy) == expected


@pytest.mark.parametrize("bad", [None, False, "", "bad", "a@b.com,b@b.com", "A <a@b.com>",
                                 "a@b.com\r\nBcc:other@example.com"])
def test_bad_personal_email_falls_back_to_work(bad):
    person = {"private_email": bad, "work_email": "work@example.com"}
    assert email.recipient(person, "personal_then_work") == "work@example.com"
    assert email.recipient(person, "personal") is None


def test_unknown_policy_fails_closed():
    with pytest.raises(ValueError):
        email.recipient({"work_email": "work@example.com"}, "typo")


def test_message_has_dates_year_and_both_languages_without_private_notes(row):
    row.update(note="private medical details", leave_type="Medical", name="<script>")
    subject, body = email.render_email(row)
    assert "approved" in subject.lower()
    assert "2030-10-01" in body and "2030-10-02" in body
    assert "aprobado" in body and "Full days" in body
    assert "medical" not in body.lower() and "<script>" not in body


@pytest.mark.parametrize("shape", ["late_arrival", "early_leave", "midday_gap"])
def test_partial_day_message_shows_time_off_hours(row, shape):
    row.update(shape=shape, hour_from=8.5, hour_to=10.25)
    _, body = email.render_email(row)
    assert "8:30 AM" in body and "10:15 AM" in body
    assert "Central" in body


@pytest.fixture
def setup_delivery(monkeypatch, row):
    saved = []
    monkeypatch.setattr(email, "_mark", lambda r, state, **kw: saved.append((state, kw)))
    monkeypatch.setattr(email, "_current_request", lambda r: dict(row, state="validate", local_record=True))
    monkeypatch.setattr(email, "_today", lambda: date(2030, 9, 1))
    monkeypatch.setenv("TIME_OFF_APPROVAL_EMAIL_RECIPIENT", "personal_then_work")
    rpc = Mock()

    def respond(model, method, *args, **kwargs):
        if model == "mail.mail" and method == "search_read":
            return []
        if model == "hr.employee":
            return [{"id": 7, "active": True, "private_email": "staff@example.com", "work_email": False}]
        if model == "mail.mail" and method == "default_get":
            return {"email_from": '"Existing sender" <sender@example.com>'}
        if model == "mail.mail" and method == "create":
            assert saved[-1][0] == "sending", "sending intent must be committed first"
            return 91
        raise AssertionError((model, method, args, kwargs))

    rpc.side_effect = respond
    monkeypatch.setattr(email.odoo_client, "execute", rpc)
    monkeypatch.setattr(email, "_create_mail", lambda payload: rpc("mail.mail", "create", payload))
    return saved, rpc


def test_delivery_queues_once_for_exact_employee(row, setup_delivery):
    saved, rpc = setup_delivery
    email.process_one(row)
    assert [s for s, _ in saved] == ["sending", "queued"]
    assert saved[-1][1]["mail_id"] == 91
    calls = [c for c in rpc.call_args_list if c.args[:2] == ("mail.mail", "create")]
    assert len(calls) == 1
    payload = calls[0].args[2]
    assert payload["email_to"] == "staff@example.com"
    assert payload["email_from"] == "GPI Plant Manager <sender@example.com>"
    assert payload["auto_delete"] is False
    assert payload["message_id"] == "<gpi-time-off-a-test-key@gpiplantmanager.com>"
    assert "recipient_ids" not in payload and "email_cc" not in payload


def test_missing_address_retries_without_creating_mail(row, setup_delivery):
    saved, rpc = setup_delivery
    original = rpc.side_effect
    rpc.side_effect = lambda model, method, *a, **kw: (
        [{"id": 7, "active": True, "private_email": False, "work_email": False}]
        if model == "hr.employee" else original(model, method, *a, **kw)
    )
    email.process_one(row)
    assert saved[-1][0] == "pending" and saved[-1][1]["error"] == "missing_email"
    assert not any(c.args[:2] == ("mail.mail", "create") for c in rpc.call_args_list)


@pytest.mark.parametrize("change", [None, {"state": "cancel"}, {"date_to": date(2030, 10, 4)},
                                    {"person_odoo_id": 8}, {"hour_from": 9}])
def test_stale_request_skips_mail(monkeypatch, row, setup_delivery, change):
    saved, rpc = setup_delivery
    current = None if change is None else dict(row, state="validate") | change
    monkeypatch.setattr(email, "_current_request", lambda r: current)
    email.process_one(row)
    assert saved[-1][0] == "skipped"
    assert not any(c.args[:2] == ("mail.mail", "create") for c in rpc.call_args_list)


def test_create_timeout_is_not_blindly_retried(row, setup_delivery):
    saved, rpc = setup_delivery
    original = rpc.side_effect

    def timeout(model, method, *a, **kw):
        if (model, method) == ("mail.mail", "create"):
            raise TimeoutError("private exception text")
        return original(model, method, *a, **kw)

    rpc.side_effect = timeout
    email.process_one(row)
    assert saved[-1][0] == "sending" and saved[-1][1]["error"] == "delivery_unknown"
    row["state"] = "sending"
    rpc.reset_mock()
    email.process_one(row)
    assert not any(c.args[:2] == ("mail.mail", "create") for c in rpc.call_args_list)


def test_definitive_create_fault_can_retry(row, setup_delivery):
    saved, rpc = setup_delivery
    original = rpc.side_effect

    def fail(model, method, *a, **kw):
        if (model, method) == ("mail.mail", "create"):
            raise xmlrpc.client.Fault(1, "private server details")
        return original(model, method, *a, **kw)

    rpc.side_effect = fail
    email.process_one(row)
    assert saved[-1][0] == "pending" and saved[-1][1]["error"] == "queue_rejected"


@pytest.mark.parametrize(("remote", "local"), [("sent", "sent"), ("outgoing", "queued"),
                                               ("exception", "attention"), ("cancel", "attention")])
def test_existing_message_is_read_back_without_duplicate(row, setup_delivery, remote, local):
    saved, rpc = setup_delivery
    rpc.return_value = [{"id": 91, "state": remote, "message_id": email.message_id(row)}]
    rpc.side_effect = None
    row["state"] = "sending"
    email.process_one(row)
    assert saved[-1][0] == local
    assert not any(c.args[:2] == ("mail.mail", "create") for c in rpc.call_args_list)


def test_disabled_worker_does_not_access_db_or_odoo(monkeypatch):
    monkeypatch.setenv("TIME_OFF_APPROVAL_EMAIL_ENABLED", "0")
    monkeypatch.setattr(email.db, "cursor", Mock(side_effect=AssertionError("must not access DB")))
    assert email.run_once() == 0


@pytest.mark.parametrize(("state", "employee_id", "expected"), [
    ("validate", 7, True), ("refuse", 7, False), ("validate1", 7, False),
    ("validate", 8, False),
])
def test_recheck_exact_odoo_approval_before_queuing(monkeypatch, row, state, employee_id, expected):
    monkeypatch.setattr(email, "_today", lambda: date(2030, 9, 1))
    monkeypatch.setattr(email.time_off_sync, "_company_shift_bounds", lambda: (6, 14.5))
    rpc = Mock(return_value=[{
        "id": 99, "state": state, "employee_id": [employee_id, "Employee"],
        "request_date_from": "2030-10-01", "request_date_to": "2030-10-02",
        "number_of_days": 2,
    }])
    monkeypatch.setattr(email.odoo_client, "execute", rpc)
    current = dict(row, state="validate", odoo_leave_id=99)
    assert email._still_approved(row, current) is expected
    assert rpc.call_args.args[:3] == ("hr.leave", "read", [99])


def test_ambiguous_remote_message_never_creates_another(row, setup_delivery):
    saved, rpc = setup_delivery
    rpc.side_effect = None
    rpc.return_value = [{"id": 91}, {"id": 92}]
    email.process_one(row)
    assert saved[-1] == ("attention", {"error": "invalid_delivery_data"})
    assert rpc.call_count == 1


def test_inactive_employee_is_skipped(row, setup_delivery):
    saved, rpc = setup_delivery
    original = rpc.side_effect
    rpc.side_effect = lambda model, method, *a, **kw: (
        [{"id": 7, "active": False, "private_email": "staff@example.com"}]
        if model == "hr.employee" else original(model, method, *a, **kw)
    )
    email.process_one(row)
    assert saved[-1][0] == "skipped"
    assert not any(c.args[:2] == ("mail.mail", "create") for c in rpc.call_args_list)


@pytest.mark.parametrize("secure", [False, True])
def test_mail_create_transport_never_replays_after_dropped_response(secure):
    transport = email._mail_transport(secure)
    transport.single_request = Mock(side_effect=http.client.RemoteDisconnected("response lost"))
    with pytest.raises(http.client.RemoteDisconnected):
        transport.request("odoo.example", "/xmlrpc/2/object", b"request")
    assert transport.single_request.call_count == 1


def test_remote_only_hour_change_prevents_stale_email(monkeypatch, row):
    monkeypatch.setattr(email, "_today", lambda: date(2030, 9, 1))
    monkeypatch.setattr(email.time_off_sync, "_company_shift_bounds", lambda: (6, 14.5))
    row.update(shape="midday_gap", hour_from=10, hour_to=12)
    leave = {"id": 99, "state": "validate", "employee_id": [7, "Employee"],
             "request_date_from": "2030-10-01", "request_date_to": "2030-10-02",
             "request_unit_hours": True, "request_hour_from": "10", "request_hour_to": "12"}
    monkeypatch.setattr(email.odoo_client, "execute", Mock(return_value=[leave]))
    current = dict(row, state="validate", odoo_leave_id=99)
    assert email._still_approved(row, current)
    leave.update(request_hour_from="13", request_hour_to="14")
    assert not email._still_approved(row, current)
    leave.update(request_unit_hours=False, number_of_days=2)
    assert not email._still_approved(row, current)
