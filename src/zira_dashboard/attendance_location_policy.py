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
        activated_at = _parse_datetime(
            raw_gate.get("activated_at"), optional=True
        )
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


def _validate_cutover(cutover_at: datetime | None) -> None:
    if cutover_at is None or not _is_aware(cutover_at):
        raise ValueError("cutover_timezone_required")
    local = cutover_at.astimezone(shift_config.SITE_TZ)
    if local.time().replace(tzinfo=None) != shift_config.shift_start_for(local.date()):
        raise ValueError("cutover_boundary_required")


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
    if config.cutover_at is not None:
        _validate_cutover(config.cutover_at)
    if config.mode == "live":
        _validate_cutover(config.cutover_at)
        _validate_live_gate(config.live_gate)
    value = {
        "mode": config.mode,
        "cutover_at": (
            config.cutover_at.isoformat() if config.cutover_at is not None else None
        ),
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
    return bool(
        config.mode == "live"
        and gate is not None
        and gate.activated_at is not None
        and gate.activated_at.astimezone(UTC) <= now_utc
    )


def live_is_active(*, now_utc: datetime | None = None) -> bool:
    """True only after a live rollout's boundary activation is recorded."""
    return _live_is_active(get_rollout_config(), _aware_utc(now_utc))


def strict_days() -> set[date]:
    """Return days whose production has already used the strict matcher."""
    return {row["day"] for row in db.query("SELECT day FROM attendance_strict_days")}


def day_is_strict(day: date) -> bool:
    """Keep recorded strict days strict, including after a future rollback."""
    if day in strict_days():
        return True
    config = get_rollout_config()
    if config.cutover_at is None or not _live_is_active(config, _utc_now()):
        return False
    return day >= config.cutover_at.astimezone(shift_config.SITE_TZ).date()


def match_state_for_day(
    day: date, *, now_utc: datetime | None = None
) -> MatchState:
    """Resolve whether recomputation should use, wait for, or avoid strict matching."""
    if day in strict_days():
        return "strict"
    config = get_rollout_config()
    if config.mode != "live" or config.cutover_at is None:
        return "legacy"
    now = _aware_utc(now_utc)
    cutover = config.cutover_at.astimezone(UTC)
    cutover_day = config.cutover_at.astimezone(shift_config.SITE_TZ).date()
    if day < cutover_day or now < cutover:
        return "legacy"
    if _live_is_active(config, now):
        return "strict"
    return "pending"


def _normalized_department_name(department_name: str | None) -> str:
    if not department_name:
        return ""
    return _NUMBERED_DEPARTMENT_PREFIX.sub("", department_name).strip().lower()


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
    return _normalized_department_name(department_name) not in {
        "maintenance",
        "supervisor",
    }


def set_department_requirement(department_name: str, required: bool) -> None:
    """Save an explicit administrator choice that bootstrap will not replace."""
    name = department_name.strip()
    if not name:
        raise ValueError("department_required")
    db.execute(
        "UPDATE departments SET requires_work_center = %s, "
        "requires_work_center_explicit = TRUE WHERE name = %s",
        (bool(required), name),
    )
