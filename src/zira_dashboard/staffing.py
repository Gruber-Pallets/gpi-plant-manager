"""Staffing data layer: locations, roster, daily schedules.

Storage is Postgres (people, person_skills, schedules, schedule_assignments).
Roster + skills are synced from Odoo; schedules are edited in the app. The
Plant Scheduler CSV is a read-only first-run seed for default assignments.
"""

from __future__ import annotations

import csv
import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, UTC
from pathlib import Path
from threading import RLock
from typing import Mapping
from uuid import uuid4

# Skill categories (columns in Dale's Google Sheet skill matrix).
SKILLS: tuple[str, ...] = (
    "Repair",
    "Dismantler",
    "Trim Saw",
    "Woodpecker",
    "Junior",
    "Master Recycler",
    "Hand Build",
    "Chop/Notch",
    "Forklift: Load/Jockey",
    "Forklift: Tablets",
    "Mechanic",
)

LOADING_JOCKEYING_REQUIRED_SKILLS: tuple[str, ...] = (
    "Loading",
    "CPUs/VDOs",
    "Trailer Jockeying",
)

# Scheduling groups retain the names used by work-center configuration and
# persisted rotation preferences. Odoo calls the corresponding production
# skill "Dismantle", so resolve the group to that source-of-truth skill when
# reading qualifications or creating a training block.
_SCHEDULING_GROUP_SKILL_NAMES = {"Dismantler": "Dismantle"}


def skill_name_for_scheduling_group(group: str) -> str:
    """Return the matrix/Odoo skill that supplies a scheduling group's level."""
    return _SCHEDULING_GROUP_SKILL_NAMES.get(group, group)


def scheduling_group_for_skill(skill: str) -> str:
    """Return the persisted scheduling-group key for a matrix/Odoo skill."""
    for group, skill_name in _SCHEDULING_GROUP_SKILL_NAMES.items():
        if skill == skill_name:
            return group
    return skill


@dataclass(frozen=True)
class Location:
    name: str                               # unique display name
    skill: str                              # default required skill (legacy; required_skills is the source of truth)
    bay: str                                # grouping for display
    department: str                         # Recycled / New / Supervisor / Maintenance
    meter_id: str | None                    # Zira station ID if mapped, else None
    min_ops: int = 1                        # minimum operators required to run
    max_ops: int | None = 1                 # max operators; None = unlimited
    required_skills: tuple[str, ...] = ()   # if empty, defaults to (skill,)
    note: str | None = None                 # user-editable free-form note shown under name


# 22 work centers, in Plant-Scheduler-sheet order.
LOCATIONS: tuple[Location, ...] = (
    # Bay 1
    Location("Repair 1", "Repair", "Bay 1", "Recycled", "40721"),
    Location("Repair 2", "Repair", "Bay 1", "Recycled", "40720"),
    Location("Repair 3", "Repair", "Bay 1", "Recycled", "40719"),
    # Bay 2
    Location("Dismantler 4", "Dismantler", "Bay 2", "Recycled", "42715"),
    Location("Dismantler 3", "Dismantler", "Bay 2", "Recycled", "42714"),
    # Bay 3
    Location("Dismantler 2", "Dismantler", "Bay 3", "Recycled", "42713"),
    Location("Dismantler 1", "Dismantler", "Bay 3", "Recycled", "42711"),
    # Bay 4
    Location("Trim Saw 1", "Trim Saw", "Bay 4", "Recycled", "43286", min_ops=2, max_ops=2),
    Location("Master Recycler", "Master Recycler", "Bay 4", "Recycled", None),
    # Bay 5
    Location("Repair 4", "Repair", "Bay 5", "Recycled", None),
    Location("Repair 5", "Repair", "Bay 5", "Recycled", None),
    Location("Hand Build #2", "Hand Build", "Bay 5", "New", None, min_ops=2, max_ops=2),
    # Bay 6
    Location("Hand Build #1", "Hand Build", "Bay 6", "New", None, min_ops=2, max_ops=2),
    # Bay 14
    Location("Chop/Notch", "Chop/Notch", "Bay 14", "New", None),
    Location("Big Build #1", "Hand Build", "Bay 14", "New", None, min_ops=2, max_ops=2),
    # Bay 16
    Location("Woodpecker #1", "Woodpecker", "Bay 16", "New", None, min_ops=1, max_ops=3),
    Location("Junior #1", "Junior", "Bay 16", "New", None),
    # Bay 17
    Location("Junior #2", "Junior", "Bay 17", "New", "42345"),
    Location("Junior #3", "Junior", "Bay 17", "New", None),
    # Forklift
    Location(
        "Loading/Jockeying",
        "Forklift: Load/Jockey",
        "Forklift",
        "Supervisor",
        None,
        required_skills=LOADING_JOCKEYING_REQUIRED_SKILLS,
    ),
    Location("Tablets", "Tablets", "Forklift", "Supervisor", None, min_ops=1, max_ops=None),
    # Maint.
    Location("Work Orders", "Mechanic", "Maint.", "Maintenance", None, min_ops=1, max_ops=None),
    # Transportation
    Location("Truck Driver", "CDL (Automatics) Certified", "Transportation", "Transportation", None, min_ops=1, max_ops=None),
)


