from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from zira_dashboard import attendance_corrections, db, exception_inbox
from zira_dashboard.routes import exceptions


ITEM_KEY = "production_unassigned_run:Dismantler 1:2026-08-28T16:55:00+00:00"
START = datetime(2026, 8, 28, 16, 55, tzinfo=UTC)
END = datetime(2026, 8, 28, 17, 10, tzinfo=UTC)
MANAGER_HEADERS = {
    "x-test-upn": "manager@gruberpallets.com",
    "x-test-name": "Floor Manager",
}


def _run_row(**changes):
    row = {
        "item_key": ITEM_KEY,
        "kind": "production_unassigned_run",
        "name": "Dismantler 1",
        "label": "12 units",
        "detail": "Dismantler 1 · 2026-08-28T16:55:00+00:00 to 2026-08-28T17:10:00+00:00 · 3 samples",
        "app_work_center_name": "Dismantler 1",
        "start_utc": START.isoformat(),
        "end_utc": END.isoformat(),
        "end_is_open": False,
        "units": 12.0,
        "sample_count": 3,
        "comparison_only": False,
        "raw_work_center_labels": [],
        "odoo_work_center_ids": [],
    }
    row.update(changes)
    return row


def _snapshot(*rows):
    return {
        "queue": list(rows or (_run_row(),)),
        "work_centers": ["Dismantler 1", "Repair 1"],
    }


def _source_row(*, end=END, write_minute=40):
    return {
        "odoo_attendance_id": 501,
        "employee_odoo_id": 44,
        "check_in_utc": datetime(2026, 8, 28, 16, 40, tzinfo=UTC),
        "check_out_utc": end,
        "odoo_work_center_id": 90,
        "odoo_department_id": 3,
        "odoo_write_date": datetime(2026, 8, 28, 16, write_minute, tzinfo=UTC),
        "odoo_work_center_name": "Odoo Old Center",
    }


def _live_preview(*, end=END, write_minute=40):
    plan = attendance_corrections.plan_correction(
        rows=[_source_row(end=end, write_minute=write_minute)],
        employee_odoo_id=44,
        start_utc=START,
        end_utc=end,
        odoo_work_center_id=91,
        odoo_department_id=3,
    )
    return attendance_corrections.CorrectionPreview(
        item_key=ITEM_KEY,
        employee_odoo_ids=(44,),
        target_work_center_name="Dismantler 1",
        target_odoo_work_center_id=91,
        target_odoo_department_id=3,
        start_utc=START,
        end_utc=end,
        plans=(plan,),
    )


def _payload(*, end=END, employee_ids=None, work_center="Dismantler 1", item_key=ITEM_KEY):
    return {
        "item_key": item_key,
        "employee_odoo_ids": employee_ids or [44],
        "work_center_name": work_center,
        "start_utc": START.isoformat(),
        "end_utc": end.isoformat() if end is not None else None,
    }


@pytest.fixture
def client(monkeypatch):
    test_app = FastAPI()

    @test_app.middleware("http")
    async def identity(request: Request, call_next):
        if request.headers.get("x-test-upn"):
            request.state.user_upn = request.headers["x-test-upn"]
        if request.headers.get("x-test-name"):
            request.state.user_name = request.headers["x-test-name"]
        return await call_next(request)

    test_app.include_router(exceptions.router)
    monkeypatch.setattr(exception_inbox, "build_snapshot", lambda: _snapshot())
    monkeypatch.setattr(
        db,
        "query",
        lambda sql, params=(): (
            [{"odoo_id": 44, "name": "Maria Worker"}]
            if "FROM people" in sql
            else []
        ),
    )
    return TestClient(test_app, follow_redirects=False)


def test_preview_rejects_signed_out_request_before_snapshot_or_odoo(client, monkeypatch):
    monkeypatch.setattr(
        exception_inbox,
        "build_snapshot",
        lambda: pytest.fail("signed-out request reached the inbox"),
    )
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **kwargs: pytest.fail(f"signed-out request reached Odoo: {kwargs}"),
    )

    response = client.post("/api/exceptions/attendance-correction/preview", json=_payload())

    assert response.status_code == 401
    assert response.json() == {
        "ok": False,
        "code": "manager_identity_required",
        "error": "Sign in again before correcting attendance.",
    }


