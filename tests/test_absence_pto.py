from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from zira_dashboard import absence_pto as domain


TODAY = date(2026, 8, 28)
PERIOD_START = date(2026, 8, 17)
PERIOD_END = date(2026, 8, 30)
ABSENCE_DAY = date(2026, 8, 20)
LIVE_NOW = datetime(2026, 8, 28, 14, 30, tzinfo=UTC)


def _type_row(type_id=7, **overrides):
    row = {
        "holiday_status_id": type_id,
        "name": "Paid Time Off",
        "request_unit": "day",
        "requires_allocation": "yes",
        "active": True,
    }
    row.update(overrides)
    return row


def _request(day=ABSENCE_DAY, state="pending"):
    return SimpleNamespace(absence_day=day, state=state)


def _wire_candidate(
    monkeypatch,
    *,
    absence_day=ABSENCE_DAY,
    balance=4.0,
    requests=(),
    type_rows=None,
):
    monkeypatch.setattr(
        domain.staffing_hours,
        "current_pay_period_bounds",
        lambda today: (PERIOD_START, PERIOD_END),
    )

    def fake_query(sql, params=None):
        if "FROM manual_absences" in sql:
            assert "reason" not in sql.lower()
            if "WHERE day = %s" in sql:
                assert params == (absence_day, "44")
            else:
                assert params[0] == "44"
            return [{"day": absence_day, "odoo_leave_id": 91}]
        if "FROM leave_types_cache" in sql:
            return [_type_row()] if type_rows is None else type_rows
        raise AssertionError(sql)

    monkeypatch.setattr(domain.db, "query", fake_query)
    monkeypatch.setattr(
        domain.absence_pto_store,
        "list_for_person",
        lambda emp_id: list(requests),
    )
    refreshed = []
    monkeypatch.setattr(
        domain.time_off_balances,
        "refresh_for_employee",
        lambda odoo_id: refreshed.append(odoo_id),
    )
    monkeypatch.setattr(
        domain.time_off_balances,
        "get_for_employee",
        lambda odoo_id: [
            {"holiday_status_id": 7, "available_practical": Decimal(str(balance))}
        ],
    )
    return refreshed


def test_resolve_paid_time_off_type_requires_one_exact_allocated_day_type(monkeypatch):
    monkeypatch.setattr(
        domain.db,
        "query",
        lambda *_: [
            _type_row(1, name="PTO"),
            _type_row(2, request_unit="hour"),
            _type_row(3, requires_allocation="no"),
            _type_row(4, active=False),
            _type_row(7),
        ],
    )

    assert domain.resolve_paid_time_off_type() == domain.PtoType(7, "Paid Time Off")


@pytest.mark.parametrize("rows", [[], [_type_row(7), _type_row(8, request_unit="half_day")]])
def test_resolve_paid_time_off_type_rejects_missing_or_ambiguous_exact_types(
    monkeypatch, rows
):
    monkeypatch.setattr(domain.db, "query", lambda *_: rows)

    with pytest.raises(domain.SubmissionError, match="Paid Time Off is not available"):
        domain.resolve_paid_time_off_type()


def test_candidate_stays_visible_but_disabled_when_balance_is_low(monkeypatch):
    _wire_candidate(monkeypatch, balance=0.5)

    rows = domain.list_candidates(44, TODAY)

    assert rows == [
        domain.AbsenceCandidate(
            ABSENCE_DAY,
            eligible=False,
            blocked_reason="You need 1 PTO day. You have 0.5.",
            available_practical=0.5,
        )
    ]


def test_candidates_are_limited_to_the_authenticated_odoo_employee(monkeypatch):
    seen = []
    monkeypatch.setattr(
        domain.staffing_hours,
        "current_pay_period_bounds",
        lambda today: (PERIOD_START, PERIOD_END),
    )

    def fake_query(sql, params=None):
        if "manual_absences" in sql:
            seen.append(params)
            return []
        return [_type_row()]

    monkeypatch.setattr(domain.db, "query", fake_query)
    monkeypatch.setattr(domain.absence_pto_store, "list_for_person", lambda emp_id: [])
    monkeypatch.setattr(domain.time_off_balances, "refresh_for_employee", lambda _: 0)
    monkeypatch.setattr(domain.time_off_balances, "get_for_employee", lambda _: [])

    assert domain.list_candidates(44, TODAY) == []
    assert seen == [("44", PERIOD_START, TODAY)]