def required_skills_for(loc: Location) -> tuple[str, ...]:
    """Returns the required skills for this work center. Falls back to the single
    `skill` field when `required_skills` is unset, for backward compatibility."""
    return loc.required_skills if loc.required_skills else (loc.skill,)


def location_by_name(name: str) -> Location | None:
    """Return the configured work center with this exact display name."""
    return next((loc for loc in LOCATIONS if loc.name == name), None)


@dataclass(frozen=True)
class SchedulingPreferenceTarget:
    key: str
    label: str
    centers: tuple[str, ...]
    required_skills: tuple[str, ...]


def scheduling_preference_targets() -> tuple[SchedulingPreferenceTarget, ...]:
    single_skill_centers: dict[str, list[str]] = {}
    for loc in LOCATIONS:
        required = required_skills_for(loc)
        if len(required) == 1:
            single_skill_centers.setdefault(required[0], []).append(loc.name)
    grouped_skills = {
        skill for skill, centers in single_skill_centers.items() if len(centers) > 1
    }
    # Trim Saw is a Recycled rotation group even though it currently has one
    # center. Its group key drives the scheduler's pairing and training rules.
    grouped_skills.add("Trim Saw")

    targets = []
    emitted_groups = set()
    for loc in LOCATIONS:
        required = required_skills_for(loc)
        if len(required) == 1 and required[0] in grouped_skills:
            skill = required[0]
            if skill not in emitted_groups:
                targets.append(SchedulingPreferenceTarget(
                    skill, skill, tuple(single_skill_centers[skill]), (skill,)
                ))
                emitted_groups.add(skill)
        else:
            targets.append(SchedulingPreferenceTarget(
                loc.name, loc.name, (loc.name,), required
            ))
    return tuple(targets)


def eligible_scheduling_preference_targets(
    person: Person,
) -> tuple[SchedulingPreferenceTarget, ...]:
    return tuple(
        target for target in scheduling_preference_targets()
        if all(person.level(skill) >= 1 for skill in target.required_skills)
    )


# Static work-center -> department map, built once from LOCATIONS. This is the
# `Location.department` classification (Recycled / New / Supervisor /
# Maintenance / Transportation) — NOT the user-editable
# work_centers_store.department. Drives department-based punch rounding.
_LOCATION_DEPARTMENT: dict[str, str] = {loc.name: loc.department for loc in LOCATIONS}


def department_for_wc(wc_name: str | None) -> str | None:
    """Static department for a work-center name, or None if unknown/blank."""
    if not wc_name:
        return None
    return _LOCATION_DEPARTMENT.get(wc_name)


DEPARTMENT_ORDER = ("Recycled", "New", "Supervisor", "Maintenance", "Transportation")

BAY_SUBTITLES: dict[str, str] = {
    "Bay 5": "Complete yesterdays trailers",
}

