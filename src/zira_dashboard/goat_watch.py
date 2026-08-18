"""GOAT Watch — live contenders plus persisted dashboard GOAT alerts.

Two surfaces:

  1. **Live contenders** (during the shift, after the final break has
     passed): `contenders_for_now(day, now)` returns every group whose
     leading WC is projecting >= 98 % of that group's GOAT record.

  2. **Persisted NEW GOAT alerts**: `active_alerts(today)` returns the
     visible (un-dismissed, within next-business-day window) rows.
     Finalized records are selected and delivered by `goat_notifications`;
     the compatibility bridge here can trigger its durable due-day worker
     while the dashboard is visited.

Detection threshold: live banner triggers at `>= 98 %` of the prior
GOAT record. Finalized GOAT records are durable, and the worker only
creates them when a result strictly beats the prior record.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, UTC

CONTENDER_THRESHOLD = 0.98  # 98 % of GOAT → show in live banner


# ---------- next-business-day helper ----------


def next_business_day(d: date) -> date:
    """Return the next date on which the plant operates.

    Uses the shared operational-day decision. Used to decide how long a NEW
    GOAT alert remains visible after the record was set.
    """
    from . import shift_config

    nxt = d
    fallback_work_days: frozenset[int] | None = None
    for _ in range(14):
        nxt += timedelta(days=1)
        try:
            if shift_config.is_workday(nxt):
                return nxt
        except Exception:
            if fallback_work_days is None:
                try:
                    fallback_work_days = shift_config.work_weekdays()
                except Exception:
                    # Defensive fallback: Mon–Fri
                    fallback_work_days = frozenset({0, 1, 2, 3, 4})
                # Preserve the old empty-calendar escape hatch.
                if not fallback_work_days:
                    return d + timedelta(days=1)
            if nxt.weekday() in fallback_work_days:
                return nxt
    return d + timedelta(days=1)


# ---------- live contenders ----------


@dataclass(frozen=True)
class Contender:
    group: str
    person: str
    wc: str
    units_today: int
    projected: int
    record_units: int
    record_holder: str
    record_day: date


def _final_break_passed(day: date, now_utc: datetime) -> bool:
    """True if the last mid-shift break on `day` has already ended (in SITE_TZ).

    Used to gate the live banner — no contender alerts before the
    final break wraps up. A "Cleanup" period scheduled to run right
    up to shift end is excluded: it ends at shift_end, which would
    otherwise gate the banner until end-of-shift and defeat the
    point of a *live* contender alert.
    """
    from . import shift_config
    try:
        breaks = shift_config.breaks_for(day) or ()
    except Exception:
        breaks = ()
    try:
        s_end = shift_config.shift_end_for(day)
    except Exception:
        s_end = None
    # Exclude end-of-shift wind-down "breaks" (e.g. Cleanup 15:15–15:30
    # when shift_end=15:30). Those aren't breaks operators come back from.
    if s_end is not None:
        real_breaks = [b for b in breaks if b.end < s_end]
    else:
        real_breaks = list(breaks)
    if not real_breaks:
        # No mid-shift breaks → no gate; treat as always passed.
        return True
    last_end: time = max(b.end for b in real_breaks)
    now_local = now_utc.astimezone(shift_config.SITE_TZ)
    if now_local.date() != day:
        # Different calendar day in local TZ — banner is irrelevant.
        return now_local.date() > day
    return now_local.time() >= last_end


def _group_names_today() -> list[str]:
    """Distinct group names across active LOCATIONS."""
    from . import staffing, work_centers_store
    groups: set[str] = set()
    for loc in staffing.LOCATIONS:
        for g in (work_centers_store.groups(loc) or []):
            if g:
                groups.add(g)
    return sorted(groups)


def _wc_units_today(wc_name: str, day: date) -> int:
    """Today's pallet count for one WC from the cached leaderboard."""
    from .deps import client
    from .leaderboard import station_total_for
    from .stations import Station
    from . import staffing
    loc = next((l for l in staffing.LOCATIONS if l.name == wc_name), None)
    if loc is None or not loc.meter_id:
        return 0
    station = Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)
    total = station_total_for(client, station, day)
    return int(total.units) if total is not None else 0


def _primary_operator(wc_name: str, day: date) -> str | None:
    """Schedule's primary (first-listed) operator for this WC on `day`."""
    from . import staffing
    try:
        sched = staffing.load_schedule(day)
    except Exception:
        return None
    ops = sched.assignments.get(wc_name) or []
    return ops[0] if ops else None


