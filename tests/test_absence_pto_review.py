from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import xmlrpc.client
from uuid import UUID

import pytest

from zira_dashboard import absence_pto_conversion as conversion
from zira_dashboard import absence_pto_review as review
from zira_dashboard import absence_pto_store as store


NOW = datetime(2026, 8, 28, 14, 30, tzinfo=UTC)
OWNER = UUID("5b2bca59-35fd-464a-ab5f-531d2fb4a6b4")


def _request(request_id=41, **changes):
    row = store.AbsencePtoRequest(
        id=request_id,
        absence_day=date(2026, 8, 20),
        emp_id=str(request_id),
        person_odoo_id=request_id,
        person_name=f"Person {request_id}",
        holiday_status_id=7,
        leave_type_name="Paid Time Off",
        balance_at_submit=Decimal("4"),
        original_absence_leave_id=70 + request_id,
        pto_leave_id=None,
        state="converting",
        conversion_step="absence_refused",
        employee_note=None,
        denial_reason=None,
        manual_resolution_note=None,
        sync_error=None,
        odoo_task_id=None,
        task_attempts=0,
        task_next_at=None,
        lease_owner=OWNER,
        lease_until=NOW + timedelta(seconds=120),
        requested_by_person_id=None,
        decided_by_upn="manager@example.com",
        decided_by_name="Manager",
        requested_at=NOW - timedelta(days=2),
        decided_at=NOW - timedelta(minutes=5),
        resolved_at=None,
        created_at=NOW - timedelta(days=2),
        updated_at=NOW,
    )
    return replace(row, **changes)


def _row(request):
    return {field: getattr(request, field) for field in request.__dataclass_fields__}


def _needs_review(**changes):
    defaults = {
        "state": "needs_review",
        "conversion_step": "absence_refused",
        "sync_error": "The original absence could not be restored.",
        "task_next_at": NOW,
    }
    defaults.update(changes)
    return _request(**defaults)


class ReviewFake:
    def __init__(self, *, users=None, task_ids=None, create_error=None):
        self.users = (
            [{"id": 17, "login": "wendy@gruberpallets.com"}]
            if users is None
            else users
        )
        self.task_ids = [] if task_ids is None else list(task_ids)
        self.create_error = create_error
        self.created = []
        self.updated = []
        self.messages = []
        self.closed = []
        self.events = []

    def ensure_feedback_project(self):
        return 7

    def find_active_feedback_task_ids(self, project_id, name):
        self.events.append(("search", project_id, name))
        return list(self.task_ids)

    def find_active_users_by_login(self, login, limit=2):
        self.events.append(("users", login, limit))
        return list(self.users)

    def create_feedback_task(self, **values):
        self.created.append(values)
        self.events.append(("create", values))
        if self.create_error is not None:
            error = self.create_error
            self.create_error = None
            raise error
        self.task_ids = [501]
        return 501

    def update_task(self, task_id, **fields):
        self.updated.append((task_id, fields))
        self.events.append(("update", task_id, fields))

    def post_task_message(self, task_id, body):
        self.messages.append((task_id, body))
        self.events.append(("message", task_id, body))

    def close_task(self, task_id):
        self.closed.append(task_id)
        self.events.append(("close", task_id))