TIME_OFF_KEY = "__time_off"  # pseudo-location for day-off list; not in LOCATIONS


@dataclass
class Person:
    name: str
    active: bool = True
    reserve: bool = False
    skills: dict[str, int] = field(default_factory=dict)
    employee_id: int | None = None  # Odoo hr.employee.id; None for legacy
    wage_type: str | None = None    # Odoo hr.employee.wage_type: 'hourly' | 'monthly' | None
    is_flexible: bool = False       # Odoo "Schedule Type" flexible; excluded from late report

    def level(self, skill: str) -> int:
        return int(self.skills.get(skill_name_for_scheduling_group(skill), 0))


PLANT_SCHEDULER_CSV = Path("Plant Scheduler(Plant Scheduler).csv")


# ---------- CSV bootstrap helper ----------

def _default_assignments_from_plant_scheduler() -> dict[str, list[str]]:
    """Parse 'Defaults for New Day' column to get default person per position."""
    out: dict[str, list[str]] = {}
    if not PLANT_SCHEDULER_CSV.exists():
        return out
    try:
        rows = list(csv.reader(PLANT_SCHEDULER_CSV.open(encoding="utf-8-sig")))
    except (OSError, csv.Error):
        return out
    known = {loc.name for loc in LOCATIONS}
    for r in rows[3:]:
        if len(r) < 8:
            continue
        station = (r[6] or "").strip().split("\n")[0].strip()
        default_person = (r[5] or "").strip()
        if station in known and default_person:
            out.setdefault(station, []).append(default_person)
    return out


# ---------- roster ----------

# In-process cache. The roster only changes on save_roster() and Odoo sync,
# both of which call _invalidate_roster_cache() — so a short TTL buys no
# freshness, it just forces cold JOIN-heavy reloads on long-tail pages
# (player cards, unusual leaderboard ranges) that aren't covered by the page
# warmer. 1 hour TTL minimizes cache misses without sacrificing freshness.
_ROSTER_CACHE: tuple[list[Person], float] | None = None
_ROSTER_CACHE_LOCK = RLock()
_ROSTER_CACHE_TTL_SECONDS = 3600.0


def _invalidate_roster_cache() -> None:
    global _ROSTER_CACHE
    with _ROSTER_CACHE_LOCK:
        _ROSTER_CACHE = None


def load_roster() -> list[Person]:
    """Load all NON-EXCLUDED people from Postgres. Inactive people
    are returned too (sorted to the bottom). Excluded people are
    filtered out — they're hidden from current views via the
    Settings → Roster Filter UI. Cached in-process for 1 hour
    (_ROSTER_CACHE_TTL_SECONDS); invalidated on save_roster()."""
    import time as _time
    global _ROSTER_CACHE
    with _ROSTER_CACHE_LOCK:
        if _ROSTER_CACHE is not None:
            cached, expires_at = _ROSTER_CACHE
            if _time.time() < expires_at:
                return cached
    from . import db
    rows = db.query(
        "SELECT p.id, p.name, p.active, p.reserve, p.odoo_id, p.wage_type, p.is_flexible, "
        "  COALESCE(json_object_agg(s.name, ps.level) "
        "           FILTER (WHERE s.name IS NOT NULL), '{}'::json)::text AS skills_json "
        "FROM people p "
        "LEFT JOIN person_skills ps ON ps.person_id = p.id "
        "LEFT JOIN skills s ON s.id = ps.skill_id "
        "WHERE NOT p.excluded "
        "GROUP BY p.id "
        "ORDER BY (NOT p.active), lower(p.name)"
    )
    out: list[Person] = []
    for r in rows:
        out.append(Person(
            name=r["name"],
            active=r["active"],
            reserve=r["reserve"],
            skills={k: int(v) for k, v in (json.loads(r["skills_json"]) or {}).items()},
            employee_id=r["odoo_id"],
            wage_type=r["wage_type"],
            is_flexible=bool(r["is_flexible"]),
        ))
    with _ROSTER_CACHE_LOCK:
        _ROSTER_CACHE = (out, _time.time() + _ROSTER_CACHE_TTL_SECONDS)
    return out


