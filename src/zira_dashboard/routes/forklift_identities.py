from __future__ import annotations

import asyncio
import logging
import os
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import forklift_identity_store, forklift_identity_view, inbox_log
from ..plant_day import today as plant_today


router = APIRouter()


def _wants_json(request: Request) -> bool:
    return (request.headers.get("accept") or "").startswith("application/json")


def _redirect(day: date, *, saved: bool = False, error: str = ""):
    query = {"section": "forklift", "identity_day": day.isoformat()}
    if saved:
        query["identity_saved"] = "1"
    if error:
        query["identity_error"] = error
    return RedirectResponse(
        url=f"/settings?{urlencode(query)}#forklift-identities", status_code=303
    )


def _error(request: Request, day: date, message: str, status_code: int):
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": message}, status_code=status_code)
    return _redirect(day, error=message)


def _optional_version(raw: object) -> int | None:
    value = str(raw or "").strip()
    if not value:
        return None
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("mapping version must be positive")
    return parsed


def _observed_source_name(context: dict, driver_id: str) -> str:
    unresolved = next(
        (
            row
            for row in context["unresolved"]
            if row["external_driver_id"] == driver_id
        ),
        None,
    )
    if unresolved is not None:
        return " / ".join(unresolved["source_names"])
    current = next(
        (
            row
            for row in context["mappings"]
            if row["external_driver_id"] == driver_id
        ),
        None,
    )
    if current is not None:
        return current["source_name"]
    raise ValueError("Choose an identity shown in Settings.")


@router.post("/settings/forklift-identities")
async def save_forklift_identity(request: Request):
    form = await request.form()
    today = plant_today()
    try:
        selected_day = date.fromisoformat(str(form.get("day") or ""))
    except ValueError:
        selected_day = today
        return _error(request, selected_day, "Choose a valid day.", 422)
    if selected_day > today:
        return _error(request, today, "Choose today or an earlier day.", 422)
    action = str(form.get("action") or "").strip()
    driver_id = str(form.get("external_driver_id") or "").strip()
    actor_upn, actor_name = inbox_log.actor_from(request)
    if not actor_upn and os.environ.get("AUTH_DISABLED") == "1":
        actor_upn = "auth-disabled"
    if not actor_upn:
        return _error(
            request,
            selected_day,
            "Sign in again before changing identities.",
            401,
        )
    try:
        version = _optional_version(form.get("expected_version"))
        if action == "save":
            employee_id = int(str(form.get("employee_odoo_id") or ""))
            identity_context = await asyncio.to_thread(
                forklift_identity_view.identity_context, selected_day
            )
            source_name = _observed_source_name(identity_context, driver_id)
            await asyncio.to_thread(
                forklift_identity_store.save_mapping,
                driver_id,
                source_name,
                employee_id,
                expected_version=version,
                actor_upn=actor_upn,
                actor_name=actor_name,
            )
            try:
                await asyncio.to_thread(
                    forklift_identity_view.identity_context, selected_day
                )
            except Exception:
                logging.warning(
                    "Forklift identity saved but immediate re-resolution failed",
                    exc_info=True,
                )
        elif action == "remove" and version is not None:
            await asyncio.to_thread(
                forklift_identity_store.remove_mapping,
                driver_id,
                expected_version=version,
                actor_upn=actor_upn,
                actor_name=actor_name,
            )
        else:
            raise ValueError("Choose a valid identity action.")
    except forklift_identity_store.MappingConflict as exc:
        return _error(request, selected_day, str(exc), 409)
    except (TypeError, ValueError):
        return _error(request, selected_day, "Choose a valid active employee.", 422)
    except Exception:
        logging.warning("Forklift identity change unavailable", exc_info=True)
        return _error(
            request,
            selected_day,
            "Forklift identities are unavailable right now. No change was made.",
            503,
        )
    if _wants_json(request):
        return JSONResponse({"ok": True})
    return _redirect(selected_day, saved=True)