def wire_review(monkeypatch, fake, row, *, matching=None):
    current = {"row": row}

    def claim_request(request_id, owner, now, *, lease_seconds=120):
        assert request_id == current["row"].id
        return replace(
            current["row"],
            lease_owner=owner,
            lease_until=now + timedelta(seconds=lease_seconds),
        )

    def save_task_delivery(
        request_id,
        owner,
        *,
        task_id,
        attempts,
        next_at,
        error,
        now,
    ):
        current["row"] = replace(
            current["row"],
            lease_owner=owner,
            lease_until=now + timedelta(seconds=120),
            odoo_task_id=task_id,
            task_attempts=attempts,
            task_next_at=next_at,
            sync_error=error,
            updated_at=now,
        )
        fake.events.append(("save", task_id, attempts, next_at, error))
        return current["row"]

    monkeypatch.setattr(review, "_clock", lambda: NOW)
    monkeypatch.setattr(review.store, "claim_request", claim_request)
    monkeypatch.setattr(
        review.store,
        "renew_claim",
        lambda request_id, owner, now, lease_seconds=120: replace(
            current["row"],
            lease_owner=owner,
            lease_until=now + timedelta(seconds=lease_seconds),
        ),
    )
    monkeypatch.setattr(review.store, "save_task_delivery", save_task_delivery)
    monkeypatch.setattr(review.store, "release_claim", lambda *args, **kwargs: True)
    monkeypatch.setattr(review.store, "get_request", lambda request_id: current["row"])
    monkeypatch.setattr(review.odoo_client, "ensure_feedback_project", fake.ensure_feedback_project)
    monkeypatch.setattr(
        review.odoo_client,
        "find_active_feedback_project_ids",
        lambda name: [7],
        raising=False,
    )
    monkeypatch.setattr(
        review.odoo_client,
        "find_active_feedback_task_ids",
        fake.find_active_feedback_task_ids,
        raising=False,
    )
    monkeypatch.setattr(
        review.odoo_client, "find_active_users_by_login", fake.find_active_users_by_login
    )
    monkeypatch.setattr(review.odoo_client, "create_feedback_task", fake.create_feedback_task)
    monkeypatch.setattr(
        review.odoo_client,
        "create_review_task_user_ids",
        lambda **values: review.odoo_client.create_feedback_task(**values),
        raising=False,
    )
    monkeypatch.setattr(
        review.odoo_client,
        "create_review_task_user_id",
        fake.create_feedback_task,
        raising=False,
    )
    monkeypatch.setattr(review.odoo_client, "update_task", fake.update_task)
    monkeypatch.setattr(
        review.odoo_client,
        "update_feedback_task",
        lambda task_id, **fields: fake.update_task(
            task_id,
            description=fields["description_html"],
            active=fields["active"],
            assignee_uid=fields["assignee_uid"],
            deadline=fields["deadline"],
        ),
    )
    def update_review_task(**fields):
        task_id = fields.pop("task_id")
        fake.update_task(
            task_id,
            name=fields["name"],
            project_id=fields["project_id"],
            description=fields["description_html"],
            active=True,
            assignee_uid=fields["assignee_uid"],
            deadline=fields["deadline"],
        )

    monkeypatch.setattr(
        review.odoo_client,
        "update_review_task_user_ids",
        update_review_task,
        raising=False,
    )
    monkeypatch.setattr(
        review.odoo_client,
        "update_review_task_user_id",
        update_review_task,
        raising=False,
    )
    monkeypatch.setattr(
        review.odoo_client,
        "fetch_feedback_task_identity",
        lambda task_id: {
            "id": task_id,
            "name": review.task_name(current["row"]),
            "project_id": 7,
            "active": (
                task_id in fake.task_ids or current["row"].odoo_task_id == task_id
            ),
        },
        raising=False,
    )
    monkeypatch.setattr(
        review.odoo_client, "find_task_message_ids", lambda *args: [], raising=False
    )

    def save_resolution_delivery(
        request_id,
        owner,
        *,
        expected_step,
        new_step,
        attempts,
        next_at,
        error,
        now,
    ):
        current["row"] = replace(
            current["row"],
            task_resolution_step=new_step,
            task_resolution_attempts=attempts,
            task_resolution_next_at=next_at,
            task_resolution_error=error,
        )
        fake.events.append(("resolution_save", new_step, attempts, next_at, error))
        return current["row"]

    monkeypatch.setattr(
        review.store,
        "save_resolution_delivery",
        save_resolution_delivery,
        raising=False,
    )
    monkeypatch.setattr(review.odoo_client, "post_task_message", fake.post_task_message)
    monkeypatch.setattr(review.odoo_client, "close_task", fake.close_task)
    monkeypatch.setattr(
        review.odoo_client,
        "find_matching_leaves",
        lambda *args, **kwargs: [] if matching is None else list(matching),
    )
    monkeypatch.setattr(
        review.odoo_client,
        "fetch_leave_snapshot",
        lambda leave_id: next(
            (item for item in (matching or []) if item["id"] == leave_id), None
        ),
    )
    monkeypatch.setattr(
        review, "_next_business_day", lambda day: date(2026, 8, 31), raising=False
    )
    return current


def test_due_claim_is_bounded_skip_locked_and_takes_expired_conversions_or_rollovers(
    monkeypatch,
):
    seen = {}
    claimed = _request()
    monkeypatch.setattr(
        store.db,
        "query",
        lambda sql, params: seen.update(sql=sql, params=params) or [_row(claimed)],
    )

    rows = store.claim_due(
        OWNER,
        NOW,
        period_start=date(2026, 8, 16),
        period_end=date(2026, 8, 29),
        limit=25,
    )

    assert rows == [claimed]
    assert "FOR UPDATE SKIP LOCKED" in seen["sql"]
    assert "state = 'converting'" in seen["sql"]
    assert "state = 'pending'" in seen["sql"]
    assert "absence_day NOT BETWEEN %s AND %s" in seen["sql"]
    assert "lease_until IS NULL OR request.lease_until <= %s" in seen["sql"]
    assert "LIMIT %s" in seen["sql"]
    assert 25 in seen["params"]


