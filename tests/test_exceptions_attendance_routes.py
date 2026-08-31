from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

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


def _many_source_preview(*, employee_count=1, intervals_per_employee=201):
    employee_ids = (44,) if employee_count == 1 else tuple(range(1, employee_count + 1))
    plans = []
    for employee_id in employee_ids:
        base = START - timedelta(minutes=intervals_per_employee + 1)
        rows = []
        for index in range(intervals_per_employee):
            start = base + timedelta(minutes=index)
            rows.append(
                {
                    "odoo_attendance_id": employee_id * 10_000 + index + 1,
                    "employee_odoo_id": employee_id,
                    "check_in_utc": start,
                    "check_out_utc": start + timedelta(minutes=1),
                    "odoo_work_center_id": 90,
                    "odoo_department_id": 3,
                    "odoo_write_date": start,
                }
            )
        plans.append(
            attendance_corrections.plan_correction(
                rows=rows,
                employee_odoo_id=employee_id,
                start_utc=START,
                end_utc=END,
                odoo_work_center_id=91,
                odoo_department_id=3,
            )
        )
    return attendance_corrections.CorrectionPreview(
        item_key=ITEM_KEY,
        employee_odoo_ids=employee_ids,
        target_work_center_name="Dismantler 1",
        target_odoo_work_center_id=91,
        target_odoo_department_id=3,
        start_utc=START,
        end_utc=END,
        plans=tuple(plans),
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
            [{"odoo_id": 44, "name": "Maria Worker"}] if "FROM people" in sql else []
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
def test_preview_rejects_stale_key_or_another_exception_kind(client, monkeypatch, row, item_key):
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


@pytest.mark.parametrize("comparison_only", [True, 0, None])
def test_preview_and_apply_reject_nonliteral_false_comparison_items(
    client, monkeypatch, comparison_only
):
    monkeypatch.setattr(
        exception_inbox,
        "build_snapshot",
        lambda: _snapshot(_run_row(comparison_only=comparison_only)),
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


def test_preview_rejects_duration_over_task8_horizon_before_odoo(client, monkeypatch):
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **kwargs: pytest.fail(f"oversized duration reached Odoo: {kwargs}"),
    )

    response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json={
            **_payload(),
            "end_utc": (START + timedelta(days=501)).isoformat(),
        },
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_time_range"


def test_preview_rejects_more_than_task8_operation_limit(client, monkeypatch):
    preview = object.__new__(attendance_corrections.CorrectionPreview)
    for name, value in {
        "item_key": ITEM_KEY,
        "employee_odoo_ids": (44,),
        "target_work_center_name": "Dismantler 1",
        "target_odoo_work_center_id": 91,
        "target_odoo_department_id": 3,
        "start_utc": START,
        "end_utc": END,
        "plans": (SimpleNamespace(operations=(None,) * 1001),),
    }.items():
        object.__setattr__(preview, name, value)
    monkeypatch.setattr(attendance_corrections, "correction_preview", lambda **_kwargs: preview)

    response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(),
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "preview_too_large"


def test_preview_rejects_any_plan_that_cannot_be_shown_in_full(client, monkeypatch):
    preview = _many_source_preview()
    monkeypatch.setattr(attendance_corrections, "correction_preview", lambda **_kwargs: preview)

    response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(),
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "code": "preview_too_large",
        "error": "This correction has too many attendance rows to review safely. Choose fewer workers or a shorter time range.",
    }
    assert "preview_token" not in response.json()


def test_preview_rejects_a_generated_token_over_the_apply_limit(client, monkeypatch):
    preview = _many_source_preview(employee_count=25, intervals_per_employee=199)
    people = [
        {"odoo_id": employee_id, "name": f"Worker {employee_id}"}
        for employee_id in preview.employee_odoo_ids
    ]
    monkeypatch.setattr(db, "query", lambda sql, params=(): people if "FROM people" in sql else [])
    monkeypatch.setattr(attendance_corrections, "correction_preview", lambda **_kwargs: preview)

    response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(employee_ids=list(preview.employee_odoo_ids)),
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "preview_too_large"
    assert "preview_token" not in response.json()


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


