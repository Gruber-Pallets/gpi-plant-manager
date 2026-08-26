"""Local-first user feedback submission and a per-user status list."""

from __future__ import annotations

import html
import logging

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from .. import feedback_store, odoo_client
from ..feedback_content import safe_page_url
from ..feedback_image import ImageRejected, MAX_INPUT_BYTES, normalize_image

router = APIRouter()
log = logging.getLogger(__name__)

_TYPE_TAG = {"bug": "Bug", "feature": "Feature request"}
_TYPE_TITLE = {"bug": "Bug", "feature": "Feature"}
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


def _title_from(kind: str, description: str) -> str:
    first = description.strip().splitlines()[0] if description.strip() else "feedback"
    if len(first) > _TITLE_MAX:
        first = first[: _TITLE_MAX - 1].rstrip() + "…"
    return f"[{_TYPE_TITLE.get(kind, 'Bug')}] {first}"


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


@router.post("/feedback")
async def submit_feedback(
    request: Request,
    type: str = Form("bug"),
    description: str = Form(...),
    page_url: str | None = Form(None),
    screenshot: UploadFile | None = File(default=None),
) -> JSONResponse:
    kind = "feature" if type == "feature" else "bug"
    text = (description or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "Description is required."},
                            status_code=400)

    submitter = getattr(request.state, "user_upn", None)
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
        submitter=submitter,
        page_url=safe_url,
        task_type=kind,
        status="requested",
        before_image=before_image,
    )
    return JSONResponse({"ok": True, "id": new_id})


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
            "type": row.get("task_type") or "bug",
            "title": title,
            "created_at": str(row.get("created_at") or ""),
            "page_url": row.get("page_url"),
            "status": status,
        })

    return JSONResponse({"ok": True, "items": items, "status_available": status_available})