def test_reconcile_rollover_and_resume_are_isolated_with_exact_counts(monkeypatch):
    rolled = _request(
        41,
        absence_day=date(2026, 8, 15),
        state="pending",
        conversion_step="not_started",
    )
    approved = _request(42)
    escalated = _request(43)
    broken = _request(44)
    claims = [rolled, approved, escalated, broken]
    errors = {}
    released = []
    monkeypatch.setattr(review, "_clock", lambda: NOW)
    monkeypatch.setattr(
        review.staffing_hours,
        "current_pay_period_bounds",
        lambda today: (date(2026, 8, 16), date(2026, 8, 29)),
    )
    monkeypatch.setattr(
        review.store,
        "claim_due",
        lambda owner, now, **kwargs: [replace(row, lease_owner=owner) for row in claims],
    )

    def mark_needs_review(request_id, owner, *, error, now):
        errors[request_id] = error
        return next(row for row in claims if row.id == request_id)

    monkeypatch.setattr(review.store, "mark_needs_review", mark_needs_review)
    monkeypatch.setattr(
        review.store,
        "release_claim",
        lambda request_id, owner, now: released.append(request_id) or True,
    )

    def resume_claimed(request, owner):
        if request.id == 42:
            return conversion.ConversionResult("approved", "approved", request)
        if request.id == 43:
            return conversion.ConversionResult("needs_review", "review", request)
        raise RuntimeError("one row broke")

    monkeypatch.setattr(review.conversion, "resume_claimed", resume_claimed)

    result = review.reconcile_once(NOW, limit=4)

    assert result == review.ReconcileResult(
        scanned=4,
        resumed=1,
        escalated=2,
        failed=1,
    )
    assert errors == {41: "Configured pay period closed before approval."}
    assert released == [41, 42, 43, 44]


def test_reconcile_release_exception_keeps_operation_context_and_continues(
    monkeypatch, caplog
):
    first = _request(41)
    second = _request(42)
    processed = []
    monkeypatch.setattr(review, "_clock", lambda: NOW)
    monkeypatch.setattr(
        review.staffing_hours,
        "current_pay_period_bounds",
        lambda today: (date(2026, 8, 16), date(2026, 8, 29)),
    )
    monkeypatch.setattr(
        review.store,
        "claim_due",
        lambda owner, now, **kwargs: [
            replace(first, lease_owner=owner),
            replace(second, lease_owner=owner),
        ],
    )

    def resume_claimed(request, owner):
        processed.append(request.id)
        if request.id == 41:
            raise RuntimeError("resume exploded")
        return conversion.ConversionResult("approved", "approved", request)

    def release_claim(request_id, owner, now):
        if request_id == 41:
            raise RuntimeError("release exploded")
        return True

    monkeypatch.setattr(review.conversion, "resume_claimed", resume_claimed)
    monkeypatch.setattr(review.store, "release_claim", release_claim)

    result = review.reconcile_once(NOW, limit=2)

    assert result == review.ReconcileResult(2, 1, 0, 1)
    assert processed == [41, 42]
    assert "resume exploded" in caplog.text
    assert "release exploded" in caplog.text


def test_reconcile_unsuccessful_release_reclassifies_once_and_continues(monkeypatch):
    first = _request(41)
    second = _request(42)
    processed = []
    monkeypatch.setattr(review, "_clock", lambda: NOW)
    monkeypatch.setattr(
        review.staffing_hours,
        "current_pay_period_bounds",
        lambda today: (date(2026, 8, 16), date(2026, 8, 29)),
    )
    monkeypatch.setattr(
        review.store,
        "claim_due",
        lambda owner, now, **kwargs: [
            replace(first, lease_owner=owner),
            replace(second, lease_owner=owner),
        ],
    )

    def resume_claimed(request, owner):
        processed.append(request.id)
        status = "approved" if request.id == 41 else "needs_review"
        return conversion.ConversionResult(status, status, request)

    monkeypatch.setattr(review.conversion, "resume_claimed", resume_claimed)
    monkeypatch.setattr(
        review.store,
        "release_claim",
        lambda request_id, owner, now: request_id != 41,
    )

    result = review.reconcile_once(NOW, limit=2)

    assert result == review.ReconcileResult(2, 0, 1, 1)
    assert result.resumed + result.escalated + result.failed == result.scanned
    assert processed == [41, 42]


def test_reconcile_passes_a_new_owner_when_taking_over_an_expired_lease(monkeypatch):
    expired_owner = UUID("d796a59e-b4b6-4836-8daa-945ce57bb7f9")
    expired = _request(
        lease_owner=expired_owner,
        lease_until=NOW - timedelta(seconds=1),
    )
    seen = {}
    monkeypatch.setattr(review, "_clock", lambda: NOW)
    monkeypatch.setattr(
        review.staffing_hours,
        "current_pay_period_bounds",
        lambda today: (date(2026, 8, 16), date(2026, 8, 29)),
    )

    def claim_due(owner, now, **kwargs):
        seen["owner"] = owner
        return [replace(expired, lease_owner=owner, lease_until=NOW + timedelta(seconds=120))]

    monkeypatch.setattr(review.store, "claim_due", claim_due)
    monkeypatch.setattr(
        review.conversion,
        "resume_claimed",
        lambda request, owner: conversion.ConversionResult("pending", "restored", request),
    )
    monkeypatch.setattr(review.store, "release_claim", lambda *args, **kwargs: True)

    result = review.reconcile_once(NOW, limit=1)

    assert result == review.ReconcileResult(1, 1, 0, 0)
    assert seen["owner"] != expired_owner