@pytest.mark.parametrize(
    "state", ["pending", "converting", "needs_review", "approved", "resolved_manually"]
)
def test_active_approved_and_manual_resolution_requests_block_candidate(
    monkeypatch, state
):
    _wire_candidate(monkeypatch, requests=[_request(state=state)])

    row = domain.list_candidates(44, TODAY)[0]

    assert row.eligible is False
    assert row.blocked_reason == "A PTO request already exists for this absence."


def test_denied_request_allows_resubmission_while_still_eligible(monkeypatch):
    _wire_candidate(monkeypatch, requests=[_request(state="denied")])

    row = domain.list_candidates(44, TODAY)[0]

    assert row.eligible is True
    assert row.blocked_reason is None
    assert row.available_practical == 4.0


def test_deny_records_reason_and_actor_without_calling_odoo(monkeypatch):
    pending = _request(state="pending")
    denied = SimpleNamespace(
        absence_day=pending.absence_day,
        state="denied",
        denial_reason="Save the PTO",
        decided_by_upn="dale@gruberpallets.com",
        decided_by_name="Dale",
    )
    calls = []
    monkeypatch.setattr(
        domain.absence_pto_store,
        "claim_request",
        lambda request_id, owner, now, lease_seconds=120: pending,
    )
    monkeypatch.setattr(
        domain.absence_pto_store,
        "finalize_denied",
        lambda request_id, owner, **kwargs: calls.append(
            (request_id, owner, kwargs)
        )
        or denied,
    )
    monkeypatch.setattr(
        domain.absence_pto_store,
        "release_claim",
        lambda request_id, owner, now=None: True,
    )
    from zira_dashboard import odoo_client

    monkeypatch.setattr(
        odoo_client,
        "refuse_leave",
        lambda *args: pytest.fail("denial must not mutate Odoo"),
    )
    monkeypatch.setattr(
        odoo_client,
        "post_leave_message",
        lambda *args: pytest.fail("denial must not write Odoo chatter"),
    )

    result = domain.deny(
        41,
        "dale@gruberpallets.com",
        "Dale",
        "  Save the PTO  ",
        "page",
    )

    assert result is denied
    assert len(calls) == 1
    assert calls[0][0] == 41
    assert calls[0][2]["reason"] == "Save the PTO"
    assert calls[0][2]["actor_upn"] == "dale@gruberpallets.com"
    assert calls[0][2]["actor_name"] == "Dale"
    assert calls[0][2]["source"] == "page"


def test_deny_requires_a_reason_before_claiming(monkeypatch):
    monkeypatch.setattr(
        domain.absence_pto_store,
        "claim_request",
        lambda *args: pytest.fail("blank reason must fail before a claim"),
    )

    with pytest.raises(domain.DecisionError, match="reason is required"):
        domain.deny(41, "dale@gruberpallets.com", "Dale", " ", "page")


