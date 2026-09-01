"""Typed rollout and department policy for Odoo attendance locations.

This module is the single boundary for the operational rollout setting.  The
strict matcher is not enabled by this task: callers can store ``off`` or
``shadow``, while a future readiness check must supply a fresh ``LiveGate``
before ``live`` can be persisted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from . import app_settings, db, shift_config


Mode = Literal["off", "shadow", "live"]
MatchState = Literal["legacy", "pending", "strict"]

_SETTING_KEY = "odoo_attendance_location"
_LIVE_GATE_MAX_AGE = timedelta(minutes=5)
_NUMBERED_DEPARTMENT_PREFIX = re.compile(r"^\s*[0-9]+\s*")


@dataclass(frozen=True)
class LiveGate:
    checked_at: datetime
    report_digest: str
    activated_at: datetime | None


@dataclass(frozen=True)
class RolloutConfig:
    mode: Mode
    cutover_at: datetime | None
    live_gate: LiveGate | None


def lock_rollout_decision_cur(cur) -> None:
    """Take the shared rollout write fence in the one canonical order."""
    cur.execute("LOCK TABLE app_settings, attendance_strict_days IN SHARE ROW EXCLUSIVE MODE")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _parse_datetime(value: object, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid datetime")
    parsed = datetime.fromisoformat(value)
    if not _is_aware(parsed):
        raise ValueError("datetime must include a timezone")
    return parsed


def _parse_config(raw: object) -> RolloutConfig:
    if not isinstance(raw, dict):
        raise ValueError("invalid rollout config")
    mode = raw.get("mode")
    if mode not in ("off", "shadow", "live"):
        raise ValueError("invalid rollout mode")
    cutover_at = _parse_datetime(raw.get("cutover_at"), optional=True)
    raw_gate = raw.get("live_gate")
    live_gate = None
    if raw_gate is not None:
        if not isinstance(raw_gate, dict):
            raise ValueError("invalid live gate")
        digest = raw_gate.get("report_digest")
        if not isinstance(digest, str) or not digest:
            raise ValueError("invalid live gate digest")
        checked_at = _parse_datetime(raw_gate.get("checked_at"))
        activated_at = _parse_datetime(raw_gate.get("activated_at"), optional=True)
        assert checked_at is not None
        live_gate = LiveGate(
            checked_at=checked_at,
            report_digest=digest,
            activated_at=activated_at,
        )
    if mode == "live" and (cutover_at is None or live_gate is None):
        raise ValueError("incomplete live rollout")
    return RolloutConfig(mode=mode, cutover_at=cutover_at, live_gate=live_gate)


def get_rollout_config() -> RolloutConfig:
    """Return the stored rollout config, safely falling back to ``off``."""
    try:
        return _parse_config(app_settings.get_setting(_SETTING_KEY))
    except (TypeError, ValueError):
        return RolloutConfig(mode="off", cutover_at=None, live_gate=None)


def get_rollout_config_strict() -> RolloutConfig:
    """Read rollout state for a mutation; malformed or missing state is fatal."""
    raw = app_settings.get_setting(_SETTING_KEY)
    if raw is None:
        return RolloutConfig(mode="off", cutover_at=None, live_gate=None)
    return _parse_config(raw)


def get_rollout_config_cur(cur) -> RolloutConfig:
    """Read rollout state from the caller's fenced transaction, fail closed."""
    cur.execute(
        "SELECT value FROM app_settings WHERE key = %s",
        (_SETTING_KEY,),
    )
    row = cur.fetchone()
    if row is None:
        return RolloutConfig(mode="off", cutover_at=None, live_gate=None)
    try:
        return _parse_config(row["value"])
    except (TypeError, ValueError) as exc:
        raise ValueError("rollout_config_invalid") from exc


def _validate_cutover(cutover_at: datetime | None, *, cur=None) -> None:
    if cutover_at is None or not _is_aware(cutover_at):
        raise ValueError("cutover_timezone_required")
    local = cutover_at.astimezone(shift_config.SITE_TZ)
    shift = shift_config.snapshot_for(local.date(), cur=cur)
    if local.time().replace(tzinfo=None) != shift.shift_start:
        raise ValueError("cutover_boundary_required")
    if not shift.is_workday:
        raise ValueError("cutover_workday_required")