@pytest.mark.parametrize("limit", [0, 101, True])
def test_reconcile_rejects_an_unbounded_limit(limit):
    with pytest.raises(ValueError, match="limit"):
        review.reconcile_once(NOW, limit=limit)


def test_review_task_is_assigned_to_exact_wendy_and_saved(monkeypatch):
    fake = ReviewFake(users=[{"id": 17, "login": "WENDY@gruberpallets.com"}])
    current = wire_review(monkeypatch, fake, _needs_review())
    result = review.sync_review_task(41, NOW)
    assert result.status == "delivered"
    assert result.task_id == 501
    assert fake.created[0]["assignee_uid"] == 17
    assert current["row"].odoo_task_id == 501
    names = [event[0] for event in fake.events]
    assert names.index("save") > names.index("create")
    assert names.index("save") < names.index("update")


@pytest.mark.parametrize(
    "users",
    [
        [],
        [
            {"id": 17, "login": "wendy@gruberpallets.com"},
            {"id": 18, "login": "Wendy@gruberpallets.com"},
        ],
    ],
)
def test_review_task_wendy_identity_errors_retry_without_api_user_fallback(
    monkeypatch, users
):
    fake = ReviewFake(users=users)
    current = wire_review(monkeypatch, fake, _needs_review())
    result = review.sync_review_task(41, NOW)
    assert result.status == "retry"
    assert fake.created == []
    assert current["row"].task_attempts == 1
    assert current["row"].task_next_at > NOW
    assert "Wendy" in current["row"].sync_error


def test_review_task_has_deterministic_private_escaped_html_and_deadline(monkeypatch):
    row = _needs_review(
        person_name='Ana <Admin> & "A"',
        leave_type_name="PTO <paid>",
        employee_note="Private medical detail",
        decided_by_name="Boss <One>",
        sync_error="Unsafe <stop>",
    )
    fake = ReviewFake()
    wire_review(monkeypatch, fake, row)
    review.sync_review_task(41, NOW)
    created = fake.created[0]
    assert created["name"] == "[GPI-PM-PTO-41] Review Ana <Admin> & \"A\" — 2026-08-20"
    assert created["deadline"] == "2026-08-31"
    body = created["description_html"]
    assert "Ana &lt;Admin&gt; &amp; &quot;A&quot;" in body
    assert "PTO &lt;paid&gt;" in body
    assert "Boss &lt;One&gt;" in body
    assert "Unsafe &lt;stop&gt;" in body
    assert "Private medical detail" not in body
    assert "https://gpiplantmanager.com/staffing/time-off" in body


def test_review_task_adopts_exact_task_after_ambiguous_create_timeout(monkeypatch):
    fake = ReviewFake(create_error=TimeoutError("response lost"))
    wire_review(monkeypatch, fake, _needs_review())
    original_create = fake.create_feedback_task

    def timeout_then_appear(**values):
        try:
            return original_create(**values)
        except TimeoutError:
            fake.task_ids = [777]
            raise

    monkeypatch.setattr(review.odoo_client, "create_feedback_task", timeout_then_appear)
    result = review.sync_review_task(41, NOW)
    assert result.status == "delivered"
    assert result.task_id == 777
    assert len(fake.created) == 1
    assert [event[0] for event in fake.events].count("search") == 2


def test_duplicate_exact_active_tasks_are_permanently_blocked(monkeypatch):
    fake = ReviewFake(task_ids=[501, 502])
    current = wire_review(monkeypatch, fake, _needs_review(task_attempts=2))
    result = review.sync_review_task(41, NOW)
    assert result.status == "blocked"
    assert fake.created == []
    assert current["row"].task_attempts == 3
    assert current["row"].task_next_at.year == 9999
    assert "More than one active exact Odoo task" in current["row"].sync_error


def test_adopted_task_id_is_saved_before_update_and_no_second_task_is_created(monkeypatch):
    fake = ReviewFake(task_ids=[880])
    current = wire_review(monkeypatch, fake, _needs_review())

    def update_after_save(task_id, **fields):
        assert current["row"].odoo_task_id == task_id
        fake.update_task(task_id, **fields)

    monkeypatch.setattr(
        review.odoo_client,
        "update_feedback_task",
        lambda task_id, **fields: update_after_save(
            task_id,
            description=fields["description_html"],
            active=fields["active"],
            assignee_uid=fields["assignee_uid"],
            deadline=fields["deadline"],
        ),
    )
    result = review.sync_review_task(41, NOW)
    assert result.task_id == 880
    assert fake.created == []
    assert fake.updated[0][1]["active"] is True
    assert "description" in fake.updated[0][1]


