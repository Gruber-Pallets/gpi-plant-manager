from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Lock
from uuid import UUID

import pytest

from zira_dashboard import absence_pto_conversion as conversion
from zira_dashboard import absence_pto_store as store


NOW = datetime(2026, 8, 28, 14, 30, tzinfo=UTC)
DAY = date(2026, 8, 20)


def _request(**changes) -> store.AbsencePtoRequest:
    row = store.AbsencePtoRequest(
        id=41,
        absence_day=DAY,
        emp_id="44",
        person_odoo_id=44,
        person_name="Maria Example",
        holiday_status_id=7,
        leave_type_name="Paid Time Off",
        balance_at_submit=Decimal("4"),
        original_absence_leave_id=70,
        pto_leave_id=None,
        state="pending",
        conversion_step="not_started",
        employee_note="Please use PTO.",
        denial_reason=None,
        manual_resolution_note=None,
        sync_error=None,
        odoo_task_id=None,
        task_attempts=0,
        task_next_at=None,
        lease_owner=None,
        lease_until=None,
        requested_by_person_id=14,
        decided_by_upn=None,
        decided_by_name=None,
        requested_at=NOW,
        decided_at=None,
        resolved_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    return replace(row, **changes)


def _leave(leave_id: int, employee_id: int, type_id: int, state: str) -> dict:
    return {
        "id": leave_id,
        "employee_id": employee_id,
        "holiday_status_id": type_id,
        "date_from": DAY,
        "date_to": DAY,
        "state": state,
    }


class MutableClock:
    def __init__(self, current=NOW):
        self.current = current

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)


class FakeOdoo:
    """Stateful Odoo double: every read observes the latest mutation."""

    def __init__(self, *, absence=None, pto=None, lost_create=False, two_step=False):
        self.leaves = {}
        for leave in (absence, pto):
            if leave:
                self.leaves[leave["id"]] = dict(leave)
        self.events = []
        self.next_id = 71
        self.lost_create = lost_create
        self.two_step = two_step
        self.on_mutation = None
        self.after_mutation = None
        self.timeline = []

    def _mutating(self, action, leave_id):
        if self.on_mutation:
            callback, self.on_mutation = self.on_mutation, None
            callback()
        self.timeline.append((action, leave_id))

    def _mutated(self, action, leave_id):
        if self.after_mutation:
            callback, self.after_mutation = self.after_mutation, None
            callback(action, leave_id, self)

    def fetch_leave_snapshot(self, leave_id):
        leave = self.leaves.get(leave_id)
        return dict(leave) if leave else None

    def find_matching_leaves(self, employee_id, type_id, day, include_terminal=True):
        rows = [
            dict(leave)
            for leave in self.leaves.values()
            if leave["employee_id"] == employee_id
            and leave["holiday_status_id"] == type_id
            and leave["date_from"] == day
            and leave["date_to"] == day
            and (include_terminal or leave["state"] not in {"cancel", "refuse"})
        ]
        return sorted(rows, key=lambda row: row["id"])[:2]

    def refuse_leave(self, leave_id):
        self._mutating("refuse", leave_id)
        self.events.append(("refuse", leave_id))
        self.leaves[leave_id]["state"] = "refuse"
        self._mutated("refuse", leave_id)

    def create_leave(
        self,
        employee_odoo_id,
        holiday_status_id,
        date_from,
        date_to,
        hour_from=None,
        hour_to=None,
        note=None,
    ):
        leave_id = self.next_id
        self._mutating("create", leave_id)
        self.next_id += 1
        self.events.append(("create", employee_odoo_id, holiday_status_id, date_from))
        self.leaves[leave_id] = _leave(leave_id, employee_odoo_id, holiday_status_id, "draft")
        self._mutated("create", leave_id)
        if self.lost_create:
            self.lost_create = False
            raise TimeoutError("response was lost")
        return leave_id

    def confirm_leave(self, leave_id):
        self._mutating("confirm", leave_id)
        if self.leaves[leave_id]["state"] == "draft":
            self.events.append(("confirm", leave_id))
            self.leaves[leave_id]["state"] = "confirm"
        self._mutated("confirm", leave_id)

    def approve_leave_once(self, leave_id):
        self._mutating("approve", leave_id)
        if self.leaves[leave_id]["state"] in {"confirm", "validate1"}:
            self.events.append(("approve", leave_id))
            self.leaves[leave_id]["state"] = (
                "validate1"
                if self.two_step and self.leaves[leave_id]["state"] == "confirm"
                else "validate"
            )
        self._mutated("approve", leave_id)