def save_roster(people: list[Person]) -> None:
    """Upsert each person + their skill levels. Skills not in p.skills are
    left untouched (sync owns server-mastered fields); levels at 0 are
    deleted from person_skills."""
    from . import db
    with db.cursor() as cur:
        for p in people:
            cur.execute(
                "INSERT INTO people (name, active, reserve, odoo_id, local_dirty) "
                "VALUES (%s, %s, %s, %s, TRUE) "
                "ON CONFLICT (name) DO UPDATE SET active = EXCLUDED.active, "
                "reserve = EXCLUDED.reserve, "
                "odoo_id = COALESCE(EXCLUDED.odoo_id, people.odoo_id), "
                "local_dirty = TRUE",
                (p.name, p.active, p.reserve, p.employee_id),
            )
            for skill_name, level in (p.skills or {}).items():
                if level > 0:
                    cur.execute(
                        "INSERT INTO person_skills (person_id, skill_id, level, local_dirty) "
                        "SELECT pe.id, sk.id, %s, TRUE FROM people pe, skills sk "
                        "WHERE pe.name = %s AND sk.name = %s "
                        "ON CONFLICT (person_id, skill_id) DO UPDATE SET "
                        "  level = EXCLUDED.level, local_dirty = TRUE",
                        (level, p.name, skill_name),
                    )
                else:
                    cur.execute(
                        "DELETE FROM person_skills WHERE "
                        "person_id = (SELECT id FROM people WHERE name = %s) AND "
                        "skill_id = (SELECT id FROM skills WHERE name = %s)",
                        (p.name, skill_name),
                    )
    _invalidate_roster_cache()


# ---------- daily schedule ----------

@dataclass
class Schedule:
    day: date
    published: bool = False
    # location name → list of person names (ordered as the user chose)
    assignments: dict[str, list[str]] = field(default_factory=dict)
    notes: str = ""                                     # day-wide note
    wc_notes: dict[str, str] = field(default_factory=dict)  # per-work-center notes
    testing_day: bool = False  # flag: training/override day; output not counted toward people
    # When the user edits a previously-posted day, we snapshot the posted version here
    # so they can toggle between draft and posted in the UI. Cleared on re-publish.
    published_snapshot: dict | None = None
    published_delivery: dict[str, str] = field(default_factory=dict)
    # Per-day shift override: {"start": "HH:MM", "end": "HH:MM",
    # "breaks": [{"start": "HH:MM", "end": "HH:MM", "name": "..."}, ...]}.
    # None means "use the global schedule from schedule_store".
    custom_hours: dict | None = None
    # Recycled smart-rotation metadata. Old schedules default to normal/manual-free.
    rotation_mode: str = "normal"
    assignment_sources: dict[str, dict[str, str]] = field(default_factory=dict)


_ASSIGNMENT_SOURCES = frozenset(("default", "generated", "manual"))


def _validate_assignment_sources(value) -> dict[str, dict[str, str]]:
    """Validate and copy the persisted ``work center -> person -> source`` map."""
    if not isinstance(value, dict):
        raise ValueError("assignment_sources must be a work center mapping")
    normalized: dict[str, dict[str, str]] = {}
    for wc_name, people in value.items():
        if not isinstance(wc_name, str) or not isinstance(people, dict):
            raise ValueError("assignment_sources must map work centers to person mappings")
        normalized_people: dict[str, str] = {}
        for person_name, source in people.items():
            if (
                not isinstance(person_name, str)
                or not isinstance(source, str)
                or source not in _ASSIGNMENT_SOURCES
            ):
                raise ValueError(
                    "assignment_sources values must be 'default', 'generated', or 'manual'"
                )
            normalized_people[person_name] = source
        normalized[wc_name] = normalized_people
    return normalized


