# Per-Schedule Punch Rounding — Design

**Date:** 2026-06-01
**Status:** Approved (brainstorming → implementation planning)

## Context

Timeclock punch rounding is currently **plant-wide**. A single
`rounding_settings` row (id=1) holds four windows — `in_before_min`,
`in_after_min`, `out_before_min`, `out_after_min` — and a single
`global_schedule` row (id=1) holds the one shift start/end those windows
round toward. At punch time, [`_open_log_row`](../../../src/zira_dashboard/routes/timeclock.py)
always feeds `shift_config.shift_start_for(date)` / `shift_end_for(date)`
plus `rounding_store.current()` into [`apply_rounding`](../../../src/zira_dashboard/rounding.py).

Dale needs the transportation drivers on a different shift than the plant
floor — they work **5:45 AM – 2:30 PM** — and a different rounding policy:
**20-minute rounding at the start, none at the end.** More generally, he
wants the ability to set the rounding numbers **per work schedule**, with
the same four-window behavior the settings page already has.

The drivers' hours already live in Odoo as a working schedule
(`resource.calendar`). So the natural grouping key is the **Odoo work
schedule itself**, not the department: an employee inherits a schedule's
rounding simply by being assigned that work schedule in Odoo. Hours and
rounding then always travel together, and there's no separate membership
list to maintain.

This is **not** a change to the rounding math. `apply_rounding` stays
byte-for-byte the same. The work is making the *inputs* (shift boundaries
+ which four windows) resolve per-employee from their Odoo work schedule,
with the existing plant-wide values as the fallback.

## Goals

1. Let Dale configure the four rounding windows **per Odoo work schedule**
   from the settings page — same controls, same `0–60` validation, same
   round-toward-the-boundary behavior as today's global rounding form.
2. Source each configured schedule's **shift boundaries from Odoo**
   (`resource.calendar` attendance lines), synced into local storage so
   punches stay fast and survive an Odoo outage.
3. Resolve an employee's schedule at punch time via their Odoo
   `resource_calendar_id` — no department mapping, no manual roster.
4. Seed the drivers' case: a "Drivers" work-schedule override with
   `in_before=20, in_after=0, out_before=0, out_after=0`, rounding toward
   a 5:45 start that comes from Odoo.
5. Leave the plant floor **completely unchanged**: anyone without a
   configured override keeps using `global_schedule` + `rounding_settings`
   id=1, and every production-analytics path keeps reading `global_schedule`.

## Non-goals

- **Per-employee rounding.** The key is the work schedule; all employees
  on the same Odoo schedule get the same rules.
- **Setting rounding numbers in Odoo.** Considered (custom fields on
  `resource.calendar` via Studio) and rejected — Dale wants the existing
  in-app rounding controls, no Studio dependency. The app owns the
  numbers; Odoo owns only the hours.
- **Department-based membership.** Dropped in favor of the work-schedule
  key. Department still exists in Odoo; it just plays no role here.
- **Migrating the plant default into the new table.** `rounding_settings`
  id=1 + `global_schedule` stay as the implicit default. Folding "Plant"
  into the overrides table is a possible later UI nicety, out of scope now.
- **Changing `apply_rounding`.** The four-window logic is untouched.
- **Overnight shifts.** Still unsupported (existing limitation). 5:45–2:30
  is same-day, so this is fine.
- **Live Odoo reads on the punch path.** Hours are synced into local
  storage, not read live at clock-in.

## Design

### Two-part resolution

At punch time, an employee resolves to a shift + rounding via one lookup:

```
employee.resource_calendar_id  ──►  work_schedules row (override)?
                                       ├─ yes, and weekday has hours:
                                       │     shift_start/end = synced Odoo hours
                                       │     windows         = the row's 4 numbers
                                       └─ no  (or no hours for this weekday):
                                             shift_start/end = global_schedule  (plant default)
                                             windows         = rounding_settings id=1
```

Everything resolves safely to the plant default when anything is missing,
so a misconfiguration never mis-rounds a punch.

### Data model

New table — one row **per schedule Dale chooses to configure** (row
existence = an active override):

