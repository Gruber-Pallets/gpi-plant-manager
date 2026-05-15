"""GOAT Watch API — dismiss a NEW GOAT alert.

The Recycling VS template reads alerts from `goat_watch.active_alerts`
on each render and emits the banner. When the user clicks the
"Dismiss" button, the banner POSTs to this endpoint to mark the alert
as dismissed so it stops appearing.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import goat_watch

router = APIRouter()


@router.post("/api/goat-alerts/{alert_id}/dismiss")
def dismiss_goat_alert(alert_id: int):
    ok = goat_watch.dismiss_alert(alert_id)
    return JSONResponse({"ok": ok})
