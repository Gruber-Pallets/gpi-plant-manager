"""Staffing data layer: locations, roster, daily schedules.

Storage is flat JSON files so you get free history and easy manual edits.
- roster.json               — people + per-skill levels (0–3) + active flag
- schedules/YYYY-MM-DD.json — one file per day with assignments
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from threading import RLock

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
    Location("Loading/Jockeying", "Forklift: Load/Jockey", "Forklift", "Supervisor", None),
    Location("Tablets", "Forklift: Tablets", "Forklift", "Supervisor", None, min_ops=1, max_ops=None),
    # Maint.
    Location("Work Orders", "Mechanic", "Maint.", "Maintenance", None, min_ops=1, max_ops=None),
)


def required_skills_for(loc: Location) -> tuple[str, ...]:
    """Returns the required skills for this work center. Falls back to the single
    `skill` field when `required_skills` is unset, for backward compatibility."""
    return loc.required_skills if loc.required_skills else (loc.skill,)

DEPARTMENT_ORDER = ("Recycled", "New", "Supervisor", "Maintenance")

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

    def level(self, skill: str) -> int:
        return int(self.skills.get(skill, 0))


ROSTER_PATH = Path("roster.json")
PLANT_SCHEDULER_CSV = Path("Plant Scheduler(Plant Scheduler).csv")
SCHEDULES_DIR = Path("schedules")

_lock = RLock()


# ---------- bootstrap seed ----------

# Core roster pulled from skill-matrix screenshot + names added from Plant Scheduler CSV.
_SEED_ACTIVE = [
    "Ben", "Carlos", "Christian", "Eulogio", "Francisco", "Gerardo G", "Gerardo V",
    "Iban", "Isidro", "Jesus C", "Jesus M", "Jose L", "Juan", "Lauro", "Louie",
    "Lupe", "Isaac", "Trent", "Jose O",
    # names from CSV only
    "Ian", "Dale", "Luke",
]
_SEED_INACTIVE = [
    "Adrian", "Alejandro", "Humberto", "Jesus G", "Jose C", "Pascual", "Porfirio",
]

# Partial skills that are visible in the Plant Scheduler CSV (example day 04/27/2026).
# Fill the rest in via the Roster page or by dropping a skill-matrix CSV later.
_SEED_SKILL_HINTS: dict[str, dict[str, int]] = {
    "Jesus M":    {"Trim Saw": 3},
    "Lupe":       {"Hand Build": 3},
    "Gerardo G":  {"Hand Build": 3},
    "Isaac":      {"Woodpecker": 1},
    "Carlos":     {"Woodpecker": 3},
    "Gerardo V":  {"Junior": 3},
    "Jesus C":    {"Forklift: Load/Jockey": 2},
    "Lauro":      {"Forklift: Tablets": 1},
    "Trent":      {"Forklift: Tablets": 3},
    "Isidro":     {"Forklift: Tablets": 3},
    "Iban":       {"Forklift: Tablets": 3},
    "Juan":       {"Forklift: Tablets": 3},
    "Francisco":  {"Mechanic": 3},
    "Ben":        {"Mechanic": 3},
}


def _seed_roster() -> list[Person]:
    out: list[Person] = []
    for name in _SEED_ACTIVE:
        skills = {s: 0 for s in SKILLS}
        for k, v in _SEED_SKILL_HINTS.get(name, {}).items():
            skills[k] = v
        out.append(Person(name=name, active=True, skills=skills))
    for name in _SEED_INACTIVE:
        out.append(Person(name=name, active=False, skills={s: 0 for s in SKILLS}))
    return out


# ---------- CSV import helpers ----------

def _import_skill_matrix_csv(path: Path) -> list[Person] | None:
    """Recognize a skill-matrix CSV by presence of 'Master List' + skill columns."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames or "Master List" not in reader.fieldnames:
                return None
            people: list[Person] = []
            for row in reader:
                name = (row.get("Master List") or "").strip()
                if not name:
                    continue
                active_raw = (row.get("is Scheduled") or "").strip().lower()
                active = active_raw in {"true", "1", "yes", "y"}
                skills: dict[str, int] = {}
                for s in SKILLS:
                    raw = (row.get(s) or "0").strip()
                    try:
                        skills[s] = max(0, min(3, int(raw)))
                    except ValueError:
                        skills[s] = 0
                people.append(Person(name=name, active=active, skills=skills))
            return people or None
    except (OSError, csv.Error):
        return None


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