def test_saved_task_is_updated_directly_as_review_state_changes(monkeypatch):
    fake = ReviewFake(task_ids=[999])
    wire_review(
        monkeypatch,
        fake,
        _needs_review(odoo_task_id=999, pto_leave_id=123, sync_error="New safe fact"),
    )
    result = review.sync_review_task(41, NOW)
    assert result.task_id == 999
    assert fake.created == []
    assert any(event[0] == "search" for event in fake.events)
    assert "Replacement PTO ID:</strong> 123" in fake.updated[0][1]["description"]
    assert "New safe fact" in fake.updated[0][1]["description"]


def test_external_approved_pto_uses_atomic_finalizer_then_message_then_close(monkeypatch):
    matching = [
        {
            "id": 991,
            "employee_id": 41,
            "holiday_status_id": 7,
            "date_from": date(2026, 8, 20),
            "date_to": date(2026, 8, 20),
            "state": "validate",
        }
    ]
    fake = ReviewFake(task_ids=[501])
    current = wire_review(
        monkeypatch, fake, _needs_review(odoo_task_id=501), matching=matching
    )

    def adopt_external(request_id, owner, *, pto_leave_id, now):
        current["row"] = replace(current["row"], pto_leave_id=pto_leave_id)
        fake.events.append(("adopt_pto", pto_leave_id))
        return current["row"]

    def finalize_approved(request_id, owner, **kwargs):
        assert current["row"].pto_leave_id == 991
        current["row"] = replace(current["row"], state="approved")
        fake.events.append(("finalize", 991))
        return current["row"]

    monkeypatch.setattr(review.store, "adopt_external_pto", adopt_external, raising=False)
    monkeypatch.setattr(review.store, "finalize_approved", finalize_approved)
    monkeypatch.setattr(review.conversion, "_invalidate_after_commit", lambda row: None)
    result = review.resolve_external_pto(41, NOW)
    assert result.status == "approved"
    names = [event[0] for event in fake.events]
    assert names.index("finalize") < names.index("message") < names.index("close")
    assert fake.closed == [501]


@pytest.mark.parametrize(
    "matching",
    [
        [],
        [
            {
                "id": 991,
                "employee_id": 41,
                "holiday_status_id": 7,
                "date_from": date(2026, 8, 20),
                "date_to": date(2026, 8, 20),
                "state": "confirm",
            }
        ],
        [
            {
                "id": 991,
                "employee_id": 41,
                "holiday_status_id": 7,
                "date_from": date(2026, 8, 20),
                "date_to": date(2026, 8, 20),
                "state": "validate",
            },
            {
                "id": 992,
                "employee_id": 41,
                "holiday_status_id": 7,
                "date_from": date(2026, 8, 20),
                "date_to": date(2026, 8, 20),
                "state": "validate",
            },
        ],
    ],
)
def test_external_resolution_requires_one_exact_validated_pto(monkeypatch, matching):
    fake = ReviewFake()
    wire_review(monkeypatch, fake, _needs_review(odoo_task_id=501), matching=matching)
    monkeypatch.setattr(
        review.store,
        "finalize_approved",
        lambda *args, **kwargs: pytest.fail("must not finalize"),
    )
    result = review.resolve_external_pto(41, NOW)
    assert result.status == "needs_review"
    assert fake.closed == []


def test_manual_resolution_requires_nonblank_note_before_claim(monkeypatch):
    monkeypatch.setattr(
        review.store,
        "claim_request",
        lambda *args, **kwargs: pytest.fail("must validate before claiming"),
    )
    with pytest.raises(ValueError, match="note"):
        review.resolve_manually(41, "manager@example.com", "Manager", "  ", NOW)


def test_manual_resolution_audits_posts_escaped_note_then_closes_without_pto(monkeypatch):
    fake = ReviewFake()
    current = wire_review(monkeypatch, fake, _needs_review(odoo_task_id=501))

    def finalize_manual(request_id, owner, *, actor_upn, actor_name, note, now):
        current["row"] = replace(
            current["row"],
            state="resolved_manually",
            manual_resolution_note=note,
            decided_by_upn=actor_upn,
            decided_by_name=actor_name,
            resolved_at=now,
        )
        fake.events.append(("manual", actor_upn, actor_name, note, now))
        return current["row"]

    monkeypatch.setattr(review.store, "finalize_manual", finalize_manual, raising=False)
    monkeypatch.setattr(
        review.odoo_client,
        "find_matching_leaves",
        lambda *args, **kwargs: pytest.fail("manual resolution must not inspect/create PTO"),
    )
    result = review.resolve_manually(
        41, "manager@example.com", "Manager", "Paid by check <#9>", NOW
    )
    assert result.status == "resolved_manually"
    assert current["row"].resolved_at == NOW
    assert "Paid by check &lt;#9&gt;" in fake.messages[0][1]
    names = [event[0] for event in fake.events]
    assert names.index("manual") < names.index("message") < names.index("close")


