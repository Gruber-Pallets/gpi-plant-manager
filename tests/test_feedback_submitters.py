import pytest

from zira_dashboard import feedback_submitters, odoo_client
from zira_dashboard.feedback_submitters import (
    SubmitterError,
    active_choices,
    resolve_private,
    resolve_timeclock,
)


def employee(employee_id, name, active, work_email):
    return {
        "id": employee_id,
        "name": name,
        "active": active,
        "work_email": work_email,
    }


def test_active_choices_returns_unique_active_employees_sorted_by_name(monkeypatch):
    monkeypatch.setattr(
        odoo_client,
        "fetch_employee_statuses",
        lambda: [
            employee(42, "zoe", True, " ZOE@Example.com "),
            employee(41, "Ana", True, "ana@example.com"),
            employee(43, "Inactive", False, "inactive@example.com"),
            employee(42, "Duplicate", True, "other@example.com"),
        ],
    )

    assert active_choices() == (feedback_submitters.SubmitterChoice(employee_id=41, name="Ana"),)


def test_timeclock_submitter_requires_one_exact_active_employee(monkeypatch):
    monkeypatch.setattr(
        odoo_client,
        "fetch_employee_statuses",
        lambda: [employee(41, "Ana", True, "ana@gruberpallets.com")],
    )

    assert resolve_timeclock(41) == feedback_submitters.ResolvedSubmitter(
        employee_id=41,
        name="Ana",
        email="ana@gruberpallets.com",
    )
    with pytest.raises(SubmitterError):
        resolve_timeclock(99)


@pytest.mark.parametrize("employee_id", [None, True, 0, -1, "41"])
def test_timeclock_submitter_rejects_absent_or_non_positive_ids(monkeypatch, employee_id):
    monkeypatch.setattr(odoo_client, "fetch_employee_statuses", lambda: [])

    with pytest.raises(SubmitterError):
        resolve_timeclock(employee_id)


@pytest.mark.parametrize(
    "rows",
    [
        [employee(41, "Ana", False, "ana@example.com")],
        [
            employee(41, "Ana", True, "ana@example.com"),
            employee(41, "Ana Duplicate", True, "ana2@example.com"),
        ],
    ],
)
def test_timeclock_submitter_rejects_inactive_or_duplicate_ids(monkeypatch, rows):
    monkeypatch.setattr(odoo_client, "fetch_employee_statuses", lambda: rows)

    with pytest.raises(SubmitterError):
        resolve_timeclock(41)


def test_private_submitter_matches_normalized_work_email(monkeypatch):
    monkeypatch.setattr(
        odoo_client,
        "fetch_employee_statuses",
        lambda: [employee(41, "Ana", True, " Ana@GruberPallets.com ")],
    )

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
        [employee(41, "Ana", False, "ana@example.com")],
        [
            employee(41, "Ana", True, "ana@example.com"),
            employee(42, "Other Ana", True, "ANA@example.com"),
        ],
    ],
)
def test_private_submitter_rejects_missing_inactive_or_duplicate_email(monkeypatch, rows):
    monkeypatch.setattr(odoo_client, "fetch_employee_statuses", lambda: rows)

    with pytest.raises(SubmitterError):
        resolve_private("ana@example.com")