def test_deny_keeps_a_supplied_workflow_time_out_of_live_lease_checks(monkeypatch):
    workflow_now = datetime(2031, 4, 10, 14, 30, tzinfo=UTC)
    pending = _request(state="pending")
    seen = {}
    monkeypatch.setattr(domain, "_clock", lambda: LIVE_NOW)

    def claim(request_id, owner, now, lease_seconds=120):
        seen["claim_now"] = now
        return pending

    def finalize(request_id, owner, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(state="denied", absence_day=ABSENCE_DAY)

    def release(request_id, owner, now=None):
        seen["release_now"] = now
        return True

    monkeypatch.setattr(domain.absence_pto_store, "claim_request", claim)
    monkeypatch.setattr(domain.absence_pto_store, "finalize_denied", finalize)
    monkeypatch.setattr(domain.absence_pto_store, "release_claim", release)

    domain.deny(
        41,
        "dale@gruberpallets.com",
        "Dale",
        "Save the PTO",
        "page",
        now=workflow_now,
    )

    assert seen["claim_now"] == LIVE_NOW
    assert seen["lease_now"] == LIVE_NOW
    assert seen["release_now"] == LIVE_NOW
    assert seen["workflow_now"] == workflow_now


@pytest.mark.parametrize("day", [TODAY, date(2026, 8, 29)])
def test_submit_rejects_current_or_future_day_before_database_lookup(monkeypatch, day):
    monkeypatch.setattr(
        domain.staffing_hours,
        "current_pay_period_bounds",
        lambda today: (PERIOD_START, PERIOD_END),
    )
    monkeypatch.setattr(
        domain.db, "query", lambda *_: pytest.fail("must reject before querying")
    )

    with pytest.raises(domain.SubmissionError, match="before today"):
        domain.submit(3, 44, "Ana", day, "", TODAY)


def test_submit_rejects_forged_or_prior_period_day(monkeypatch):
    _wire_candidate(monkeypatch, absence_day=date(2026, 8, 15), balance=4.0)

    with pytest.raises(domain.SubmissionError, match="current pay period"):
        domain.submit(3, 44, "Ana", date(2026, 8, 15), "", TODAY)


def test_submit_requires_exact_manual_absence_for_authenticated_employee(monkeypatch):
    _wire_candidate(monkeypatch)
    original_query = domain.db.query

    def fake_query(sql, params=None):
        if "manual_absences" in sql:
            assert params == (ABSENCE_DAY, "44")
            return []
        return original_query(sql, params)

    monkeypatch.setattr(domain.db, "query", fake_query)

    with pytest.raises(domain.SubmissionError, match="not found for this employee"):
        domain.submit(3, 44, "Ana", ABSENCE_DAY, "", TODAY)


def test_submit_refreshes_then_rereads_the_matching_balance(monkeypatch):
    refreshed = _wire_candidate(monkeypatch, balance=2.25)
    created = {}
    monkeypatch.setattr(
        domain.absence_pto_store,
        "create_request",
        lambda **kwargs: created.update(kwargs) or SimpleNamespace(id=12),
    )

    linked = domain.submit(3, 44, "Ana", ABSENCE_DAY, " hello ", TODAY)

    assert linked.id == 12
    assert refreshed == [44]
    assert created == {
        "absence_day": ABSENCE_DAY,
        "emp_id": "44",
        "person_odoo_id": 44,
        "person_name": "Ana",
        "holiday_status_id": 7,
        "leave_type_name": "Paid Time Off",
        "balance_at_submit": Decimal("2.25"),
        "original_absence_leave_id": 91,
        "employee_note": "hello",
        "requested_by_person_id": 3,
    }


def test_submit_rejects_low_balance_after_refresh(monkeypatch):
    _wire_candidate(monkeypatch, balance=0.75)

    with pytest.raises(domain.SubmissionError, match=r"need 1 PTO day.*0\.75"):
        domain.submit(3, 44, "Ana", ABSENCE_DAY, "", TODAY)


def test_submit_translates_only_named_partial_unique_race(monkeypatch):
    _wire_candidate(monkeypatch)

    class NamedUniqueRace(Exception):
        def __init__(self, constraint_name):
            self.diag = SimpleNamespace(constraint_name=constraint_name)

    monkeypatch.setattr(domain, "UniqueViolation", NamedUniqueRace)
    monkeypatch.setattr(
        domain.absence_pto_store,
        "create_request",
        lambda **kwargs: (_ for _ in ()).throw(
            NamedUniqueRace("absence_pto_requests_active_uniq")
        ),
    )

    with pytest.raises(domain.SubmissionError, match="already exists"):
        domain.submit(3, 44, "Ana", ABSENCE_DAY, "", TODAY)


def test_submit_reraises_unrelated_database_failure(monkeypatch):
    _wire_candidate(monkeypatch)

    class NamedUniqueRace(Exception):
        def __init__(self, constraint_name):
            self.diag = SimpleNamespace(constraint_name=constraint_name)

    monkeypatch.setattr(domain, "UniqueViolation", NamedUniqueRace)
    failure = NamedUniqueRace("some_other_constraint")
    monkeypatch.setattr(
        domain.absence_pto_store,
        "create_request",
        lambda **kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(NamedUniqueRace) as caught:
        domain.submit(3, 44, "Ana", ABSENCE_DAY, "", TODAY)
    assert caught.value is failure


def test_employee_requests_uses_canonical_odoo_employee_key(monkeypatch):
    expected = [_request()]
    monkeypatch.setattr(
        domain.absence_pto_store,
        "list_for_person",
        lambda emp_id: expected if emp_id == "44" else pytest.fail(emp_id),
    )

    assert domain.employee_requests(44) is expected