def test_preview_rejects_more_than_task8_employee_limit_before_odoo(client, monkeypatch):
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **kwargs: pytest.fail(f"oversized employee list reached Odoo: {kwargs}"),
    )

    response = client.post(
        "/api/exceptions/attendance-correction/preview",
        json=_payload(employee_ids=list(range(1, 102))),
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
    assert body["preview"]["selected_people"] == [{"employee_odoo_id": 44, "name": "Maria Worker"}]
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

    response = client.post(
        "/api/exceptions/attendance-correction/apply",
        json={"preview_token": "x" * 20_001},
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


def test_apply_does_not_offer_a_truncated_refreshed_preview(client, monkeypatch):
    previews = [_live_preview(), _many_source_preview()]
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **_kwargs: previews.pop(0),
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
    assert response.json()["code"] == "source_changed"
    assert "preview" not in response.json()
    assert "preview_token" not in response.json()


@pytest.mark.parametrize(
    "failure",
    [
        "target work center has no saved Odoo mapping",
        "selected employee is no longer active in Odoo",
    ],
)
def test_apply_treats_mapping_or_source_gap_as_source_changed(client, monkeypatch, failure):
    preview = _live_preview()
    reads = 0

    def current_preview(**_kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            return preview
        raise ValueError(failure)

    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        current_preview,
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
        "code": "source_changed",
        "error": "Odoo changed after this preview. Preview the correction again.",
    }


@pytest.mark.parametrize("gap", ["worker", "work_center"])
def test_apply_treats_current_roster_or_work_center_gap_as_source_changed(client, monkeypatch, gap):
    if gap == "worker":
        monkeypatch.setattr(db, "query", lambda _sql, _params=(): [])
    else:
        current = _snapshot()
        current["work_centers"] = ["Repair 1"]
        monkeypatch.setattr(exception_inbox, "build_snapshot", lambda: current)
    token = exceptions._preview_token(_live_preview())

    response = client.post(
        "/api/exceptions/attendance-correction/apply",
        json={"preview_token": token},
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "source_changed"


def test_apply_rejects_item_that_became_shadow_comparison_before_odoo(client, monkeypatch):
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **kwargs: pytest.fail(f"shadow apply reached Odoo: {kwargs}"),
    )
    monkeypatch.setattr(
        exception_inbox,
        "build_snapshot",
        lambda: _snapshot(_run_row(comparison_only=True)),
    )
    token = exceptions._preview_token(_live_preview())

    response = client.post(
        "/api/exceptions/attendance-correction/apply",
        json={"preview_token": token},
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "stale_item"


def test_apply_persists_the_exact_revalidated_preview_without_third_odoo_read(client, monkeypatch):
    preview = _live_preview()
    reads = []
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **kwargs: reads.append(kwargs) or preview,
    )
    persisted = []
    monkeypatch.setattr(
        attendance_corrections,
        "create_job_from_preview",
        lambda *, preview, actor_email, actor_name: (
            persisted.append((preview, actor_email, actor_name)) or 177
        ),
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

    assert response.status_code == 202
    assert len(reads) == 2
    assert persisted == [(preview, "manager@gruberpallets.com", "Floor Manager")]


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
    reusable_lookups = []

    def reusable_job(**kwargs):
        reusable_lookups.append(kwargs)
        return 77 if calls else None

    def create_job(**kwargs):
        calls.append(kwargs)
        return 77

    monkeypatch.setattr(
        attendance_corrections,
        "find_reusable_job_for_binding",
        reusable_job,
        raising=False,
    )
    monkeypatch.setattr(attendance_corrections, "create_job_from_preview", create_job)
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
    assert len(reusable_lookups) == 2
    assert reusable_lookups[1]["binding"] == exceptions._preview_binding(preview)
    assert reusable_lookups[1]["item_key"] == ITEM_KEY
    assert calls[0]["actor_email"] == "manager@gruberpallets.com"
    assert calls[0]["actor_name"] == "Floor Manager"
    assert calls[0]["preview"] is preview
    assert "operations" not in calls[0]


def test_duplicate_apply_authenticates_durable_binding_before_reusing_job(client, monkeypatch):
    preview = _live_preview()
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **_kwargs: pytest.fail("duplicate apply rebuilt Odoo"),
    )
    seen = []

    def reusable_job(**kwargs):
        seen.append(kwargs)
        return 83

    monkeypatch.setattr(
        attendance_corrections,
        "find_reusable_job_for_binding",
        reusable_job,
        raising=False,
    )
    monkeypatch.setattr(
        attendance_corrections,
        "create_job_from_preview",
        lambda **kwargs: pytest.fail(f"duplicate apply created a job: {kwargs}"),
    )

    response = client.post(
        "/api/exceptions/attendance-correction/apply",
        json={"preview_token": exceptions._preview_token(preview)},
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 202
    assert response.json()["job_id"] == 83
    assert seen == [{"item_key": ITEM_KEY, "binding": exceptions._preview_binding(preview)}]


def test_apply_does_not_reuse_active_item_job_for_different_request(client, monkeypatch):
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **kwargs: _live_preview(),
    )

    monkeypatch.setattr(
        attendance_corrections,
        "create_job_from_preview",
        lambda **_kwargs: (_ for _ in ()).throw(
            attendance_corrections.CorrectionRequestConflict(78)
        ),
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
    calls = []

    def lose_race(**kwargs):
        calls.append(kwargs)
        raise attendance_corrections.CorrectionRequestConflict(79)

    monkeypatch.setattr(attendance_corrections, "create_job_from_preview", lose_race)
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
    assert len(calls) == 1
    assert calls[0]["preview"].target_odoo_work_center_id == 91


def test_apply_reports_concurrent_mapping_or_source_gap_as_source_changed(client, monkeypatch):
    preview = _live_preview()
    monkeypatch.setattr(
        attendance_corrections,
        "correction_preview",
        lambda **_kwargs: preview,
    )
    monkeypatch.setattr(
        attendance_corrections,
        "create_job_from_preview",
        lambda **_kwargs: (_ for _ in ()).throw(
            attendance_corrections.CorrectionRequestConflict(80, source_changed=True)
        ),
    )
    token = exceptions._preview_token(preview)

    response = client.post(
        "/api/exceptions/attendance-correction/apply",
        json={"preview_token": token},
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "source_changed"


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
    response = client.get("/api/exceptions/attendance-correction/77", headers=MANAGER_HEADERS)

    assert signed_out.status_code == 401
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["retryable"] is True
    assert body["completed_operation_count"] == 1
    assert len(body["error"]) <= 300
    assert "source_snapshot" not in body
    assert "operations" not in body


def test_job_status_returns_bounded_json_when_durable_state_is_unavailable(client, monkeypatch):
    monkeypatch.setattr(
        db,
        "query",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("secret durable trace")),
    )

    response = client.get("/api/exceptions/attendance-correction/77", headers=MANAGER_HEADERS)

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "code": "status_unavailable",
        "error": "Correction status is not available right now. Checking again is safe.",
    }
    assert "secret" not in response.text


def test_correction_request_body_is_rejected_before_unbounded_json_buffering(client, monkeypatch):
    monkeypatch.setattr(
        exception_inbox,
        "build_snapshot",
        lambda: pytest.fail("oversized body reached synchronous work"),
    )

    response = client.post(
        "/api/exceptions/attendance-correction/preview",
        content=b"{" + b'"padding":"' + (b"x" * 70_000) + b'"}',
        headers={**MANAGER_HEADERS, "content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"


def test_correction_request_stream_failure_returns_bounded_invalid_json():
    class BrokenRequest:
        headers = {}

        async def stream(self):
            raise RuntimeError("private transport detail")
            yield b""  # pragma: no cover - makes this an async generator

    response = asyncio.run(exceptions._correction_json(BrokenRequest()))

    assert isinstance(response, exceptions.JSONResponse)
    assert response.status_code == 400
    assert b"private" not in response.body


def test_preview_and_apply_move_snapshot_database_and_odoo_work_to_threads(client, monkeypatch):
    preview = _live_preview()
    monkeypatch.setattr(attendance_corrections, "correction_preview", lambda **_kwargs: preview)
    monkeypatch.setattr(
        attendance_corrections,
        "create_job_from_preview",
        lambda **_kwargs: 188,
        raising=False,
    )
    calls = []

    async def to_thread(function, *args, **kwargs):
        calls.append(function.__name__)
        return function(*args, **kwargs)

    monkeypatch.setattr(exceptions.asyncio, "to_thread", to_thread)

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
    assert calls == ["_attendance_preview_sync", "_attendance_apply_sync"]


def test_preview_tokens_expire_and_fail_after_session_secret_rotation(monkeypatch):
    token = exceptions._preview_token(_live_preview())
    monkeypatch.setattr(exceptions, "_CORRECTION_PREVIEW_MAX_AGE", -1)
    expired = exceptions._load_preview_token(token)
    assert isinstance(expired, exceptions.JSONResponse)
    assert expired.status_code == 409

    monkeypatch.setattr(exceptions, "_CORRECTION_PREVIEW_MAX_AGE", 600)
    monkeypatch.setattr(exceptions.auth, "_session_secret", lambda: "rotated-secret")
    rotated = exceptions._load_preview_token(token)
    assert isinstance(rotated, exceptions.JSONResponse)
    assert rotated.status_code == 400