def _json_mapping(value) -> dict:
    """Safely normalize persisted assignment-source JSONB values."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    try:
        return _validate_assignment_sources(value)
    except ValueError:
        return {}


def new_published_delivery() -> dict[str, str]:
    return {"version": uuid4().hex}


def _delivery_mapping(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    keys = ("version", "printed_at", "slack_posted_at", "slack_permalink")
    return {key: value[key] for key in keys if isinstance(value.get(key), str) and value[key]}


def draft_from_posted(schedule: Schedule) -> Schedule:
    if not schedule.published:
        return schedule
    draft = deepcopy(schedule)
    draft.published_snapshot = (
        deepcopy(schedule.published_snapshot)
        if schedule.published_snapshot else snapshot_of(schedule)
    )
    draft.published = False
    draft.published_delivery = {}
    return draft


def snapshot_of(sched: Schedule) -> dict:
    """Return a serializable snapshot of the schedule's posted-visible fields."""
    return {
        "assignments": {k: list(v) for k, v in (sched.assignments or {}).items()},
        "notes": sched.notes or "",
        "wc_notes": dict(sched.wc_notes or {}),
        "testing_day": bool(sched.testing_day),
        "custom_hours": deepcopy(sched.custom_hours),
        "published_delivery": _delivery_mapping(sched.published_delivery),
        "rotation_mode": sched.rotation_mode or "normal",
        "assignment_sources": {
            wc_name: dict(sources or {})
            for wc_name, sources in (sched.assignment_sources or {}).items()
        },
    }


# Per-day Schedule cache. shift_start_for / shift_end_for / breaks_for
# call load_schedule(d) inside hot loops (per-sample, per-bucket), so a
# naive Postgres round-trip per call exhausts the connection pool. We
# cache by day; save_schedule() invalidates the matching entry.
_schedule_cache: dict[date, Schedule] = {}
_schedule_cache_lock = RLock()


def _invalidate_schedule_cache(day: date) -> None:
    with _schedule_cache_lock:
        _schedule_cache.pop(day, None)


def invalidate_schedule_cache(day: date) -> None:
    """Public cache invalidator for transactional schedule lifecycle changes."""
    _invalidate_schedule_cache(day)


def load_schedule(day: date) -> Schedule:
    """Hydrate a Schedule from Postgres (schedules + schedule_assignments
    + schedule_wc_notes). Returns an empty Schedule
    if the day has no row yet. Cached in-process per-day; invalidated on
    save_schedule()."""
    with _schedule_cache_lock:
        cached = _schedule_cache.get(day)
        if cached is not None:
            return cached
    sched = _load_schedule_from_db(day)
    with _schedule_cache_lock:
        _schedule_cache[day] = sched
    return sched


def iter_saved_schedules():
    """Yield (date, Schedule) for every persisted schedule in Postgres,
    newest first. Past schedules used to live as local JSON files; that
    storage was retired when the app moved to Railway/Postgres."""
    from . import db
    rows = db.query("SELECT day FROM schedules ORDER BY day DESC")
    for r in rows:
        day_val = r["day"]
        if not isinstance(day_val, date):
            try:
                day_val = date.fromisoformat(str(day_val))
            except ValueError:
                continue
        yield day_val, load_schedule(day_val)


def _load_schedule_from_db(day: date) -> Schedule:
    from . import db
    rows = db.query(
        "SELECT day, published, testing_day, notes, custom_hours, published_snapshot, published_delivery, "
        "recycled_rotation_mode, assignment_sources "
        "FROM schedules WHERE day = %s",
        (day,),
    )
    if not rows:
        return Schedule(day=day, published=False, assignments={})
    r = rows[0]
    asg_rows = db.query(
        "SELECT wc.name AS wc_name, pe.name AS person_name "
        "FROM schedule_assignments sa "
        "JOIN work_centers wc ON wc.id = sa.wc_id "
        "JOIN people pe ON pe.id = sa.person_id "
        "WHERE sa.day = %s ORDER BY sa.wc_id, sa.sort_order",
        (day,),
    )
    notes_rows = db.query(
        "SELECT wc.name AS wc_name, sn.note "
        "FROM schedule_wc_notes sn JOIN work_centers wc ON wc.id = sn.wc_id "
        "WHERE sn.day = %s",
        (day,),
    )
    assignments: dict[str, list[str]] = {}
    for a in asg_rows:
        assignments.setdefault(a["wc_name"], []).append(a["person_name"])
    # Time-off is sourced from the Odoo-backed time_off_requests mirror, not the local DB.
    wc_notes = {n["wc_name"]: n["note"] for n in notes_rows}
    return Schedule(
        day=day,
        published=r["published"],
        assignments=assignments,
        notes=r["notes"] or "",
        wc_notes=wc_notes,
        testing_day=r["testing_day"],
        custom_hours=r["custom_hours"],
        published_snapshot=r["published_snapshot"],
        published_delivery=_delivery_mapping(r.get("published_delivery")),
        rotation_mode=r.get("recycled_rotation_mode") or "normal",
        assignment_sources=_json_mapping(r.get("assignment_sources")),
    )