def test_preview_requires_both_manager_upn_and_display_name(client):
    response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(),
        headers={"x-test-upn": MANAGER_HEADERS["x-test-upn"]},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "manager_identity_required"


@pytest.mark.parametrize(
    ("row", "item_key"),
    [
        (_run_row(), "production_unassigned_run:Dismantler 1:stale"),
        (_run_row(kind="attendance_missing_location"), ITEM_KEY),
    ],
)
def test_preview_rejects_stale_key_or_another_exception_kind(
    client, monkeypatch, row, item_key
):
    monkeypatch.setattr(exception_inbox, "build_snapshot", lambda: _snapshot(row))
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **kwargs: pytest.fail(f"invalid item reached Odoo: {kwargs}"),
    )

    response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(item_key=item_key),
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "stale_item"


def test_preview_and_apply_reject_comparison_only_shadow_item(client, monkeypatch):
    monkeypatch.setattr(
        exception_inbox,
        "build_snapshot",
        lambda: _snapshot(_run_row(comparison_only=True)),
    )
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **kwargs: pytest.fail(f"shadow item reached Odoo: {kwargs}"),
    )

    preview_response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(),
        headers=MANAGER_HEADERS,
    )
    apply_response = client.post(
        "/api/exceptions/attendance-correction/apply",
        json={"preview_token": exceptions._preview_token(_live_preview())},
        headers=MANAGER_HEADERS,
    )

    assert preview_response.status_code == apply_response.status_code == 409
    assert preview_response.json()["code"] == "stale_item"
    assert apply_response.json()["code"] == "stale_item"


def test_preview_rejects_target_not_in_current_app_work_centers(client, monkeypatch):
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **kwargs: pytest.fail(f"unknown app work center reached Odoo: {kwargs}"),
    )

    response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(work_center="Not In Plant Manager"),
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_work_center"


def test_preview_turns_unmapped_target_into_manager_readable_error(client, monkeypatch):
    def fail(**kwargs):
        raise ValueError("target work center has no saved Odoo mapping")

    monkeypatch.setattr(attendance_corrections, "correction_preview", fail)

    response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(),
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "code": "preview_unavailable",
        "error": "This work center is not ready for Odoo corrections. Check its Odoo mapping, then try again.",
    }


def test_preview_rejects_invalid_time_range_before_odoo(client, monkeypatch):
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **kwargs: pytest.fail(f"invalid time reached Odoo: {kwargs}"),
    )

    response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json={**_payload(), "end_utc": START.isoformat()},
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_time_range"


def test_preview_rejects_employee_not_active_in_local_people(client, monkeypatch):
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **kwargs: pytest.fail(f"invalid person reached Odoo: {kwargs}"),
    )

    response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(employee_ids=[99]),
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_employee"


def test_preview_returns_bounded_json_when_fresh_inbox_is_unavailable(client, monkeypatch):
    monkeypatch.setattr(
        exception_inbox,
        "build_snapshot",
        lambda: (_ for _ in ()).throw(RuntimeError("secret database trace")),
    )

    response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(),
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "code": "inbox_unavailable",
        "error": "The current inbox could not be checked. Nothing was changed. Try again.",
    }
    assert "secret" not in response.text


@pytest.mark.parametrize(
    ("end", "open_label"),
    [(END, "8/28/2026 12:10 PM CDT"), (None, "Still working")],
)
def test_preview_returns_display_safe_live_odoo_plan_in_plant_time(
    client, monkeypatch, end, open_label
):
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **kwargs: _live_preview(end=end),
    )

    response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(end=end),
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["preview_token"]
    assert body["preview"]["selected_people"] == [
        {"employee_odoo_id": 44, "name": "Maria Worker"}
    ]
    assert body["preview"]["start_utc"] == "2026-08-28T16:55:00+00:00"
    assert body["preview"]["start_label"] == "8/28/2026 11:55 AM CDT"
    assert body["preview"]["end_label"] == open_label
    assert body["preview"]["end_is_open"] is (end is None)
    person = body["preview"]["employees"][0]
    assert person["source_intervals"][0]["work_center_name"] == "Odoo Old Center"
    assert person["before_intervals"] == person["source_intervals"]
    assert person["after_intervals"]
    assert person["operation_summary"]["total"] >= 1
    assert "operations" not in body["preview"]


