"""Dedicated forklift-driver leaderboard: GET /staffing/forklift.

Four ranked cards (overall GOAT score, most calls, on-time %, fastest
response) over the same window presets the production leaderboards use.
Defensive: any forklift store / score-config failure degrades to empty
cards rather than 500ing the page (mirrors forklift_advisor's posture).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .. import forklift_awards, forklift_settings
from ..deps import resolve_range, templates
from ..plant_day import today as plant_today

_log = logging.getLogger(__name__)

router = APIRouter()


def _apply_display_names(lb: dict) -> None:
    """Set `display_name` on every leaderboard row, resolving the forklift
    first-name to its full plant name via the driver name-map. Best-effort:
    on any failure every row falls back to its raw `name`."""
    try:
        from .. import forklift_store
        nm = forklift_store.name_map("driver") or {}
    except Exception as exc:  # noqa: BLE001 - display sugar, never 500
        _log.warning("forklift leaderboard: name-map lookup failed: %s", exc)
        nm = {}
    for rows in lb.values():
        if not isinstance(rows, list):
            continue
        for r in rows:
            if isinstance(r, dict):
                r["display_name"] = nm.get(r.get("name"), r.get("name"))

FORKLIFT_MIN_CALLS = forklift_awards.DEFAULT_MIN_CALLS

_WINDOW_LABELS = {
    "today": "Today", "week": "Week", "month": "Month",
    "quarter": "Quarter", "year": "Year", "alltime": "All Time",
}


@router.get("/staffing/forklift", response_class=HTMLResponse)
def forklift_leaderboards(
    request: Request,
    window: str = Query(default="alltime"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    today_d = plant_today()
    start_d, end_d, custom_range_active = resolve_range(window, start, end, today_d)

    # Resolve the score config + compute the four lists best-effort. score_config
    # only reads the score-override fields, so algo_throughput is a don't-care.
    try:
        cfg = forklift_settings.resolve(
            forklift_settings.current(), algo_throughput=0.0
        ).score_config()
        lb = forklift_awards.leaderboard(
            start_d, end_d, cfg, min_calls=FORKLIFT_MIN_CALLS
        )
    except Exception as exc:  # noqa: BLE001 - never 500 the page on a data hiccup
        _log.warning("forklift leaderboard: render context failed: %s", exc)
        lb = forklift_awards.empty_leaderboard()

    # Disambiguate first-name-only forklift names (the plant has three
    # "Jesus"es) by resolving each through the driver name-map to its full
    # plant name. Unmapped names fall through unchanged. `display_name` also
    # drives the player-card link, which resolves plant→forklift internally.
    _apply_display_names(lb)

    label = "Custom" if custom_range_active else _WINDOW_LABELS.get(window, window)
    return templates.TemplateResponse(
        request,
        "forklift_leaderboards.html",
        {
            "active": "forklift_leaderboards",
            "window": window,
            "window_label": label,
            "custom_range_active": custom_range_active,
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "lb": lb,
            "min_calls": FORKLIFT_MIN_CALLS,
        },
    )
