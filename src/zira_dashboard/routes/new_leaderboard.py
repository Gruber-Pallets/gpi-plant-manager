from __future__ import annotations

from datetime import date
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .. import awards, goat_categories, production_history, production_metrics, shift_config, staffing
from .._http_cache import get_cached_response, set_cache_headers, store_cached_response
from ..deps import templates
from ..plant_day import today as plant_today

router = APIRouter()
_log = logging.getLogger(__name__)

_FAMILY_SKILLS = tuple(
    (category.label, category.skill, category.leaderboard_label)
    for category in goat_categories.new_categories()
)


def _family_wc_names(locations=None) -> dict[str, set[str]]:
    source = staffing.LOCATIONS if locations is None else locations
    return {
        family: {loc.name for loc in source if loc.skill == skill}
        for family, skill, _goat_label in _FAMILY_SKILLS
    }


def _leaderboard_payload(today: date) -> dict:
    records = production_history.normalized_daily_records(
        awards.AWARDS_DATA_FLOOR,
        today,
    )
    family_wc_names = _family_wc_names()
    data = production_metrics.build_family_leaderboard(
        records,
        today=today,
        standard_full_day_hours=shift_config.productive_minutes_per_day() / 60.0,
        family_wc_names=family_wc_names,
    )
    try:
        overrides = awards.load_overrides()
    except Exception:
        _log.exception("New-Leaderboard award overrides failed")
        overrides = []
    goat_labels = {family: label for family, _skill, label in _FAMILY_SKILLS}
    goats: list[dict] = []
    for family in data["active_families"]:
        category = goat_categories.category_for_group_name(family)
        if category is not None and not goat_categories.is_goat_ready(category, records):
            continue
        try:
            winner = awards.goat_for_wc_names(
                family_wc_names[family],
                group_name=family,
                records=records,
                today=today,
                overrides=overrides,
            )
        except Exception:
            _log.exception("New-Leaderboard GOAT lookup failed for %s", family)
            winner = None
        if winner is not None and winner.get("name"):
            goats.append({
                "label": goat_labels[family],
                "group": family,
                "name": winner["name"],
                "units": winner.get("units"),
                "day": winner.get("day"),
            })
    data["current_goats"] = goats
    data["error_message"] = None
    return data


def _empty_payload(today: date, message: str) -> dict:
    data = production_metrics.build_family_leaderboard(
        [],
        today=today,
        # Empty records never reach normalization; avoid consulting the
        # database-backed schedule while rendering an outage state.
        standard_full_day_hours=0.0,
        family_wc_names=_family_wc_names(),
    )
    data["current_goats"] = []
    data["error_message"] = message
    return data


def _render_new_leaderboard(
    request: Request,
    *,
    tv_mode: bool,
    tv_theme: str = "dark",
) -> HTMLResponse:
    today = plant_today()
    safe_theme = tv_theme if tv_theme in ("light", "dark") else "dark"
    cache_key = ("new_leaderboard", today.isoformat(), tv_mode, safe_theme)
    cached = get_cached_response(cache_key, includes_today=True)
    if cached is not None:
        return cached
    try:
        data = _leaderboard_payload(today)
    except Exception:
        _log.exception("New-Leaderboard payload failed")
        data = _empty_payload(today, "Production data is temporarily unavailable.")
    context = {
        "tv_mode": tv_mode,
        "tv_theme": safe_theme,
        "data": data,
        "active_dashboard_key": "vs_new_leaderboard",
    }
    response = templates.TemplateResponse(request, "new_leaderboard_tv.html", context)
    set_cache_headers(response, includes_today=True)
    store_cached_response(cache_key, includes_today=True, response=response)
    return response


def render_new_leaderboard_tv(
    request: Request,
    *,
    tv_theme: str = "dark",
) -> HTMLResponse:
    return _render_new_leaderboard(request, tv_mode=True, tv_theme=tv_theme)


@router.get("/new-leaderboard", response_class=HTMLResponse)
def new_leaderboard(request: Request):
    return _render_new_leaderboard(request, tv_mode=False)


@router.get("/tv/new-leaderboard", response_class=HTMLResponse)
def tv_new_leaderboard(request: Request, theme: str | None = Query(default=None)):
    from .. import tv_displays_store

    try:
        row = tv_displays_store.by_slug("new-leaderboard")
    except Exception:
        row = None
    stored_theme = row["theme"] if row is not None else "dark"
    tv_theme = "light" if theme == "light" else (
        "dark" if theme == "dark" else stored_theme
    )
    return render_new_leaderboard_tv(request, tv_theme=tv_theme)
