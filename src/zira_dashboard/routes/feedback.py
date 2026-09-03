"""Local-first user feedback submission and a per-user status list."""

from __future__ import annotations

import html
import logging

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from .. import feedback_store, feedback_submitters, odoo_client
from ..feedback_content import safe_page_url
from ..feedback_image import ImageRejected, MAX_INPUT_BYTES, normalize_image
from ..feedback_types import feedback_type, feedback_type_or_legacy_bug

router = APIRouter()
log = logging.getLogger(__name__)

_TITLE_MAX = 70
_MAX_FILE_BYTES = 10 * 1024 * 1024
_ALLOWED_PREFIXES = ("image/",)
_ALLOWED_TYPES = ("application/pdf",)
_SOURCE_APP = "GPI Plant Manager (plant)"
_LEGACY_STATUS = {
    "open": "requested",
    "done": "completed",
    "rejected": "declined",
}


@router.get("/api/feedback/submitters")
def feedback_submitter_choices() -> JSONResponse:
    people = feedback_submitters.active_choices()
    return JSONResponse(
        {
            "ok": True,
            "people": [
                {"employee_id": person.employee_id, "name": person.name}
                for person in people
            ],
        }
    )


def _title_from(kind: str, description: str) -> str:
    first = description.strip().splitlines()[0] if description.strip() else "feedback"
    if len(first) > _TITLE_MAX:
        first = first[: _TITLE_MAX - 1].rstrip() + "…"
    return f"[{feedback_type(kind).label}] {first}"


def _allowed_upload(upload: UploadFile) -> bool:
    ct = (upload.content_type or "").lower()
    return ct.startswith(_ALLOWED_PREFIXES) or ct in _ALLOWED_TYPES


def _description_html(description: str, submitter: str | None,
                      name: str | None, page_url: str | None) -> str:
    who = name or submitter or "unknown"
    if name and submitter:
        who = f"{name} ({submitter})"
    # Escape every dynamic value before interpolating — this HTML lands in the
    # Odoo task's `description` field. Matches the escaping convention in
    # routes/changelog.py; keeps descriptions with <, &, or " rendering as typed.
    body = html.escape(description.strip()).replace("\n", "<br>")
    parts = [f"<p>{body}</p>"]
    meta = [
        f"Source app: {_SOURCE_APP}",
        f"Submitted by {html.escape(who)}",
    ]
    if page_url:
        safe_url = html.escape(page_url, quote=True)
        meta.append(f'Page: <a href="{safe_url}">{safe_url}</a>')
    parts.append("<p><small>" + " · ".join(meta) + "</small></p>")
    return "".join(parts)


@router.post("/timeclock/feedback")
@router.post("/feedback")
async def submit_feedback(
    request: Request,
    type: str = Form("bug"),
    description: str = Form(...),
    page_url: str | None = Form(None),
    submitter_employee_id: str | None = Form(None),
    screenshot: UploadFile | None = File(default=None),
) -> JSONResponse:
    try:
        canonical_type = feedback_type(type)
        if canonical_type.behavior == "external":
            raise ValueError("external feedback type")
        kind = canonical_type.value
    except ValueError:
        return JSONResponse(
            {"ok": False, "error": "Unsupported feedback type."}, status_code=400
        )
    text = (description or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "Description is required."},
                            status_code=400)

    matched_route = request.scope.get("route")
    is_timeclock_submission = (
        getattr(matched_route, "path", None) == "/timeclock/feedback"
    )
    private_upn = getattr(request.state, "user_upn", None)
    try:
        if private_upn is not None and not is_timeclock_submission:
            resolved_submitter = feedback_submitters.resolve_private(private_upn)
        else:
            raw_employee_id = (submitter_employee_id or "").strip()
            if not raw_employee_id.isascii() or not raw_employee_id.isdecimal():
                raise feedback_submitters.SubmitterError("employee id is required")
            try:
                parsed_employee_id = int(raw_employee_id)
            except ValueError as error:
                raise feedback_submitters.SubmitterError(
                    "employee id is invalid"
                ) from error
            resolved_submitter = feedback_submitters.resolve_timeclock(
                parsed_employee_id
            )
    except feedback_submitters.SubmitterError:
        return JSONResponse(
            {"ok": False, "error": "Choose your name and try again."},
            status_code=400,
        )
    safe_url = safe_page_url(page_url)

    before_image = None
    if screenshot is not None:
        raw = await screenshot.read(MAX_INPUT_BYTES + 1)
        try:
            before_image = normalize_image(raw)
        except ImageRejected as error:
            return JSONResponse(
                {"ok": False, "error": str(error)},
                status_code=400,
            )

    new_id = feedback_store.create_submission(
        message=text,
        submitter=resolved_submitter.email,
        submitter_employee_odoo_id=resolved_submitter.employee_id,
        page_url=safe_url,
        task_type=kind,
        status="requested",
        before_image=before_image,
    )
    return JSONResponse({"ok": True, "id": new_id, "task_delivery": "queued"})


@router.get("/api/feedback/mine")
def my_feedback(request: Request) -> JSONResponse:
    submitter = getattr(request.state, "user_upn", None)
    rows = feedback_store.for_submitter(submitter)
    task_ids = [
        row["odoo_task_id"]
        for row in rows
        if row.get("status") is None and row.get("odoo_task_id")
    ]
    status_available = True
    try:
        stages = odoo_client.fetch_task_stage_names(task_ids) if task_ids else {}
    except Exception:
        log.exception("feedback: could not read legacy task stages")
        stages = {}
        status_available = False

    items = []
    for row in rows:
        canonical_type = feedback_type_or_legacy_bug(row.get("task_type"))
        message = (row.get("message") or "").strip()
        title = message.splitlines()[0] if message else "(no description)"
        if len(title) > _TITLE_MAX:
            title = title[: _TITLE_MAX - 1].rstrip() + "…"
        status = row.get("status")
        if status is None:
            legacy_bucket = odoo_client.feedback_status_bucket(
                stages.get(row.get("odoo_task_id"))
            )
            status = _LEGACY_STATUS[legacy_bucket]
        items.append({
            "type": canonical_type.value,
            "type_label": canonical_type.label,
            "title": title,
            "created_at": str(row.get("created_at") or ""),
            "page_url": row.get("page_url"),
            "status": status,
        })

    return JSONResponse({"ok": True, "items": items, "status_available": status_available})