def test_marking_odoo_task_done_does_not_claim_pto_was_paid(monkeypatch):
    row = _needs_review(odoo_task_id=501)
    monkeypatch.setattr(
        review.odoo_client,
        "fetch_task_stage_names",
        lambda *args, **kwargs: pytest.fail(
            "absence PTO reconciliation must not read task stages"
        ),
    )
    monkeypatch.setattr(review, "_clock", lambda: NOW)
    monkeypatch.setattr(
        review.staffing_hours,
        "current_pay_period_bounds",
        lambda today: (date(2026, 8, 16), date(2026, 8, 29)),
    )
    monkeypatch.setattr(
        review.store,
        "claim_due",
        lambda owner, now, **kwargs: [replace(row, lease_owner=owner)],
    )
    monkeypatch.setattr(review.store, "release_claim", lambda *args, **kwargs: True)
    monkeypatch.setattr(review.odoo_client, "find_matching_leaves", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        review, "_sync_claimed_task", lambda request, owner: "escalated", raising=False
    )
    result = review.reconcile_once(NOW)
    assert result == review.ReconcileResult(1, 0, 1, 0)
    assert row.state == "needs_review"


@pytest.mark.parametrize(("project_ids", "status"), [([], "retry"), ([7, 8], "blocked")])
def test_review_delivery_requires_one_existing_exact_active_project(
    monkeypatch, project_ids, status
):
    fake = ReviewFake()
    wire_review(monkeypatch, fake, _needs_review())
    monkeypatch.setattr(
        review.odoo_client,
        "find_active_feedback_project_ids",
        lambda name: list(project_ids),
        raising=False,
    )
    monkeypatch.setattr(
        review.odoo_client,
        "ensure_feedback_project",
        lambda: pytest.fail("review delivery must never create a project"),
    )

    result = review.sync_review_task(41, NOW)

    assert result.status == status
    assert fake.created == []


def test_active_exact_task_wins_over_different_saved_archived_id(monkeypatch):
    fake = ReviewFake(task_ids=[502])
    current = wire_review(monkeypatch, fake, _needs_review(odoo_task_id=501))
    monkeypatch.setattr(
        review.odoo_client,
        "find_active_feedback_project_ids",
        lambda name: [7],
        raising=False,
    )

    result = review.sync_review_task(41, NOW)

    assert result.task_id == 502
    assert current["row"].odoo_task_id == 502
    assert fake.updated[0][0] == 502


def test_correct_archived_saved_task_is_verified_then_reactivated(monkeypatch):
    fake = ReviewFake(task_ids=[])
    wire_review(monkeypatch, fake, _needs_review(odoo_task_id=501))
    checked = []
    monkeypatch.setattr(
        review.odoo_client,
        "find_active_feedback_project_ids",
        lambda name: [7],
        raising=False,
    )
    monkeypatch.setattr(
        review.odoo_client,
        "fetch_feedback_task_identity",
        lambda task_id: checked.append(task_id)
        or {
            "id": 501,
            "name": review.task_name(_needs_review()),
            "project_id": 7,
            "active": False,
        },
        raising=False,
    )

    result = review.sync_review_task(41, NOW)

    assert result.task_id == 501
    assert checked == [501]
    assert fake.updated[0][0] == 501


@pytest.mark.parametrize(
    "identity",
    [
        {
            "id": 501,
            "name": "renamed",
            "project_id": 7,
            "active": False,
        },
        {
            "id": 501,
            "name": "[GPI-PM-PTO-41] Review Person 41 — 2026-08-20",
            "project_id": 8,
            "active": False,
        },
        {
            "id": 501,
            "name": "[GPI-PM-PTO-41] Review Person 41 — 2026-08-20",
            "project_id": 7,
            "active": True,
        },
        None,
    ],
)
def test_unverified_saved_task_identity_fails_permanently(monkeypatch, identity):
    fake = ReviewFake(task_ids=[])
    current = wire_review(monkeypatch, fake, _needs_review(odoo_task_id=501))
    monkeypatch.setattr(
        review.odoo_client,
        "find_active_feedback_project_ids",
        lambda name: [7],
        raising=False,
    )
    monkeypatch.setattr(
        review.odoo_client,
        "fetch_feedback_task_identity",
        lambda task_id: identity,
        raising=False,
    )

    result = review.sync_review_task(41, NOW)

    assert result.status == "blocked"
    assert current["row"].task_next_at.year == 9999
    assert fake.updated == []
    assert fake.created == []


