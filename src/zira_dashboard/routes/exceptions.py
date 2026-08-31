"""Daily Exception Inbox."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, UTC
from typing import Any

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .. import (
    attendance_corrections,
    auth,
    breakdown_actions,
    exception_inbox,
    inbox_keys,
    inbox_log,
    plant_day,
    time_off_audit,
)
from ..deps import templates

router = APIRouter()
_log = logging.getLogger(__name__)


@router.get("/exceptions", response_class=HTMLResponse)
def exceptions_page(request: Request):
    # The nav Inbox-count bootstrap is rendered by _topnav.html (via
    # nav_inbox_summary()), so this route no longer needs to pass it.
    snapshot = exception_inbox.build_snapshot()
    try:
        correction_people = _active_correction_people()
    except Exception:  # noqa: BLE001 - keep the inbox readable during roster outages
        _log.exception("attendance correction people could not load for inbox")
        correction_people = []
    queue = _display_exception_queue(snapshot.get("queue") or [])
    return templates.TemplateResponse(
        request,
        "exceptions.html",
        {
            "snapshot": snapshot,
            "sections": snapshot["sections"],
            "queue": queue,
            "work_centers": snapshot.get("work_centers") or [],
            "people": snapshot.get("people") or [],
            "correction_people": correction_people,
            "can_manage_work_centers": auth.request_is_super_admin(request),
            "plant_timezone": str(plant_day.SITE_TZ),
        },
    )


@router.get("/api/exceptions")
def exceptions_json():
    return JSONResponse(jsonable_encoder(exception_inbox.build_snapshot()))


@router.get("/api/exceptions/summary")
def exceptions_summary_json():
    return JSONResponse(exception_inbox.build_summary())


_TIME_OFF_STATES = {
    "draft",
    "draft_edit",
    "draft_cancel",
    "confirm",
    "validate1",
    "validate",
    "refuse",
    "cancel",
}
_PENDING_TIME_OFF_STATES = {"draft", "draft_edit", "confirm", "validate1"}
_TERMINAL_TIME_OFF_STATES = {"refuse", "cancel"}

_UNDOABLE = {
    ("missing_wc", "assign"),
    ("missing_wc", "dismiss"),
    ("late", "absent"),
    ("late", "reason"),
    ("breakdown", "transfer"),
    ("breakdown", "dismiss"),
}
_UNDO_WINDOW = timedelta(minutes=10)


def _load_time_off_request(request_id: int) -> dict[str, Any] | None:
    from .. import db

    rows = db.query(
        "SELECT r.id, r.person_odoo_id, r.originating_kiosk_user, r.shape, "
        "r.holiday_status_id, r.date_from, r.date_to, r.hour_from, r.hour_to, "
        "r.note, r.state, r.odoo_leave_id, r.sync_error, r.local_record, "
        "COALESCE(p.name, '#' || r.person_odoo_id::text) AS person_name, "
        "COALESCE(lt.name, 'Time off') AS leave_type "
        "FROM time_off_requests r "
        "LEFT JOIN people p ON p.odoo_id = r.person_odoo_id "
        "LEFT JOIN leave_types_cache lt ON lt.holiday_status_id = r.holiday_status_id "
        "WHERE r.id = %s",
        (request_id,),
    )
    return rows[0] if rows else None


def _json_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


_CORRECTION_PREVIEW_SALT = "exception-inbox-attendance-correction-v1"
_CORRECTION_PREVIEW_MAX_AGE = 10 * 60
_CORRECTION_REQUEST_FIELDS = frozenset(
    ("item_key", "employee_odoo_ids", "work_center_name", "start_utc", "end_utc")
)
_CORRECTION_APPLY_FIELDS = frozenset(("preview_token",))
_CORRECTION_ITEM_KIND = "production_unassigned_run"
_CORRECTION_TEXT_LIMIT = 500
_CORRECTION_DISPLAY_INTERVAL_LIMIT = 200
_CORRECTION_PREVIEW_TOKEN_LIMIT = 20_000
_CORRECTION_ERROR_LIMIT = 300
_CORRECTION_REQUEST_BODY_LIMIT = 64 * 1024
_CORRECTION_MAX_DURATION = timedelta(days=500)


def _correction_error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "code": code, "error": message},
        status_code=status_code,
    )


def _correction_manager(request: Request) -> tuple[str, str] | JSONResponse:
    upn, name = _actor_from(request)
    if not isinstance(upn, str) or not upn.strip():
        return _correction_error(
            "manager_identity_required",
            "Sign in again before correcting attendance.",
            401,
        )
    if not isinstance(name, str) or not name.strip():
        return _correction_error(
            "manager_identity_required",
            "Sign in again before correcting attendance.",
            401,
        )
    return upn.strip()[:320], name.strip()[:320]


async def _correction_json(request: Request) -> Mapping[str, object] | JSONResponse:
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            if int(raw_length) > _CORRECTION_REQUEST_BODY_LIMIT:
                return _correction_error(
                    "request_too_large", "The correction request is too large.", 413
                )
        except ValueError:
            return _correction_error("invalid_request", "Send a valid JSON request.", 400)
    try:
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > _CORRECTION_REQUEST_BODY_LIMIT:
                return _correction_error(
                    "request_too_large", "The correction request is too large.", 413
                )
            chunks.append(chunk)
        payload = json.loads(b"".join(chunks))
    except Exception:  # noqa: BLE001 - transport and JSON details stay server-side
        return _correction_error("invalid_request", "Send a valid JSON request.", 400)
    if not isinstance(payload, Mapping):
        return _correction_error("invalid_request", "Send a JSON object.", 400)
    return payload


def _correction_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or len(value) > 50:
        raise ValueError(f"{field_name} must be an ISO time with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO time with a timezone") from error
    if parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be an ISO time with a timezone")
    return parsed.astimezone(UTC)


def _correction_request_values(
    payload: Mapping[str, object],
) -> dict[str, object] | JSONResponse:
    if set(payload) != _CORRECTION_REQUEST_FIELDS:
        return _correction_error(
            "invalid_request",
            "The correction request has missing or unexpected fields.",
            400,
        )
    item_key = payload.get("item_key")
    target = payload.get("work_center_name")
    employee_values = payload.get("employee_odoo_ids")
    if (
        not isinstance(item_key, str)
        or not item_key.strip()
        or len(item_key.strip()) > _CORRECTION_TEXT_LIMIT
    ):
        return _correction_error("invalid_request", "Choose a current inbox item.", 400)
    if not isinstance(target, str) or not target.strip() or len(target.strip()) > 200:
        return _correction_error("invalid_work_center", "Choose a work center.", 422)
    if (
        isinstance(employee_values, (str, bytes))
        or not isinstance(employee_values, Sequence)
        or not employee_values
        or len(employee_values) > 100
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in employee_values
        )
    ):
        return _correction_error(
            "invalid_employee",
            "Choose at least one active worker.",
            422,
        )
    employee_ids = sorted(set(employee_values))
    try:
        start = _correction_datetime(payload.get("start_utc"), "start_utc")
        raw_end = payload.get("end_utc")
        end = None if raw_end is None else _correction_datetime(raw_end, "end_utc")
    except ValueError:
        return _correction_error(
            "invalid_time_range",
            "Enter a valid start and optional end time.",
            422,
        )
    if end is not None and end <= start:
        return _correction_error(
            "invalid_time_range",
            "The end time must be later than the start time.",
            422,
        )
    horizon_end = end or datetime.now(UTC)
    if horizon_end > start and horizon_end - start > _CORRECTION_MAX_DURATION:
        return _correction_error(
            "invalid_time_range",
            "Choose a time range of 500 days or less.",
            422,
        )
    return {
        "item_key": item_key.strip(),
        "employee_odoo_ids": employee_ids,
        "work_center_name": target.strip(),
        "start_utc": start,
        "end_utc": end,
    }


def _active_correction_people() -> list[dict[str, object]]:
    from .. import db

    rows = db.query(
        "SELECT odoo_id, name FROM people "
        "WHERE active = TRUE AND odoo_id IS NOT NULL AND odoo_id > 0 "
        "ORDER BY lower(name), odoo_id"
    )
    people = []
    seen: set[int] = set()
    for row in rows:
        value = row.get("odoo_id")
        name = row.get("name")
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            or value in seen
            or not isinstance(name, str)
            or not name.strip()
        ):
            continue
        seen.add(value)
        people.append({"employee_odoo_id": value, "name": name.strip()[:200]})
    return people[:1000]


def _current_correction_context(
    values: Mapping[str, object],
) -> tuple[dict[str, object], dict[int, str]] | JSONResponse:
    try:
        snapshot = exception_inbox.build_snapshot()
    except Exception:  # noqa: BLE001 - source details stay in server logs
        _log.exception("attendance correction current inbox lookup failed")
        return _correction_error(
            "inbox_unavailable",
            "The current inbox could not be checked. Nothing was changed. Try again.",
            503,
        )
    item_key = values["item_key"]
    matches = [
        row
        for row in snapshot.get("queue") or []
        if isinstance(row, Mapping)
        and row.get("item_key") == item_key
        and row.get("kind") == _CORRECTION_ITEM_KIND
        and row.get("comparison_only") is False
    ]
    if len(matches) != 1:
        return _correction_error(
            "stale_item",
            "This inbox item changed or is no longer open. Refresh the inbox and try again.",
            409,
        )
    work_centers = {
        item.strip()
        for item in snapshot.get("work_centers") or []
        if isinstance(item, str) and item.strip()
    }
    if values["work_center_name"] not in work_centers:
        return _correction_error(
            "invalid_work_center",
            "Choose a current Plant Manager work center.",
            422,
        )
    try:
        people = _active_correction_people()
    except Exception:  # noqa: BLE001 - local roster availability is manager-readable
        _log.exception("attendance correction local roster lookup failed")
        return _correction_error(
            "people_unavailable",
            "Worker choices are not available right now. Try again.",
            503,
        )
    names = {int(person["employee_odoo_id"]): str(person["name"]) for person in people}
    if any(employee_id not in names for employee_id in values["employee_odoo_ids"]):
        return _correction_error(
            "invalid_employee",
            "Choose only active workers from the current list.",
            422,
        )
    return dict(matches[0]), names


def _utc_iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _local_time_label(value: datetime | None) -> str:
    if value is None:
        return "Still working"
    return value.astimezone(plant_day.SITE_TZ).strftime("%-m/%-d/%Y %-I:%M %p %Z")


def _display_exception_queue(rows: Sequence[object]) -> list[object]:
    displayed: list[object] = []
    attendance_kinds = {
        "production_unassigned_run",
        "attendance_unmapped_location",
    }
    for value in rows:
        if not isinstance(value, Mapping) or value.get("kind") not in attendance_kinds:
            displayed.append(value)
            continue
        row = dict(value)
        for field, label_field in (
            ("start_utc", "start_label"),
            ("end_utc", "end_label"),
        ):
            raw_time = row.get(field)
            if raw_time is None:
                row[label_field] = "Still working"
                continue
            try:
                row[label_field] = _local_time_label(_correction_datetime(raw_time, field))
            except ValueError:
                row[label_field] = "Time unavailable"
        workers = []
        raw_workers = row.get("affected_workers")
        if isinstance(raw_workers, Sequence) and not isinstance(raw_workers, (str, bytes)):
            for worker in raw_workers[:12]:
                if not isinstance(worker, Mapping):
                    continue
                employee_id = worker.get("employee_odoo_id")
                name = worker.get("employee_name")
                if (
                    isinstance(employee_id, int)
                    and not isinstance(employee_id, bool)
                    and employee_id > 0
                    and isinstance(name, str)
                    and name.strip()
                ):
                    workers.append(
                        {
                            "employee_odoo_id": employee_id,
                            "employee_name": name.strip()[:100],
                        }
                    )
        row["affected_workers"] = workers
        row["affected_workers_truncated"] = (
            isinstance(raw_workers, Sequence)
            and not isinstance(raw_workers, (str, bytes))
            and len(raw_workers) > 12
        )
        reason = row.get("reason")
        if isinstance(reason, str) and reason.strip():
            words = reason.strip()[:200].replace("_", " ").split()
            readable = " ".join("Odoo" if word.lower() == "odoo" else word for word in words)
            row["reason_label"] = readable[:1].upper() + readable[1:]
        displayed.append(row)
    return displayed


def _display_interval(
    value: Mapping[str, object], preview: attendance_corrections.CorrectionPreview
) -> dict[str, object]:
    start = value.get("check_in_utc")
    end = value.get("check_out_utc")
    if not isinstance(start, datetime) or (end is not None and not isinstance(end, datetime)):
        raise ValueError("preview interval has invalid times")
    work_center_name = value.get("odoo_work_center_name")
    if value.get("odoo_work_center_id") == preview.target_odoo_work_center_id:
        work_center_name = preview.target_work_center_name
    if not isinstance(work_center_name, str) or not work_center_name.strip():
        work_center_name = "No Odoo work center"
    attendance_id = value.get("odoo_attendance_id")
    return {
        "attendance_id": attendance_id if isinstance(attendance_id, int) else None,
        "start_utc": _utc_iso(start),
        "end_utc": _utc_iso(end),
        "start_label": _local_time_label(start),
        "end_label": _local_time_label(end),
        "end_is_open": end is None,
        "work_center_name": work_center_name.strip()[:200],
    }


def _operation_summary(plan: attendance_corrections.CorrectionPlan) -> dict[str, int]:
    counts = {"create": 0, "update": 0, "delete": 0}
    for operation in plan.operations:
        counts[operation.kind] += 1
    return {**counts, "total": sum(counts.values())}


def _preview_binding(
    preview: attendance_corrections.CorrectionPreview,
) -> dict[str, object]:
    return attendance_corrections.preview_job_binding(preview)


def _preview_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(auth._session_secret(), salt=_CORRECTION_PREVIEW_SALT)


def _preview_token(preview: attendance_corrections.CorrectionPreview) -> str:
    token = _preview_serializer().dumps(_preview_binding(preview))
    if len(token) > _CORRECTION_PREVIEW_TOKEN_LIMIT:
        raise ValueError("preview token is too large")
    return token


def _load_preview_token(value: object) -> Mapping[str, object] | JSONResponse:
    if not isinstance(value, str) or not value or len(value) > _CORRECTION_PREVIEW_TOKEN_LIMIT:
        return _correction_error(
            "invalid_preview", "Preview this correction again before applying it.", 400
        )
    try:
        payload = _preview_serializer().loads(value, max_age=_CORRECTION_PREVIEW_MAX_AGE)
    except SignatureExpired:
        return _correction_error(
            "preview_expired", "This preview expired. Preview the correction again.", 409
        )
    except BadSignature:
        return _correction_error(
            "invalid_preview", "Preview this correction again before applying it.", 400
        )
    if not isinstance(payload, Mapping) or set(payload) != {"version", "request", "plans"}:
        return _correction_error(
            "invalid_preview", "Preview this correction again before applying it.", 400
        )
    if payload.get("version") != 1 or not isinstance(payload.get("plans"), list):
        return _correction_error(
            "invalid_preview", "Preview this correction again before applying it.", 400
        )
    return payload


def _safe_preview(
    preview: attendance_corrections.CorrectionPreview, names: Mapping[int, str]
) -> dict[str, object]:
    selected_people = [
        {"employee_odoo_id": employee_id, "name": names[employee_id]}
        for employee_id in preview.employee_odoo_ids
    ]
    employees = []
    aggregate = {"create": 0, "update": 0, "delete": 0, "total": 0}
    for employee_id, plan in zip(preview.employee_odoo_ids, preview.plans, strict=True):
        source_values = list(plan.source_intervals)
        expected_values = list(plan.expected_intervals)
        source = [_display_interval(item, preview) for item in source_values]
        after = [_display_interval(item, preview) for item in expected_values]
        summary = _operation_summary(plan)
        for key in aggregate:
            aggregate[key] += summary[key]
        employees.append(
            {
                "employee_odoo_id": employee_id,
                "name": names[employee_id],
                "source_intervals": source,
                "before_intervals": list(source),
                "after_intervals": after,
                "operation_summary": summary,
                "intervals_truncated": False,
            }
        )
    return {
        "item_key": preview.item_key,
        "selected_people": selected_people,
        "target_work_center_name": preview.target_work_center_name,
        "start_utc": _utc_iso(preview.start_utc),
        "end_utc": _utc_iso(preview.end_utc),
        "start_label": _local_time_label(preview.start_utc),
        "end_label": _local_time_label(preview.end_utc),
        "end_is_open": preview.end_utc is None,
        "employees": employees,
        "operation_summary": aggregate,
    }


def _build_live_preview(
    values: Mapping[str, object],
) -> attendance_corrections.CorrectionPreview | JSONResponse:
    try:
        return attendance_corrections.correction_preview(
            item_key=values["item_key"],
            employee_odoo_ids=values["employee_odoo_ids"],
            target_work_center_name=values["work_center_name"],
            start_utc=values["start_utc"],
            end_utc=values["end_utc"],
        )
    except ValueError as error:
        text = str(error).lower()
        if "mapping" in text or "work center" in text or "department" in text:
            message = (
                "This work center is not ready for Odoo corrections. "
                "Check its Odoo mapping, then try again."
            )
        elif "employee" in text:
            message = "A selected worker is no longer active in Odoo. Refresh and try again."
        else:
            message = (
                "Odoo could not build this correction preview. Check the choices and try again."
            )
        return _correction_error("preview_unavailable", message, 422)
    except Exception:  # noqa: BLE001 - never expose Odoo faults or traces
        _log.exception("attendance correction preview failed")
        return _correction_error(
            "preview_unavailable",
            "Odoo could not build the preview right now. Nothing was changed.",
            503,
        )


def _bounded_live_preview(
    values: Mapping[str, object],
) -> attendance_corrections.CorrectionPreview | JSONResponse:
    preview = _build_live_preview(values)
    if isinstance(preview, JSONResponse):
        return preview
    try:
        attendance_corrections.validate_preview_for_job(preview)
    except (TypeError, ValueError):
        return _correction_error(
            "preview_too_large",
            "This correction is too large to preview safely. Choose fewer workers or a shorter time range.",
            422,
        )
    if any(
        len(plan.source_intervals) > _CORRECTION_DISPLAY_INTERVAL_LIMIT
        or len(plan.expected_intervals) > _CORRECTION_DISPLAY_INTERVAL_LIMIT
        for plan in preview.plans
    ):
        return _correction_error(
            "preview_too_large",
            "This correction has too many attendance rows to review safely. "
            "Choose fewer workers or a shorter time range.",
            422,
        )
    return preview


def _queued_correction_response(job_id: int) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "job_id": job_id,
            "status": "planned",
            "message": "Correction queued. Plant Manager is checking Odoo.",
        },
        status_code=202,
    )


def _correction_in_progress_response() -> JSONResponse:
    return _correction_error(
        "correction_in_progress",
        "Another correction for this inbox item is already in progress. "
        "Check its status before changing the request.",
        409,
    )


def _source_changed_without_refresh_response() -> JSONResponse:
    return _correction_error(
        "source_changed",
        "Odoo changed after this preview. Preview the correction again.",
        409,
    )


@router.post("/api/exceptions/attendance-correction/preview")
async def attendance_correction_preview(request: Request):
    manager = _correction_manager(request)
    if isinstance(manager, JSONResponse):
        return manager
    payload = await _correction_json(request)
    if isinstance(payload, JSONResponse):
        return payload
    values = _correction_request_values(payload)
    if isinstance(values, JSONResponse):
        return values
    return await asyncio.to_thread(_attendance_preview_sync, values)


def _attendance_preview_sync(values: Mapping[str, object]) -> JSONResponse:
    context = _current_correction_context(values)
    if isinstance(context, JSONResponse):
        return context
    _row, names = context
    preview = _bounded_live_preview(values)
    if isinstance(preview, JSONResponse):
        return preview
    try:
        token = _preview_token(preview)
    except ValueError:
        return _correction_error(
            "preview_too_large",
            "This correction is too large to preview safely. "
            "Choose fewer workers or a shorter time range.",
            422,
        )
    return JSONResponse(
        {
            "ok": True,
            "preview": _safe_preview(preview, names),
            "preview_token": token,
        }
    )


@router.post("/api/exceptions/attendance-correction/apply")
async def attendance_correction_apply(request: Request):
    manager = _correction_manager(request)
    if isinstance(manager, JSONResponse):
        return manager
    payload = await _correction_json(request)
    if isinstance(payload, JSONResponse):
        return payload
    if set(payload) != _CORRECTION_APPLY_FIELDS:
        return _correction_error(
            "invalid_request",
            "Apply accepts only the preview confirmation.",
            400,
        )
    signed = _load_preview_token(payload.get("preview_token"))
    if isinstance(signed, JSONResponse):
        return signed
    raw_request = signed.get("request")
    if not isinstance(raw_request, Mapping):
        return _correction_error(
            "invalid_preview", "Preview this correction again before applying it.", 400
        )
    values = _correction_request_values(raw_request)
    if isinstance(values, JSONResponse):
        return _correction_error(
            "invalid_preview", "Preview this correction again before applying it.", 400
        )
    return await asyncio.to_thread(_attendance_apply_sync, values, signed, manager)


def _attendance_apply_sync(
    values: Mapping[str, object],
    signed: Mapping[str, object],
    manager: tuple[str, str],
) -> JSONResponse:
    context = _current_correction_context(values)
    if isinstance(context, JSONResponse):
        if context.status_code == 422:
            return _source_changed_without_refresh_response()
        return context
    _row, names = context
    try:
        reusable_job_id = attendance_corrections.find_reusable_job_for_binding(
            item_key=values["item_key"],
            binding=signed,
        )
    except attendance_corrections.CorrectionRequestConflict as error:
        if error.source_changed:
            return _source_changed_without_refresh_response()
        return _correction_in_progress_response()
    except Exception:  # noqa: BLE001 - durable details stay server-side
        _log.exception("attendance correction durable binding lookup failed")
        return _correction_error(
            "apply_unavailable",
            "The correction queue could not be checked safely. Nothing was changed.",
            503,
        )
    if reusable_job_id is not None:
        return _queued_correction_response(reusable_job_id)
    preview = _bounded_live_preview(values)
    if isinstance(preview, JSONResponse):
        if preview.status_code == 422:
            return _source_changed_without_refresh_response()
        return preview
    fresh_binding = _preview_binding(preview)
    if signed != fresh_binding:
        try:
            refreshed_token = _preview_token(preview)
        except ValueError:
            return _source_changed_without_refresh_response()
        return JSONResponse(
            {
                "ok": False,
                "code": "source_changed",
                "error": (
                    "Odoo changed after this preview. Review the refreshed preview, "
                    "then confirm again."
                ),
                "preview": _safe_preview(preview, names),
                "preview_token": refreshed_token,
            },
            status_code=409,
        )
    upn, manager_name = manager
    try:
        job_id = attendance_corrections.create_job_from_preview(
            preview=preview,
            actor_email=upn,
            actor_name=manager_name,
        )
    except attendance_corrections.CorrectionRequestConflict as error:
        if error.source_changed:
            return _source_changed_without_refresh_response()
        return _correction_in_progress_response()
    except Exception:  # noqa: BLE001 - durable/Odoo details stay server-side
        _log.exception("attendance correction job creation failed")
        return _correction_error(
            "apply_unavailable",
            "The correction could not be queued. Nothing was changed. Preview and try again.",
            503,
        )
    return _queued_correction_response(job_id)


def _completed_operation_count(value: object) -> int:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return 0
    if not isinstance(value, list):
        return 0
    return sum(
        1
        for item in value[:1000]
        if isinstance(item, Mapping)
        and isinstance(item.get("operation_key"), str)
        and item.get("kind") in {"create", "update", "delete"}
    )


@router.get("/api/exceptions/attendance-correction/{job_id}")
def attendance_correction_status(job_id: int, request: Request):
    manager = _correction_manager(request)
    if isinstance(manager, JSONResponse):
        return manager
    if job_id <= 0:
        return _correction_error("job_not_found", "Correction job not found.", 404)
    from .. import db

    try:
        rows = db.query(
            "SELECT id, status, attempt_count, completed_operations, last_error, "
            "updated_at, completed_at FROM attendance_correction_jobs WHERE id = %s",
            (job_id,),
        )
    except Exception:  # noqa: BLE001 - never return durable storage details
        _log.exception("attendance correction status lookup failed")
        return _correction_error(
            "status_unavailable",
            "Correction status is not available right now. Checking again is safe.",
            503,
        )
    if not rows:
        return _correction_error("job_not_found", "Correction job not found.", 404)
    row = rows[0]
    status = row.get("status")
    if status not in {
        "planned",
        "applying",
        "verifying",
        "recalculating",
        "complete",
        "failed",
    }:
        return _correction_error(
            "status_unavailable", "Correction status is not available right now.", 503
        )
    failed = status == "failed"
    active = status in {"planned", "applying", "verifying", "recalculating"}
    error = (
        "Odoo could not finish this correction. It is safe to try again."
        if failed and row.get("last_error")
        else None
    )
    return JSONResponse(
        {
            "ok": True,
            "job_id": int(row["id"]),
            "status": status,
            "attempt_count": max(0, int(row.get("attempt_count") or 0)),
            "completed_operation_count": _completed_operation_count(
                row.get("completed_operations")
            ),
            "retryable": failed,
            "done": status in {"complete", "failed"},
            "poll_after_ms": 2000 if active else None,
            "error": error[:_CORRECTION_ERROR_LIMIT] if error else None,
            "updated_at": jsonable_encoder(row.get("updated_at")),
            "completed_at": jsonable_encoder(row.get("completed_at")),
        }
    )


# Odoo's hr.leave ValidationError when the employee's Working Schedule has
# no attendance on the requested day(s). Locale-dependent by nature — the
# API user's language must stay English for this (and _friendly_odoo_error)
# to match.
_WORK_SCHEDULE_CONFLICT_SNIPPET = "not supposed to work during that period"


def _fault_text(e: Exception) -> str:
    """The useful text of an Odoo/xmlrpc exception, whitespace-collapsed.
    xmlrpc Faults stringify as the noisy ``<Fault N: '...'>`` repr; the
    real message lives on ``.faultString``."""
    msg = getattr(e, "faultString", None) or str(e)
    return " ".join(str(msg).split())


def _is_work_schedule_conflict(e: Exception) -> bool:
    return _WORK_SCHEDULE_CONFLICT_SNIPPET in _fault_text(e)


def _friendly_odoo_error(e: Exception) -> str:
    """Turn an Odoo/xmlrpc exception into a clean, user-facing message.

    Collapse whitespace so it fits the inbox's one-line status. For Odoo's
    work-schedule rejection ("not supposed to work during that period"),
    prepend a hint on how to resolve it — that one is a Working Schedule
    data issue HR fixes in Odoo, not something the manager can force here.
    (The approve path normally records such absences locally instead; this
    message only surfaces when that fallback itself could not settle the
    Odoo copy.)
    """
    msg = _fault_text(e)
    if _WORK_SCHEDULE_CONFLICT_SNIPPET in msg:
        return (
            "Odoo won't approve this — the employee's Working Schedule in "
            "Odoo doesn't include the requested day(s). Ask HR to fix their "
            "Working Schedule, then try again. Odoo said: " + msg
        )
    return msg


def _actor_from(request: Request) -> tuple[str | None, str | None]:
    return (
        getattr(request.state, "user_upn", None),
        getattr(request.state, "user_name", None),
    )


def _iso_day(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _decision_time_label(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(plant_day.SITE_TZ).strftime("%-m/%-d %-I:%M %p")


def _group_archive_by_day(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group newest-first events into plant-local day buckets for the archive."""
    today = plant_day.today()
    yesterday = today - timedelta(days=1)
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for r in rows:
        resolved = r["resolved_at"]
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=UTC)
        local = resolved.astimezone(plant_day.SITE_TZ)
        day = local.date()
        if day == today:
            label = "Today"
        elif day == yesterday:
            label = "Yesterday"
        else:
            label = local.strftime("%A, %b %-d")
        if current is None or current["day"] != day.isoformat():
            current = {"day": day.isoformat(), "label": label, "events": []}
            groups.append(current)
        current["events"].append({
            "id": r["id"],
            "item_kind": r.get("item_kind"),
            "item_key": r.get("item_key"),
            "person_name": r.get("person_name"),
            "category_label": r.get("category_label"),
            "action": r.get("action"),
            "outcome": r.get("outcome"),
            "before_value": r.get("before_value"),
            "after_value": r.get("after_value"),
            "reason": r.get("reason"),
            "actor_name": r.get("actor_name"),
            "actor_upn": r.get("actor_upn"),
            "auto": r.get("actor_upn") is None,
            "time_label": local.strftime("%-I:%M %p"),
        })
    return groups


