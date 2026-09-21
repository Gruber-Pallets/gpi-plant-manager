"""Explicit personal-access policy. New routes are closed to non-admins by default.

TV displays and bearer integrations are authenticated separately in auth.py.
The context variable partitions render caches, including synchronous workers.
"""
from __future__ import annotations

from contextvars import ContextVar
from starlette.routing import compile_path

current_role: ContextVar[str] = ContextVar("access_role", default="system")
ROLE_LEVEL = {"visitor": 0, "manager": 1, "hr": 2, "admin": 3}

# Snapshot of reviewed routes, not prefix grants. A new endpoint requires an
# explicit policy entry and its own review even inside an existing router.
ROUTES = {
    'visitor': (
        ('GET', '/api/layout/{page}'),
        ('GET', '/api/widget/{page}/{widget_id}'),
        ('GET', '/changelog'),
        ('GET', '/changelog/latest'),
        ('GET', '/changelog.md'),
        ('GET', '/'),
        ('GET', '/work-centers'),
        ('GET', '/api/leaderboard'),
        ('GET', '/recycling'),
        ('GET', '/new'),
        ('GET', '/staffing/forklift'),
        ('GET', '/staffing/leaderboards'),
        ('GET', '/api/staffing/leaderboards/person-days'),
        ('GET', '/new-leaderboard'),
        ('GET', '/people-performance'),
        ('GET', '/people-performance/rows'),
        ('GET', '/people-performance/warnings/{warning_key_value}'),
        ('GET', '/recycling-leaderboard'),
        ('GET', '/trophies'),
        ('GET', '/wc/{slug}'),
        ('GET', '/operator'),
    ),
    'manager': (
        ('GET', '/exceptions'),
        ('GET', '/api/exceptions'),
        ('GET', '/api/exceptions/summary'),
        ('POST', '/api/exceptions/breakdown/transfer'),
        ('POST', '/api/exceptions/breakdown/snooze'),
        ('POST', '/api/exceptions/breakdown/dismiss'),
        ('POST', '/api/exceptions/breakdown/report'),
        ('GET', '/api/feedback/submitters'),
        ('POST', '/timeclock/feedback'),
        ('POST', '/feedback'),
        ('GET', '/api/feedback/mine'),
        ('POST', '/api/goat-alerts/{alert_id}/dismiss'),
        ('POST', '/api/late-report/declare-absent'),
        ('POST', '/api/late-report/forgot-punch-in'),
        ('POST', '/api/late-report/snooze'),
        ('POST', '/api/late-report/running-late'),
        ('POST', '/api/late-report/undo-absent'),
        ('POST', '/staffing/leaderboards/order'),
        ('GET', '/api/missing-wc'),
        ('POST', '/missing-wc/assign'),
        ('POST', '/missing-wc/dismiss'),
        ('POST', '/staffing/past/unpublish'),
        ('POST', '/staffing/past/delete'),
        ('GET', '/staffing/past'),
        ('GET', '/staffing/people'),
        ('GET', '/staffing/people/{name}'),
        ('POST', '/api/rotations/validate-current'),
        ('POST', '/api/rotations/preferences'),
        ('POST', '/api/rotations/training-blocks'),
        ('POST', '/api/rotations/training-blocks/{block_id}'),
        ('POST', '/api/rotations/training-blocks/{block_id}/complete'),
        ('POST', '/api/rotations/training-blocks/{block_id}/pause'),
        ('POST', '/api/rotations/training-blocks/{block_id}/resume'),
        ('POST', '/api/rotations/training-blocks/{block_id}/end'),
        ('POST', '/api/rotations/auto-work-centers'),
        ('POST', '/api/rotations/rebuild'),
        ('POST', '/api/staffing/saturday-recruiting/activate'),
        ('POST', '/api/staffing/saturday-recruiting/activate-from-schedule'),
        ('POST', '/api/staffing/saturday-recruiting/openings'),
        ('POST', '/api/staffing/saturday-recruiting/commitments/{person_id}/cancel'),
        ('POST', '/api/staffing/saturday-recruiting/cancel'),
        ('POST', '/staffing/share-to-slack'),
        ('GET', '/staffing/skills'),
        ('POST', '/staffing/skills'),
        ('POST', '/staffing/skills/cell'),
        ('POST', '/staffing/skills/refresh'),
        ('POST', '/staffing/skills/views'),
        ('PUT', '/staffing/skills/views/{name}'),
        ('DELETE', '/staffing/skills/views/default'),
        ('DELETE', '/staffing/skills/views/{name}'),
        ('POST', '/staffing/skills/views/{name}/default'),
        ('GET', '/staffing'),
        ('POST', '/staffing'),
        ('GET', '/staffing/live'),
        ('POST', '/staffing/mark-printed'),
        ('POST', '/staffing/hours'),
        ('POST', '/api/staffing/attribute'),
        ('DELETE', '/api/staffing/attribute/{attribution_id}'),
        ('POST', '/api/staffing/attribute-with-testing'),
        ('POST', '/api/staffing/transfer/undo'),
        ('GET', '/api/assignments-todo'),
        ('GET', '/api/late-report'),
        ('POST', '/api/staffing/saturday-availability'),
        ('POST', '/api/staffing/clear-partial'),
        ('POST', '/api/staffing/clear-testing-day'),
        ('POST', '/api/staffing/restore-partial'),
    ),
    'hr': (
        ('POST', '/api/exceptions/absence-pto/{request_id}/approve'),
        ('POST', '/api/exceptions/absence-pto/{request_id}/deny'),
        ('POST', '/api/exceptions/absence-pto/{request_id}/handled'),
        ('GET', '/auto-salaried/flags'),
        ('POST', '/auto-salaried/flags/{flag_id}/resolve'),
        ('POST', '/api/exceptions/attendance-correction/preview'),
        ('POST', '/api/exceptions/attendance-correction/apply'),
        ('GET', '/api/exceptions/attendance-correction/{job_id}'),
        ('GET', '/api/exceptions/archive'),
        ('POST', '/api/exceptions/time-off/{request_id}/approve'),
        ('POST', '/api/exceptions/time-off/{request_id}/refuse'),
        ('POST', '/api/exceptions/undo/{event_id}'),
        ('POST', '/api/exceptions/quick-punch/ack'),
        ('GET', '/api/missed-punch-out'),
        ('POST', '/missed-punch-out/correct'),
        ('GET', '/staffing/people/{name}/acknowledgements'),
        ('POST', '/staffing/people/add'),
        ('POST', '/staffing/people/delete'),
        ('POST', '/api/staffing/time-off/{request_id}/edit'),
        ('POST', '/api/staffing/time-off/{request_id}/cancel'),
        ('GET', '/staffing/hours'),
        ('GET', '/staffing/time-off'),
        ('GET', '/staffing/time-off/approvals'),
        ('GET', '/timeclock'),
        ('GET', '/timeclock/start/{person_id}'),
        ('GET', '/timeclock/dashboard/{token}'),
        ('GET', '/timeclock/notifications/{token}'),
        ('POST', '/timeclock/notifications/ack/{token}'),
        ('GET', '/timeclock/celebration/{token}'),
        ('POST', '/timeclock/celebration/ack/{token}'),
        ('GET', '/timeclock/pick-wc/{token}'),
        ('POST', '/timeclock/clock-in/{token}'),
        ('POST', '/timeclock/clock-in/confirm/{token}'),
        ('POST', '/timeclock/clock-out/{token}'),
        ('POST', '/timeclock/transfer/{token}'),
        ('GET', '/timeclock/time-off/past-absence/{token}'),
        ('POST', '/timeclock/time-off/past-absence/{token}/{day}'),
        ('GET', '/timeclock/time-off/past-absence/{token}/requests/{request_id}'),
        ('GET', '/timeclock/saturday/{token}'),
        ('GET', '/timeclock/saturday/partial/{token}'),
        ('POST', '/timeclock/saturday/partial/{token}'),
        ('POST', '/timeclock/saturday/confirm/{token}'),
        ('POST', '/timeclock/saturday/commit/{token}'),
        ('POST', '/timeclock/saturday/decline/{token}'),
        ('POST', '/timeclock/saturday/later/{token}'),
        ('POST', '/timeclock/saturday/cancel/{token}'),
        ('GET', '/timeclock/time-off/{token}'),
        ('GET', '/timeclock/time-off/request/{token}'),
        ('GET', '/timeclock/time-off/request/{token}/details'),
        ('POST', '/timeclock/time-off/request/{token}/submit'),
        ('GET', '/timeclock/time-off/mine/{token}'),
        ('GET', '/timeclock/time-off/mine/{token}/{rid}'),
        ('POST', '/timeclock/time-off/mine/{token}/{rid}/cancel'),
        ('GET', '/timeclock/time-off/mine/{token}/{rid}/edit'),
        ('POST', '/timeclock/time-off/mine/{token}/{rid}/edit'),
        ('GET', '/timeclock/time-off/calendar/{token}'),
        ('GET', '/timeclock/whos-out'),
    ),
    'admin': (
        ('GET', '/admin/person-state'),
        ('GET', '/admin/data-status'),
        ('GET', '/admin/ribbon-vs-widget'),
        ('GET', '/admin/pph-debug'),
        ('GET', '/admin/zira-probe'),
        ('GET', '/admin/zira-backfill'),
        ('POST', '/admin/precompute-run'),
        ('GET', '/admin/page-usage'),
        ('GET', '/admin/devices'),
        ('POST', '/admin/devices'),
        ('POST', '/admin/devices/{token_id}/revoke'),
        ('POST', '/api/layout/{page}'),
        ('POST', '/api/widget/{page}/{widget_id}'),
        ('DELETE', '/api/widget/{page}/{widget_id}'),
        ('POST', '/api/exceptions/attendance-unmapped-location/dismiss'),
        ('GET', '/admin/feedback'),
        ('POST', '/admin/feedback/{feedback_id}/status'),
        ('POST', '/settings/forklift-identities'),
        ('POST', '/staffing/leaderboards/wc/{name}/inactive'),
        ('POST', '/staffing/leaderboards/wc/{name}/active'),
        ('GET', '/settings'),
        ('POST', '/settings/attendance-location'),
        ('POST', '/settings/staffing-hours-pay-period'),
        ('POST', '/settings/api-keys'),
        ('POST', '/settings/api-keys/{key_id}/revoke'),
        ('POST', '/settings/schedule'),
        ('POST', '/settings/saturday_schedule'),
        ('POST', '/settings/rounding_system'),
        ('POST', '/settings/rounding_system/add'),
        ('POST', '/settings/rounding_system/remove'),
        ('POST', '/settings/department_rounding'),
        ('POST', '/settings/auto_lunch'),
        ('POST', '/settings/quick_punch_fix'),
        ('POST', '/settings/forklift'),
        ('POST', '/settings/work_schedule_rounding'),
        ('POST', '/settings/work_schedule_rounding/add'),
        ('POST', '/settings/work_schedule_rounding/remove'),
        ('POST', '/settings/groups/add'),
        ('POST', '/settings/work_centers'),
        ('POST', '/settings'),
        ('POST', '/api/settings/roster-filter/toggle'),
        ('POST', '/api/settings/time-off/hidden-types'),
        ('POST', '/api/settings/time-off/refresh-now'),
        ('GET', '/api/settings/time-off/diagnostics'),
        ('POST', '/staffing/skills/automation/{group}'),
        ('POST', '/api/awards/override'),
        ('POST', '/api/tv-displays'),
        ('POST', '/api/tv-displays/{display_id}/theme'),
        ('DELETE', '/api/tv-displays/{display_id}'),
        ('GET', '/admin/users'),
        ('POST', '/admin/users'),
    ),
}
# Independent kiosk grant: no desktop role inheritance and no prefix bypass.
TIMECLOCK_ROUTES = (
    ('GET', '/timeclock'),
    ('GET', '/timeclock/start/{person_id}'),
    ('GET', '/timeclock/dashboard/{token}'),
    ('GET', '/timeclock/notifications/{token}'),
    ('POST', '/timeclock/notifications/ack/{token}'),
    ('GET', '/timeclock/celebration/{token}'),
    ('POST', '/timeclock/celebration/ack/{token}'),
    ('GET', '/timeclock/pick-wc/{token}'),
    ('POST', '/timeclock/clock-in/{token}'),
    ('POST', '/timeclock/clock-in/confirm/{token}'),
    ('POST', '/timeclock/clock-out/{token}'),
    ('POST', '/timeclock/transfer/{token}'),
    ('GET', '/timeclock/time-off/past-absence/{token}'),
    ('POST', '/timeclock/time-off/past-absence/{token}/{day}'),
    ('GET', '/timeclock/time-off/past-absence/{token}/requests/{request_id}'),
    ('GET', '/timeclock/saturday/{token}'),
    ('GET', '/timeclock/saturday/partial/{token}'),
    ('POST', '/timeclock/saturday/partial/{token}'),
    ('POST', '/timeclock/saturday/confirm/{token}'),
    ('POST', '/timeclock/saturday/commit/{token}'),
    ('POST', '/timeclock/saturday/decline/{token}'),
    ('POST', '/timeclock/saturday/later/{token}'),
    ('POST', '/timeclock/saturday/cancel/{token}'),
    ('GET', '/timeclock/time-off/{token}'),
    ('GET', '/timeclock/time-off/request/{token}'),
    ('GET', '/timeclock/time-off/request/{token}/details'),
    ('POST', '/timeclock/time-off/request/{token}/submit'),
    ('GET', '/timeclock/time-off/mine/{token}'),
    ('GET', '/timeclock/time-off/mine/{token}/{rid}'),
    ('POST', '/timeclock/time-off/mine/{token}/{rid}/cancel'),
    ('GET', '/timeclock/time-off/mine/{token}/{rid}/edit'),
    ('POST', '/timeclock/time-off/mine/{token}/{rid}/edit'),
    ('GET', '/timeclock/time-off/calendar/{token}'),
    ('GET', '/timeclock/whos-out'),
    ('POST', '/timeclock/feedback'),
    ('GET', '/api/feedback/submitters'),
    ('GET', '/api/feedback/mine'),
)
_TIMECLOCK_COMPILED = tuple(
    (method, compile_path(path)[0]) for method, path in TIMECLOCK_ROUTES
)
_COMPILED = tuple(
    (method, compile_path(path)[0], ROLE_LEVEL[role])
    for role, entries in ROUTES.items() for method, path in entries
)


