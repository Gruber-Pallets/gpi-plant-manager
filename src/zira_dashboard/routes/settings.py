"""Settings page + save endpoints.

Routes:
  GET  /settings                  — render the full settings page
  POST /settings/schedule         — save shift schedule + breaks
  POST /settings/work_centers     — save WC rows, group registry, group/VS overrides
  POST /settings                  — legacy global save (kept for backward compat)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from urllib.parse import quote_plus, urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import (
    app_settings,
    attendance_location_policy,
    attendance_readiness,
    auth,
    db,
    odoo_client,
    schedule_store,
    settings_context,
    settings_store,
    shift_config,
    staffing,
    staffing_hours,
    work_centers_store,
)
from ..deps import templates
from ..plant_day import today as plant_today
from ..stations import CATEGORIES, STATIONS
from .staffing import _default_auto_work_centers, _save_default_auto_work_centers

router = APIRouter()


class InvalidOdooWorkCenterMapping(ValueError):
    pass


def _odoo_work_center_field(loc) -> str:
    key = loc.meter_id or f"name:{loc.name}"
    return f"wc__{key}__odoo_work_center_id"


def _odoo_work_center_updates(
    form,
    options: list[dict],
) -> dict[str, dict]:
    """Resolve posted Odoo record IDs against the freshly-read active catalog."""
    active = {int(option["id"]): option["name"] for option in options}
    result: dict[str, dict] = {}
    claimed_by_posted_location: dict[int, str] = {}
    for loc in staffing.LOCATIONS:
        field = _odoo_work_center_field(loc)
        if field not in form:
            continue
        raw = (form.get(field) or "").strip()
        if not raw:
            result[loc.name] = {"odoo_id": None, "odoo_name": None}
            continue
        try:
            odoo_id = int(raw)
        except ValueError as exc:
            raise InvalidOdooWorkCenterMapping(
                f"Invalid Odoo work center for {loc.name}."
            ) from exc
        odoo_name = active.get(odoo_id)
        if odoo_name is None:
            raise InvalidOdooWorkCenterMapping(
                f"{loc.name} points to an inactive or unknown Odoo work center."
            )
        owner = claimed_by_posted_location.get(odoo_id)
        if owner is not None:
            raise InvalidOdooWorkCenterMapping(
                f"{loc.name} and {owner} cannot use the same Odoo work center."
            )
        claimed_by_posted_location[odoo_id] = loc.name
        result[loc.name] = {"odoo_id": odoo_id, "odoo_name": odoo_name}
    return result


def _odoo_configured() -> bool:
    """True when the four Odoo env vars are set so XML-RPC calls won't
    raise OdooConfigError. Used to gate the Time Off settings panel's
    leave-types fetch when running on a dev box without Odoo wiring."""
    import os
    return all(os.environ.get(k) for k in
               ("ODOO_URL", "ODOO_DB", "ODOO_LOGIN", "ODOO_API_KEY"))


def _clamp(raw) -> int:
    """Clamp a rounding-window form value to 0..60 minutes (bad input -> 0).
    Shared by the rounding-system and work-schedule rounding save routes."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(60, v))


# Form time inputs are HH:MM strings; schedule_store's parser is the shared
# canon for that shape (also reused by the saturday/work-schedule stores).
_parse_hhmm = schedule_store._parse_time


def _hours_display(work_hours: dict) -> str:
    """Short, human label for a schedule's synced hours, e.g. '5:45 AM –
    2:30 PM'. Collapses to a single range when every configured weekday
    shares it; 'varies by day' otherwise."""
    if not work_hours:
        return "— not synced from Odoo yet —"

    def fmt(t) -> str:
        h = t.hour % 12 or 12
        ap = "AM" if t.hour < 12 else "PM"
        return f"{h}:{t.minute:02d} {ap}"

    ranges = {(s, e) for (s, e) in work_hours.values()}
    if len(ranges) == 1:
        s, e = next(iter(ranges))
        return f"{fmt(s)} – {fmt(e)}"
    return "varies by day"


def _loc_by_key(key: str):
    for loc in staffing.LOCATIONS:
        if (loc.meter_id or f"name:{loc.name}") == key:
            return loc
    return None


def _settings_default_auto_work_centers() -> list[str]:
    """Resolve Settings' template through the shared first-run initializer."""
    return _default_auto_work_centers(plant_today())


def _ordered_default_auto_work_centers(names) -> list[str]:
    """Normalize a Settings default-template selection to location order."""
    selected = {
        str(name).strip()
        for name in (names or [])
        if str(name or "").strip() in {loc.name for loc in staffing.LOCATIONS}
    }
    return [loc.name for loc in staffing.LOCATIONS if loc.name in selected]