def test_tenth_recoverable_attempt_becomes_permanent_and_stops_future_mutations(monkeypatch):
    fake = ReviewFake(users=[])
    current = wire_review(monkeypatch, fake, _needs_review(task_attempts=9))

    first = review.sync_review_task(41, NOW)
    fake.events.clear()
    second = review.sync_review_task(41, NOW)

    assert first.status == "blocked"
    assert second.status == "blocked"
    assert current["row"].task_attempts == 10
    assert current["row"].task_next_at.year == 9999
    assert not {"users", "search", "create", "update"}.intersection(
        event[0] for event in fake.events
    )


def test_create_compatibility_fallback_stops_when_lease_is_lost(monkeypatch):
    fake = ReviewFake()
    wire_review(monkeypatch, fake, _needs_review())
    monkeypatch.setattr(
        review.odoo_client,
        "find_active_feedback_project_ids",
        lambda name: [7],
        raising=False,
    )
    primary = []
    fallback = []
    monkeypatch.setattr(
        review.odoo_client,
        "create_review_task_user_ids",
        lambda **values: primary.append(values)
        or (_ for _ in ()).throw(xmlrpc.client.Fault(1, "unknown field user_ids")),
        raising=False,
    )
    monkeypatch.setattr(
        review.odoo_client,
        "create_review_task_user_id",
        lambda **values: fallback.append(values) or 501,
        raising=False,
    )
    renewals = 0

    def renew(*args, **kwargs):
        nonlocal renewals
        renewals += 1
        if renewals == 2:
            raise store.StaleTransition("taken over")
        return _needs_review(lease_owner=args[1], lease_until=NOW + timedelta(seconds=120))

    monkeypatch.setattr(review.store, "renew_claim", renew)

    review.sync_review_task(41, NOW)

    assert len(primary) == 1
    assert fallback == []


def test_update_compatibility_fallback_stops_when_lease_is_lost(monkeypatch):
    fake = ReviewFake(task_ids=[501])
    wire_review(monkeypatch, fake, _needs_review(odoo_task_id=501))
    monkeypatch.setattr(
        review.odoo_client,
        "find_active_feedback_project_ids",
        lambda name: [7],
        raising=False,
    )
    primary = []
    fallback = []
    monkeypatch.setattr(
        review.odoo_client,
        "update_review_task_user_ids",
        lambda **values: primary.append(values)
        or (_ for _ in ()).throw(xmlrpc.client.Fault(1, "unknown field user_ids")),
        raising=False,
    )
    monkeypatch.setattr(
        review.odoo_client,
        "update_review_task_user_id",
        lambda **values: fallback.append(values),
        raising=False,
    )
    renewals = 0

    def renew(*args, **kwargs):
        nonlocal renewals
        renewals += 1
        if renewals == 2:
            raise store.StaleTransition("taken over")
        return _needs_review(
            odoo_task_id=501,
            lease_owner=args[1],
            lease_until=NOW + timedelta(seconds=120),
        )

    monkeypatch.setattr(review.store, "renew_claim", renew)

    review.sync_review_task(41, NOW)

    assert len(primary) == 1
    assert fallback == []


def wire_resolution_progress(monkeypatch, current, fake):
    def save_resolution_delivery(
        request_id,
        owner,
        *,
        expected_step,
        new_step,
        attempts,
        next_at,
        error,
        now,
    ):
        assert current["row"].task_resolution_step == expected_step
        current["row"] = replace(
            current["row"],
            task_resolution_step=new_step,
            task_resolution_attempts=attempts,
            task_resolution_next_at=next_at,
            task_resolution_error=error,
        )
        fake.events.append(("resolution_save", new_step, attempts, next_at, error))
        return current["row"]

    monkeypatch.setattr(
        review.store,
        "save_resolution_delivery",
        save_resolution_delivery,
        raising=False,
    )
    monkeypatch.setattr(
        review.odoo_client,
        "fetch_feedback_task_identity",
        lambda task_id: {
            "id": task_id,
            "name": review.task_name(current["row"]),
            "project_id": 7,
            "active": True,
        },
        raising=False,
    )
    monkeypatch.setattr(
        review.odoo_client,
        "find_active_feedback_project_ids",
        lambda name: [7],
        raising=False,
    )
    monkeypatch.setattr(
        review.odoo_client, "find_task_message_ids", lambda *args: [], raising=False
    )