```sql
CREATE TABLE IF NOT EXISTS work_schedules (
  resource_calendar_id  INTEGER PRIMARY KEY,           -- Odoo resource.calendar id
  name                  TEXT NOT NULL,                 -- synced label, e.g. "Drivers 5:45-2:30"
  work_hours            JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {"0":["05:45","14:30"], ...}
  in_before_min         INTEGER NOT NULL DEFAULT 0,
  in_after_min          INTEGER NOT NULL DEFAULT 0,
  out_before_min        INTEGER NOT NULL DEFAULT 0,
  out_after_min         INTEGER NOT NULL DEFAULT 0,
  last_synced_at        TIMESTAMPTZ,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

New column on `people`:

```sql
ALTER TABLE people ADD COLUMN IF NOT EXISTS resource_calendar_id INTEGER;
```

- **`work_hours` keys are weekday integers as strings, 0=Monday..6=Sunday.**
  This aligns with Python's `date.weekday()` *and* Odoo's
  `resource.calendar.attendance.dayofweek` (both 0=Monday), so no remap.
- **Values are `["HH:MM", "HH:MM"]`** (24h, zero-padded) = `[min(hour_from),
  max(hour_to)]` across that weekday's attendance lines. A lunch split
  produces two lines; we collapse to the outer boundaries, which is exactly
  what rounding (clock_in/clock_out at shift edges) needs.
- Odoo float hours → `HH:MM`: `h = int(f); m = round((f - h) * 60)` (5.75 →
  `05:45`, 14.5 → `14:30`), with carry if `m == 60`.

### `work_schedule_store.py` (new)

Mirrors [`rounding_store`](../../../src/zira_dashboard/rounding_store.py) /
[`schedule_store`](../../../src/zira_dashboard/schedule_store.py): an
in-process dict cache (keyed by `resource_calendar_id`) behind an `RLock`,
invalidated on write, so the punch path is a cache read, never a DB hit.

```python
@dataclass(frozen=True)
class WorkScheduleOverride:
    resource_calendar_id: int
    name: str
    work_hours: dict[int, tuple[time, time]]   # weekday -> (start, end)
    rounding: RoundingSettings

def get(resource_calendar_id: int) -> WorkScheduleOverride | None  # cache read, punch path
def all() -> list[WorkScheduleOverride]                            # settings UI
def save_rounding(cal_id: int, r: RoundingSettings) -> None        # settings POST; touches only the 4 numbers
def refresh_synced(cal_id: int, name: str, work_hours: dict) -> None  # sync; touches only name+hours+last_synced_at
def create(cal_id: int) -> None                                    # admin adds an override (rounding defaults 0)
def delete(cal_id: int) -> None                                    # admin removes an override
def reload() -> None
```

The split between `save_rounding` (admin-owned numbers) and
`refresh_synced` (Odoo-owned name+hours) is deliberate: the sync must
never clobber the rounding numbers, and a settings save must never clobber
the synced hours.

### Punch-time resolver (`routes/timeclock.py`)

A private helper, used by `_open_log_row` in place of the current
hard-coded plant lookup:

```python
def _shift_for_punch(person_odoo_id: int, local_date: date) -> tuple[time, time, RoundingSettings]:
    rows = db.query(
        "SELECT resource_calendar_id FROM people WHERE odoo_id = %s", (person_odoo_id,)
    )  # one cheap indexed read
    cal_id = rows[0]["resource_calendar_id"] if rows else None
    ws = work_schedule_store.get(cal_id) if cal_id else None
    wd = local_date.weekday()
    if ws and wd in ws.work_hours:
        start, end = ws.work_hours[wd]
        return start, end, ws.rounding
    return (
        shift_config.shift_start_for(local_date),
        shift_config.shift_end_for(local_date),
        rounding_store.current(),
    )