def test_apply_rejects_client_operations_and_tampered_token(client):
    response = client.post(
        "/api/exceptions/attendance-correction/apply",
        json={"preview_token": "not-signed", "operations": [{"kind": "delete"}]},
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"

    response = client.post(
        "/api/exceptions/attendance-correction/apply",
        json={"preview_token": "not-signed"},
        headers=MANAGER_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_preview"


def test_apply_rebuilds_changed_source_and_requires_second_confirmation(client, monkeypatch):
    previews = [_live_preview(write_minute=40), _live_preview(write_minute=41)]
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **kwargs: previews.pop(0),
    )
    monkeypatch.setattr(
        attendance_corrections,
        "create_job",
        lambda **kwargs: pytest.fail(f"changed source created job: {kwargs}"),
    )
    monkeypatch.setattr(
        attendance_corrections,
        "create_job_from_preview",
        lambda **kwargs: pytest.fail(f"changed source persisted job: {kwargs}"),
        raising=False,
    )
    preview_response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(),
        headers=MANAGER_HEADERS,
    )

    response = client.post(
        "/api/exceptions/attendance-correction/apply",
        json={"preview_token": preview_response.json()["preview_token"]},
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "source_changed"
    assert body["preview_token"] != preview_response.json()["preview_token"]
    assert body["preview"]["selected_people"][0]["name"] == "Maria Worker"


def test_duplicate_apply_returns_same_active_job_and_passes_authenticated_actor(
    client, monkeypatch
):
    preview = _live_preview()
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **kwargs: preview,
    )
    calls = []
    active = False

    def query(sql, params=()):
        if "FROM people" in sql:
            return [{"odoo_id": 44, "name": "Maria Worker"}]
        if "FROM attendance_correction_jobs" in sql and active:
            return [
                {
                    "id": 77,
                    "target_work_center_name": "Dismantler 1",
                    "start_utc": START,
                    "end_utc": END,
                    "employee_odoo_ids": [44],
                }
            ]
        return []

    def create_job_from_preview(**kwargs):
        nonlocal active
        calls.append(kwargs)
        active = True
        return 77

    monkeypatch.setattr(db, "query", query)
    monkeypatch.setattr(
        attendance_corrections,
        "create_job_from_preview",
        create_job_from_preview,
        raising=False,
    )
    monkeypatch.setattr(
        attendance_corrections,
        "create_job",
        lambda **kwargs: pytest.fail(f"apply re-read Odoo while creating: {kwargs}"),
    )
    preview_response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(),
        headers=MANAGER_HEADERS,
    )
    token = preview_response.json()["preview_token"]

    first = client.post(
        "/api/exceptions/attendance-correction/apply",
        json={"preview_token": token},
        headers=MANAGER_HEADERS,
    )
    duplicate = client.post(
        "/api/exceptions/attendance-correction/apply",
        json={"preview_token": token},
        headers=MANAGER_HEADERS,
    )

    assert first.status_code == duplicate.status_code == 202
    assert first.json()["job_id"] == duplicate.json()["job_id"] == 77
    assert len(calls) == 1
    assert calls[0]["actor_email"] == "manager@gruberpallets.com"
    assert calls[0]["actor_name"] == "Floor Manager"
    assert calls[0]["preview"] is preview
    assert "operations" not in calls[0]