def _validate_live_gate(gate: LiveGate | None) -> None:
    if gate is None or not _is_aware(gate.checked_at):
        raise ValueError("live_readiness_required")
    if not gate.report_digest:
        raise ValueError("live_readiness_required")
    if gate.activated_at is not None and not _is_aware(gate.activated_at):
        raise ValueError("live_readiness_required")
    age = _utc_now() - gate.checked_at.astimezone(UTC)
    if age < timedelta(0) or age > _LIVE_GATE_MAX_AGE:
        raise ValueError("live_readiness_required")


def set_rollout_config(config: RolloutConfig, *, cur=None) -> None:
    """Validate and persist one typed rollout config.

    A live save is accepted only with the short-lived readiness proof that
    Task 13 will create.  This keeps partial deployments operationally off or
    shadow without preventing that later task from using this boundary.
    """
    if config.mode not in ("off", "shadow", "live"):
        raise ValueError("invalid_rollout_mode")
    if config.mode == "off" and live_is_active():
        raise ValueError("rollback_boundary_required")
    if config.cutover_at is not None:
        _validate_cutover(config.cutover_at, cur=cur)
    if config.mode == "live":
        _validate_cutover(config.cutover_at, cur=cur)
        _validate_live_gate(config.live_gate)
    if (
        config.mode == "shadow"
        and config.live_gate is not None
        and config.live_gate.activated_at is not None
    ):
        if not _is_aware(config.live_gate.activated_at):
            raise ValueError("live_readiness_required")
        _validate_cutover(config.cutover_at, cur=cur)
        assert config.cutover_at is not None
        if config.cutover_at.astimezone(UTC) <= _utc_now():
            raise ValueError("rollback_future_boundary_required")
    value = {
        "mode": config.mode,
        "cutover_at": (config.cutover_at.isoformat() if config.cutover_at is not None else None),
        "live_gate": None,
    }
    if config.live_gate is not None:
        value["live_gate"] = {
            "checked_at": config.live_gate.checked_at.isoformat(),
            "report_digest": config.live_gate.report_digest,
            "activated_at": (
                config.live_gate.activated_at.isoformat()
                if config.live_gate.activated_at is not None
                else None
            ),
        }
    app_settings.set_setting(_SETTING_KEY, value, cur=cur)


def _aware_utc(value: datetime | None) -> datetime:
    resolved = value or _utc_now()
    if not _is_aware(resolved):
        raise ValueError("now_utc must be timezone-aware")
    return resolved.astimezone(UTC)


def _live_is_active(config: RolloutConfig, now_utc: datetime) -> bool:
    gate = config.live_gate
    if gate is None or gate.activated_at is None or gate.activated_at.astimezone(UTC) > now_utc:
        return False
    if config.mode == "live":
        return True
    # A scheduled Shadow rollback changes ownership only when its fenced
    # settlement transaction succeeds. This prevents a later schedule edit
    # from turning an obsolete timestamp into a silent mid-shift handoff.
    return config.mode == "shadow" and config.cutover_at is not None


def live_is_active(*, now_utc: datetime | None = None) -> bool:
    """True only after a live rollout's boundary activation is recorded."""
    return _live_is_active(get_rollout_config(), _aware_utc(now_utc))


def live_is_active_cur(*, cur, now_utc: datetime | None = None) -> bool:
    """Resolve active ownership from the caller's rollout-fenced transaction."""
    return _live_is_active(
        get_rollout_config_cur(cur),
        _aware_utc(now_utc),
    )


def require_no_pending_boundary_cur(cur) -> None:
    """Reject schedule mutations that would silently move a saved boundary."""
    config = get_rollout_config_cur(cur)
    gate = config.live_gate
    if (
        config.cutover_at is not None
        and gate is not None
        and (
            (config.mode == "live" and gate.activated_at is None)
            or (config.mode == "shadow" and gate.activated_at is not None)
        )
    ):
        raise ValueError("attendance_rollout_boundary_pending")


def strict_days() -> set[date]:
    """Return days whose production has already used the strict matcher."""
    return {row["day"] for row in db.query("SELECT day FROM attendance_strict_days")}


