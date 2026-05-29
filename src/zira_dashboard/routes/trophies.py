"""Trophy Case page + override endpoint."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import awards, work_centers_store
from ..deps import templates

router = APIRouter()

VALID_SCOPES = {
    "badge",
    "trophy_top_day",
    "trophy_best_avg_group",
    "trophy_best_avg_wc",
    "award_goat",
}
VALID_ACTIONS = {"replace", "delete", "reset"}


@router.get("/trophies", response_class=HTMLResponse)
def trophies_page(
    request: Request,
    year: int | None = Query(default=None),
    month: int | None = Query(default=None),
):
    today = datetime.now(timezone.utc).date()
    y = year or today.year
    m = month or today.month
    groups = work_centers_store.registered_groups()
    overrides = awards._load_overrides()

    # GOATs section — one per group
    goats = []
    for g in groups:
        live = awards.goat(g)
        final = awards.apply_overrides_single(
            live, scope="award_goat", group_name=g, overrides=overrides,
        )
        goats.append({"group": g, "winner": final})

    # Annual section — for selected year, per group
    annual = []
    for g in groups:
        top = awards.apply_overrides(
            awards.annual_top_days(g, y),
            scope="trophy_top_day", group_name=g, year=y, overrides=overrides,
        )
        ba = awards.apply_overrides_single(
            awards.annual_best_avg_group(g, y),
            scope="trophy_best_avg_group", group_name=g, year=y, overrides=overrides,
        )
        wc_winners = []
        for wc_name in sorted({loc.name for loc in work_centers_store.members("group", g)}):
            wcw = awards.apply_overrides_single(
                awards.annual_best_avg_wc(wc_name, y),
                scope="trophy_best_avg_wc", wc_name=wc_name, year=y, overrides=overrides,
            )
            if wcw:
                wc_winners.append({"wc": wc_name, "winner": wcw})
        annual.append({
            "group": g, "top_days": top, "best_avg": ba, "wc_winners": wc_winners,
        })

    # Monthly section — for selected (year, month), per group
    monthly = []
    for g in groups:
        badges = awards.apply_overrides(
            awards.monthly_badges(g, y, m),
            scope="badge", group_name=g, year=y, month=m, overrides=overrides,
        )
        monthly.append({"group": g, "badges": badges})

    return templates.TemplateResponse(
        request,
        "trophy_case.html",
        {
            "active": "trophies",
            "today": today.isoformat(),
            "year": y,
            "month": m,
            "goats": goats,
            "annual": annual,
            "monthly": monthly,
        },
    )


@router.post("/api/awards/override")
async def award_override(request: Request):
    """Body (JSON):
        {scope, group_name?, wc_name?, year?, month?, position,
         action: 'replace' | 'delete' | 'reset', name?, note?}
    """
    from .. import db
    body = await request.json()
    scope = body.get("scope")
    if scope not in VALID_SCOPES:
        return JSONResponse({"ok": False, "error": "bad scope"}, status_code=400)
    action = body.get("action")
    if action not in VALID_ACTIONS:
        return JSONResponse({"ok": False, "error": "bad action"}, status_code=400)

    group_name = body.get("group_name") or None
    wc_name = body.get("wc_name") or None
    year = body.get("year") or None
    month = body.get("month") or None
    try:
        position = int(body.get("position") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "position required (int)"}, status_code=400)
    if position < 1:
        return JSONResponse({"ok": False, "error": "position must be >= 1"}, status_code=400)

    if action == "reset":
        db.execute(
            "DELETE FROM award_overrides "
            "WHERE scope = %s AND COALESCE(group_name, '') = COALESCE(%s, '') "
            "  AND COALESCE(wc_name, '') = COALESCE(%s, '') "
            "  AND COALESCE(year, 0) = COALESCE(%s, 0) "
            "  AND COALESCE(month, 0) = COALESCE(%s, 0) "
            "  AND position = %s",
            (scope, group_name, wc_name, year, month, position),
        )
        from .. import _http_cache
        _http_cache.invalidate_all_cache()
        return JSONResponse({"ok": True})

    name = body.get("name")
    if action == "replace" and not name:
        return JSONResponse({"ok": False, "error": "replace requires name"}, status_code=400)
    note = body.get("note")

    db.execute(
        "INSERT INTO award_overrides "
        "  (scope, group_name, wc_name, year, month, position, action, name, note) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (scope, COALESCE(group_name,''), COALESCE(wc_name,''), "
        "             COALESCE(year,0), COALESCE(month,0), position) "
        "DO UPDATE SET action = EXCLUDED.action, name = EXCLUDED.name, note = EXCLUDED.note, "
        "              created_at = NOW()",
        (scope, group_name, wc_name, year, month, position, action, name, note),
    )
    from .. import _http_cache
    _http_cache.invalidate_all_cache()
    return JSONResponse({"ok": True})
