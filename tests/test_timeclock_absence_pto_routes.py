from datetime import date
from types import SimpleNamespace

from fastapi.testclient import TestClient

from zira_dashboard import absence_pto
from zira_dashboard.app import app
from zira_dashboard.routes import timeclock_absence_pto as routes


PERSON = {
    "id": 3,
    "name": "Ana",
    "odoo_id": 44,
    "wage_type": "hourly",
    "spanish_speaker": False,
    "spanish_level": 0,
}
DAY = date(2026, 8, 20)


def _request(request_id=12, person_odoo_id=44, note="hello"):
    return SimpleNamespace(
        id=request_id,
        absence_day=DAY,
        person_odoo_id=person_odoo_id,
        person_name="Ana" if person_odoo_id == 44 else "Other Worker",
        leave_type_name="Paid Time Off",
        balance_at_submit=3.0,
        state="pending",
        employee_note=note,
        denial_reason=None,
        requested_at="2026-08-28 08:00",
    )


def _wire_identity(monkeypatch):
    monkeypatch.setattr(routes, "_verify_token", lambda token: 3)
    monkeypatch.setattr(routes, "_person_by_id", lambda person_id: PERSON)
    monkeypatch.setattr(routes, "_mint_token", lambda person_id: "fresh-token")


def test_bad_token_redirects_to_expired_timeclock(monkeypatch):
    monkeypatch.setattr(routes, "_verify_token", lambda token: None)

    response = TestClient(app).get(
        "/timeclock/time-off/past-absence/bad-token", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/timeclock?expired=1"


def test_valid_token_renders_only_authenticated_person_rows(monkeypatch):
    _wire_identity(monkeypatch)
    seen = []
    candidate = absence_pto.AbsenceCandidate(DAY, True, None, 4.0)
    monkeypatch.setattr(
        routes.absence_pto,
        "list_candidates",
        lambda odoo_id, today: seen.append((odoo_id, today)) or [candidate],
    )
    monkeypatch.setattr(
        routes.absence_pto,
        "employee_requests",
        lambda odoo_id: [_request()] if odoo_id == 44 else [_request(13, 99)],
    )
    monkeypatch.setattr(routes, "plant_today", lambda: date(2026, 8, 28))

    response = TestClient(app).get(
        "/timeclock/time-off/past-absence/token", follow_redirects=False
    )

    assert response.status_code == 200
    assert seen == [(44, date(2026, 8, 28))]
    assert "Ana" in response.text
    assert "Other Worker" not in response.text
    assert "reason" not in response.text.lower()


def test_post_uses_only_token_identity_route_day_and_optional_note(monkeypatch):
    _wire_identity(monkeypatch)
    monkeypatch.setattr(routes, "plant_today", lambda: date(2026, 8, 28))
    captured = {}

    def reject(*args):
        captured["args"] = args
        raise absence_pto.SubmissionError("That absence was not found for this employee.")

    monkeypatch.setattr(routes.absence_pto, "submit", reject)
    monkeypatch.setattr(routes.absence_pto, "list_candidates", lambda *_: [])
    monkeypatch.setattr(routes.absence_pto, "employee_requests", lambda *_: [])

    response = TestClient(app).post(
        f"/timeclock/time-off/past-absence/token/{DAY}",
        data={
            "note": "  Please check  ",
            "person_id": "999",
            "person_odoo_id": "999",
            "holiday_status_id": "999",
            "balance": "999",
            "day": "1999-01-01",
        },
        follow_redirects=False,
    )

    assert response.status_code == 422
    assert captured["args"] == (
        3,
        44,
        "Ana",
        DAY,
        "Please check",
        date(2026, 8, 28),
    )


def test_duplicate_submission_returns_conflict(monkeypatch):
    _wire_identity(monkeypatch)
    monkeypatch.setattr(
        routes.absence_pto,
        "submit",
        lambda *_: (_ for _ in ()).throw(
            absence_pto.SubmissionError("A PTO request already exists for this absence.")
        ),
    )
    monkeypatch.setattr(routes.absence_pto, "list_candidates", lambda *_: [])
    monkeypatch.setattr(routes.absence_pto, "employee_requests", lambda *_: [])

    response = TestClient(app).post(
        f"/timeclock/time-off/past-absence/token/{DAY}", follow_redirects=False
    )

    assert response.status_code == 409
    assert "already exists" in response.text


def test_success_redirects_to_owned_request_detail_with_fresh_token(monkeypatch):
    _wire_identity(monkeypatch)
    monkeypatch.setattr(routes.absence_pto, "submit", lambda *_: _request())

    response = TestClient(app).post(
        f"/timeclock/time-off/past-absence/token/{DAY}", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/timeclock/time-off/past-absence/fresh-token/requests/12"
    )


def test_detail_rejects_another_persons_request_without_disclosure(monkeypatch):
    _wire_identity(monkeypatch)
    monkeypatch.setattr(
        routes.absence_pto.absence_pto_store,
        "get_request",
        lambda request_id: _request(request_id, person_odoo_id=99),
    )

    response = TestClient(app).get(
        "/timeclock/time-off/past-absence/token/requests/13",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/timeclock/time-off/past-absence/fresh-token"
    )
    assert "Other Worker" not in response.text


def test_detail_escapes_employee_note(monkeypatch):
    _wire_identity(monkeypatch)
    monkeypatch.setattr(
        routes.absence_pto.absence_pto_store,
        "get_request",
        lambda request_id: _request(note='<script>alert("x")</script>'),
    )

    response = TestClient(app).get(
        "/timeclock/time-off/past-absence/token/requests/12",
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert '<script>alert("x")</script>' not in response.text
    assert "&lt;script&gt;" in response.text


def test_disabled_candidate_has_accessible_explanation(monkeypatch):
    _wire_identity(monkeypatch)
    candidate = absence_pto.AbsenceCandidate(
        DAY, False, "You need 1 PTO day. You have 0.5.", 0.5
    )
    monkeypatch.setattr(routes.absence_pto, "list_candidates", lambda *_: [candidate])
    monkeypatch.setattr(routes.absence_pto, "employee_requests", lambda *_: [])

    response = TestClient(app).get("/timeclock/time-off/past-absence/token")

    assert response.status_code == 200
    assert 'aria-describedby="blocked-2026-08-20"' in response.text
    assert "You need 1 PTO day. You have 0.5." in response.text
    assert "disabled" in response.text