class FakeStore:
    def __init__(self, request, clock):
        self.request = request
        self.clock = clock
        self.owner = None
        self.guard = Lock()
        self.finalizations = []
        self.timeline = []

    def claim_request(self, request_id, owner, now, lease_seconds=120):
        with self.guard:
            if self.owner is not None and self.owner != owner and self.request.lease_until > now:
                return None
            self.owner = owner
            self.request = replace(
                self.request,
                lease_owner=owner,
                lease_until=now + timedelta(seconds=lease_seconds),
            )
            self.timeline.append(("claim", owner, now))
            return self.request

    def renew_claim(self, request_id, owner, now, lease_seconds=120):
        with self.guard:
            if (
                self.owner != owner
                or self.request.lease_until is None
                or self.request.lease_until <= now
            ):
                raise store.StaleTransition("claim renewal lost its current lease")
            self.request = replace(
                self.request,
                lease_until=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
            self.timeline.append(("renew", owner, now))
            return self.request

    def release_claim(self, request_id, owner, now=None):
        with self.guard:
            current = now or self.clock.current
            if (
                self.owner != owner
                or self.request.lease_until is None
                or self.request.lease_until <= current
            ):
                return False
            self.owner = None
            self.request = replace(self.request, lease_owner=None, lease_until=None)
            return True

    def transition(
        self,
        request_id,
        owner,
        *,
        expected_state,
        expected_step,
        new_state,
        new_step,
        now=None,
        **changes,
    ):
        current = now or self.clock.current
        if (
            self.owner != owner
            or self.request.lease_until is None
            or self.request.lease_until <= current
        ):
            raise store.StaleTransition("request no longer matches its lease")
        assert self.request.state == expected_state
        assert self.request.conversion_step == expected_step
        allowed = {key: value for key, value in changes.items() if value is not store._UNSET}
        self.request = replace(
            self.request,
            state=new_state,
            conversion_step=new_step,
            updated_at=now or NOW,
            **allowed,
        )
        self.timeline.append(("transition", owner, current, new_step))
        return self.request

    def mark_needs_review(self, request_id, owner, *, error, now=None):
        current = now or NOW
        if self.owner != owner or self.request.lease_until <= current:
            raise store.StaleTransition("review transition lost its current lease")
        self.request = replace(
            self.request,
            state="needs_review",
            sync_error=error,
            task_next_at=now or NOW,
            updated_at=now or NOW,
        )
        return self.request

    def finalize_approved(self, request_id, owner, **kwargs):
        if (
            self.owner != owner
            or self.request.lease_until is None
            or self.request.lease_until <= kwargs["now"]
        ):
            raise store.StaleTransition("approval finalization lost its current lease")
        assert self.request.conversion_step == "pto_approved"
        self.finalizations.append(kwargs)
        self.request = replace(
            self.request,
            state="approved",
            decided_by_upn=kwargs["actor_upn"],
            decided_by_name=kwargs["actor_name"],
            decided_at=kwargs["now"],
            updated_at=kwargs["now"],
        )
        return self.request


def wire(monkeypatch, fake_odoo, request, *, balance=4.0, clock=None):
    clock = clock or MutableClock()
    fake_store = FakeStore(request, clock)
    fake_odoo.timeline = fake_store.timeline
    monkeypatch.setattr(conversion, "_clock", lambda: clock.current)
    monkeypatch.setattr(conversion.store, "claim_request", fake_store.claim_request)
    monkeypatch.setattr(conversion.store, "release_claim", fake_store.release_claim)
    monkeypatch.setattr(conversion.store, "renew_claim", fake_store.renew_claim)
    monkeypatch.setattr(conversion.store, "transition", fake_store.transition)
    monkeypatch.setattr(conversion.store, "mark_needs_review", fake_store.mark_needs_review)
    monkeypatch.setattr(conversion.store, "finalize_approved", fake_store.finalize_approved)
    monkeypatch.setattr(
        conversion.odoo_client, "fetch_leave_snapshot", fake_odoo.fetch_leave_snapshot
    )
    monkeypatch.setattr(
        conversion.odoo_client, "find_matching_leaves", fake_odoo.find_matching_leaves
    )
    monkeypatch.setattr(conversion.odoo_client, "refuse_leave", fake_odoo.refuse_leave)
    monkeypatch.setattr(conversion.odoo_client, "create_leave", fake_odoo.create_leave)
    monkeypatch.setattr(conversion.odoo_client, "confirm_leave", fake_odoo.confirm_leave)
    monkeypatch.setattr(conversion.odoo_client, "approve_leave_once", fake_odoo.approve_leave_once)
    monkeypatch.setattr(conversion.absence_sync, "resolve_absence_leave_type_id", lambda: 9)
    monkeypatch.setattr(
        conversion.absence_pto,
        "resolve_paid_time_off_type",
        lambda: conversion.absence_pto.PtoType(7, "Paid Time Off"),
    )
    monkeypatch.setattr(
        conversion.staffing_hours,
        "current_pay_period_bounds",
        lambda today: (date(2026, 8, 16), date(2026, 8, 29)),
    )

    def refresh_balance(employee_id):
        fake_store.timeline.append(("preflight_balance", fake_store.owner, clock.current))
        return 1

    monkeypatch.setattr(conversion.time_off_balances, "refresh_for_employee", refresh_balance)
    monkeypatch.setattr(
        conversion.time_off_balances,
        "get_for_employee",
        lambda employee_id: [{"holiday_status_id": 7, "available_practical": balance}],
    )
    monkeypatch.setattr(
        conversion.db,
        "query",
        lambda sql, params=None: (
            [
                {
                    "day": DAY,
                    "emp_id": "44",
                    "odoo_leave_id": (
                        fake_store.request.pto_leave_id
                        if fake_store.request.state == "approved"
                        else fake_store.request.original_absence_leave_id
                    ),
                }
            ]
            if "FROM manual_absences" in sql
            else []
        ),
    )
    monkeypatch.setattr(conversion, "_invalidate_after_commit", lambda request: None)
    return fake_store


def test_approve_refuses_absence_then_creates_and_approves_pto(monkeypatch):
    fake = FakeOdoo(absence=_leave(70, 44, 9, "validate"))
    fake_store = wire(monkeypatch, fake, _request(original_absence_leave_id=70))

    result = conversion.approve(41, "dale@gruberpallets.com", "Dale", "page", NOW)

    assert result.status == "approved"
    assert fake.events == [
        ("refuse", 70),
        ("create", 44, 7, DAY),
        ("confirm", 71),
        ("approve", 71),
    ]
    assert fake.fetch_leave_snapshot(71)["state"] == "validate"
    assert fake_store.request.pto_leave_id == 71
    assert fake_store.finalizations[0]["original_absence_leave_id"] == 70
    previous_mutation = -1
    for mutation_index, event in enumerate(fake_store.timeline):
        if event[0] not in {"refuse", "create", "confirm", "approve"}:
            continue
        boundary = fake_store.timeline[previous_mutation + 1 : mutation_index]
        assert any(item[0] == "renew" for item in boundary)
        assert any(item[0] == "preflight_balance" for item in boundary)
        previous_mutation = mutation_index


def test_second_approve_adopts_verified_result_without_mutating_odoo(monkeypatch):
    fake = FakeOdoo(pto=_leave(71, 44, 7, "validate"))
    fake_store = wire(
        monkeypatch,
        fake,
        _request(
            state="approved",
            conversion_step="pto_approved",
            original_absence_leave_id=70,
            pto_leave_id=71,
        ),
    )

    result = conversion.approve(41, "dale@gruberpallets.com", "Dale", "page", NOW)

    assert result.status == "approved"
    assert fake.events == []
    assert fake_store.finalizations == []


def test_local_only_absence_skips_refusal(monkeypatch):
    fake = FakeOdoo()
    wire(monkeypatch, fake, _request(original_absence_leave_id=None))

    result = conversion.approve(41, "dale@example.com", "Dale", "page", NOW)

    assert result.status == "approved"
    assert fake.events == [
        ("create", 44, 7, DAY),
        ("confirm", 71),
        ("approve", 71),
    ]


def test_lost_create_response_adopts_one_exact_pto(monkeypatch):
    fake = FakeOdoo(absence=_leave(70, 44, 9, "validate"), lost_create=True)
    fake_store = wire(monkeypatch, fake, _request())

    result = conversion.approve(41, "dale@example.com", "Dale", "page", NOW)

    assert result.status == "approved"
    assert fake_store.request.pto_leave_id == 71
    assert [event[0] for event in fake.events].count("create") == 1


def test_balance_below_one_returns_to_pending_without_odoo_mutation(monkeypatch):
    fake = FakeOdoo(absence=_leave(70, 44, 9, "validate"))
    fake_store = wire(monkeypatch, fake, _request(), balance=0.5)

    result = conversion.approve(41, "dale@example.com", "Dale", "page", NOW)

    assert result.status == "pending"
    assert result.message == "The current PTO balance is below one day."
    assert result.request.state == "pending"
    assert fake.events == []
    assert fake_store.owner is None


def test_failed_live_balance_refresh_fails_closed_before_odoo_mutation(monkeypatch):
    fake = FakeOdoo(absence=_leave(70, 44, 9, "validate"))
    fake_store = wire(monkeypatch, fake, _request())
    monkeypatch.setattr(conversion.time_off_balances, "refresh_for_employee", lambda employee_id: 0)

    result = conversion.approve(41, "dale@example.com", "Dale", "page", NOW)

    assert result.status == "needs_review"
    assert fake_store.request.state == "needs_review"
    assert fake.events == []


def test_balance_drop_after_claim_returns_converting_request_to_pending(monkeypatch):
    fake = FakeOdoo(absence=_leave(70, 44, 9, "validate"))
    fake_store = wire(monkeypatch, fake, _request())
    balances = iter([1.0, 0.5])
    monkeypatch.setattr(
        conversion.time_off_balances,
        "get_for_employee",
        lambda employee_id: [{"holiday_status_id": 7, "available_practical": next(balances)}],
    )

    result = conversion.approve(41, "dale@example.com", "Dale", "page", NOW)

    assert result.status == "pending"
    assert result.request.state == "pending"
    assert result.request.conversion_step == "not_started"
    assert fake_store.request.state == "pending"
    assert fake.events == []


def test_resume_does_not_turn_an_unapproved_pending_request_into_pto(monkeypatch):
    fake = FakeOdoo(absence=_leave(70, 44, 9, "validate"))
    fake_store = wire(monkeypatch, fake, _request())

    result = conversion.resume(41, NOW)

    assert result.status == "pending"
    assert result.request.state == "pending"
    assert fake_store.request.state == "pending"
    assert fake.events == []


def test_cache_failure_after_atomic_finalization_does_not_reverse_success(monkeypatch):
    fake = FakeOdoo(absence=_leave(70, 44, 9, "validate"))
    fake_store = wire(monkeypatch, fake, _request())
    monkeypatch.setattr(
        conversion,
        "_invalidate_after_commit",
        lambda request: (_ for _ in ()).throw(RuntimeError("cache unavailable")),
    )

    result = conversion.approve(41, "dale@example.com", "Dale", "page", NOW)

    assert fake_store.request.state == "approved"
    assert result.status == "approved"


@pytest.mark.parametrize(
    "wrong_leave",
    [
        _leave(70, 45, 9, "validate"),
        _leave(70, 44, 8, "validate"),
        {**_leave(70, 44, 9, "validate"), "date_from": date(2026, 8, 19)},
    ],
)
def test_wrong_original_odoo_identity_fails_closed(monkeypatch, wrong_leave):
    fake = FakeOdoo(absence=wrong_leave)
    fake_store = wire(monkeypatch, fake, _request())

    result = conversion.approve(41, "dale@example.com", "Dale", "page", NOW)

    assert result.status == "needs_review"
    assert fake_store.request.state == "needs_review"
    assert fake.events == []


def test_two_matching_pto_records_fail_closed_without_duplicate(monkeypatch):
    fake = FakeOdoo(absence=_leave(70, 44, 9, "refuse"), pto=_leave(71, 44, 7, "draft"))
    fake.leaves[72] = _leave(72, 44, 7, "confirm")
    fake_store = wire(
        monkeypatch,
        fake,
        _request(state="converting", conversion_step="absence_refused"),
    )

    result = conversion.resume(41, NOW)

    assert result.status == "needs_review"
    assert fake_store.request.state == "needs_review"
    assert fake.events == []


def test_unexpired_lease_contention_returns_busy(monkeypatch):
    fake = FakeOdoo()
    fake_store = wire(monkeypatch, fake, _request())
    fake_store.owner = UUID("17678a53-16f6-4f9f-ad94-c0503630cf1a")
    fake_store.request = replace(
        fake_store.request,
        lease_owner=fake_store.owner,
        lease_until=NOW + timedelta(seconds=120),
    )

    result = conversion.approve(41, "dale@example.com", "Dale", "page", NOW)

    assert result == conversion.ConversionResult(
        "busy", "This request is already being checked.", None
    )


def test_two_managers_cannot_mutate_odoo_at_the_same_time(monkeypatch):
    fake = FakeOdoo(absence=_leave(70, 44, 9, "validate"))
    wire(monkeypatch, fake, _request())
    competing = []
    fake.on_mutation = lambda: competing.append(
        conversion.approve(41, "two@example.com", "Manager Two", "page", NOW)
    )

    first = conversion.approve(41, "one@example.com", "Manager One", "page", NOW)

    assert first.status == "approved"
    assert [result.status for result in competing] == ["busy"]
    assert [event[0] for event in fake.events].count("create") == 1


def test_two_step_approval_renews_and_revalidates_between_approval_mutations(
    monkeypatch,
):
    fake = FakeOdoo(absence=_leave(70, 44, 9, "validate"), two_step=True)
    fake_store = wire(monkeypatch, fake, _request())

    result = conversion.approve(41, "one@example.com", "Manager One", "page", NOW)

    assert result.status == "approved"
    assert [event for event in fake.events if event[0] == "approve"] == [
        ("approve", 71),
        ("approve", 71),
    ]
    approval_indexes = [
        index for index, event in enumerate(fake_store.timeline) if event == ("approve", 71)
    ]
    between = fake_store.timeline[approval_indexes[0] + 1 : approval_indexes[1]]
    assert any(event[0] == "renew" for event in between)
    assert any(event[0] == "preflight_balance" for event in between)
    assert any(event[0] == "transition" for event in between)


def test_expired_manager_stops_after_refusal_when_second_manager_takes_lease(
    monkeypatch,
):
    clock = MutableClock()
    fake = FakeOdoo(absence=_leave(70, 44, 9, "validate"))
    wire(monkeypatch, fake, _request(), clock=clock)
    competing = []

    def takeover(action, leave_id, _fake):
        assert (action, leave_id) == ("refuse", 70)
        clock.advance(121)
        competing.append(conversion.approve(41, "two@example.com", "Manager Two", "page", NOW))

    fake.after_mutation = takeover

    stale = conversion.approve(41, "one@example.com", "Manager One", "page", NOW)

    assert stale.status == "busy"
    assert [result.status for result in competing] == ["approved"]
    assert [event[0] for event in fake.events].count("refuse") == 1
    assert [event[0] for event in fake.events].count("create") == 1


def test_expired_manager_cannot_second_approve_after_validate1_takeover(monkeypatch):
    clock = MutableClock()
    fake = FakeOdoo(absence=_leave(70, 44, 9, "validate"), two_step=True)
    fake_store = wire(monkeypatch, fake, _request(), clock=clock)
    competing = []

    def takeover(action, leave_id, current_fake):
        if (action, current_fake.leaves[leave_id]["state"]) != (
            "approve",
            "validate1",
        ):
            current_fake.after_mutation = takeover
            return
        clock.advance(121)
        competing.append(conversion.approve(41, "two@example.com", "Manager Two", "page", NOW))

    fake.after_mutation = takeover

    stale = conversion.approve(41, "one@example.com", "Manager One", "page", NOW)

    assert stale.status == "busy"
    assert [result.status for result in competing] == ["approved"]
    assert [event[0] for event in fake.events].count("create") == 1
    assert [event for event in fake.events if event[0] == "approve"] == [
        ("approve", 71),
        ("approve", 71),
    ]
    approval_indexes = [
        index for index, event in enumerate(fake_store.timeline) if event == ("approve", 71)
    ]
    between = fake_store.timeline[approval_indexes[0] + 1 : approval_indexes[1]]
    assert any(event[0] == "renew" for event in between)
    assert any(event[0] == "preflight_balance" for event in between)


class FakeCursor:
    def __init__(self, request_row):
        self.request_row = request_row
        self.operations = []
        self._rows = []

    def execute(self, sql, params=None):
        self.operations.append((" ".join(sql.split()), params))
        if "FROM absence_pto_requests" in sql and "FOR UPDATE" in sql:
            self._rows = [self.request_row]
        elif "FROM manual_absences" in sql and "FOR UPDATE" in sql:
            self._rows = [{"day": DAY, "emp_id": "44", "odoo_leave_id": 70}]
        elif "UPDATE absence_pto_requests" in sql and "RETURNING" in sql:
            approved = dict(self.request_row)
            approved.update(state="approved", decided_at=NOW, updated_at=NOW)
            self._rows = [approved]
        elif "UPDATE manual_absences" in sql and "RETURNING day" in sql:
            self._rows = [{"day": DAY}]
        elif "RETURNING id" in sql:
            self._rows = [{"id": 501}]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0]


