"""GOAT Watch — Recycling-VS banner that surfaces operators on pace
to beat their group's all-time single-day record, and the persisted
"NEW GOAT" alert once someone actually does.

Two surfaces:

  1. **Live contenders** (during the shift, after the final break has
     passed): `contenders_for_now(day, now)` returns every group whose
     leading WC is projecting >= 98 % of that group's GOAT record.

  2. **Finalized NEW GOAT alerts**: at shift end, `finalize_day(day)`
     writes a `goat_alerts` row for every WC-day that strictly beat
     its group's record. `active_alerts(today)` returns the visible
     (un-dismissed, within next_business_day window) rows.

Detection threshold: live banner triggers at `>= 98 %` of the prior
GOAT record. The NEW GOAT alert is strict — fires only when actual
units > prior record (ties keep the existing holder).

The "credited operator" is the schedule's primary assignment for the
WC that day. The "credited WC" is the work-center itself — group
GOATs are derived per WC-day, the highest of which represents the
group's all-time best.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

CONTENDER_THRESHOLD = 0.98  # 98 % of GOAT → show in live banner


# ---------- next-business-day helper ----------

def next_business_day(d: date) -> date:
    """Return the next date on which the plant operates.

    Skips weekends (and any non-working weekdays per
    `shift_config.work_weekdays()`). Used to decide how long a NEW
    GOAT alert remains visible after the record was set.
    """
    from . import shift_config
    try:
        work_days = shift_config.work_weekdays()
    except Exception:
        # Defensive fallback: Mon–Fri
        work_days = frozenset({0, 1, 2, 3, 4})
    nxt = d + timedelta(days=1)
    # Guard against an empty work_weekdays set so we don't loop forever.
    if not work_days:
        return nxt
    while nxt.weekday() not in work_days:
        nxt += timedelta(days=1)
    return nxt


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
    """True if the last break on `day` has already ended (in SITE_TZ).

    Used to gate the live banner — no contender alerts before the
    final break wraps up.
    """
    from . import shift_config
    try:
        breaks = shift_config.breaks_for(day) or ()
    except Exception:
        breaks = ()
    if not breaks:
        # No breaks defined → treat as "always after" (no gate).
        return True
    last_end: time = max(b.end for b in breaks)
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
    from .leaderboard import cached_leaderboard
    from .stations import Station
    from . import staffing
    loc = next((l for l in staffing.LOCATIONS if l.name == wc_name), None)
    if loc is None or not loc.meter_id:
        return 0
    stations = [Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)]
    try:
        results = cached_leaderboard(client, stations, day)
    except Exception:
        return 0
    for r in results:
        if r.station.name == wc_name:
            return int(r.units)
    return 0


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

def finalize_day(day: date) -> list[dict]:
    """Idempotent end-of-day sweep.

    For each group, find every WC whose `day` total strictly beat the
    prior group GOAT record. Write one `goat_alerts` row per match.
    Skips WCs already finalized (UNIQUE (achieved_day, group_name,
    wc_name)).

    Returns the list of rows written (or [] when nothing qualified or
    the table is unavailable).
    """
    from . import awards, db, work_centers_store

    written: list[dict] = []
    for group_name in _group_names_today():
        goat = awards.goat(group_name)
        # `goat()` includes today's data once it lands in production_history.
        # If today's data is what made the new record, `goat.units` IS the
        # new value — so for the "prior record" we look at the second-best.
        # Simpler: compute today's WC totals first, compare each against
        # `awards.goat()` BEFORE today existed by snapshotting the record
        # at finalization time. But that requires history slicing. For now
        # use the simpler rule: a goat_alerts row is written when today's
        # WC total > the SECOND-best person-day in the group's all-time
        # history. That's the prior record from today's perspective.
        prior_record = _prior_record_excluding_day(group_name, day)
        prior_units = int(prior_record["units"]) if prior_record else 0
        if prior_units <= 0:
            continue

        for loc in work_centers_store.members("group", group_name):
            units_today = _wc_units_today(loc.name, day)
            if units_today <= prior_units:
                continue
            person = _primary_operator(loc.name, day)
            if not person:
                continue
            row = {
                "achieved_day": day,
                "group_name": group_name,
                "person": person,
                "wc_name": loc.name,
                "units": units_today,
                "prior_record_units": prior_units,
                "prior_record_holder": str(prior_record.get("name") or ""),
                "prior_record_day": prior_record.get("day"),
            }
            try:
                db.execute(
                    "INSERT INTO goat_alerts "
                    "  (achieved_day, group_name, person, wc_name, units, "
                    "   prior_record_units, prior_record_holder, prior_record_day) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (achieved_day, group_name, wc_name) DO NOTHING",
                    (
                        row["achieved_day"],
                        row["group_name"],
                        row["person"],
                        row["wc_name"],
                        row["units"],
                        row["prior_record_units"],
                        row["prior_record_holder"],
                        row["prior_record_day"],
                    ),
                )
                written.append(row)
            except Exception:
                # Don't let one bad insert kill the rest of the sweep.
                continue
    return written


def _prior_record_excluding_day(group_name: str, day: date) -> dict | None:
    """All-time best person-day in `group_name`, EXCLUDING `day`.

    Used at finalize-time to determine the record-to-beat. Without
    excluding `day`, today's own data would already be reflected in
    `awards.goat()` and we'd never detect a beat.
    """
    from . import awards
    try:
        rows = awards.person_days_in_group(group_name, date(1970, 1, 1), date(9999, 12, 31))
    except Exception:
        return None
    rows = [r for r in rows if r.get("day") != day]
    if not rows:
        return None
    rows.sort(key=lambda r: (-int(r["units"]), r["day"], r["name"]))
    top = rows[0]
    return {"name": top["name"], "day": top["day"], "units": int(top["units"])}


# In-process "already finalized this day" memo — avoids running the
# end-of-shift sweep on every render after the shift ends. Resets on
# worker restart; the DB-level UNIQUE constraint makes re-runs safe.
_FINALIZED_DAYS: set[date] = set()


def maybe_finalize_today(today: date) -> None:
    """Lazily run `finalize_day(today)` once per process after the
    shift has ended. Idempotent: in-memory flag prevents repeat
    sweeps, and the goat_alerts UNIQUE constraint prevents duplicates
    across worker restarts.
    """
    if today in _FINALIZED_DAYS:
        return
    from . import shift_config
    try:
        now_local = datetime.now(timezone.utc).astimezone(shift_config.SITE_TZ)
        shift_end = shift_config.shift_end_for(today)
    except Exception:
        return
    if now_local.date() < today:
        return
    if now_local.date() == today and now_local.time() < shift_end:
        return
    finalize_day(today)
    _FINALIZED_DAYS.add(today)


def active_alerts(today: date) -> list[dict]:
    """Visible (un-dismissed, within next-business-day window) alert rows.

    A row is visible when:
      - `dismissed_at` IS NULL
      - `today <= next_business_day(achieved_day)`

    Calls `maybe_finalize_today` first so a newly-finished shift's
    records are persisted before the banner renders.
    """
    maybe_finalize_today(today)
    from . import db
    try:
        rows = db.query(
            "SELECT id, achieved_day, group_name, person, wc_name, units, "
            "       prior_record_units, prior_record_holder, prior_record_day "
            "FROM goat_alerts "
            "WHERE dismissed_at IS NULL "
            "ORDER BY achieved_day DESC, id DESC"
        )
    except Exception:
        return []
    out: list[dict] = []
    for r in rows:
        ach = r["achieved_day"]
        if today <= next_business_day(ach):
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
