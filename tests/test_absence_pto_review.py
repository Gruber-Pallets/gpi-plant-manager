from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
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
