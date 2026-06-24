"""User feedback submission → Odoo task, and a per-user status list."""

from __future__ import annotations

import html
import logging
from datetime import date
from urllib.parse import urlparse

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from .. import feedback_store, odoo_client

router = APIRouter()
log = logging.getLogger(__name__)

_TYPE_TAG = {"bug": "Bug", "feature": "Feature request"}
_TYPE_TITLE = {"bug": "Bug", "feature": "Feature"}
_TITLE_MAX = 70
_MAX_FILE_BYTES = 10 * 1024 * 1024
_ALLOWED_PREFIXES = ("image/",)
_ALLOWED_TYPES = ("application/pdf",)


def _optional_text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _safe_page_url(value: str | None) -> str | None:
    value = _optional_text(value)
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https"):
        return value
    if not parsed.scheme and value.startswith("/") and not value.startswith("//"):
        return value
    return None


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
    meta = [f"Submitted by {html.escape(who)}"]
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
    files: list[UploadFile] = File(default=[]),
) -> JSONResponse:
    kind = "feature" if type == "feature" else "bug"
    text = (description or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "Description is required."},
                            status_code=400)

    submitter = getattr(request.state, "user_upn", None)
    name = getattr(request.state, "user_name", None)
    safe_url = _safe_page_url(page_url)

    blobs: list[tuple[str, str | None, bytes]] = []
    for upload in files or []:
        if not upload.filename or not _allowed_upload(upload):
            continue
        raw = await upload.read()
        if not raw or len(raw) > _MAX_FILE_BYTES:
            continue
        blobs.append((upload.filename, upload.content_type, raw))

    try:
        project_id = odoo_client.ensure_feedback_project()
        tag_id = odoo_client.ensure_feedback_tag(_TYPE_TAG[kind])
        task_id = odoo_client.create_feedback_task(
            project_id=project_id,
            name=_title_from(kind, text),
            description_html=_description_html(text, submitter, name, safe_url),
            assignee_uid=odoo_client.authenticate(),
            tag_id=tag_id,
            deadline=date.today().isoformat(),
        )
    except Exception:
        log.exception("feedback: failed to create Odoo task")
        return JSONResponse(
            {"ok": False, "error": "Couldn't reach Odoo — please try again."},
            status_code=502,
        )

    for filename, mimetype, raw in blobs:
        try:
            odoo_client.add_task_attachment(
                task_id=task_id, filename=filename, mimetype=mimetype, raw_bytes=raw,
            )
        except Exception:
            log.exception("feedback: attachment upload failed for task %s", task_id)

    new_id = feedback_store.insert(
        message=text,
        submitter=submitter,
        page_url=safe_url,
        task_type=kind,
        odoo_task_id=task_id,
    )
    return JSONResponse({"ok": True, "id": new_id, "task_id": task_id})


@router.get("/api/feedback/mine")
def my_feedback(request: Request) -> JSONResponse:
    submitter = getattr(request.state, "user_upn", None)
    rows = feedback_store.for_submitter(submitter)
    task_ids = [r["odoo_task_id"] for r in rows if r.get("odoo_task_id")]
    status_available = True
    try:
        stages = odoo_client.fetch_task_stage_names(task_ids) if task_ids else {}
    except Exception:
        log.exception("feedback: could not read task stages")
        stages = {}
        status_available = False

    items = []
    for r in rows:
        message = (r.get("message") or "").strip()
        title = message.splitlines()[0] if message else "(no description)"
        if len(title) > _TITLE_MAX:
            title = title[: _TITLE_MAX - 1].rstrip() + "…"
        items.append({
            "type": r.get("task_type") or "bug",
            "title": title,
            "created_at": str(r.get("created_at") or ""),
            "page_url": r.get("page_url"),
            "status": odoo_client.feedback_status_bucket(stages.get(r.get("odoo_task_id"))),
        })

    return JSONResponse({"ok": True, "items": items, "status_available": status_available})