def load_schedules_bulk(
    start: date | None = None,
    end: date | None = None,
    published_only: bool = False,
) -> list[tuple[date, Schedule]]:
    """Hydrate many saved schedules in ONE set-based pass (3 queries total:
    schedules + schedule_assignments + schedule_wc_notes), newest first.
    Optional date/published filters are pushed into SQL.

    Built for history views (/staffing/past): per-day load_schedule() would
    run 3 queries per day AND pin every day into the per-day cache forever,
    so this deliberately does NOT touch _schedule_cache."""
    from . import db
    conds: list[str] = []
    params: list = []
    if start is not None:
        conds.append("day >= %s")
        params.append(start)
    if end is not None:
        conds.append("day <= %s")
        params.append(end)
    if published_only:
        conds.append("published = TRUE")
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    sched_rows = db.query(
        "SELECT day, published, testing_day, notes, custom_hours, published_snapshot, published_delivery, "
        "recycled_rotation_mode, assignment_sources "
        f"FROM schedules{where} ORDER BY day DESC",
        tuple(params) if params else None,
    )
    days: list[date] = []
    for r in sched_rows:
        day_val = r["day"]
        if not isinstance(day_val, date):
            try:
                day_val = date.fromisoformat(str(day_val))
            except ValueError:
                continue
        r["day"] = day_val
        days.append(day_val)
    if not days:
        return []
    asg_rows = db.query(
        "SELECT sa.day, wc.name AS wc_name, pe.name AS person_name "
        "FROM schedule_assignments sa "
        "JOIN work_centers wc ON wc.id = sa.wc_id "
        "JOIN people pe ON pe.id = sa.person_id "
        "WHERE sa.day = ANY(%s) ORDER BY sa.day, sa.wc_id, sa.sort_order",
        (days,),
    )
    notes_rows = db.query(
        "SELECT sn.day, wc.name AS wc_name, sn.note "
        "FROM schedule_wc_notes sn JOIN work_centers wc ON wc.id = sn.wc_id "
        "WHERE sn.day = ANY(%s)",
        (days,),
    )
    assignments_by_day: dict[date, dict[str, list[str]]] = {}
    for a in asg_rows:
        assignments_by_day.setdefault(a["day"], {}).setdefault(
            a["wc_name"], []).append(a["person_name"])
    wc_notes_by_day: dict[date, dict[str, str]] = {}
    for n in notes_rows:
        wc_notes_by_day.setdefault(n["day"], {})[n["wc_name"]] = n["note"]
    out: list[tuple[date, Schedule]] = []
    for r in sched_rows:
        if not isinstance(r["day"], date):
            continue
        d = r["day"]
        out.append((d, Schedule(
            day=d,
            published=r["published"],
            assignments=assignments_by_day.get(d, {}),
            notes=r["notes"] or "",
            wc_notes=wc_notes_by_day.get(d, {}),
            testing_day=r["testing_day"],
            custom_hours=r["custom_hours"],
            published_snapshot=r["published_snapshot"],
            published_delivery=_delivery_mapping(r.get("published_delivery")),
            rotation_mode=r.get("recycled_rotation_mode") or "normal",
            assignment_sources=_json_mapping(r.get("assignment_sources")),
        )))
    return out