```

`_open_log_row` then calls `apply_rounding(action, occurred_at, start, end,
windows)` exactly as it does today. The lone added cost on the hot path is
one indexed `people` lookup; the override itself comes from the in-process
cache. The existing `try/except` that preserves the raw punch on any
rounding failure stays in place.

### Odoo client additions (`odoo_client.py`)

- **`fetch_employees()`** — add `resource_calendar_id` to the field list.
  Odoo returns a many2one as `[id, name]` or `False`; extract the id.
- **`fetch_work_schedules() -> list[dict]`** — active `resource.calendar`
  (`id`, `name`). Feeds the settings "Add schedule" dropdown.
- **`fetch_calendar_hours(calendar_ids) -> dict[int, dict[int, tuple[str,str]]]`**
  — read `resource.calendar.attendance` (`calendar_id`, `dayofweek`,
  `hour_from`, `hour_to`) for the given calendars and reduce to per-weekday
  `[min(hour_from), max(hour_to)]`.

### Sync (`odoo_sync.py`, existing employee cycle)

1. During the employee upsert, write `resource_calendar_id` onto each
   `people` row (extracted from the `fetch_employees` many2one).
2. After the employee pass, for the **configured** `work_schedules` rows
   only: call `fetch_calendar_hours([...])` (one batched read) plus
   `fetch_work_schedules()` for current names, derive per-weekday hours,
   and `work_schedule_store.refresh_synced(...)`. Rounding numbers are left
   untouched.

A schedule change in Odoo is reflected after the next sync cycle (minutes),
which is the accepted trade-off of the synced-local model.

### Settings UI (`routes/settings.py` + `settings.html`)

Under the existing rounding controls (which get relabeled "Default — all
schedules without an override"), add a **"Per-schedule rounding"** block:

- One sub-card per configured schedule: name, **read-only synced hours**
  ("5:45 AM – 2:30 PM · from Odoo"), and the four number inputs (same
  `0–60` clamp as `/settings/rounding`), with Save and Remove.
- An **"Add schedule"** control: a dropdown of Odoo work schedules not yet
  configured (from `fetch_work_schedules()` minus the configured ids).
  Selecting one creates the override (`create`) and immediately syncs its
  hours.

Routes, mirroring the existing `POST /settings/rounding` validation:

- `POST /settings/work_schedule_rounding` — save one schedule's four
  numbers (`save_rounding`), redirect back to the schedule section.
- `POST /settings/work_schedule_rounding/add` — `create(cal_id)` + sync
  its hours.
- `POST /settings/work_schedule_rounding/remove` — `delete(cal_id)`.

The GET `/settings` handler adds `work_schedules` (configured overrides,
shaped for display) and `available_schedules` (for the dropdown) to context.

## Acceptance criteria

- A "Drivers" override configured with `20/0/0/0`, with drivers assigned
  the 5:45–2:30 work schedule in Odoo:
  - A driver clock_in at **5:30** records **5:45** (rounded up, `in_before=20`).
  - A driver clock_in at **5:52** records **5:52** (no late grace, `in_after=0`).
  - A driver clock_out at **2:25** or **2:40** records **as-punched** (no
    end rounding).
  - A driver `transfer_in`/`transfer_out` is never rounded.
- A plant-floor employee (no override on their schedule) rounds exactly as
  before — `global_schedule` boundaries + `rounding_settings` id=1.
- An employee whose schedule has an override but who punches on a weekday
  the schedule doesn't cover falls back to the plant default for that punch.
- An override whose hours never synced (Odoo unreachable) falls back to the
  plant default and logs a warning; no punch is mis-rounded.
- The settings page shows each configured schedule's four numbers + its
  synced hours (read-only); saving validates `0–60` and persists; Remove
  reverts affected employees to the plant default.
- Leaderboard / staffing / dashboards are unchanged (still read
  `global_schedule`).

## Risks

- **Odoo many2one shape.** `resource_calendar_id` may come back as
  `[id, name]` or `False`. Extraction must handle both; a missing calendar
  → `NULL` → plant default.
- **Calendar with no attendance lines.** `work_hours` ends up `{}`; the
  override never matches a weekday and every punch on it falls back to
  plant default. Safe, but worth a warning log so Dale notices a
  misconfigured schedule.
- **Float→time conversion.** Odd Odoo values (e.g. `5.7583`) must round to
  the nearest minute with carry (`m == 60` → bump the hour). Covered by
  unit tests.
- **Two sources of truth, one key.** Name + hours from Odoo, rounding from
  the app, joined on `resource_calendar_id`. If a calendar is deleted in
  Odoo, the override orphans — it simply stops matching employees; Dale can
  Remove it. Sync tolerates a calendar id that no longer exists.
- **Sync vs. save races.** Mitigated by the `refresh_synced` /
  `save_rounding` split — each touches a disjoint set of columns.
- **Hot-path lookup.** One extra indexed `people` read per punch. Cheap and
  acceptable (the punch already writes a row in the same flow); the override
  itself is served from the in-process cache.

## File touch list

- Modify: `src/zira_dashboard/db.py` — `CREATE TABLE work_schedules`;
  `ALTER TABLE people ADD COLUMN resource_calendar_id`.
- New: `src/zira_dashboard/work_schedule_store.py` — cached store +
  `WorkScheduleOverride`.
- Modify: `src/zira_dashboard/odoo_client.py` — `resource_calendar_id` in
  `fetch_employees`; new `fetch_work_schedules`, `fetch_calendar_hours`.
- Modify: `src/zira_dashboard/odoo_sync.py` — write
  `people.resource_calendar_id`; refresh configured schedules' hours.
- Modify: `src/zira_dashboard/routes/timeclock.py` — `_shift_for_punch`
  resolver; `_open_log_row` uses it.
- Modify: `src/zira_dashboard/routes/settings.py` — GET context
  (`work_schedules`, `available_schedules`); POST save / add / remove.
- Modify: `src/zira_dashboard/templates/settings.html` — per-schedule
  rounding block + Add control; relabel global rounding as "Default".
- New tests: `tests/test_work_schedule_store.py` (cache/save/refresh/delete,
  mirroring `test_rounding_store.py`); `tests/test_work_schedule_rounding.py`
  (resolution + the driver cases above); hours-derivation + many2one
  extraction tests alongside `test_odoo_client.py`; settings route
  GET/POST/validation tests.

## Testing note

Per the local environment (Python 3.9; the suite targets a newer runtime
and can't run locally), tests run in CI / on Railway. Locally, verify with
`py_compile` + a small ast-exec smoke of the pure functions
(`work_schedule_store` derivation, `_shift_for_punch` resolution).