def load_roster() -> list[Person]:
    """Load all people + their skill levels from Postgres. Inactive people
    are returned too (sorted to the bottom)."""
    from . import db
    rows = db.query(
        "SELECT p.id, p.name, p.active, p.reserve, p.odoo_id, "
        "  COALESCE(json_object_agg(s.name, ps.level) "
        "           FILTER (WHERE s.name IS NOT NULL), '{}'::json)::text AS skills_json "
        "FROM people p "
        "LEFT JOIN person_skills ps ON ps.person_id = p.id "
        "LEFT JOIN skills s ON s.id = ps.skill_id "
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
        ))
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
    # Per-day shift override: {"start": "HH:MM", "end": "HH:MM",
    # "breaks": [{"start": "HH:MM", "end": "HH:MM", "name": "..."}, ...]}.
    # None means "use the global schedule from schedule_store".
    custom_hours: dict | None = None


def snapshot_of(sched: "Schedule") -> dict:
    """Return a serializable snapshot of the schedule's posted-visible fields."""
    return {
        "assignments": {k: list(v) for k, v in (sched.assignments or {}).items()},
        "notes": sched.notes or "",
        "wc_notes": dict(sched.wc_notes or {}),
        "testing_day": bool(sched.testing_day),
    }


# Per-day Schedule cache. shift_start_for / shift_end_for / breaks_for
# call load_schedule(d) inside hot loops (per-sample, per-bucket), so a
# naive Postgres round-trip per call exhausts the connection pool. We
# cache by day; save_schedule() invalidates the matching entry.
_schedule_cache: dict[date, "Schedule"] = {}
_schedule_cache_lock = RLock()


def _invalidate_schedule_cache(day: date) -> None:
    with _schedule_cache_lock:
        _schedule_cache.pop(day, None)


def load_schedule(day: date) -> Schedule:
    """Hydrate a Schedule from Postgres (schedules + schedule_assignments
    + schedule_time_off + schedule_wc_notes). Returns an empty Schedule
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


def _load_schedule_from_db(day: date) -> "Schedule":
    from . import db
    rows = db.query(
        "SELECT day, published, testing_day, notes, custom_hours, published_snapshot "
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
    assignments: dict[str, list[str]] = {}
    for a in asg_rows:
        assignments.setdefault(a["wc_name"], []).append(a["person_name"])
    # Time-off is now sourced from StratusTime (sub-project #2), not the local DB.
    notes_rows = db.query(
        "SELECT wc.name AS wc_name, sn.note "
        "FROM schedule_wc_notes sn JOIN work_centers wc ON wc.id = sn.wc_id "
        "WHERE sn.day = %s",
        (day,),
    )
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
    )


def save_schedule(schedule: Schedule) -> None:
    """Upsert the day's schedule + replace its assignments / time off /
    wc_notes atomically (delete-then-insert inside one transaction)."""
    from . import db
    _invalidate_schedule_cache(schedule.day)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (day, published, testing_day, notes, "
            "custom_hours, published_snapshot, updated_at) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, now()) "
            "ON CONFLICT (day) DO UPDATE SET "
            "  published = EXCLUDED.published, "
            "  testing_day = EXCLUDED.testing_day, "
            "  notes = EXCLUDED.notes, "
            "  custom_hours = EXCLUDED.custom_hours, "
            "  published_snapshot = EXCLUDED.published_snapshot, "
            "  updated_at = now()",
            (
                schedule.day,
                schedule.published,
                bool(schedule.testing_day),
                schedule.notes or "",
                json.dumps(schedule.custom_hours) if schedule.custom_hours else None,
                json.dumps(schedule.published_snapshot) if schedule.published_snapshot else None,
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


def effective_minutes_worked(name: str, day, window_start_utc, window_end_utc) -> int:
    """Minutes the person `name` was actually working in [window_start_utc, window_end_utc]
    on `day`, after subtracting any partial-day StratusTime off-intervals that overlap.

    Falls back to the full window width when StratusTime is unreachable or the
    person has no partial-off on `day`.

    `window_start_utc` and `window_end_utc` must be timezone-aware UTC datetimes.
    """
    from . import stratustime_client
    if window_end_utc <= window_start_utc:
        return 0
    base = int((window_end_utc - window_start_utc).total_seconds() // 60)
    try:
        intervals_by_name = stratustime_client.partial_off_intervals_for_day(day)
    except Exception:
        return base
    intervals = intervals_by_name.get(name) or []
    overlap_min = 0
    for s, e in intervals:
        lo = max(s, window_start_utc)
        hi = min(e, window_end_utc)
        if hi > lo:
            overlap_min += int((hi - lo).total_seconds() // 60)
    return max(0, base - overlap_min)