def save_schedule(schedule: Schedule) -> None:
    """Upsert the day's schedule + replace its assignments / time off /
    wc_notes atomically (delete-then-insert inside one transaction)."""
    from . import db
    assignment_sources = _validate_assignment_sources(schedule.assignment_sources)
    _invalidate_schedule_cache(schedule.day)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (day, published, testing_day, notes, "
            "custom_hours, published_snapshot, published_delivery, recycled_rotation_mode, assignment_sources, updated_at) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s::jsonb, now()) "
            "ON CONFLICT (day) DO UPDATE SET "
            "  published = EXCLUDED.published, "
            "  testing_day = EXCLUDED.testing_day, "
            "  notes = EXCLUDED.notes, "
            "  custom_hours = EXCLUDED.custom_hours, "
            "  published_snapshot = EXCLUDED.published_snapshot, "
            "  published_delivery = EXCLUDED.published_delivery, "
            "  recycled_rotation_mode = EXCLUDED.recycled_rotation_mode, "
            "  assignment_sources = EXCLUDED.assignment_sources, "
            "  updated_at = now()",
            (
                schedule.day,
                schedule.published,
                bool(schedule.testing_day),
                schedule.notes or "",
                json.dumps(schedule.custom_hours) if schedule.custom_hours else None,
                json.dumps(schedule.published_snapshot) if schedule.published_snapshot else None,
                json.dumps(_delivery_mapping(schedule.published_delivery)),
                schedule.rotation_mode or "normal",
                json.dumps(assignment_sources),
            ),
        )
        cur.execute("DELETE FROM schedule_assignments WHERE day = %s", (schedule.day,))
        cur.execute("DELETE FROM schedule_wc_notes WHERE day = %s", (schedule.day,))
        for wc_name, names in (schedule.assignments or {}).items():
            if wc_name == TIME_OFF_KEY:
                # Legacy in-memory key from older snapshots; time-off is now
                # StratusTime-driven and never persisted locally. Skip silently.
                continue
            for i, n in enumerate(names or []):
                cur.execute(
                    "INSERT INTO schedule_assignments (day, wc_id, person_id, sort_order) "
                    "SELECT %s, wc.id, pe.id, %s FROM work_centers wc, people pe "
                    "WHERE wc.name = %s AND pe.name = %s",
                    (schedule.day, i, wc_name, n),
                )
        for wc_name, note in (schedule.wc_notes or {}).items():
            if not note:
                continue
            cur.execute(
                "INSERT INTO schedule_wc_notes (day, wc_id, note) "
                "SELECT %s, wc.id, %s FROM work_centers wc WHERE wc.name = %s",
                (schedule.day, note, wc_name),
            )


def schedule_revision(day: date) -> str | None:
    from . import db
    rows = db.query(
        "SELECT updated_at::text AS revision FROM schedules WHERE day = %s", (day,)
    )
    return rows[0]["revision"] if rows else None


def delivery_for_version(day: date, version: str) -> dict[str, str] | None:
    schedule = load_schedule(day)
    current = _delivery_mapping(schedule.published_delivery)
    if schedule.published and current.get("version") == version:
        return current
    prior = _delivery_mapping((schedule.published_snapshot or {}).get("published_delivery"))
    return prior if prior.get("version") == version else None


def record_delivery(
    day: date, version: str, fields: Mapping[str, str],
) -> dict[str, str] | None:
    from . import db
    patch = {
        key: value for key, value in fields.items()
        if key in {"printed_at", "slack_posted_at", "slack_permalink"}
        and isinstance(value, str) and value
    }
    if not version or not patch:
        return None
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE schedules
               SET published_delivery = CASE
                     WHEN published THEN published_delivery || %s::jsonb
                     ELSE published_delivery END,
                   published_snapshot = CASE
                     WHEN NOT published THEN jsonb_set(
                       published_snapshot, '{published_delivery}',
                       COALESCE(published_snapshot->'published_delivery', '{}'::jsonb) || %s::jsonb)
                     ELSE published_snapshot END,
                   updated_at = now()
             WHERE day = %s
               AND ((published AND published_delivery->>'version' = %s)
                 OR (NOT published AND published_snapshot->'published_delivery'->>'version' = %s))
             RETURNING CASE WHEN published THEN published_delivery
                            ELSE published_snapshot->'published_delivery' END AS delivery
            """,
            (json.dumps(patch), json.dumps(patch), day, version, version),
        )
        row = cur.fetchone()
    _invalidate_schedule_cache(day)
    return _delivery_mapping(row["delivery"]) if row else None


def default_assignments() -> dict[str, list[str]]:
    """From the Plant Scheduler CSV's 'Defaults for New Day' column, if present."""
    return _default_assignments_from_plant_scheduler()