def _split_roster_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split roster-filter rows into (active, inactive) by the `active`
    flag. Input order is preserved within each list (the query already
    sorts by name)."""
    active = [r for r in rows if r.get("active")]
    inactive = [r for r in rows if not r.get("active")]
    return active, inactive


def _roster_filter_lists() -> tuple[list[dict], list[dict]]:
    """Load Odoo-synced people for the Settings roster filter, split into
    (active, inactive). Active and inactive are each alphabetical by name."""
    from .. import db
    rows = db.query(
        "SELECT odoo_id, name, excluded, active "
        "FROM people "
        "WHERE odoo_id IS NOT NULL "
        "ORDER BY lower(name)"
    )
    return _split_roster_rows(rows)


def _parse_api_key_scopes(form) -> list[str]:
    if form.get("scope_admin"):
        return ["admin:*"]
    scopes: list[str] = []
    if form.get("scope_read"):
        scopes.append("object:read")
    if form.get("scope_write"):
        scopes.append("object:write")
    if form.get("scope_unlink"):
        scopes.append("object:unlink")
    return scopes or ["object:read"]


def _can_manage_api_keys(request: Request) -> bool:
    return auth.request_is_super_admin(request)


def _api_settings_forbidden() -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": "super_admin_required"},
        status_code=403,
    )


def _attendance_location_context() -> dict:
    """Build the read-only rollout health + department policy view."""
    config = attendance_location_policy.get_rollout_config()
    try:
        department_rows = db.query(
            "SELECT name, requires_work_center FROM departments ORDER BY lower(name)"
        )
        sync_rows = db.query(
            "SELECT s.baseline_completed_at, "
            "s.last_incremental_completed_at AS mirror_freshness, "
            "s.last_full_sweep_completed_at, s.last_error, "
            "(SELECT COUNT(*) FROM odoo_attendance_mirror m "
            "WHERE m.deleted_at IS NULL AND m.check_out_utc IS NULL "
            "AND (COALESCE(s.last_incremental_observed_at, "
            "s.baseline_completed_at) IS NULL "
            "OR m.last_seen_at < COALESCE(s.last_incremental_observed_at, "
            "s.baseline_completed_at))) AS open_rows_not_refreshed "
            "FROM odoo_attendance_sync_state s WHERE s.singleton = TRUE"
        )
    except Exception:  # noqa: BLE001 - Settings health must survive DB rollout skew
        logging.warning("Attendance-location Settings health unavailable", exc_info=True)
        department_rows = []
        sync_rows = []
    sync = sync_rows[0] if sync_rows else {}
    try:
        persisted_readiness = app_settings.get_setting(
            "odoo_attendance_readiness_report"
        )
    except Exception:  # noqa: BLE001 - persisted status is optional display data
        persisted_readiness = None
    if not isinstance(persisted_readiness, dict):
        persisted_readiness = None
    cutover_local = (
        config.cutover_at.astimezone(shift_config.SITE_TZ)
        if config.cutover_at is not None
        else None
    )
    return {
        "mode": config.mode,
        "live_active": attendance_location_policy.live_is_active(),
        "cutover_at": cutover_local,
        "cutover_local_input": (
            cutover_local.strftime("%Y-%m-%dT%H:%M") if cutover_local else ""
        ),
        "sync_health_available": bool(sync_rows),
        "baseline_completed_at": sync.get("baseline_completed_at"),
        "mirror_freshness": sync.get("mirror_freshness"),
        "last_full_sweep": sync.get("last_full_sweep_completed_at"),
        "last_error": sync.get("last_error"),
        "open_rows_not_refreshed": sync.get("open_rows_not_refreshed"),
        "departments": [
            {
                "name": row["name"],
                "requires_work_center": bool(row["requires_work_center"]),
            }
            for row in department_rows
        ],
        "readiness": persisted_readiness,
    }


def _attendance_location_error(request: Request, error: str, status_code: int):
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": False, "error": error}, status_code=status_code)
    query = urlencode({"section": "timeclock", "error": error})
    return RedirectResponse(
        url=f"/settings?{query}#attendance-location", status_code=303
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    saved: int = Query(default=0),
    section: str = Query(default="work_centers"),
    defaults_error: str = Query(default=""),
    error: str = Query(default=""),
    identity_day: date | None = Query(default=None),
    identity_saved: int = Query(default=0),
    identity_error: str = Query(default=""),
):
    if section not in ("work_centers", "integrations", "api", "roster_filter", "tvs", "timeclock", "time_off", "forklift", "diagnostics"):
        section = "work_centers"
    settings_today = plant_today()
    forklift_identities_ctx: dict | None = None
    if section == "forklift":
        selected_identity_day = identity_day or settings_today
        if selected_identity_day > settings_today:
            raise HTTPException(
                status_code=400, detail="Choose today or an earlier day"
            )
        try:
            from .. import forklift_identity_view

            forklift_identities_ctx = forklift_identity_view.identity_context(
                selected_identity_day
            )
        except Exception:
            logging.warning(
                "Forklift identity Settings context unavailable", exc_info=True
            )
            forklift_identities_ctx = {
                "day": selected_identity_day.isoformat(),
                "mappings": (),
                "unresolved": (),
                "employee_options": (),
                "unavailable": (
                    "Forklift identities are unavailable right now. Try again later."
                ),
            }
    can_manage_api_keys = _can_manage_api_keys(request)
    if section == "api" and not can_manage_api_keys:
        return HTMLResponse("Forbidden", status_code=403)
    odoo_work_centers: list[dict] = []
    odoo_work_centers_error = ""
    if section == "work_centers":
        try:
            odoo_work_centers = odoo_client.fetch_manufacturing_work_centers()
        except Exception:  # noqa: BLE001 - settings remains usable during an Odoo outage
            logging.warning("Settings: Odoo work-center catalog unavailable", exc_info=True)
            odoo_work_centers_error = (
                "Odoo work centers are unavailable. Saved mappings are shown below "
                "but cannot be changed right now."
            )
    roster_filter_active: list[dict] = []
    roster_filter_inactive: list[dict] = []
    if section == "roster_filter":
        roster_filter_active, roster_filter_inactive = _roster_filter_lists()
    integration_status = None
    api_keys_rows: list[dict] = []
    new_api_key = None
    if section == "api":
        from .. import api_keys as _api_keys
        try:
            new_api_key = request.session.pop("new_api_key", None)
        except AssertionError:
            new_api_key = None
        api_keys_rows = _api_keys.list_keys()
    kiosk_recent_punches: list[dict] = []
    kiosk_recent_variances: list[dict] = []
    timeclock_sync_status: dict | None = None
    available_schedules: list[dict] = []
    if section == "timeclock":
        from .. import db
        kiosk_recent_punches = db.query(
            "SELECT kpl.id, kpl.person_odoo_id, p.name AS person_name, "
            "kpl.action, kpl.wc_name, kpl.occurred_at, kpl.synced_to_odoo, "
            "kpl.sync_error, kpl.synced_at, kpl.odoo_attendance_id "
            "FROM timeclock_punches_log kpl "
            "LEFT JOIN people p ON p.odoo_id = kpl.person_odoo_id "
            "ORDER BY kpl.occurred_at DESC LIMIT 50"
        )
        kiosk_recent_variances = db.query(
            "SELECT ksv.id, ksv.person_odoo_id, p.name AS person_name, "
            "ksv.scheduled_wc_name, ksv.actual_wc_name, ksv.occurred_at, "
            "ksv.reviewed_at "
            "FROM timeclock_schedule_variances ksv "
            "LEFT JOIN people p ON p.odoo_id = ksv.person_odoo_id "
            "ORDER BY ksv.occurred_at DESC LIMIT 50"
        )
        status_rows = db.query(
            "SELECT "
            "COUNT(*) FILTER (WHERE synced_to_odoo = FALSE) AS unsynced, "
            "COUNT(*) AS total_7d, "
            "MAX(synced_at) AS last_sync_at, "
            "COUNT(*) FILTER (WHERE sync_error IS NOT NULL AND synced_to_odoo = FALSE) AS error_count "
            "FROM timeclock_punches_log "
            "WHERE occurred_at > now() - interval '7 days'"
        )
        timeclock_sync_status = status_rows[0] if status_rows else None
        from .. import odoo_client as _oc, work_schedule_store
        try:
            _configured = {o.resource_calendar_id for o in work_schedule_store.all_overrides()}
            available_schedules = [
                {"id": c["id"], "name": c.get("name") or f"Schedule {c['id']}"}
                for c in _oc.fetch_work_schedules()
                if c["id"] not in _configured
            ]
        except Exception:
            available_schedules = []
    time_off_settings: dict | None = None
    if section == "time_off":
        from .. import db
        import logging as _logging
        _settings_log = _logging.getLogger(__name__)
        # Primary source: the local leave_types_cache table populated by
        # the 60s poller. This guarantees the panel mirrors what the
        # kiosk picker sees, and stays usable during an Odoo outage. We
        # map holiday_status_id -> id for the template (which iterates
        # `t.id`/`t.name`).
        cache_rows = db.query(
            "SELECT holiday_status_id, name, request_unit, "
            "requires_allocation, color, active "
            "FROM leave_types_cache WHERE active = TRUE "
            "ORDER BY name"
        )
        leave_types = [
            {
                "id": r["holiday_status_id"],
                "name": r["name"],
                "request_unit": r["request_unit"],
                "requires_allocation": r["requires_allocation"],
                "color": r["color"],
                "active": r["active"],
            }
            for r in cache_rows
        ]
        # Fallback: if the table is empty (poller hasn't run yet on a
        # fresh box) AND Odoo is wired up, hit Odoo directly so the
        # panel isn't blank on first load.
        odoo_error: str | None = None
        odoo_error_class: str | None = None
        if not leave_types and _odoo_configured():
            try:
                leave_types = odoo_client.fetch_leave_types()
            except Exception as e:  # noqa: BLE001
                # Surface the error to the template so the user can
                # see *why* the panel is empty. We capture the exception
                # class name so the template can pick a class-specific
                # hint (config vs auth vs permission vs unknown) instead
                # of the old one-size-fits-all "lacks hr.leave.type read
                # permission" hint, which is misleading for auth failures.
                _settings_log.warning(
                    "Settings: Odoo fetch_leave_types failed: %s",
                    e, exc_info=True,
                )
                odoo_error = f"{type(e).__name__}: {e}"
                odoo_error_class = type(e).__name__
                leave_types = []
        time_off_settings = {
            "leave_types": leave_types,
            "hidden_ids": settings_store.get_hidden_leave_type_ids(),
            "odoo_configured": _odoo_configured(),
            "odoo_error": odoo_error,
            "odoo_error_class": odoo_error_class,
        }
    tv_displays_rows: list[dict] = []
    all_dashboards_for_picker: list[dict] = []
    if section == "tvs":
        from .. import tv_displays_store
        tv_displays_rows = tv_displays_store.list_displays()
        all_dashboards_for_picker = [
            {"kind": "vs_recycling", "ref": "", "name": "Recycling"},
            {"kind": "vs_new", "ref": "", "name": "New"},
            {
                "kind": "vs_recycling_leaderboard",
                "ref": "",
                "name": "Recycling-leaderboard",
            },
            {
                "kind": "vs_new_leaderboard",
                "ref": "",
                "name": "New-Leaderboard",
            },
            # (vs_work_centers dropped 2026-07-22 with the Work Centers page;
            # the TV dispatcher never had a branch for it, so a display
            # created with that kind would have 500'd.)
        ]
        for loc in staffing.LOCATIONS:
            all_dashboards_for_picker.append(
                {"kind": "wc", "ref": loc.name, "name": loc.name}
            )
    from .. import odoo_sync
    # TTL-checked sync so /settings self-heals after a Railway redeploy
    # where the ephemeral roster.json got reset to the legacy seed.
    odoo_sync.sync(force=False)
    productive_min = shift_config.productive_minutes_per_day()

    # Active roster (objects, not just names) so we can compute per-WC skill
    # levels and reserve flags for the Default People picker.
    roster = staffing.load_roster()
    active_people_objs = [p for p in roster if p.active]
    active_people = sorted((p.name for p in active_people_objs), key=str.lower)

    # The whole roster, not just the active half: work_center_rows offers only
    # active people as choices, but keeps an already-saved default in the pool
    # after they go inactive so the next save can't silently drop them.
    wc_rows = settings_context.work_center_rows(
        staffing.LOCATIONS, roster, work_centers_store.effective
    )
    default_auto_work_centers = _settings_default_auto_work_centers()
    group_rows = settings_context.group_summary(
        "group",
        all_names=work_centers_store.all_group_names,
        members=work_centers_store.members,
        auto_goal=work_centers_store.group_goal_auto,
        override_goal=work_centers_store.group_goal_override,
        effective_goal=work_centers_store.group_goal,
    )
    group_rows = settings_context.with_group_default_context(
        group_rows,
        active_people_objs,
        members_for=work_centers_store.members,
        required_skills_for=lambda loc: tuple(
            work_centers_store.required_skills(loc)
        ),
        defaults_for=work_centers_store.group_default_people,
        conflicts=work_centers_store.default_target_conflicts(),
    )
    dept_rows = settings_context.group_summary(
        "department",
        all_names=work_centers_store.all_group_names,
        members=work_centers_store.members,
        auto_goal=work_centers_store.group_goal_auto,
        override_goal=work_centers_store.group_goal_override,
        effective_goal=work_centers_store.group_goal,
    )
    sched = schedule_store.current()
    schedule_ctx = settings_context.schedule_context(sched, schedule_store.WEEKDAY_NAMES)
    from .. import work_schedule_store
    _work_schedules = work_schedule_store.all_overrides()
    work_schedules_ctx = settings_context.work_schedule_context(
        _work_schedules, _hours_display
    )
    from .. import rounding_system_store
    _systems = rounding_system_store.all_systems()
    rounding_systems_ctx = settings_context.rounding_system_context(_systems)
    _dept_map = rounding_system_store.department_map()
    department_rounding_ctx = settings_context.department_rounding_context(
        staffing.DEPARTMENT_ORDER, _dept_map
    )
    # Skill list comes directly from the `skills` table — Odoo's
    # Production + Supervisor skill types. Production first (alphabetical),
    # then Supervisor.
    from .. import db as _db
    _skill_rows = _db.query(
        "SELECT name FROM skills "
        "WHERE skill_type IN ('Production Skills', 'Supervisor Skills') "
        "ORDER BY skill_type, lower(name)"
    )
    skills_all = [r["name"] for r in _skill_rows]
    from .. import saturday_schedule_store
    _sat = saturday_schedule_store.current()
    saturday_schedule_ctx = settings_context.saturday_schedule_context(_sat)
    from .. import auto_lunch_settings
    _al = auto_lunch_settings.current()
    auto_lunch_ctx = settings_context.auto_lunch_context(_al)
    try:
        auto_lunch_history_ctx = settings_context.auto_lunch_history_context(
            auto_lunch_settings.recent_events(20)
        )
    except Exception:
        logging.warning("Auto-Lunch history unavailable", exc_info=True)
        auto_lunch_history_ctx = []
    # Forklift demand-advisor settings + a live forecast summary for the next
    # working day. Wrapped so the settings page never 500s if the forklift data
    # source (or DB) is unavailable.
    forklift_ctx: dict | None = None
    try:
        from .. import forklift_advisor, forklift_settings
        from .staffing import _next_working_day
        _fl = forklift_settings.current()
        _target_day = _next_working_day(settings_today)
        forklift_ctx = {
            "enabled": _fl.enabled,
            "include_loading_jockeying": _fl.include_loading_jockeying,
            "coldstart_calls_per_day": _fl.coldstart_calls_per_day,
            "target_day_label": _target_day.strftime("%a %b %-d"),
            "weekday_label": _target_day.strftime("%A"),
            # demand_summary carries both recommendations, the algorithm baseline
            # values (grey ticks), the current overrides (None=auto), the sorted
            # per-hour call counts (JS preview), and the slider ranges.
            **forklift_advisor.demand_summary(_target_day),
        }
        # GOAT-Score subsection context: the resolved score config (current
        # slider values), the algorithm defaults (grey ticks), the per-knob
        # overrides (None = auto), and one sample scored day for the live
        # worked example. Best-effort so a data hiccup just hides the panel.
        try:
            forklift_ctx.update(_forklift_score_ctx(_fl))
        except Exception:
            logging.debug("forklift GOAT-Score context unavailable", exc_info=True)
    except Exception:
        # Never 500 the whole settings page if the forklift data source / DB is
        # unreachable; the template guards on these keys being absent.
        forklift_ctx = {"enabled": True}
    pay_period = staffing_hours.current_pay_period_config()
    attendance_location = (
        _attendance_location_context() if section == "timeclock" else None
    )
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "wc_rows": wc_rows,
            "odoo_work_centers": odoo_work_centers,
            "odoo_work_centers_error": odoo_work_centers_error,
            "default_auto_work_centers": default_auto_work_centers,
            "skills_all": skills_all,
            "departments": work_centers_store.synced_departments(),
            "groups_all": work_centers_store.registered_groups(),
            "group_rows": group_rows,
            "dept_rows": dept_rows,
            "active_people": active_people,
            "saved": bool(saved),
            "defaults_error": defaults_error,
            "error": error,
            "active_section": section,
            "roster_filter_active": roster_filter_active,
            "roster_filter_inactive": roster_filter_inactive,
            "productive_minutes": productive_min,
            "schedule": schedule_ctx,
            "saturday_schedule": saturday_schedule_ctx,
            "rounding_systems": rounding_systems_ctx,
            "department_rounding": department_rounding_ctx,
            "auto_lunch": auto_lunch_ctx,
            "auto_lunch_history": auto_lunch_history_ctx,
            "work_schedules": work_schedules_ctx,
            "available_schedules": available_schedules,
            "integration_status": integration_status,
            "api_keys_rows": api_keys_rows,
            "new_api_key": new_api_key,
            "can_manage_api_keys": can_manage_api_keys,
            "tv_displays_rows": tv_displays_rows,
            "all_dashboards_for_picker": all_dashboards_for_picker,
            "wc_locations_for_picker": [{"name": loc.name} for loc in staffing.LOCATIONS],
            "kiosk_recent_punches": kiosk_recent_punches,
            "kiosk_recent_variances": kiosk_recent_variances,
            "timeclock_sync_status": timeclock_sync_status,
            "attendance_location": attendance_location,
            "can_manage_attendance_location": can_manage_api_keys,
            "staffing_hours_pay_period": {
                "anchor": pay_period.anchor.isoformat(),
                "cycle_days": pay_period.cycle_days,
            },
            "time_off_settings": time_off_settings,
            "forklift": forklift_ctx,
            "forklift_identities": forklift_identities_ctx,
            "identity_saved": bool(identity_saved),
            "identity_error": identity_error,
            "today": settings_today.isoformat(),
        },
    )


def _save_non_live_attendance_location(
    *,
    mode: str,
    cutover_at: datetime | None,
    selected_departments: set[str],
    departments,
    expected_config: attendance_location_policy.RolloutConfig,
) -> None:
    """CAS one Off/Shadow request behind the canonical rollout fence."""
    with db.cursor() as cur:
        attendance_location_policy.lock_rollout_decision_cur(cur)
        current = attendance_location_policy.get_rollout_config_strict()
        if current != expected_config:
            raise ValueError("rollout_save_superseded")
        now = attendance_location_policy._utc_now()  # noqa: SLF001
        live_active = attendance_location_policy._live_is_active(  # noqa: SLF001
            current,
            now,
        )
        if mode == "off" and live_active:
            raise ValueError("rollback_boundary_required")
        rollback_gate = None
        if mode == "shadow" and live_active:
            if cutover_at is None:
                raise ValueError("rollback_boundary_required")
            rollback_gate = current.live_gate
        config = attendance_location_policy.RolloutConfig(
            mode=mode,
            cutover_at=cutover_at,
            live_gate=rollback_gate,
        )
        attendance_location_policy.set_rollout_config(config, cur=cur)
        if mode == "shadow" and current.mode == "off":
            attendance_readiness.start_shadow_epoch_cur(cur, entered_at=now)
            attendance_readiness._record_rollout_audit_cur(  # noqa: SLF001
                cur,
                event_kind="shadow_started",
                rollout_mode="shadow",
                checked_at=now,
            )
        elif mode == "shadow" and live_active:
            attendance_readiness._record_rollout_audit_cur(  # noqa: SLF001
                cur,
                event_kind="rollback_scheduled",
                rollout_mode="shadow",
                cutover_at=cutover_at,
                checked_at=now,
                report_fingerprint=(
                    current.live_gate.report_digest
                    if current.live_gate is not None
                    else None
                ),
            )
        elif mode == "shadow" and current.mode == "live":
            attendance_readiness._record_rollout_audit_cur(  # noqa: SLF001
                cur,
                event_kind="live_cancelled",
                rollout_mode="shadow",
                cutover_at=current.cutover_at,
                checked_at=now,
                report_fingerprint=(
                    current.live_gate.report_digest
                    if current.live_gate is not None
                    else None
                ),
            )
        elif mode == "off":
            attendance_readiness.clear_shadow_evidence_cur(cur)
            attendance_readiness.clear_cutover_blocked_cur(cur)
            attendance_readiness._record_rollout_audit_cur(  # noqa: SLF001
                cur,
                event_kind="off",
                rollout_mode="off",
                checked_at=now,
            )
        for department_name in departments:
            attendance_location_policy.set_department_requirement(
                department_name,
                department_name in selected_departments,
                cur=cur,
            )


@router.post("/settings/attendance-location")
async def settings_save_attendance_location(request: Request):
    """Save rollout and department policy through the super-admin boundary."""
    if not auth.request_is_super_admin(request):
        return _api_settings_forbidden()
    form = await request.form()
    mode = (form.get("rollout_mode") or "").strip()
    if mode == "live":
        raw_cutover = (form.get("cutover_at") or "").strip()
        if not raw_cutover:
            return _attendance_location_error(
                request, "cutover_required", status_code=422
            )
        try:
            cutover_at = attendance_readiness.parse_local_cutover(raw_cutover)
        except ValueError as exc:
            return _attendance_location_error(request, str(exc), status_code=422)
        selected_departments = set(form.getlist("department_requires_work_center"))

        def _schedule_live():
            if "departments_present" in form:
                current = db.query(
                    "SELECT name, requires_work_center FROM departments "
                    "ORDER BY lower(name)"
                )
                if any(
                    bool(row["requires_work_center"])
                    != (row["name"] in selected_departments)
                    for row in current
                ):
                    raise ValueError("live_policy_save_separately")
            return attendance_readiness.schedule_live_cutover(
                cutover_at,
                now_utc=datetime.now(UTC),
            )

        try:
            await asyncio.to_thread(_schedule_live)
        except attendance_readiness.DecisionSourceChanged:
            return _attendance_location_error(
                request, "live_readiness_superseded", status_code=409
            )
        except ValueError as exc:
            return _attendance_location_error(request, str(exc), status_code=422)
        if (request.headers.get("accept") or "").startswith("application/json"):
            return JSONResponse({"ok": True})
        return RedirectResponse(
            url="/settings?saved=1&section=timeclock#attendance-location",
            status_code=303,
        )
    if mode not in ("off", "shadow"):
        return _attendance_location_error(
            request, "invalid_rollout_mode", status_code=422
        )
    try:
        expected_config = attendance_location_policy.get_rollout_config_strict()
    except ValueError:
        return _attendance_location_error(
            request, "rollout_config_invalid", status_code=422
        )

    cutover_at = None
    raw_cutover = (form.get("cutover_at") or "").strip()
    if raw_cutover:
        try:
            cutover_at = attendance_readiness.parse_local_cutover(raw_cutover)
        except ValueError as exc:
            return _attendance_location_error(
                request, str(exc), status_code=422
            )

    selected_departments = set(form.getlist("department_requires_work_center"))
    departments = (
        work_centers_store.synced_departments()
        if "departments_present" in form
        else []
    )

    def _save() -> None:
        _save_non_live_attendance_location(
            mode=mode,
            cutover_at=cutover_at,
            selected_departments=selected_departments,
            departments=departments,
            expected_config=expected_config,
        )

    try:
        await asyncio.to_thread(_save)
    except ValueError as exc:
        status_code = 409 if str(exc) == "rollout_save_superseded" else 422
        return _attendance_location_error(request, str(exc), status_code=status_code)
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": True})
    return RedirectResponse(
        url="/settings?saved=1&section=timeclock#attendance-location",
        status_code=303,
    )


@router.post("/settings/staffing-hours-pay-period")
async def settings_save_staffing_hours_pay_period(request: Request):
    """Save the local Staffing Hours payroll schedule without touching Odoo."""
    form = await request.form()
    try:
        staffing_hours.save_pay_period_config(
            form.get("anchor", ""), form.get("cycle_days", "")
        )
    except ValueError as exc:
        return RedirectResponse(
            f"/settings?section=timeclock&error={quote_plus(str(exc))}",
            status_code=303,
        )
    return RedirectResponse("/settings?saved=1&section=timeclock", status_code=303)


@router.post("/settings/api-keys")
async def settings_create_api_key(request: Request):
    if not _can_manage_api_keys(request):
        return _api_settings_forbidden()
    from .. import api_keys as _api_keys

    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    allowed_ips = [
        item.strip()
        for item in str(form.get("allowed_ips") or "").split(",")
        if item.strip()
    ]
    created_by = getattr(request.state, "user_upn", None) or "settings"
    key_id, token = await asyncio.to_thread(
        _api_keys.create_key,
        name,
        _parse_api_key_scopes(form),
        created_by,
        allowed_ips,
    )
    request.session["new_api_key"] = token
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": True, "id": key_id, "token": token})
    return RedirectResponse(url="/settings?saved=1&section=api", status_code=303)


@router.post("/settings/api-keys/{key_id}/revoke")
async def settings_revoke_api_key(key_id: int, request: Request):
    if not _can_manage_api_keys(request):
        return _api_settings_forbidden()
    from .. import api_keys as _api_keys

    await asyncio.to_thread(_api_keys.revoke_key, key_id)
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/settings?saved=1&section=api", status_code=303)


@router.post("/settings/schedule")
async def settings_save_schedule(request: Request):
    form = await request.form()

    def _work():
        current = schedule_store.current()
        shift_s = _parse_hhmm(form.get("shift_start")) or current.shift_start
        shift_e = _parse_hhmm(form.get("shift_end")) or current.shift_end
        if shift_e <= shift_s:
            shift_e = current.shift_end
        weekday_set = set()
        for i in range(7):
            if form.get(f"weekday_{i}"):
                weekday_set.add(i)
        if not weekday_set:
            weekday_set = set(current.work_weekdays)
        # Collect breaks from indexed form fields (start_N, end_N, name_N).
        breaks_new: list[schedule_store.Break] = []
        idx = 0
        while True:
            bs = _parse_hhmm(form.get(f"break_start_{idx}"))
            be = _parse_hhmm(form.get(f"break_end_{idx}"))
            bn = (form.get(f"break_name_{idx}") or "").strip() or "Break"
            if bs is None and be is None and not form.get(f"break_name_{idx}"):
                # No form fields at this index → stop scanning.
                if idx > 50:
                    break
                idx += 1
                if idx > 50:
                    break
                continue
            if bs and be and be > bs:
                breaks_new.append(schedule_store.Break(bs, be, bn[:40]))
            idx += 1
            if idx > 50:
                break
        breaks_new.sort(key=lambda b: b.start)
        replacement = schedule_store.Schedule(
            shift_start=shift_s,
            shift_end=shift_e,
            work_weekdays=frozenset(weekday_set),
            breaks=tuple(breaks_new),
        )
        with db.cursor() as cur:
            attendance_location_policy.lock_rollout_decision_cur(cur)
            attendance_location_policy.require_no_pending_boundary_cur(cur)
            schedule_store.save(replacement, cur=cur)
        schedule_store.reload()
        if (request.headers.get("accept") or "").startswith("application/json"):
            return JSONResponse({"ok": True})
        return RedirectResponse(url="/settings?saved=1&section=timeclock", status_code=303)

    try:
        return await asyncio.to_thread(_work)
    except ValueError as exc:
        if str(exc) != "attendance_rollout_boundary_pending":
            raise
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@router.post("/settings/saturday_schedule")
async def settings_save_saturday_schedule(request: Request):
    """Save the plant Saturday default (shift bookends + breaks). Mirrors
    settings_save_schedule: unparseable / end<=start values fall back to the
    current value rather than rejecting the submission."""
    from .. import saturday_schedule_store
    form = await request.form()

    def _work():
        current = saturday_schedule_store.current()
        shift_s = _parse_hhmm(form.get("shift_start")) or current.shift_start
        shift_e = _parse_hhmm(form.get("shift_end")) or current.shift_end
        if shift_e <= shift_s:
            shift_e = current.shift_end
        breaks_new: list[schedule_store.Break] = []
        idx = 0
        while idx <= 50:
            bs = _parse_hhmm(form.get(f"break_start_{idx}"))
            be = _parse_hhmm(form.get(f"break_end_{idx}"))
            bn = (form.get(f"break_name_{idx}") or "").strip() or "Break"
            if bs and be and be > bs:
                breaks_new.append(schedule_store.Break(bs, be, bn[:40]))
            idx += 1
        breaks_new.sort(key=lambda b: b.start)
        replacement = saturday_schedule_store.SaturdaySchedule(
            shift_start=shift_s,
            shift_end=shift_e,
            breaks=tuple(breaks_new),
        )
        with db.cursor() as cur:
            attendance_location_policy.lock_rollout_decision_cur(cur)
            attendance_location_policy.require_no_pending_boundary_cur(cur)
            saturday_schedule_store.save(replacement, cur=cur)
        saturday_schedule_store.reload()
        if (request.headers.get("accept") or "").startswith("application/json"):
            return JSONResponse({"ok": True})
        return RedirectResponse(url="/settings?saved=1&section=timeclock", status_code=303)

    try:
        return await asyncio.to_thread(_work)
    except ValueError as exc:
        if str(exc) != "attendance_rollout_boundary_pending":
            raise
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@router.post("/settings/rounding_system")
async def settings_save_rounding_system(request: Request):
    """Save the four windows for ONE rounding system (by id). Same 0..60 clamp
    as /settings/rounding."""
    from .. import rounding_system_store
    from ..rounding import RoundingSettings
    form = await request.form()
    try:
        system_id = int(form.get("system_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)

    def _work():
        rounding_system_store.save_system_windows(system_id, RoundingSettings(
            in_before_min=_clamp(form.get("in_before_min")),
            in_after_min=_clamp(form.get("in_after_min")),
            out_before_min=_clamp(form.get("out_before_min")),
            out_after_min=_clamp(form.get("out_after_min")),
        ))
        if (request.headers.get("accept") or "").startswith("application/json"):
            return JSONResponse({"ok": True})
        return RedirectResponse(url="/settings?saved=1&section=timeclock#rules", status_code=303)

    return await asyncio.to_thread(_work)


@router.post("/settings/rounding_system/add")
async def settings_add_rounding_system(request: Request):
    """Create a new (all-zero) rounding system by name."""
    from .. import rounding_system_store
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "bad name"}, status_code=400)
    await asyncio.to_thread(rounding_system_store.add_system, name)
    return RedirectResponse(url="/settings?saved=1&section=timeclock#rules", status_code=303)


@router.post("/settings/rounding_system/remove")
async def settings_remove_rounding_system(request: Request):
    """Delete a rounding system. Departments mapped to it fall back to no rounding."""
    from .. import rounding_system_store
    form = await request.form()
    try:
        system_id = int(form.get("system_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    await asyncio.to_thread(rounding_system_store.delete_system, system_id)
    return RedirectResponse(url="/settings?saved=1&section=timeclock#rules", status_code=303)


@router.post("/settings/department_rounding")
async def settings_save_department_rounding(request: Request):
    """Map one static department to a rounding system, or to no rounding
    (system_id 'none'/blank)."""
    from .. import rounding_system_store
    form = await request.form()
    department = (form.get("department") or "").strip()
    if not department:
        return JSONResponse({"ok": False, "error": "bad department"}, status_code=400)
    raw = form.get("system_id")
    if raw in (None, "", "none", "0"):
        system_id = None
    else:
        try:
            system_id = int(raw)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    await asyncio.to_thread(rounding_system_store.set_department_system, department, system_id)
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/settings?saved=1&section=timeclock#rules", status_code=303)


def _auto_lunch_mode_flags(mode, current_enabled: bool,
                           current_observe: bool) -> tuple[bool, bool]:
    """Map the 3-way Auto-Lunch mode selector to (enabled, observe_only).
    Unknown/blank mode keeps the current flags (defensive)."""
    m = (mode or "").strip().lower()
    if m == "live":
        return True, False
    if m == "observe":
        return True, True
    if m == "off":
        return False, True
    return current_enabled, current_observe


@router.post("/settings/auto_lunch")
async def settings_save_auto_lunch(request: Request):
    """Save the Auto-Lunch master mode + the flex rule. Takes effect
    immediately (the store updates its in-process cache), so no restart is
    needed. Unparseable / out-of-range flex values fall back to the current
    value rather than rejecting the submission."""
    from .. import auto_lunch_settings, inbox_log
    form = await request.form()
    actor_upn, actor_name = inbox_log.actor_from(request)

    def _num(raw, lo, hi, fallback, *, integer):
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return fallback
        v = max(lo, min(hi, v))
        return int(v) if integer else v

    def _work():
        current = auto_lunch_settings.current()
        enabled, observe_only = _auto_lunch_mode_flags(
            form.get("mode"), current.enabled, current.observe_only)
        updated = auto_lunch_settings.Settings(
            enabled=enabled,
            observe_only=observe_only,
            flex_after_hours=_num(form.get("flex_after_hours"), 0.0, 24.0,
                                  current.flex_after_hours, integer=False),
            flex_minutes=_num(form.get("flex_minutes"), 0, 120,
                              current.flex_minutes, integer=True),
        )
        auto_lunch_settings.save(
            updated, actor_upn=actor_upn, actor_name=actor_name
        )
        if (request.headers.get("accept") or "").startswith("application/json"):
            return JSONResponse({"ok": True})
        return RedirectResponse(url="/settings?saved=1&section=timeclock", status_code=303)

    return await asyncio.to_thread(_work)


def _score_cfg_dict(cfg) -> dict:
    """Flatten a forklift_score.ScoreConfig to a plain dict for the template."""
    return {
        "weights": dict(cfg.weights),
        "target_calls": cfg.target_calls,
        "ontime_floor": cfg.ontime_floor,
        "fast_secs": cfg.fast_secs,
        "slow_secs": cfg.slow_secs,
        "min_calls": cfg.min_calls,
    }


# A static fallback sample day for the live worked example when no eligible day
# is in the store yet (fresh install / data source down).
_SCORE_SAMPLE_FALLBACK = {
    "name": "Example", "day_label": "—", "calls": 25, "on_time": 24, "late": 1,
    "avg_ms": 45000, "utilization_pct": 60.0,
}


def _forklift_score_ctx(settings) -> dict:
    """Build the GOAT-Score subsection context: the resolved config (current
    values), the algorithm defaults (grey ticks), the per-knob overrides
    (None = auto), and one sample scored day for the live worked example."""
    from .. import forklift_score, forklift_settings, forklift_store

    # algo_throughput is a don't-care here: it only feeds the demand-advisor
    # knobs, not score_config(), which is all we read off the resolved settings.
    resolved = forklift_settings.resolve(settings, algo_throughput=0.0)
    cfg = resolved.score_config()
    algo = forklift_score.DEFAULT_SCORE_CONFIG

    # The most recent GOAT-eligible day (>= min_calls) makes the liveliest
    # example; fall back to a static sample if none is available.
    sample = dict(_SCORE_SAMPLE_FALLBACK)
    try:
        import datetime as _dt
        today = _dt.date.today()
        rows = forklift_store.driver_days_between(today - _dt.timedelta(days=120), today)
        eligible = [r for r in rows if (r.get("calls") or 0) >= cfg.min_calls]
        if eligible:
            r = max(eligible, key=lambda r: r["day"])
            sample = {
                "name": r.get("name") or r.get("driver_id") or "Driver",
                "day_label": r["day"].strftime("%b %-d") if hasattr(r["day"], "strftime") else str(r["day"]),
                "calls": int(r.get("calls") or 0),
                "on_time": int(r.get("on_time") or 0),
                "late": int(r.get("late") or 0),
                "avg_ms": int(r.get("avg_ms") or 0),
                "utilization_pct": float(r.get("utilization_pct") or 0.0),
            }
    except Exception:
        pass

    return {
        "score": _score_cfg_dict(cfg),
        "score_algo": _score_cfg_dict(algo),
        "score_overrides": {
            "calls": settings.score_w_calls,
            "ontime": settings.score_w_ontime,
            "speed": settings.score_w_speed,
            "util": settings.score_w_util,
            "target_calls": settings.score_target_calls,
            "ontime_floor": settings.score_ontime_floor,
            "fast_secs": settings.score_fast_secs,
            "slow_secs": settings.score_slow_secs,
            "min_calls": settings.score_min_calls,
        },
        "score_sample": sample,
    }


def _parse_forklift_overrides(form) -> forklift_settings.Settings:  # noqa: F821
    """Build a forklift_settings.Settings (nullable overrides) from POST form
    values. Each numeric knob: the literal string "auto" or blank → None (follow
    the algorithm); otherwise parse + clamp. "Reset all to algorithm" is just a
    submit with every numeric field = "auto". Utilization arrives as a PERCENT
    (slider range 40-100) → stored as a fraction. Checkboxes via truthiness."""
    from .. import forklift_settings

    def _override(key, lo, hi, *, integer, scale=1.0):
        raw = form.get(key)
        if raw is None or str(raw).strip().lower() in ("", "auto"):
            return None
        try:
            v = float(raw) * scale
        except (TypeError, ValueError):
            return None
        v = max(lo, min(hi, v))
        return int(round(v)) if integer else round(v, 4)

    coldstart = _override("coldstart_calls_per_day", 0.0, 100000.0, integer=False)
    return forklift_settings.Settings(
        enabled=bool(form.get("enabled")),
        throughput_override=_override("throughput", 5.0, 30.0, integer=False),
        utilization_override=_override("utilization_pct", 0.05, 1.0,
                                       integer=False, scale=0.01),
        plan_for_percentile_override=_override("plan_for", 0.5, 1.0, integer=False),
        history_samples_override=_override("history_samples", 2, 20, integer=True),
        include_loading_jockeying=bool(form.get("include_loading_jockeying")),
        coldstart_calls_per_day=coldstart if coldstart is not None else 0.0,
        # GOAT composite-score overrides (blank/"auto" -> None; clamp per knob).
        # Weights are stored raw (renormalized at compute time).
        score_w_calls=_override("score_w_calls", 0.0, 100.0, integer=False),
        score_w_ontime=_override("score_w_ontime", 0.0, 100.0, integer=False),
        score_w_speed=_override("score_w_speed", 0.0, 100.0, integer=False),
        score_w_util=_override("score_w_util", 0.0, 100.0, integer=False),
        score_target_calls=_override("score_target_calls", 1.0, 100.0, integer=False),
        score_ontime_floor=_override("score_ontime_floor", 0.0, 99.0, integer=False),
        score_fast_secs=_override("score_fast_secs", 1.0, 600.0, integer=False),
        score_slow_secs=_override("score_slow_secs", 1.0, 600.0, integer=False),
        score_min_calls=_override("score_min_calls", 1, 100, integer=True),
    )


@router.post("/settings/forklift")
async def settings_save_forklift(request: Request):
    """Save the Forklift demand-advisor settings (nullable overrides). Takes
    effect immediately (the store updates its in-process cache), so no restart is
    needed. "Reset all to algorithm" posts every numeric field as "auto"."""
    from .. import forklift_settings
    form = await request.form()

    def _work():
        forklift_settings.save(_parse_forklift_overrides(form))
        if (request.headers.get("accept") or "").startswith("application/json"):
            return JSONResponse({"ok": True})
        return RedirectResponse(url="/settings?saved=1&section=forklift", status_code=303)

    return await asyncio.to_thread(_work)


@router.post("/settings/work_schedule_rounding")
async def settings_save_work_schedule_rounding(request: Request):
    """Save the four rounding windows for ONE Odoo work schedule (by
    resource_calendar_id). Same 0..60 clamp as /settings/rounding; leaves the
    schedule's synced hours untouched."""
    from .. import work_schedule_store
    from ..rounding import RoundingSettings
    form = await request.form()
    try:
        cal_id = int(form.get("resource_calendar_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)

    def _work():
        work_schedule_store.save_rounding(cal_id, RoundingSettings(
            in_before_min=_clamp(form.get("in_before_min")),
            in_after_min=_clamp(form.get("in_after_min")),
            out_before_min=_clamp(form.get("out_before_min")),
            out_after_min=_clamp(form.get("out_after_min")),
        ))
        if (request.headers.get("accept") or "").startswith("application/json"):
            return JSONResponse({"ok": True})
        return RedirectResponse(url="/settings?saved=1&section=timeclock", status_code=303)

    return await asyncio.to_thread(_work)


@router.post("/settings/work_schedule_rounding/add")
async def settings_add_work_schedule(request: Request):
    """Configure a new per-schedule override for an Odoo work schedule and
    immediately sync its hours (best-effort)."""
    from .. import work_schedule_store, odoo_sync
    form = await request.form()
    try:
        cal_id = int(form.get("resource_calendar_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)

    def _work():
        work_schedule_store.create(cal_id)
        try:
            odoo_sync.refresh_work_schedule_hours(only_ids=[cal_id])
        except Exception:
            pass  # row exists; hours fill in on the next periodic sync
        return RedirectResponse(url="/settings?saved=1&section=timeclock#rules", status_code=303)

    return await asyncio.to_thread(_work)


@router.post("/settings/work_schedule_rounding/remove")
async def settings_remove_work_schedule(request: Request):
    """Drop a per-schedule override. Its employees revert to plant default."""
    from .. import work_schedule_store
    form = await request.form()
    try:
        cal_id = int(form.get("resource_calendar_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    await asyncio.to_thread(work_schedule_store.delete, cal_id)
    return RedirectResponse(url="/settings?saved=1&section=timeclock#rules", status_code=303)


@router.post("/settings/groups/add")
async def settings_add_group(request: Request):
    """Quick-add endpoint for the Groups section's Enter-to-add UX. Saves
    just the named group without touching WC rows, value-stream overrides,
    or schedule fields, so power-typing groups doesn't clobber other
    in-progress edits on the page."""
    form = await request.form()
    name = (form.get("name") or "").strip()[:80]
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)

    def _work():
        if name in set(work_centers_store.registered_groups()):
            return JSONResponse({"ok": False, "error": "already exists", "name": name}, status_code=409)
        work_centers_store.add_group(name)
        return JSONResponse({"ok": True, "name": name})

    return await asyncio.to_thread(_work)


@router.post("/settings/work_centers")
async def settings_save_work_centers(request: Request):
    """Bulk save: group registry edits, WC rows, group/VS overrides."""
    form = await request.form()

    def _work():
        mapping_fields_posted = any(
            _odoo_work_center_field(loc) in form
            for loc in staffing.LOCATIONS
        )
        mapping_updates: dict[str, dict] = {}
        if mapping_fields_posted:
            try:
                options = odoo_client.fetch_manufacturing_work_centers(force=True)
            except Exception:  # noqa: BLE001 - controlled service failure for the form
                message = (
                    "Odoo work centers are unavailable. Please try again when Odoo is "
                    "available; no settings were changed."
                )
                if (request.headers.get("accept") or "").startswith("application/json"):
                    return JSONResponse({"ok": False, "error": message}, status_code=503)
                query = urlencode({"section": "work_centers", "defaults_error": message})
                return RedirectResponse(url=f"/settings?{query}", status_code=303)
            try:
                mapping_updates = _odoo_work_center_updates(form, options)
            except InvalidOdooWorkCenterMapping as exc:
                if (request.headers.get("accept") or "").startswith("application/json"):
                    return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
                query = urlencode({"section": "work_centers", "defaults_error": str(exc)})
                return RedirectResponse(url=f"/settings?{query}", status_code=303)

        original_groups = list(work_centers_store.registered_groups())
        dirty_default_fields = set(form.getlist("default_people_dirty"))
        deleted_groups = {
            name for name in original_groups if form.get(f"group_delete__{name}")
        }
        renamed_groups = {
            name: (form.get(f"group_rename__{name}") or "").strip()[:80]
            for name in original_groups
            if name not in deleted_groups
        }
        renamed_groups = {
            old: new for old, new in renamed_groups.items() if new and new != old
        }
        new_group = (form.get("group_new") or "").strip()[:80]
        default_targets_changed = bool(
            dirty_default_fields or deleted_groups or renamed_groups or new_group
        )
        exact_defaults: dict[str, list[str]] = {}
        group_defaults: dict[str, list[str]] = {}
        if default_targets_changed:
            # Start from the live database state, not from every checkbox in
            # this page. The form autosaves as one large snapshot, and a tab
            # can stay open while a newer tab saves defaults. Only a picker
            # changed since this page's last successful save is authoritative.
            exact_defaults = work_centers_store._exact_defaults_map()
            for loc in staffing.LOCATIONS:
                key = loc.meter_id or f"name:{loc.name}"
                prefix = f"wc__{key}__"
                field = prefix + "default_people"
                exact_defaults.setdefault(loc.name, [])
                if (
                    field in dirty_default_fields
                    and (prefix + "default_people_present") in form
                ):
                    exact_defaults[loc.name] = form.getlist(field)
            group_defaults = work_centers_store.group_defaults_map()
            for name in original_groups:
                field = f"group_default_people__{name}"
                group_defaults.setdefault(name, [])
                if (
                    field in dirty_default_fields
                    and f"group_default_people_present__{name}" in form
                ):
                    group_defaults[name] = form.getlist(field)

            # Apply group registry edits to the in-memory target map before
            # any database writes, so duplicate defaults reject the whole set.
            for name in deleted_groups:
                group_defaults.pop(name, None)
            for old, new in renamed_groups.items():
                group_defaults[new] = group_defaults.pop(old, [])
            if new_group:
                group_defaults.setdefault(new_group, [])

        try:
            if default_targets_changed:
                work_centers_store._normalize_default_targets(
                    exact_by_center=exact_defaults,
                    group_by_name=group_defaults,
                )
        except work_centers_store.InvalidDefaultTargets as exc:
            if (request.headers.get("accept") or "").startswith("application/json"):
                return JSONResponse(
                    {"ok": False, "error": str(exc), "conflicts": exc.conflicts},
                    status_code=422,
                )
            query = urlencode(
                {
                    "section": "work_centers",
                    "defaults_error": str(exc),
                }
            )
            return RedirectResponse(url=f"/settings?{query}", status_code=303)

        # Persist mappings before every other Settings mutation. The store
        # checks live DB ownership while holding its mapping lock, so a stale
        # cache or concurrent request is a controlled validation failure, not
        # a late unique-index 500 after row settings have already saved.
        if mapping_fields_posted:
            try:
                work_centers_store.replace_odoo_work_center_mappings(mapping_updates)
            except work_centers_store.OdooWorkCenterMappingConflict as exc:
                if (request.headers.get("accept") or "").startswith("application/json"):
                    return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
                query = urlencode({"section": "work_centers", "defaults_error": str(exc)})
                return RedirectResponse(url=f"/settings?{query}", status_code=303)

        # 1. Group registry (delete, rename, add) — do first so WC save sees updated names.
        for name in original_groups:
            if form.get(f"group_delete__{name}"):
                work_centers_store.delete_group(name)
        for name in original_groups:
            if form.get(f"group_delete__{name}"):
                continue
            new_name = (form.get(f"group_rename__{name}") or "").strip()
            if new_name and new_name != name:
                work_centers_store.rename_group(name, new_name)
        if new_group:
            work_centers_store.add_group(new_group)

        # 2. Work-center rows.
        for loc in staffing.LOCATIONS:
            key = loc.meter_id or f"name:{loc.name}"
            prefix = f"wc__{key}__"
            updates: dict = {}
            for field in ("goal_per_day", "min_ops", "max_ops", "department"):
                name = prefix + field
                if name in form:
                    updates[field] = form.get(name) or ""
            # Multi-valued: required_skills (checkbox list). The hidden
            # required_skills_present marker (settings.html) lets us
            # distinguish "no checkboxes posted" (form didn't include this
            # section — leave DB alone) from "explicitly cleared" (form
            # did include it but no skills checked — save the empty list).
            if loc.name != "Truck Driver" and (prefix + "required_skills_present") in form:
                updates["required_skills"] = form.getlist(prefix + "required_skills")
            # Single-value Group select (stored internally as a 1-element list in `groups`).
            group_field = prefix + "group"
            if group_field in form:
                v = (form.get(group_field) or "").strip()
                updates["groups"] = [v] if v else []
            if updates:
                work_centers_store.save_one(loc, updates)
        # 3. Group + VS overrides.
        for kind in work_centers_store.GROUP_KINDS:
            for name in work_centers_store.all_group_names(kind):
                field = f"group_override__{kind}__{name}"
                if field in form:
                    work_centers_store.save_group_override(kind, name, form.get(field) or "")
        if default_targets_changed:
            work_centers_store.replace_default_targets(
                exact_by_center=exact_defaults,
                group_by_name=group_defaults,
            )
        if "default_auto_work_centers_present" in form:
            _save_default_auto_work_centers(form.getlist("default_auto_work_centers"))
        if (request.headers.get("accept") or "").startswith("application/json"):
            return JSONResponse({"ok": True})
        return RedirectResponse(url="/settings?saved=1&section=work_centers", status_code=303)

    return await asyncio.to_thread(_work)


@router.post("/settings")
async def settings_save(request: Request):
    """Save the Per-Group overrides. Work-center rows post to /settings/work_center/{key}."""
    form = await request.form()
    group_targets: dict[str, int] = {}
    # Keep the legacy station_targets dict empty; goals are now stored in the work_centers table.
    station_targets: dict[str, int] = {}
    for s in STATIONS:
        raw = (form.get(f"station_{s.meter_id}") or "").strip()
        if raw:
            try:
                station_targets[s.meter_id] = max(0, int(raw))
            except ValueError:
                pass
    for c in CATEGORIES:
        raw = (form.get(f"group_{c}") or "").strip()
        if raw:
            try:
                group_targets[c] = max(0, int(raw))
            except ValueError:
                pass
    await asyncio.to_thread(settings_store.save, station_targets, group_targets)
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@router.post("/api/settings/roster-filter/toggle")
async def roster_filter_toggle(request: Request):
    """Flip the `excluded` flag on a single person.

    Body (JSON): {odoo_id: int, excluded: bool}
    Side effects: UPDATE people SET excluded = $excluded WHERE odoo_id = $odoo_id;
    invalidate the roster cache so the next /staffing render picks up
    the change.
    """
    from .. import db, staffing
    body = await request.json()
    odoo_id_raw = body.get("odoo_id")
    excluded_raw = body.get("excluded")
    try:
        odoo_id = int(odoo_id_raw)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "odoo_id required (int)"}, status_code=400)
    if not isinstance(excluded_raw, bool):
        return JSONResponse({"ok": False, "error": "excluded must be true or false"}, status_code=400)

    def _work():
        db.execute(
            "UPDATE people SET excluded = %s WHERE odoo_id = %s",
            (excluded_raw, odoo_id),
        )
        staffing._invalidate_roster_cache()
        from .. import _http_cache
        _http_cache.invalidate_today_cache()
        # The skills matrix (stable bucket) renders the roster too.
        _http_cache.invalidate_stable_cache()
        return JSONResponse({"ok": True})

    return await asyncio.to_thread(_work)


# ---------- Time Off settings (2026-05-27) ----------


def _wants_json(request: Request) -> bool:
    return (request.headers.get("accept") or "").startswith("application/json")


@router.post("/api/settings/time-off/hidden-types")
async def time_off_set_hidden_types(request: Request):
    """Persist the list of leave-type ids that should be hidden from the
    kiosk picker. Posted as a multi-valued `ids` field (one per checked
    checkbox); absent => all visible."""
    form = await request.form()
    raw = form.getlist("ids")
    ids: list[int] = []
    for v in raw:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    await asyncio.to_thread(settings_store.set_hidden_leave_type_ids, ids)
    if _wants_json(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/settings?saved=1&section=time_off",
                            status_code=303)


@router.post("/api/settings/time-off/refresh-now")
def time_off_refresh_now(request: Request):
    """One-shot admin action — runs the Odoo leaves poller synchronously
    so the next page render sees a fresh local mirror. Swallows
    exceptions so the redirect still works when Odoo is down.

    Busts the in-process leave-types cache first so the poller's call
    to ``fetch_leave_types`` actually hits Odoo instead of returning the
    cached (possibly empty) list — that's the whole point of clicking
    Refresh.
    """
    from .. import odoo_client, time_off_sync
    # Force the next fetch_leave_types() to hit Odoo, not the 10-min
    # cache. If a previous call returned [] silently (e.g. due to an
    # earlier XML-RPC permission error), the cache would otherwise hold
    # that empty list and the Refresh button would be a no-op.
    odoo_client.invalidate_leave_types_cache()
    try:
        time_off_sync.poll_odoo_leaves()
    except Exception:
        pass
    if _wants_json(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/settings?saved=1&section=time_off",
                            status_code=303)


@router.get("/api/settings/time-off/diagnostics")
def time_off_diagnostics(request: Request):
    """Read-only diagnostic for the kiosk balance panel.

    Compares the local ``leave_types_cache`` against a *live* Odoo pull so
    we can see exactly which ``requires_allocation`` / ``request_unit`` the
    app holds vs. what Odoo reports. Built to diagnose the kiosk showing
    "No allocation tracked" while Odoo itself has correct balances — the
    smoking gun is a cached ``requires_allocation='no'`` for a type that is
    ``'yes'`` in Odoo, plus any row that would fail the cache CHECK
    constraint (which is what aborts the poller's refresh).

    No writes except busting the in-process leave-types cache so the live
    pull is genuinely live.
    """
    from .. import db, odoo_client

    allowed_units = {"day", "half_day", "hour"}
    allowed_req = {"yes", "no"}

    cache_rows = db.query(
        "SELECT holiday_status_id, name, request_unit, requires_allocation, "
        "active, last_pulled_at FROM leave_types_cache ORDER BY name"
    )
    cache = [
        {
            "id": r["holiday_status_id"],
            "name": r["name"],
            "request_unit": r["request_unit"],
            "requires_allocation": r["requires_allocation"],
            "active": r["active"],
            "last_pulled_at": str(r["last_pulled_at"]),
        }
        for r in cache_rows
    ]

    live = None
    live_error = None
    would_fail_check = []
    if _odoo_configured():
        try:
            odoo_client.invalidate_leave_types_cache()
            raw = odoo_client.fetch_leave_types()
            live = []
            for t in raw:
                live.append({
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "request_unit": t.get("request_unit"),
                    "requires_allocation": t.get("requires_allocation"),
                    "active": t.get("active"),
                    "color": t.get("color"),
                })
                reasons = []
                if t.get("request_unit") not in allowed_units:
                    reasons.append(
                        f"request_unit={t.get('request_unit')!r} not in "
                        f"{sorted(allowed_units)}")
                if t.get("requires_allocation") not in allowed_req:
                    reasons.append(
                        f"requires_allocation={t.get('requires_allocation')!r} "
                        f"not in {sorted(allowed_req)}")
                color = t.get("color")
                if isinstance(color, bool) or not isinstance(color, (int, type(None))):
                    reasons.append(f"color={color!r} not int/None")
                if reasons:
                    would_fail_check.append(
                        {"id": t.get("id"), "name": t.get("name"),
                         "reasons": reasons})
        except Exception as e:  # noqa: BLE001
            live_error = repr(e)
    else:
        live_error = "Odoo env not configured on this host"

    mismatches = []
    if live is not None:
        cache_by_id = {c["id"]: c for c in cache}
        for lt in live:
            ct = cache_by_id.get(lt["id"])
            if ct is None:
                mismatches.append(
                    {"id": lt["id"], "name": lt["name"],
                     "issue": "present in Odoo, missing from local cache"})
                continue
            mismatches.extend(
                {
                    "id": lt["id"],
                    "name": lt["name"],
                    "field": field,
                    "cache": ct.get(field),
                    "odoo": lt.get(field),
                }
                for field in ("requires_allocation", "request_unit", "active")
                if str(ct.get(field)) != str(lt.get(field))
            )

    # Optional per-employee balance probe. Pass ?person=<name substr> or
    # ?employee_odoo_id=<n> to see (a) what's cached in time_off_balances and
    # (b) a LIVE fetch_balances_for() with any Odoo error surfaced — this is
    # how we catch a renamed/changed Odoo field that throws and leaves the
    # balance cache empty (kiosk then shows "—").
    balances_probe = None
    person_q = request.query_params.get("person")
    emp_id_q = request.query_params.get("employee_odoo_id")
    odoo_id = None
    matched_name = None
    if emp_id_q:
        try:
            odoo_id = int(emp_id_q)
        except ValueError:
            odoo_id = None
    elif person_q:
        prow = db.query(
            "SELECT odoo_id, name FROM people WHERE name ILIKE %s "
            "AND odoo_id IS NOT NULL ORDER BY name LIMIT 1",
            (f"%{person_q}%",),
        )
        if prow:
            odoo_id = prow[0]["odoo_id"]
            matched_name = prow[0]["name"]
    if odoo_id is not None:
        cached = db.query(
            "SELECT holiday_status_id, unit, allocated_total, taken, pending, "
            "available, available_practical, last_pulled_at "
            "FROM time_off_balances WHERE person_odoo_id = %s "
            "ORDER BY holiday_status_id",
            (odoo_id,),
        )
        live_bal = None
        live_bal_error = None
        try:
            live_bal = odoo_client.fetch_balances_for(odoo_id)
        except Exception as e:  # noqa: BLE001
            live_bal_error = repr(e)
        balances_probe = {
            "employee_odoo_id": odoo_id,
            "matched_name": matched_name,
            "cached_balances": [
                {
                    "holiday_status_id": r["holiday_status_id"],
                    "unit": r["unit"],
                    "allocated_total": float(r["allocated_total"]),
                    "taken": float(r["taken"]),
                    "pending": float(r["pending"]),
                    "available": float(r["available"]),
                    "available_practical": float(r["available_practical"]),
                    "last_pulled_at": str(r["last_pulled_at"]),
                }
                for r in cached
            ],
            "live_fetch_balances_for": live_bal,
            "live_fetch_balances_error": live_bal_error,
        }

    return JSONResponse({
        "ok": True,
        "odoo_configured": _odoo_configured(),
        "cache": cache,
        "live": live,
        "live_error": live_error,
        "rows_that_would_fail_cache_check": would_fail_check,
        "cache_vs_odoo_mismatches": mismatches,
        "balances_probe": balances_probe,
    })
