"""User feedback submission and read-only admin list."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .. import feedback_store
from ..deps import templates

router = APIRouter()


class FeedbackIn(BaseModel):
    message: str
    category: str | None = None
    page_url: str | None = None


@router.post("/feedback")
def submit_feedback(payload: FeedbackIn, request: Request) -> JSONResponse:
    message = (payload.message or "").strip()
    if not message:
        return JSONResponse(
            {"ok": False, "error": "Message is required."},
            status_code=400,
        )
    submitter = getattr(request.state, "user_upn", None)
    new_id = feedback_store.insert(
        message=message,
        submitter=submitter,
        page_url=(payload.page_url or None),
        category=(payload.category or None),
    )
    return JSONResponse({"ok": True, "id": new_id})


@router.get("/admin/feedback", response_class=HTMLResponse)
def admin_feedback(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin_feedback.html",
        {"items": feedback_store.recent(), "active": "admin"},
    )