def _shift_elapsed_fraction(day: date, now_utc: datetime) -> float:
    """Fraction of `day`'s productive shift elapsed at `now_utc`.

    Returns 0.0 before the shift starts and 1.0 after it ends.
    """
    from . import shift_config
    try:
        full = shift_config.productive_minutes_for(day)
    except Exception:
        full = 0
    if full <= 0:
        return 0.0
    try:
        elapsed = shift_config.shift_elapsed_minutes(day, now_utc)
    except Exception:
        return 0.0
    return max(0.0, min(1.0, elapsed / full))


def contenders_for_now(day: date, now_utc: datetime) -> list[Contender]:
    """One row per group whose leading WC projects >= 98 % of GOAT.

    Returns [] before the final break of the day or when no group has
    a leader meeting the threshold. Each row names the WC's primary
    operator and projected end-of-day total at current pace.
    """
    if not _final_break_passed(day, now_utc):
        return []
    elapsed_frac = _shift_elapsed_fraction(day, now_utc)
    if elapsed_frac <= 0:
        return []

    from . import awards, work_centers_store

    out: list[Contender] = []
    for group_name in _group_names_today():
        goat = awards.goat(group_name)
        if not goat:
            continue
        record_units = int(goat.get("units") or 0)
        if record_units <= 0:
            continue
        threshold = record_units * CONTENDER_THRESHOLD

        # Find the leading WC in this group today.
        best: Contender | None = None
        for loc in work_centers_store.members("group", group_name):
            units_today = _wc_units_today(loc.name, day)
            if units_today <= 0:
                continue
            projected = int(round(units_today / elapsed_frac))
            if projected < threshold:
                continue
            person = _primary_operator(loc.name, day)
            if not person:
                continue
            candidate = Contender(
                group=group_name,
                person=person,
                wc=loc.name,
                units_today=units_today,
                projected=projected,
                record_units=record_units,
                record_holder=str(goat.get("name") or ""),
                record_day=goat.get("day"),  # date or None
            )
            if best is None or candidate.projected > best.projected:
                best = candidate
        if best is not None:
            out.append(best)
    return out


# ---------- persisted NEW GOAT alerts ----------

def _zira_client():
    from .deps import client
    return client


def finalize_day(day: date) -> list[dict]:
    from . import goat_notifications
    return goat_notifications.finalize_day(day, _zira_client())


def maybe_finalize_today(today: date | None = None) -> None:
    from . import goat_notifications
    goat_notifications.run_due(datetime.now(UTC), _zira_client())


def active_alerts(today: date) -> list[dict]:
    """Visible (un-dismissed, within next-business-day window) alert rows.

    A row is visible when:
      - `dismissed_at` IS NULL
      - `today <= next_business_day(achieved_day)`

    Calls `maybe_finalize_today` first so a newly-finished shift's
    records are persisted before the banner renders.
    """
    maybe_finalize_today(today)
    from . import awards, db, goat_categories, production_history
    try:
        rows = db.query(
            "SELECT id, achieved_day, category_key, group_name, person, wc_name, units, "
            "       prior_record_units, prior_record_holder, prior_record_day "
            "FROM goat_alerts "
            "WHERE dismissed_at IS NULL "
            "ORDER BY achieved_day DESC, id DESC"
        )
    except Exception:
        return []
    out: list[dict] = []
    readiness_records_by_day: dict[date, list[dict]] = {}
    for r in rows:
        ach = r["achieved_day"]
        if not goat_categories.has_category_key(r.get("category_key")):
            continue
        if not ach <= today <= next_business_day(ach):
            continue
        category = goat_categories.category_for_key(r["category_key"])
        if category.minimum_data_days > 1:
            try:
                if ach not in readiness_records_by_day:
                    readiness_records_by_day[ach] = production_history.daily_records(
                        awards.AWARDS_DATA_FLOOR,
                        ach,
                    )
                if not goat_categories.is_goat_ready(
                    category,
                    readiness_records_by_day[ach],
                ):
                    continue
            except Exception:
                continue
        out.append(dict(r))
    return out


def dismiss_alert(alert_id: int) -> bool:
    """Mark a single alert dismissed. Returns True on success."""
    from . import db
    try:
        db.execute(
            "UPDATE goat_alerts SET dismissed_at = now() WHERE id = %s "
            "AND dismissed_at IS NULL",
            (alert_id,),
        )
        return True
    except Exception:
        return False