def test_external_truth_commits_when_resolution_message_fails_and_stays_due(monkeypatch):
    matching = [{
        "id": 991,
        "employee_id": 41,
        "holiday_status_id": 7,
        "date_from": date(2026, 8, 20),
        "date_to": date(2026, 8, 20),
        "state": "validate",
    }]
    fake = ReviewFake()
    current = wire_review(
        monkeypatch, fake, _needs_review(odoo_task_id=501), matching=matching
    )
    wire_resolution_progress(monkeypatch, current, fake)

    def adopt(*args, **kwargs):
        current["row"] = replace(current["row"], pto_leave_id=991)
        return current["row"]

    monkeypatch.setattr(review.store, "adopt_external_pto", adopt, raising=False)

    def finalize(*args, **kwargs):
        current["row"] = replace(
            current["row"],
            state="approved",
            pto_leave_id=991,
            task_resolution_step="none",
            task_resolution_attempts=0,
            task_resolution_next_at=NOW,
        )
        return current["row"]

    monkeypatch.setattr(review.store, "finalize_approved", finalize)
    monkeypatch.setattr(review.conversion, "_invalidate_after_commit", lambda row: None)
    monkeypatch.setattr(
        review.odoo_client,
        "post_task_message",
        lambda *args: (_ for _ in ()).throw(TimeoutError("message response lost")),
    )

    result = review.resolve_external_pto(41, NOW)

    assert result.status == "approved"
    assert current["row"].state == "approved"
    assert current["row"].task_resolution_step == "none"
    assert current["row"].task_resolution_attempts == 1
    assert current["row"].task_resolution_next_at > NOW
    assert fake.closed == []


def test_ambiguous_message_is_adopted_then_task_closes_once(monkeypatch):
    fake = ReviewFake()
    row = _needs_review(
        state="approved",
        odoo_task_id=501,
        task_resolution_step="none",
        task_resolution_attempts=0,
        task_resolution_next_at=NOW,
    )
    current = wire_review(monkeypatch, fake, row)
    wire_resolution_progress(monkeypatch, current, fake)
    searches = 0

    def find_messages(*args):
        nonlocal searches
        searches += 1
        return [] if searches == 1 else [901]

    monkeypatch.setattr(review.odoo_client, "find_task_message_ids", find_messages)
    monkeypatch.setattr(
        review.odoo_client,
        "post_task_message",
        lambda *args: (_ for _ in ()).throw(TimeoutError("response lost")),
    )

    outcome = review._deliver_terminal_claimed(current["row"], OWNER)

    assert outcome == "closed"
    assert searches == 2
    assert current["row"].task_resolution_step == "closed"
    assert fake.closed == [501]


def test_restart_after_message_checkpoint_skips_duplicate_message_and_closes(monkeypatch):
    fake = ReviewFake()
    row = _needs_review(
        state="approved",
        odoo_task_id=501,
        task_resolution_step="message_posted",
        task_resolution_attempts=0,
        task_resolution_next_at=NOW,
    )
    current = wire_review(monkeypatch, fake, row)
    wire_resolution_progress(monkeypatch, current, fake)
    monkeypatch.setattr(
        review.odoo_client,
        "post_task_message",
        lambda *args: pytest.fail("checkpointed message must not be posted twice"),
    )

    outcome = review._deliver_terminal_claimed(current["row"], OWNER)

    assert outcome == "closed"
    assert current["row"].task_resolution_step == "closed"
    assert fake.closed == [501]


def test_close_failure_preserves_message_checkpoint_for_retry(monkeypatch):
    fake = ReviewFake()
    row = _needs_review(
        state="resolved_manually",
        manual_resolution_note="Paid another way",
        odoo_task_id=501,
        task_resolution_step="message_posted",
        task_resolution_attempts=0,
        task_resolution_next_at=NOW,
    )
    current = wire_review(monkeypatch, fake, row)
    wire_resolution_progress(monkeypatch, current, fake)
    monkeypatch.setattr(
        review.odoo_client,
        "close_task",
        lambda task_id: (_ for _ in ()).throw(TimeoutError("close response lost")),
    )

    outcome = review._deliver_terminal_claimed(current["row"], OWNER)

    assert outcome == "retry"
    assert current["row"].task_resolution_step == "message_posted"
    assert current["row"].task_resolution_attempts == 1
    assert fake.messages == []


def test_terminal_reconcile_isolates_retry_and_continues_to_later_row(monkeypatch):
    first = _request(
        41,
        state="approved",
        odoo_task_id=501,
        task_resolution_step="none",
        task_resolution_next_at=NOW,
    )
    second = _request(
        42,
        state="resolved_manually",
        odoo_task_id=502,
        task_resolution_step="message_posted",
        task_resolution_next_at=NOW,
    )
    processed = []
    monkeypatch.setattr(review, "_clock", lambda: NOW)
    monkeypatch.setattr(
        review.staffing_hours,
        "current_pay_period_bounds",
        lambda today: (date(2026, 8, 16), date(2026, 8, 29)),
    )
    monkeypatch.setattr(
        review.store,
        "claim_due",
        lambda owner, now, **kwargs: [
            replace(first, lease_owner=owner),
            replace(second, lease_owner=owner),
        ],
    )
    monkeypatch.setattr(review.store, "release_claim", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        review,
        "_deliver_terminal_claimed",
        lambda row, owner: processed.append(row.id) or ("retry" if row.id == 41 else "closed"),
        raising=False,
    )

    result = review.reconcile_once(NOW, limit=2)

    assert processed == [41, 42]
    assert result == review.ReconcileResult(2, 1, 1, 0)