@router.get("/api/exceptions/archive")
def exceptions_archive_json(
    before: str | None = None,
    actor: str | None = None,
    include_auto: bool = False,
    limit: int = 200,
):
    before_dt = None
    if before:
        try:
            before_dt = datetime.fromisoformat(before)
        except ValueError:
            return _json_error("bad 'before' cursor", 400)
    limit = max(1, min(int(limit), 500))
    rows = inbox_log.archive(
        before=before_dt, actor_upn=actor, include_auto=include_auto, limit=limit
    )
    next_before = (
        rows[-1]["resolved_at"].isoformat() if len(rows) == limit and rows else None
    )
    return JSONResponse({
        "groups": _group_archive_by_day(rows),
        "next_before": next_before,
    })


def _hour_value(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _hour_label(value: Any) -> str:
    total_minutes = int(round(float(value) * 60))
    hour = (total_minutes // 60) % 24
    minute = total_minutes % 60
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d} {suffix}"


def _decision_date_label(row: dict[str, Any]) -> str:
    start = _iso_day(row.get("date_from")) or ""
    end = _iso_day(row.get("date_to")) or ""
    label = f"{start} to {end}" if end and end != start else start
    if row.get("hour_from") is not None and row.get("hour_to") is not None:
        label += f" - {_hour_label(row['hour_from'])} to {_hour_label(row['hour_to'])}"
    return label


def _decision_summary(
    row: dict[str, Any],
    *,
    action: str,
    result_state: str,
    reason: str | None,
    actor_upn: str | None,
    actor_name: str | None,
    source: str | None,
) -> dict[str, Any]:
    decided_at = plant_day.now()
    return {
        "action": action,
        "person_name": row.get("person_name"),
        "leave_type": row.get("leave_type"),
        "date_from": _iso_day(row.get("date_from")),
        "date_to": _iso_day(row.get("date_to")),
        "hour_from": _hour_value(row.get("hour_from")),
        "hour_to": _hour_value(row.get("hour_to")),
        "date_label": _decision_date_label(row),
        "reason": reason,
        "actor_name": actor_name,
        "actor_upn": actor_upn,
        "source": source,
        "result_state": result_state,
        "decided_at": decided_at.isoformat(),
        "decided_label": _decision_time_label(decided_at),
    }


def _refresh_time_off_surfaces() -> None:
    from .. import _http_cache
    from .staffing import _bust_after_mutation

    _bust_after_mutation()
    _http_cache.invalidate_all_cache()


def _sync_to_odoo_if_needed(row: dict[str, Any]) -> dict[str, Any] | JSONResponse:
    """Make sure a pending local draft/edit has an Odoo leave id before action."""
    if row.get("odoo_leave_id") is not None and row.get("state") != "draft_edit":
        return row

    from .. import time_off_sync

    time_off_sync.push_one(int(row["id"]))
    refreshed = _load_time_off_request(int(row["id"]))
    if refreshed is None:
        return _json_error("request was removed during sync", 409)
    if refreshed.get("odoo_leave_id") is None:
        return _json_error(refreshed.get("sync_error") or "request is not synced to Odoo yet", 409)
    return refreshed


def _set_time_off_state(old: dict[str, Any], state: str) -> None:
    from .. import db, time_off_sync

    # local_record = FALSE: a state set through a route matches what Odoo
    # holds (or is about to hold), so a previously locally-owned row hands
    # ownership back to the poller.
    db.execute(
        "UPDATE time_off_requests SET state = %s, synced_to_odoo = TRUE, "
        "sync_error = NULL, local_record = FALSE, "
        "last_pushed_at = now(), updated_at = now() "
        "WHERE id = %s",
        (state, old["id"]),
    )
    new = dict(old)
    new["state"] = state
    time_off_sync.cascade_on_state_change(old, new)
    _refresh_time_off_surfaces()


_LOCAL_RECORD_DECISION_REASON = (
    "Recorded in Plant Manager only — Odoo Working Schedule does not "
    "include the requested day(s)"
)
_LOCAL_RECORD_CHATTER = (
    "Approved and recorded in GPI Plant Manager. Odoo could not validate "
    "this request because the employee's Working Schedule does not include "
    "the requested day(s), so this Odoo copy was closed as refused. The "
    "Plant Manager record is authoritative for this absence."
)
_LOCAL_RECORD_WARNING = (
    "Odoo couldn't validate this (the Working Schedule doesn't include the "
    "day(s)); recorded here instead and the Odoo copy was closed with a note."
)


def _record_time_off_locally(old: dict[str, Any]) -> None:
    """Sibling of ``_set_time_off_state`` for the local-record fallback:
    approve the row locally and flag it ``local_record`` so the poller
    neither overwrites nor deletes it. ``sync_error`` is cleared — the
    kiosk detail page renders it as a red error, and the why lives in the
    decision audit, the inbox log, and the Odoo chatter note."""
    from .. import db, time_off_sync

    db.execute(
        "UPDATE time_off_requests SET state = 'validate', "
        "local_record = TRUE, synced_to_odoo = TRUE, sync_error = NULL, "
        "last_pushed_at = now(), updated_at = now() WHERE id = %s",
        (old["id"],),
    )
    new = dict(old)
    new["state"] = "validate"
    new["local_record"] = True
    time_off_sync.cascade_on_state_change(old, new)
    _refresh_time_off_surfaces()


def _approve_locally_despite_schedule_conflict(
    row: dict[str, Any],
    *,
    actor_upn: str | None,
    actor_name: str | None,
    source: str | None,
) -> JSONResponse | None:
    """Odoo won't validate a leave whose Working Schedule lacks the
    requested day(s) — record the absence locally instead of hard-failing.

    Order matters: (1) pre-suppress the would-be "denied" kiosk popup (the
    poller may observe the refuse before our local write lands), (2) refuse
    the Odoo copy — the only settled state Odoo allows here, (3) approve the
    local row as a poller-proof ``local_record``, (4) best-effort chatter
    note on the refused leave. Returns None when the Odoo refuse fails, so
    the caller falls back to the friendly 500 and nothing is half-recorded.
    """
    import logging

    from .. import employee_notifications, odoo_client

    log = logging.getLogger(__name__)
    leave_id = row.get("odoo_leave_id")
    try:
        employee_notifications.suppress_resolution(
            row["person_odoo_id"], row, kind="time_off_denied")
    except Exception:  # noqa: BLE001 — belt-and-braces guard, not load-bearing
        log.warning("denied-popup suppression failed for request %s",
                    row["id"], exc_info=True)
    if leave_id is not None:
        try:
            odoo_client.refuse_leave(int(leave_id))
        except Exception:  # noqa: BLE001 — abort: leave must not stay pending
            log.warning("local-record fallback aborted: Odoo refuse failed "
                        "for leave %s", leave_id, exc_info=True)
            try:
                # Don't leak the suppression row: a later genuine
                # Odoo-side denial of this still-pending request must
                # still be able to notify.
                employee_notifications.unsuppress_resolution(
                    row["id"], kind="time_off_denied")
            except Exception:  # noqa: BLE001
                log.warning("suppression cleanup failed for request %s",
                            row["id"], exc_info=True)
            return None
    _record_time_off_locally(row)
    if leave_id is not None:
        try:
            odoo_client.post_leave_message(int(leave_id), _LOCAL_RECORD_CHATTER)
        except Exception as e:  # noqa: BLE001 — record already settled
            log.warning("chatter post failed for leave %s (local record "
                        "still applied): %s", leave_id, e)
    time_off_audit.record_decision(
        request_id=row["id"],
        odoo_leave_id=leave_id,
        person_odoo_id=row.get("person_odoo_id"),
        person_name=row.get("person_name"),
        leave_type=row.get("leave_type"),
        date_from=row.get("date_from"),
        date_to=row.get("date_to"),
        hour_from=row.get("hour_from"),
        hour_to=row.get("hour_to"),
        action="approve",
        result_state="validate",
        reason=_LOCAL_RECORD_DECISION_REASON,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source=source,
    )
    inbox_log.log_event_safe(
        item_kind="time_off",
        item_key=inbox_keys.time_off(row["id"]),
        person_name=row.get("person_name"),
        category_label="Time off",
        action="approve",
        outcome="Approved (recorded locally)",
        after_value="validate",
        actor_upn=actor_upn,
        actor_name=actor_name,
        source=source,
        reversible=False,
    )
    return JSONResponse({
        "ok": True,
        "state": "validate",
        "approved": True,
        "recorded_locally": True,
        "warning": _LOCAL_RECORD_WARNING,
        "decision": _decision_summary(
            row,
            action="approve",
            result_state="validate",
            reason=_LOCAL_RECORD_DECISION_REASON,
            actor_upn=actor_upn,
            actor_name=actor_name,
            source=source,
        ),
    })


def _approve_time_off_sync(
    request_id: int,
    actor_upn: str | None = None,
    actor_name: str | None = None,
    source: str | None = None,
) -> JSONResponse:
    from .. import odoo_client

    row = _load_time_off_request(request_id)
    if row is None:
        return _json_error("request not found", 404)
    state = str(row.get("state") or "")
    if state == "validate":
        return JSONResponse({"ok": True, "state": state, "no_op": True})
    if state in _TERMINAL_TIME_OFF_STATES or state == "draft_cancel":
        return _json_error("request is already closed", 409)
    if state not in _PENDING_TIME_OFF_STATES:
        return _json_error(f"request cannot be approved from state {state}", 409)

    synced = _sync_to_odoo_if_needed(row)
    if isinstance(synced, JSONResponse):
        return synced
    try:
        final_state = odoo_client.approve_leave(int(synced["odoo_leave_id"])) or synced["state"]
    except Exception as e:
        if _is_work_schedule_conflict(e):
            fallback = _approve_locally_despite_schedule_conflict(
                synced, actor_upn=actor_upn, actor_name=actor_name,
                source=source)
            if fallback is not None:
                return fallback
        return _json_error(_friendly_odoo_error(e), 500)
    if final_state not in _TIME_OFF_STATES:
        return _json_error(f"unexpected Odoo state {final_state}", 500)
    _set_time_off_state(row, final_state)
    time_off_audit.record_decision(
        request_id=row["id"],
        odoo_leave_id=synced.get("odoo_leave_id"),
        person_odoo_id=row.get("person_odoo_id"),
        person_name=row.get("person_name"),
        leave_type=row.get("leave_type"),
        date_from=row.get("date_from"),
        date_to=row.get("date_to"),
        hour_from=row.get("hour_from"),
        hour_to=row.get("hour_to"),
        action="approve",
        result_state=final_state,
        reason=None,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source=source,
    )
    inbox_log.log_event_safe(
        item_kind="time_off",
        item_key=inbox_keys.time_off(row["id"]),
        person_name=row.get("person_name"),
        category_label="Time off",
        action="approve",
        outcome="Approved",
        after_value=final_state,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source=source,
        reversible=False,
    )
    return JSONResponse({
        "ok": True,
        "state": final_state,
        "approved": final_state == "validate",
        "decision": _decision_summary(
            row,
            action="approve",
            result_state=final_state,
            reason=None,
            actor_upn=actor_upn,
            actor_name=actor_name,
            source=source,
        ),
    })


@router.post("/api/exceptions/time-off/{request_id}/approve")
async def approve_time_off_request(request_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    source = (body or {}).get("source")
    actor_upn, actor_name = _actor_from(request)
    return await asyncio.to_thread(
        _approve_time_off_sync,
        request_id,
        actor_upn,
        actor_name,
        source,
    )


def _refuse_time_off_sync(
    request_id: int,
    reason: str,
    actor_upn: str | None = None,
    actor_name: str | None = None,
    source: str | None = None,
) -> JSONResponse:
    import logging

    from .. import odoo_client

    reason = (reason or "").strip()
    if not reason:
        return _json_error("a reason is required to deny", 400)

    row = _load_time_off_request(request_id)
    if row is None:
        return _json_error("request not found", 404)
    state = str(row.get("state") or "")
    if state in _TERMINAL_TIME_OFF_STATES:
        return JSONResponse({"ok": True, "state": state, "no_op": True})

    leave_id = row.get("odoo_leave_id")
    if leave_id is not None:
        # Locally-recorded approvals already hold a refused Odoo copy —
        # action_refuse on it raises. The deny settles locally; the reason
        # still lands on the Odoo chatter below.
        if not row.get("local_record"):
            try:
                odoo_client.refuse_leave(int(leave_id))
            except Exception as e:
                return _json_error(_friendly_odoo_error(e), 500)
        try:
            odoo_client.post_leave_message(int(leave_id), reason)
        except Exception as e:  # noqa: BLE001 -- denial already succeeded
            logging.getLogger(__name__).warning(
                "chatter post failed for leave %s (denial still applied): %s",
                leave_id,
                e,
            )
    _set_time_off_state(row, "refuse")
    time_off_audit.record_decision(
        request_id=row["id"],
        odoo_leave_id=leave_id,
        person_odoo_id=row.get("person_odoo_id"),
        person_name=row.get("person_name"),
        leave_type=row.get("leave_type"),
        date_from=row.get("date_from"),
        date_to=row.get("date_to"),
        hour_from=row.get("hour_from"),
        hour_to=row.get("hour_to"),
        action="deny",
        result_state="refuse",
        reason=reason,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source=source,
    )
    inbox_log.log_event_safe(
        item_kind="time_off",
        item_key=inbox_keys.time_off(row["id"]),
        person_name=row.get("person_name"),
        category_label="Time off",
        action="deny",
        outcome="Denied",
        reason=reason,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source=source,
        reversible=False,
    )
    return JSONResponse({
        "ok": True,
        "state": "refuse",
        "decision": _decision_summary(
            row,
            action="deny",
            result_state="refuse",
            reason=reason,
            actor_upn=actor_upn,
            actor_name=actor_name,
            source=source,
        ),
    })


@router.post("/api/exceptions/time-off/{request_id}/refuse")
async def refuse_time_off_request(request_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = (body or {}).get("reason", "")
    source = (body or {}).get("source")
    actor_upn, actor_name = _actor_from(request)
    return await asyncio.to_thread(
        _refuse_time_off_sync,
        request_id,
        reason,
        actor_upn,
        actor_name,
        source,
    )


def _event_detail(ev: dict[str, Any]) -> dict:
    """ev['detail'] is written as jsonb; normalize to a dict regardless of
    whether the driver returned it already-parsed or as a raw JSON string."""
    import json
    detail = ev.get("detail")
    if isinstance(detail, dict):
        return detail
    if isinstance(detail, str) and detail:
        try:
            return json.loads(detail)
        except (TypeError, ValueError):
            return {}
    return {}


class _UndoConflict(Exception):
    """A local reversal lost a state race and made no changes."""


def _reverse_event(ev: dict[str, Any]) -> None:
    """Reverse a resolved inbox action. Assumes (item_kind, action) is undoable."""
    from .. import absence_sync, late_report, machine_breakdown, missing_wc, odoo_client, wc_attributions

    kind, action, key = ev["item_kind"], ev["action"], ev["item_key"]
    if kind == "missing_wc":
        att_id = int(key.split(":")[1])
        if action == "assign":
            odoo_client.clear_attendance_wc(att_id)
        missing_wc.unresolve(att_id)
    elif kind == "late":
        _, emp_id, day = key.split(":", 2)
        if action == "absent":
            absence_sync.refuse_absence_leave(
                late_report.odoo_leave_id_for_absence(day, emp_id)
            )
            late_report.undo_absent(day, emp_id)
        elif action == "reason":
            late_report.undo_late_arrival(day, emp_id)
    elif kind == "breakdown":
        detail = _event_detail(ev)
        if action == "transfer":
            closed_id, new_id = detail.get("closed_id"), detail.get("new_id")
            if new_id is not None:
                odoo_client.undo_transfer(closed_id, new_id)
            attribution_id = detail.get("attribution_id")
            if attribution_id is not None:
                wc_attributions.reopen_breakdown(attribution_id)
        elif action == "dismiss":
            incident_id = detail.get("incident_id")
            if not machine_breakdown.undo_dismiss_incident(
                incident_id, detail.get("rows") or []
            ):
                raise _UndoConflict(
                    "breakdown changed after dismissal; undo was not applied"
                )


def _undo_sync(
    event_id: int,
    actor_upn: str | None = None,
    actor_name: str | None = None,
) -> JSONResponse:
    from .. import inbox_log

    ev = inbox_log.get_event(event_id)
    if ev is None:
        return _json_error("event not found", 404)
    if ev.get("undone_at") is not None:
        return _json_error("already undone", 409)
    if (ev["item_kind"], ev["action"]) not in _UNDOABLE:
        return _json_error("this action can't be undone", 400)
    resolved = ev["resolved_at"]
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=UTC)
    if plant_day.now() - resolved > _UNDO_WINDOW:
        return _json_error("undo window expired", 409)
    try:
        _reverse_event(ev)
    except _UndoConflict as e:
        return _json_error(str(e), 409)
    except Exception as e:  # noqa: BLE001 -- surface reversal failure to caller
        return _json_error(_friendly_odoo_error(e), 500)
    undo_id = inbox_log.log_event_safe(
        item_kind=ev["item_kind"],
        item_key=ev["item_key"],
        person_name=ev.get("person_name"),
        category_label=ev.get("category_label"),
        action="undo",
        outcome="Undone",
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
    )
    inbox_log.mark_undone(event_id, undo_id)
    _refresh_time_off_surfaces()
    return JSONResponse({"ok": True})


@router.post("/api/exceptions/undo/{event_id}")
async def undo_inbox_event(event_id: int, request: Request):
    actor_upn, actor_name = _actor_from(request)
    return await asyncio.to_thread(_undo_sync, event_id, actor_upn, actor_name)


def _breakdown_transfer_sync(body: dict, actor_upn=None, actor_name=None) -> JSONResponse:
    if breakdown_actions.live_transfer_is_disabled():
        return JSONResponse(
            {
                "ok": False,
                "error": breakdown_actions.LIVE_TRANSFER_MESSAGE,
            },
            status_code=410,
        )
    return breakdown_actions.transfer(
        body,
        actor_upn,
        actor_name,
        friendly_error=_friendly_odoo_error,
    )


@router.post("/api/exceptions/breakdown/transfer")
async def breakdown_transfer(request: Request):
    """Transfer an operator off a broken machine.

    Body (JSON): {incident_id, person_name, to_wc}
    """
    from .. import inbox_log
    body = await request.json()
    actor_upn, actor_name = inbox_log.actor_from(request)
    return await asyncio.to_thread(_breakdown_transfer_sync, body, actor_upn, actor_name)


def _breakdown_snooze_sync(body: dict) -> JSONResponse:
    return breakdown_actions.snooze(body)


@router.post("/api/exceptions/breakdown/snooze")
async def breakdown_snooze(request: Request):
    """Silence one operator's row on a breakdown card for 15 minutes.

    Body (JSON): {incident_id, person_name}
    """
    body = await request.json()
    return await asyncio.to_thread(_breakdown_snooze_sync, body)


def _breakdown_dismiss_sync(body: dict, actor_upn=None, actor_name=None) -> JSONResponse:
    return breakdown_actions.dismiss(body, actor_upn, actor_name)


@router.post("/api/exceptions/breakdown/dismiss")
async def breakdown_dismiss(request: Request):
    """"Not a breakdown": resolve the incident and delete its exclusion rows.

    Body (JSON): {incident_id}
    """
    from .. import inbox_log
    body = await request.json()
    actor_upn, actor_name = inbox_log.actor_from(request)
    return await asyncio.to_thread(_breakdown_dismiss_sync, body, actor_upn, actor_name)


def _breakdown_report_sync(body: dict) -> JSONResponse:
    return breakdown_actions.report(body)


@router.post("/api/exceptions/breakdown/report")
async def breakdown_report(request: Request):
    """Manually report a machine as broken down.

    Body (JSON): {wc_name}
    """
    body = await request.json()
    return await asyncio.to_thread(_breakdown_report_sync, body)
