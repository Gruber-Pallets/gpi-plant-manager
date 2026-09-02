import pytest

from zira_dashboard import feedback_submitters
from zira_dashboard.feedback_submitters import (
    SubmitterError,
    active_choices,
    resolve_private,
    resolve_timeclock,
)


def person(employee_id, name, active, work_email):
    return {
        "employee_id": employee_id,
        "name": name,
        "active": active,
        "work_email": work_email,
    }


def local_people(monkeypatch, rows):
    monkeypatch.setattr(feedback_submitters.db, "query", lambda *_args: rows)


def test_active_choices_rejects_duplicate_local_ids_and_emails(monkeypatch):
    local_people(
        monkeypatch,
        [
            person(42, "zoe", True, "zoe@example.com"),
            person(41, "Ana", True, "ana@example.com"),
            person(43, "Inactive", False, "inactive@example.com"),
            person(42, "Duplicate ID", True, "other@example.com"),
            person(44, "Duplicate Email", True, "ANA@example.com"),
        ],
    )

    assert active_choices() == ()


def test_active_choices_returns_valid_unique_people_sorted_by_name(monkeypatch):
    local_people(
        monkeypatch,
        [
            person(42, "zoe", True, "zoe@example.com"),
            person(41, "Ana", True, "ana@example.com"),
            person(43, "Inactive", False, "inactive@example.com"),
        ],
    )

    assert active_choices() == (
        feedback_submitters.SubmitterChoice(employee_id=41, name="Ana"),
        feedback_submitters.SubmitterChoice(employee_id=42, name="zoe"),
    )


def test_timeclock_submitter_requires_one_exact_active_local_employee(monkeypatch):
    local_people(monkeypatch, [person(41, "Ana", True, "ana@gruberpallets.com")])

    assert resolve_timeclock(41) == feedback_submitters.ResolvedSubmitter(
        employee_id=41,
        name="Ana",
        email="ana@gruberpallets.com",
    )
    with pytest.raises(SubmitterError):
        resolve_timeclock(99)


@pytest.mark.parametrize("employee_id", [None, True, 0, -1, "41"])
def test_timeclock_submitter_rejects_absent_or_non_positive_ids(monkeypatch, employee_id):
    local_people(monkeypatch, [])

    with pytest.raises(SubmitterError):
        resolve_timeclock(employee_id)


@pytest.mark.parametrize(
    "rows",
    [
        [person(41, "Ana", False, "ana@example.com")],
        [
            person(41, "Ana", True, "ana@example.com"),
            person(41, "Ana Duplicate", True, "ana2@example.com"),
        ],
        [
            person(41, "Ana", True, "ana@example.com"),
            person(42, "Other Ana", True, "ANA@example.com"),
        ],
        [person(41, "Ana", True, None)],
        [person(41, "Ana", True, "bad email")],
    ],
)
def test_timeclock_submitter_rejects_inactive_invalid_or_duplicate_local_rows(monkeypatch, rows):
    local_people(monkeypatch, rows)

    with pytest.raises(SubmitterError):
        resolve_timeclock(41)


def test_private_submitter_matches_normalized_local_work_email(monkeypatch):
    local_people(monkeypatch, [person(41, "Ana", True, "ana@gruberpallets.com")])

    assert resolve_private("  ANA@gruberpallets.com ") == (
        feedback_submitters.ResolvedSubmitter(
            employee_id=41,
            name="Ana",
            email="ana@gruberpallets.com",
        )
    )


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [person(41, "Ana", False, "ana@example.com")],
        [
            person(41, "Ana", True, "ana@example.com"),
            person(42, "Other Ana", True, "ana@example.com"),
        ],
        [
            person(41, "Ana", True, "ana@example.com"),
            person(41, "Duplicate ID", True, "other@example.com"),
        ],
    ],
)
def test_private_submitter_rejects_missing_inactive_or_duplicate_local_rows(monkeypatch, rows):
    local_people(monkeypatch, rows)

    with pytest.raises(SubmitterError):
        resolve_private("ana@example.com")


def test_submitter_resolution_wraps_local_query_failure(monkeypatch):
    monkeypatch.setattr(
        feedback_submitters.db,
        "query",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("private database detail")),
    )

    with pytest.raises(SubmitterError, match="employee list is unavailable") as error:
        resolve_timeclock(41)

    assert "private database detail" not in str(error.value)


def test_submitter_resolution_rejects_malformed_local_query_payload(monkeypatch):
    monkeypatch.setattr(
        feedback_submitters.db,
        "query",
        lambda *_args: [{"employee_id": 41, "name": "Ana", "work_email": "a@x"}],
    )

    with pytest.raises(SubmitterError, match="employee list is unavailable"):
        active_choices()
