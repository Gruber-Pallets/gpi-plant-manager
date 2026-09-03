"""Super-admin local feedback lifecycle routes."""

from __future__ import annotations

import os
from datetime import UTC, datetime

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, feedback_store
from ..deps import templates
from ..feedback_image import ImageRejected, MAX_INPUT_BYTES, normalize_image

router = APIRouter()


def _forbidden() -> HTMLResponse:
    return HTMLResponse("Forbidden", status_code=403)


@router.get("/admin/feedback", response_class=HTMLResponse)
def admin_feedback(request: Request):
    if not auth.request_is_super_admin(request):
        return _forbidden()
    return templates.TemplateResponse(
        request,
        "admin_feedback.html",
        {
            "items": feedback_store.for_admin(),
            "odoo_url": os.environ.get("ODOO_URL", "").rstrip("/"),
        },
    )


@router.post("/admin/feedback/{feedback_id}/status")
async def update_feedback_status(
    feedback_id: int,
    request: Request,
    status: str = Form(...),
    resolution_note: str | None = Form(None),
    after_image: UploadFile | None = File(default=None),
):
    if not auth.request_is_super_admin(request):
        return _forbidden()

    normalized_after = None
    try:
        if after_image is not None:
            raw = await after_image.read(MAX_INPUT_BYTES + 1)
            if raw:
                normalized_after = normalize_image(raw)
        feedback_store.transition(
            feedback_id=feedback_id,
            status=status,
            actor=getattr(request.state, "user_upn", ""),
            resolution_note=resolution_note,
            after_image=normalized_after,
            now=datetime.now(UTC),
        )
    except KeyError:
        return HTMLResponse("Feedback not found", status_code=404)
    except (feedback_store.InvalidTransition, ImageRejected) as error:
        return HTMLResponse(str(error), status_code=422)

    return RedirectResponse(url="/admin/feedback", status_code=303)