def _request_row(request):
    return {field: getattr(request, field) for field in request.__dataclass_fields__}


def test_finalize_approved_uses_one_transaction_for_mirrors_link_and_audits(monkeypatch):
    owner = UUID("8ff2b216-3c83-4ca2-9187-3978954dc82c")
    request = _request(
        state="converting",
        conversion_step="pto_approved",
        pto_leave_id=71,
        lease_owner=owner,
        lease_until=NOW + timedelta(seconds=120),
    )
    cursor = FakeCursor(_request_row(request))

    @contextmanager
    def cursor_context():
        yield cursor

    monkeypatch.setattr(store.db, "cursor", cursor_context)

    approved = store.finalize_approved(
        41,
        owner,
        original_absence_leave_id=70,
        pto_leave_id=71,
        actor_upn="dale@example.com",
        actor_name="Dale",
        source="page",
        now=NOW,
    )

    sql = "\n".join(operation[0] for operation in cursor.operations)
    assert approved.state == "approved"
    assert "INSERT INTO time_off_requests" in sql
    assert "UPDATE time_off_requests SET state = 'refuse'" in sql
    assert "UPDATE manual_absences SET odoo_leave_id" in sql
    assert "DELETE FROM manual_absences" not in sql
    assert "UPDATE absence_pto_requests" in sql
    assert "INSERT INTO time_off_decisions" in sql
    assert "INSERT INTO inbox_events" in sql
    decision = next(
        params for statement, params in cursor.operations if "time_off_decisions" in statement
    )
    assert "absence_pto" in decision
    decision_detail = getattr(decision[-1], "adapted", decision[-1])
    assert {
        "original_absence_leave_id": 70,
        "pto_leave_id": 71,
    }.items() <= decision_detail.items()