def day_is_strict(day: date) -> bool:
    """Keep recorded strict days strict, including after a future rollback."""
    if day in strict_days():
        return True
    config = get_rollout_config()
    now = _utc_now()
    if config.cutover_at is None or not _live_is_active(config, now):
        return False
    cutover_day = config.cutover_at.astimezone(shift_config.SITE_TZ).date()
    if config.mode == "shadow":
        assert config.live_gate is not None
        assert config.live_gate.activated_at is not None
        activated_day = config.live_gate.activated_at.astimezone(shift_config.SITE_TZ).date()
        return activated_day <= day and (
            day < cutover_day or now >= config.cutover_at.astimezone(UTC)
        )
    return day >= cutover_day


def match_state_for_day(day: date, *, now_utc: datetime | None = None) -> MatchState:
    """Resolve whether recomputation should use, wait for, or avoid strict matching."""
    if day in strict_days():
        return "strict"
    config = get_rollout_config()
    return _match_state_from_config(day, config=config, now_utc=now_utc)


def _match_state_from_config(
    day: date,
    *,
    config: RolloutConfig,
    now_utc: datetime | None,
) -> MatchState:
    if config.cutover_at is None:
        return "legacy"
    now = _aware_utc(now_utc)
    cutover = config.cutover_at.astimezone(UTC)
    cutover_day = config.cutover_at.astimezone(shift_config.SITE_TZ).date()
    if config.mode == "shadow":
        gate = config.live_gate
        activated_day = (
            gate.activated_at.astimezone(shift_config.SITE_TZ).date()
            if gate is not None and gate.activated_at is not None
            else None
        )
        rollback_due_but_unsettled = now >= cutover
        if _live_is_active(config, now) and activated_day is not None and (
            activated_day <= day < cutover_day
            or (rollback_due_but_unsettled and day >= activated_day)
        ):
            return "strict"
        return "legacy"
    if config.mode != "live":
        return "legacy"
    if day < cutover_day or now < cutover:
        return "legacy"
    if _live_is_active(config, now):
        return "strict"
    return "pending"


def match_state_for_day_cur(day: date, *, cur, now_utc: datetime | None = None) -> MatchState:
    """Resolve match state from rows locked by the caller's transaction."""
    cur.execute(
        "SELECT 1 FROM attendance_strict_days WHERE day = %s",
        (day,),
    )
    if cur.fetchone() is not None:
        return "strict"
    return _match_state_from_config(
        day,
        config=get_rollout_config_cur(cur),
        now_utc=now_utc,
    )


def _clean_department_name(department_name: str | None) -> str | None:
    if not department_name:
        return None
    cleaned = _NUMBERED_DEPARTMENT_PREFIX.sub("", department_name).strip()
    return cleaned or None


def _normalized_department_name(department_name: str | None) -> str:
    return (_clean_department_name(department_name) or "").lower()


def effective_department_name(
    attendance_department_name: str | None,
    employee_department_name: str | None,
) -> str | None:
    """Attendance department wins; employee department is fallback-only."""
    return _clean_department_name(attendance_department_name) or _clean_department_name(
        employee_department_name
    )


def default_department_requires_work_center(department_name: str | None) -> bool:
    """Default for a department that has not received an explicit choice."""
    return _normalized_department_name(department_name) not in {
        "maintenance",
        "transportation",
        "supervisor",
    }


def department_requires_work_center(department_name: str | None) -> bool:
    """Return the saved department rule, with safe defaults before DB sync."""
    if not department_name:
        return True
    rows = db.query(
        "SELECT requires_work_center FROM departments WHERE name = %s",
        (department_name,),
    )
    if rows:
        return bool(rows[0]["requires_work_center"])
    return default_department_requires_work_center(department_name)


def set_department_requirement(department_name: str, required: bool, *, cur=None) -> None:
    """Save an explicit administrator choice that bootstrap will not replace."""
    name = department_name.strip()
    if not name:
        raise ValueError("department_required")
    sql = (
        "UPDATE departments SET requires_work_center = %s, "
        "requires_work_center_explicit = TRUE WHERE name = %s"
    )
    params = (bool(required), name)
    if cur is not None:
        cur.execute(sql, params)
    else:
        db.execute(sql, params)