def allowed(role: str, method: str, path: str) -> bool:
    if role not in ROLE_LEVEL and role != "timeclock":
        return False
    # Never interpret alternative spellings as a broader route grant.
    if "\\" in path or any(segment in {".", ".."} for segment in path.split("/")):
        return False
    if role == "admin":
        return True
    method = "GET" if method == "HEAD" else method
    if role == "timeclock":
        return any(verb == method and regex.fullmatch(path) for verb, regex in _TIMECLOCK_COMPILED)
    matches = [level for verb, regex, level in _COMPILED if verb == method and regex.fullmatch(path)]
    return bool(matches) and ROLE_LEVEL[role] >= max(matches)


def role_for(request=None) -> str:
    return getattr(getattr(request, "state", None), "user_role", current_role.get()) if request is not None else current_role.get()


def can_hr(request=None) -> bool:
    return role_for(request) in {"admin", "hr", "system"}


def can_admin(request=None) -> bool:
    return role_for(request) in {"admin", "system"}


def can_operate(request=None) -> bool:
    return role_for(request) in {"admin", "hr", "manager", "system"}


def staffing_entries(entries: list[dict], *, role: str) -> list[dict]:
    if role in {"hr", "admin", "system"}:
        return entries
    # Positive field list: leave type/pay treatment never leaves this boundary.
    safe = []
    fields = {"name", "hours", "time_range", "derived", "manual_absent", "pending", "timing_label"}
    for entry in entries:
        row = {key: value for key, value in entry.items() if key in fields}
        row["editable"] = False
        if row.get("manual_absent"):
            row["timing_label"] = "Absent"
        safe.append(row)
    return safe