# ---------- color / level helpers ----------

SKILL_COLORS = {
    3: "#4ade80",   # green — trained & proficient
    2: "#e6edf3",   # foreground — trained & competent
    1: "#fb923c",   # orange — practicing
    0: "#ef4444",   # red — not trained
}

SKILL_LABELS = {
    0: "not trained",
    1: "practicing",
    2: "competent",
    3: "proficient",
}


def skill_color(level: int) -> str:
    return SKILL_COLORS.get(int(level), SKILL_COLORS[0])


def present_operators(assigned: list[dict], off_names) -> list[dict]:
    """The assigned operators actually present — i.e. not out for the full day.

    ``assigned`` is a list of {name, ...} dicts; ``off_names`` is the set of
    names with a full-day time-off/absent entry today. Used for the station
    summary and the headcount, while the full ``assigned`` list still drives
    the picker and the schedule save — so the assignment is preserved and
    undoing an absence restores the person to the slot.
    """
    off = set(off_names)
    return [a for a in assigned if a["name"] not in off]


def effective_minutes_worked(name: str, day, window_start_utc, window_end_utc,
                             partials: dict | None = None) -> int:
    """Minutes the person `name` was actually working in [window_start_utc, window_end_utc]
    on `day`. Subtracts:

    - Scheduled breaks (lunch, cleanup, etc.) that fall within the window —
      productive labor stops during these.
    - Partial-day Odoo off-intervals that overlap the window — the
      operator was clocked out for those minutes.

    Falls back gracefully when the Odoo mirror is unavailable: the off-interval
    subtraction is skipped, but break subtraction still applies.

    `window_start_utc` and `window_end_utc` must be timezone-aware UTC datetimes.
    Callers looping over many people on the same day should compute
    ``attendance.partial_off_intervals(day)`` ONCE and pass it as ``partials``
    so this doesn't re-query per person; omit it for the fetch-per-call
    behavior.
    """
    from datetime import datetime
    from . import shift_config, attendance
    if window_end_utc <= window_start_utc:
        return 0
    base = int((window_end_utc - window_start_utc).total_seconds() // 60)

    # Subtract break minutes that overlap the window. Break times are
    # local site times; convert to UTC for overlap math.
    break_minutes_in_window = 0
    try:
        for b in shift_config.breaks_for(day):
            bs_local = datetime.combine(day, b.start, tzinfo=shift_config.SITE_TZ)
            be_local = datetime.combine(day, b.end, tzinfo=shift_config.SITE_TZ)
            bs_utc = bs_local.astimezone(UTC)
            be_utc = be_local.astimezone(UTC)
            lo = max(bs_utc, window_start_utc)
            hi = min(be_utc, window_end_utc)
            if hi > lo:
                break_minutes_in_window += int((hi - lo).total_seconds() // 60)
    except Exception:
        pass

    # Subtract partial-day off intervals (Odoo time-off mirror).
    if partials is not None:
        intervals_by_name = partials
    else:
        try:
            intervals_by_name = attendance.partial_off_intervals(day)
        except Exception:
            return max(0, base - break_minutes_in_window)
    intervals = intervals_by_name.get(name) or []
    overlap_min = 0
    for s, e in intervals:
        lo = max(s, window_start_utc)
        hi = min(e, window_end_utc)
        if hi > lo:
            overlap_min += int((hi - lo).total_seconds() // 60)
    return max(0, base - break_minutes_in_window - overlap_min)