def test_duplicate_apply_reuses_matching_job_before_worker_changes_rebuild(
    client, monkeypatch
):
    preview = _live_preview()
    preview_reads = 0

    def correction_preview(**_kwargs):
        nonlocal preview_reads
        preview_reads += 1
        if preview_reads > 1:
            pytest.fail("matching duplicate re-read Odoo after worker changed it")
        return preview

    def query(sql, params=()):
        if "FROM people" in sql:
            return [{"odoo_id": 44, "name": "Maria Worker"}]
        if "FROM attendance_correction_jobs" in sql:
            return [
                {
                    "id": 83,
                    "target_work_center_name": "Dismantler 1",
                    "start_utc": START,
                    "end_utc": END,
                    "employee_odoo_ids": [44],
                }
            ]
        return []

    monkeypatch.setattr(attendance_corrections, "correction_preview", correction_preview)
    monkeypatch.setattr(db, "query", query)
    monkeypatch.setattr(
        attendance_corrections,
        "create_job_from_preview",
        lambda **kwargs: pytest.fail(f"duplicate created another job: {kwargs}"),
    )
    preview_response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(),
        headers=MANAGER_HEADERS,
    )

    duplicate = client.post(
        "/api/exceptions/attendance-correction/apply",
        json={"preview_token": preview_response.json()["preview_token"]},
        headers=MANAGER_HEADERS,
    )

    assert duplicate.status_code == 202
    assert duplicate.json()["job_id"] == 83
    assert preview_reads == 1


def test_apply_persists_exact_rebuilt_preview_without_a_third_odoo_read(
    client, monkeypatch
):
    confirmed = _live_preview(write_minute=40)
    unconfirmed_later = _live_preview(write_minute=41)
    live_reads = [confirmed, confirmed, unconfirmed_later]
    persisted = []
    active = False

    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **_kwargs: live_reads.pop(0),
    )

    def query(sql, params=()):
        if "FROM people" in sql:
            return [{"odoo_id": 44, "name": "Maria Worker"}]
        if "FROM attendance_correction_jobs" in sql and active:
            return [
                {
                    "id": 81,
                    "target_work_center_name": "Dismantler 1",
                    "start_utc": START,
                    "end_utc": END,
                    "employee_odoo_ids": [44],
                }
            ]
        return []

    def persist(*, preview, actor_email, actor_name):
        nonlocal active
        persisted.append((preview, actor_email, actor_name))
        active = True
        return 81

    monkeypatch.setattr(db, "query", query)
    monkeypatch.setattr(
        attendance_corrections,
        "create_job_from_preview",
        persist,
        raising=False,
    )
    monkeypatch.setattr(
        attendance_corrections,
        "create_job",
        lambda **kwargs: pytest.fail(f"apply used the two-read create path: {kwargs}"),
    )
    preview_response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(),
        headers=MANAGER_HEADERS,
    )

    response = client.post(
        "/api/exceptions/attendance-correction/apply",
        json={"preview_token": preview_response.json()["preview_token"]},
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 202
    assert persisted == [
        (confirmed, "manager@gruberpallets.com", "Floor Manager")
    ]
    assert live_reads == [unconfirmed_later]


def test_preview_and_apply_run_sync_snapshot_odoo_and_db_work_off_event_loop(
    client, monkeypatch
):
    preview = _live_preview()
    calls = []
    active = False
    real_to_thread = asyncio.to_thread

    async def tracked_to_thread(function, /, *args, **kwargs):
        calls.append(function)
        return await real_to_thread(function, *args, **kwargs)

    def query(sql, params=()):
        if "FROM people" in sql:
            return [{"odoo_id": 44, "name": "Maria Worker"}]
        if "FROM attendance_correction_jobs" in sql and active:
            return [
                {
                    "id": 82,
                    "target_work_center_name": "Dismantler 1",
                    "start_utc": START,
                    "end_utc": END,
                    "employee_odoo_ids": [44],
                }
            ]
        return []

    def persist(**_kwargs):
        nonlocal active
        active = True
        return 82

    monkeypatch.setattr(exceptions.asyncio, "to_thread", tracked_to_thread)
    monkeypatch.setattr(db, "query", query)
    monkeypatch.setattr(
        attendance_corrections, "correction_preview", lambda **_kwargs: preview
    )
    monkeypatch.setattr(
        attendance_corrections,
        "create_job_from_preview",
        persist,
        raising=False,
    )
    preview_response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(),
        headers=MANAGER_HEADERS,
    )
    apply_response = client.post(
        "/api/exceptions/attendance-correction/apply",
        json={"preview_token": preview_response.json()["preview_token"]},
        headers=MANAGER_HEADERS,
    )

    assert preview_response.status_code == 200
    assert apply_response.status_code == 202
    assert calls.count(exceptions._current_correction_context) == 2
    assert calls.count(exceptions._build_live_preview) == 2
    assert calls.count(exceptions._active_correction_job) == 2
    assert attendance_corrections.create_job_from_preview in calls