_OPERATIONAL_INBOX = frozenset({
    "assignments", "plant_schedule", "saturday_recruiting", "late",
    "missing_wc", "unexpected_workers", "breakdown",
})


def inbox_snapshot(snapshot: dict, *, role: str) -> dict:
    if role in {"hr", "admin", "system"}:
        return snapshot
    from .exception_inbox import _queue_from_sections
    sections = [section for section in snapshot.get("sections", []) if section["id"] in _OPERATIONAL_INBOX]
    queue = _queue_from_sections(sections)
    # Rebuild instead of copying: unknown top-level fields are not public.
    result = {key: snapshot[key] for key in (
        "today", "generated_at", "work_centers", "people", "breakdown_transfer_enabled", "attendance_location_mode"
    ) if key in snapshot}
    result.update(
        sections=sections, queue=queue, total=sum(int(s["count"]) for s in sections),
        urgent_total=sum(r.get("priority") == "urgent" for r in queue),
        follow_up_total=sum(r.get("priority") == "muted" for r in queue),
        source_errors=[],
    )
    return result


def inbox_summary() -> dict:
    from . import exception_inbox
    role = current_role.get()
    if role in {"admin", "hr", "system"}:
        return exception_inbox.build_summary()
    if role == "visitor":
        return {"total": 0, "urgent_total": 0, "source_errors": []}
    snapshot = inbox_snapshot(exception_inbox.build_snapshot(), role=role)
    return {key: snapshot[key] for key in ("total", "urgent_total", "follow_up_total", "source_errors")}