def test_apply_does_not_reuse_active_item_job_for_different_request(
    client, monkeypatch
):
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **kwargs: _live_preview(),
    )

    def query(sql, params=()):
        if "FROM people" in sql:
            return [{"odoo_id": 44, "name": "Maria Worker"}]
        if "FROM attendance_correction_jobs" in sql:
            return [
                {
                    "id": 78,
                    "target_work_center_name": "Repair 1",
                    "start_utc": START,
                    "end_utc": END,
                    "employee_odoo_ids": [44],
                }
            ]
        return []

    monkeypatch.setattr(db, "query", query)
    monkeypatch.setattr(
        attendance_corrections,
        "create_job_from_preview",
        lambda **kwargs: pytest.fail(f"different active request reused: {kwargs}"),
        raising=False,
    )
    preview_response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(),
        headers=MANAGER_HEADERS,
    )

    response = client.post(
        "/api/exceptions/attendance-correction/apply",
        json={"preview_token": preview_response.json()["preview_token"]},
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 409
    assert response.json() == {
        "ok": False,
        "code": "correction_in_progress",
        "error": "Another correction for this inbox item is already in progress. Check its status before changing the request.",
    }


def test_apply_checks_concurrent_dedupe_winner_matches_request(client, monkeypatch):
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **kwargs: _live_preview(),
    )
    active_reads = 0

    def query(sql, params=()):
        nonlocal active_reads
        if "FROM people" in sql:
            return [{"odoo_id": 44, "name": "Maria Worker"}]
        if "FROM attendance_correction_jobs" in sql:
            active_reads += 1
            if active_reads == 1:
                return []
            return [
                {
                    "id": 79,
                    "target_work_center_name": "Repair 1",
                    "start_utc": START,
                    "end_utc": END,
                    "employee_odoo_ids": [44],
                }
            ]
        return []

    monkeypatch.setattr(db, "query", query)
    monkeypatch.setattr(
        attendance_corrections,
        "create_job_from_preview",
        lambda **kwargs: 79,
        raising=False,
    )
    preview_response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(),
        headers=MANAGER_HEADERS,
    )

    response = client.post(
        "/api/exceptions/attendance-correction/apply",
        json={"preview_token": preview_response.json()["preview_token"]},
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "correction_in_progress"
    assert active_reads == 2


def test_job_status_is_read_only_bounded_and_requires_manager_identity(client, monkeypatch):
    monkeypatch.setattr(
        attendance_corrections,
        "process_job",
        lambda *_args, **_kwargs: pytest.fail("GET status advanced the durable worker"),
    )
    monkeypatch.setattr(
        db,
        "query",
        lambda sql, params=(): [
            {
                "id": 77,
                "status": "failed",
                "attempt_count": 3,
                "completed_operations": [
                    {"operation_key": "safe", "kind": "update"},
                    {"stage": "mirror_refreshed"},
                ],
                "last_error": "Odoo could not be reached. " + "x" * 1000,
                "updated_at": datetime(2026, 8, 28, 17, 12, tzinfo=UTC),
                "completed_at": None,
            }
        ],
    )

    signed_out = client.get("/api/exceptions/attendance-correction/77")
    response = client.get(
        "/api/exceptions/attendance-correction/77", headers=MANAGER_HEADERS
    )

    assert signed_out.status_code == 401
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["retryable"] is True
    assert body["completed_operation_count"] == 1
    assert len(body["error"]) <= 300
    assert "source_snapshot" not in body
    assert "operations" not in body


def test_job_status_returns_bounded_json_when_durable_state_is_unavailable(
    client, monkeypatch
):
    monkeypatch.setattr(
        db,
        "query",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("secret durable trace")
        ),
    )

    response = client.get(
        "/api/exceptions/attendance-correction/77", headers=MANAGER_HEADERS
    )

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "code": "status_unavailable",
        "error": "Correction status is not available right now. Checking again is safe.",
    }
    assert "secret" not in response.text
