# Odoo Holiday Optional Workdays Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror whole-company Odoo Public Holidays into Plant Manager, close those dates by default, and let managers deliberately recruit and publish optional holiday work through the scheduler workflow and layout they already use for Saturdays.

**Architecture:** Add a transactional local mirror for unscoped Odoo holidays and a date-neutral optional-workday service. Shared workday and shift rules give holidays precedence over weekday defaults, while the existing Saturday recruiting tables, routes, controls, and kiosk flow are generalized additively for `saturday` and `holiday` kinds. Odoo remains read-only; payroll treatment stays in Odoo.

**Tech Stack:** Python 3.12, FastAPI, PostgreSQL/psycopg2, Odoo XML-RPC `search_read`, Jinja2, vanilla JavaScript, pytest.

## Global Constraints

- Keep the current `/staffing` layout, form, URLs, CSS classes, and JavaScript hooks. Change context and visible wording, not the scheduler's structure.
- Read Odoo `resource.calendar.leaves` only with `search_read`. Never create, write, or unlink a Public Holiday from Plant Manager.
- Mirror only rows with `resource_id = false` and `calendar_id = false` as plant-wide closures.
- Keep `odoo_client.fetch_public_holidays(start, end)` as the live cached range reader, including calendar-scoped rows, because `time_off_local_backfill.py` depends on `calendar_id`.
- A mirrored holiday overrides the normal Monday-Friday schedule. A saved or previously posted weekday schedule cannot reopen it.
- A holiday becomes operational only when both its optional-workday recruiting row and its Plant Manager schedule are published.
- Preserve the legacy rule that an ordinary published Saturday is operational even when it predates recruiting.
- Use the Saturday default shift and breaks for holiday work unless the date has a custom-hours override.
- Keep the existing `saturday_*` tables and `/api/staffing/saturday-recruiting` and `/timeclock/saturday/*` URLs. Add kind/name metadata instead of moving history.
- A holiday recruiting activation may clear the current draft, but it must retain any prior posted schedule in `published_snapshot`.
- A failed or malformed Odoo refresh keeps the last-known-good holiday set. A successful empty full-list response clears it.
- Workday hot paths may read the in-memory mirror or local database; they must never call Odoo.
- Do not edit or stage the user's unrelated `.cursorignore`, `.python-version`, or `uv.lock`.
- Add a short child-friendly `CHANGELOG.md` entry in the implementation push to `origin/main`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/zira_dashboard/_schema.py` | Add holiday mirror/sync-state tables and optional-workday metadata on the existing recruiting table. |
| `src/zira_dashboard/_odoo_time_off.py` | Define the company-only full-list `search_read` operation. |
| `src/zira_dashboard/odoo_client.py` | Expose the full-list read while preserving the scoped range reader. |
| `src/zira_dashboard/company_holidays.py` | Normalize UTC datetimes, replace the mirror transactionally, expose cached lookup, and report sync health. |
| `src/zira_dashboard/optional_workday.py` | Classify Saturday/holiday dates, resolve lifecycle state, and find adjacent normal workdays. |
| `src/zira_dashboard/app.py` | Run holiday sync immediately at startup and every ten minutes. |
| `src/zira_dashboard/shift_config.py` | Make holidays override weekday rules and use Saturday default hours. |
| `src/zira_dashboard/time_off_context.py` | Read known closures from the mirror for staffing/inbox coverage. |
| `src/zira_dashboard/routes/timeclock_time_off.py` | Read known closures from the mirror for Who's Out. |
| `src/zira_dashboard/saturday_recruiting.py` | Generalize the prior-normal-workday deadline rule. |
| `src/zira_dashboard/saturday_recruiting_store.py` | Persist optional-day metadata and safely activate/cancel recruiting. |
| `src/zira_dashboard/routes/saturday_recruiting.py` | Admit holidays into the existing manager recruiting API. |
| `src/zira_dashboard/routes/staffing.py` | Stop holiday seeding, build existing UI context, enforce volunteer-only publish, and skip closed holidays. |
| `src/zira_dashboard/routes/rotations.py` | Reuse optional-day restrictions in Auto-center updates, reset, and rebuild. |
| `src/zira_dashboard/staffing_view.py` | Generalize the optional-availability input with compatibility. |
| `src/zira_dashboard/templates/staffing.html` | Render holiday-aware labels in existing controls and panels. |
| `src/zira_dashboard/static/saturday-recruiting.js` | Use optional-workday error copy; keep selectors/endpoints. |
| `src/zira_dashboard/routes/timeclock.py` | Add kind/name to kiosk banner and reminder contexts. |
| `src/zira_dashboard/routes/timeclock_saturday.py` | Build date-aware employee copy on existing URLs. |
| `src/zira_dashboard/templates/timeclock_home.html` | Render Saturday or holiday banner/schedule wording. |
| `src/zira_dashboard/templates/timeclock_saturday_offer.html` | Render Saturday or named-holiday offer wording. |
| `src/zira_dashboard/templates/timeclock_saturday_partial.html` | Render a kind-aware title and availability error. |
| `src/zira_dashboard/templates/timeclock_saturday_confirm.html` | Render a kind-aware commitment sentence. |
| `src/zira_dashboard/templates/timeclock_success.html` | Render the correct work reminder title. |
| `src/zira_dashboard/timeclock_i18n.py` | Add corresponding Spanish strings. |
| `src/zira_dashboard/saturday_work_reminder.py` | Carry persisted kind/name into punch-out reminders. |
| `src/zira_dashboard/employee_notifications.py` | Store kind-aware cancellation copy with existing dedupe. |
| `src/zira_dashboard/time_off_reminder.py` | Skip closed holidays for next operational day. |
| `src/zira_dashboard/exception_inbox.py` | Skip closed holidays for next-schedule reminders. |
| `src/zira_dashboard/goat_watch.py` | Keep alerts through the next operational day. |
| `src/zira_dashboard/rotation_training.py` | Count only shared operational workdays. |
| `tests/test_company_holidays.py` | Mirror normalization, replacement, failure, cache, and timezone coverage. |
| `tests/test_optional_workday.py` | Classification, precedence, publication, and adjacent-day tests. |
| `tests/test_odoo_client_leaves.py` | Odoo domain and read-only tests. |
| `tests/test_shift_config_for.py` | Holiday workday and effective-hours tests. |
| `tests/test_saturday_recruiting.py` | Holiday deadline tests. |
| `tests/test_saturday_recruiting_store.py` | Metadata, activation conversion, atomicity, and regressions. |
| `tests/test_saturday_recruiting_manager_routes.py` | Manager API, Odoo non-write, and cancellation tests. |
| `tests/test_staffing_holiday_work.py` | Closed/render/recruit/save/publish scheduler behavior. |
| `tests/test_staffing_rotations.py` | Optional-day Auto/reset restrictions. |
| `tests/test_staffing_static.py` | Existing browser hooks and recruiting endpoint compatibility. |
| `tests/test_staffing_view.py` | Optional-availability view-model compatibility. |
| `tests/test_time_off_context.py` | Local mirror use for coverage. |
| `tests/test_time_off_routes.py` | Local mirror use for Who's Out. |
| `tests/test_timeclock_saturday_recruiting.py` | Named-holiday offer and commitment copy. |
| `tests/test_timeclock_notifications_routes.py` | Kind-aware cancellation card rendering. |
| `tests/test_saturday_work_reminder.py` | Holiday reminder metadata/copy. |
| `tests/test_time_off_reminder.py` | Closed-holiday skip behavior. |
| `tests/test_exception_inbox.py` | Closed-holiday next-schedule reminder behavior. |
| `tests/test_goat_watch.py` | Alert visibility across closed/worked holidays. |
| `tests/test_rotation_training.py` | Closed-holiday training behavior. |
| `tests/test_page_warmer.py` | Ten-minute warmer registration. |
| `CHANGELOG.md` | Child-friendly shipped-feature note. |

## Core Contracts

The local holiday mirror exposes:

```python
@dataclass(frozen=True)
class CompanyHoliday:
    odoo_id: int
    name: str
    date_from: date
    date_to: date
    odoo_date_from: str
    odoo_date_to: str


@dataclass(frozen=True)
class HolidaySyncHealth:
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    last_error: str | None
```

Public signatures:

- `refresh(*, fetcher: Callable[[], list[dict]] | None = None, now: datetime | None = None) -> int`
- `reload() -> dict[date, CompanyHoliday]`
- `for_day(day: date) -> CompanyHoliday | None`
- `for_range(start: date, end: date) -> list[dict]`
- `sync_health() -> HolidaySyncHealth`
- `has_synced() -> bool`

The optional-workday service owns classification and publication agreement:

```python
OptionalWorkdayKind = Literal["saturday", "holiday"]


@dataclass(frozen=True)
class OptionalWorkday:
    day: date
    kind: OptionalWorkdayKind
    name: str
    holiday_odoo_id: int | None


@dataclass(frozen=True)
class OptionalWorkdayState:
    workday: OptionalWorkday
    recruiting_status: str | None
    schedule_published: bool
    operational: bool
```

Public signatures:

- `for_day(day: date) -> OptionalWorkday | None`
- `state_for_day(day: date) -> OptionalWorkdayState | None`
- `holiday_is_explicitly_published(day: date) -> bool`
- `previous_normal_workday(day: date, work_weekdays: frozenset[int]) -> date`
- `next_normal_workday(day: date, work_weekdays: frozenset[int]) -> date`

`for_day()` checks `company_holidays.for_day()` before Saturday. A holiday is
operational only when the current mirror id matches the recruiting row's
captured `holiday_odoo_id`, recruiting is `published`, and the schedule is
published. Removed/replaced Odoo holidays cannot be reopened by stale history.

---

## Task 1: Add the schema and read-only Odoo operation

**Files:**

- Modify: `src/zira_dashboard/_schema.py`
- Modify: `src/zira_dashboard/_odoo_time_off.py`
- Modify: `src/zira_dashboard/odoo_client.py`
- Modify: `tests/test_odoo_client_leaves.py`

- [ ] **Step 1: Write failing Odoo-domain tests**

Capture the low-level call:

```python
def test_fetch_company_holidays_reads_only_unscoped_company_rows():
    calls = []

    def execute(model, method, domain, **kwargs):
        calls.append((model, method, domain, kwargs))
        return []

    assert _odoo_time_off.fetch_company_holidays(execute) == []
    assert calls == [(
        "resource.calendar.leaves",
        "search_read",
        [("resource_id", "=", False), ("calendar_id", "=", False)],
        {"fields": ["id", "name", "date_from", "date_to"]},
    )]
```

Add a facade test proving `odoo_client.fetch_company_holidays()` delegates.
Retain the current TTL/range test proving `fetch_public_holidays` includes
`calendar_id`.

- [ ] **Step 2: Run the focused test and confirm the API is absent**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_odoo_client_leaves.py -q
```

Expected: FAIL because `fetch_company_holidays` is not defined.

- [ ] **Step 3: Add the Odoo reader**

```python
def fetch_company_holidays(execute_fn: Callable[..., Any]) -> list[dict]:
    return execute_fn(
        "resource.calendar.leaves",
        "search_read",
        [("resource_id", "=", False), ("calendar_id", "=", False)],
        fields=["id", "name", "date_from", "date_to"],
    )
```

Expose:

```python
def fetch_company_holidays() -> list[dict]:
    """Return the complete whole-company Public Holiday set from Odoo."""
    return _odoo_time_off.fetch_company_holidays(execute)
```

Do not change `_PUBLIC_HOLIDAYS_TTL_SECONDS`, `_public_holidays_cache`, or the
current range facade.

- [ ] **Step 4: Add idempotent schema**

```sql
CREATE TABLE IF NOT EXISTS company_holidays (
  odoo_id          INTEGER PRIMARY KEY,
  name             TEXT NOT NULL,
  date_from        DATE NOT NULL,
  date_to          DATE NOT NULL,
  odoo_date_from   TEXT NOT NULL,
  odoo_date_to     TEXT NOT NULL,
  last_pulled_at   TIMESTAMPTZ NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (date_to >= date_from)
);

CREATE TABLE IF NOT EXISTS company_holiday_sync_state (
  singleton        BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  last_success_at  TIMESTAMPTZ,
  last_attempt_at  TIMESTAMPTZ,
  last_error       TEXT
);
```

Remove the fresh table's Saturday-only day check and add:

```sql
day_kind        TEXT NOT NULL DEFAULT 'saturday'
  CHECK (day_kind IN ('saturday', 'holiday')),
event_name      TEXT,
holiday_odoo_id INTEGER
```

Add upgrade-safe statements:

```sql
ALTER TABLE saturday_recruitments
  DROP CONSTRAINT IF EXISTS saturday_recruitments_day_check;
ALTER TABLE saturday_recruitments
  ADD COLUMN IF NOT EXISTS day_kind TEXT NOT NULL DEFAULT 'saturday';
ALTER TABLE saturday_recruitments
  ADD COLUMN IF NOT EXISTS event_name TEXT;
ALTER TABLE saturday_recruitments
  ADD COLUMN IF NOT EXISTS holiday_odoo_id INTEGER;
```

Add the check idempotently:

```sql
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'saturday_recruitments_day_kind_check'
  ) THEN
    ALTER TABLE saturday_recruitments
      ADD CONSTRAINT saturday_recruitments_day_kind_check
      CHECK (day_kind IN ('saturday', 'holiday'));
  END IF;
END $$;
```

Keep `holiday_odoo_id` without a foreign key so Odoo deletion cannot erase
recruiting history.

- [ ] **Step 5: Run tests**

Run the Step 2 command.

Expected: PASS, including old scoped range-reader tests.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/_schema.py src/zira_dashboard/_odoo_time_off.py src/zira_dashboard/odoo_client.py tests/test_odoo_client_leaves.py
git commit -m "feat: add company holiday read model"
```

---

## Task 2: Build the transactional holiday mirror and warmer

**Files:**

- Create: `src/zira_dashboard/company_holidays.py`
- Create: `tests/test_company_holidays.py`
- Modify: `src/zira_dashboard/app.py`
- Modify: `tests/test_page_warmer.py`

- [ ] **Step 1: Write pure normalization tests**

Cover naive Odoo UTC to plant-local date conversion, inclusive multi-day
expansion, missing/boolean ids, blank names, invalid datetimes,
end-before-start, and rejection of the complete response when one row is bad.

```python
def test_normalize_odoo_utc_to_plant_dates():
    holiday = company_holidays.normalize_odoo_row({
        "id": 81,
        "name": "Black Friday",
        "date_from": "2026-11-27 06:00:00",
        "date_to": "2026-11-28 05:59:59",
    })

    assert holiday.date_from == date(2026, 11, 27)
    assert holiday.date_to == date(2026, 11, 27)
```

- [ ] **Step 2: Write database integration tests**

Use the repository's `DATABASE_URL` skip pattern. Test successful upsert/delete,
valid empty clearing, fetch/normalization failure preservation, bounded error
recording, local range lookup, never-synced versus synced-empty, and cache
reload only after commit.

- [ ] **Step 3: Run tests and confirm the module is absent**

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_company_holidays.py -q
```

Expected: FAIL during collection.

- [ ] **Step 4: Implement normalization and cached lookup**

```python
def _plant_date(value: object) -> date:
    if not isinstance(value, str):
        raise InvalidHolidayRow("holiday datetime must be text")
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return parsed.astimezone(SITE_TZ).date()
```

`reload()` reads every mirror row and builds an inclusive date map under a
module lock. For overlapping holidays, choose the lowest Odoo id and log it.
`for_range()` returns compatibility dictionaries with `id`, `name`, ISO
`date_from`/`date_to`, and `calendar_id=False`.

- [ ] **Step 5: Implement all-or-nothing refresh**

Normalize before opening the replacement transaction. In one `db.cursor()`:
upsert normalized rows, delete absent ids (or all rows for a valid empty set),
and upsert success state with `last_error=NULL`. On failure, update only
`last_attempt_at` and `last_error=str(exc)[:500]` in a separate transaction,
then re-raise.

After successful commit, call `reload()`,
`staffing.invalidate_all_schedule_caches()`, and
`_http_cache.invalidate_all_cache()`.

- [ ] **Step 6: Register startup/ten-minute sync**

```python
async def _tick_company_holidays():
    from . import company_holidays
    await asyncio.to_thread(company_holidays.refresh)
```

Register `("company holidays", _tick_company_holidays, 600)` in `_WARMERS`.
The existing runner performs the immediate first tick after its short stagger.
Add a structural interval test.

- [ ] **Step 7: Run focused tests**

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_company_holidays.py tests/test_page_warmer.py -q
```

Expected: PASS; DB tests may skip only when `DATABASE_URL` is absent.

- [ ] **Step 8: Commit**

```bash
git add src/zira_dashboard/company_holidays.py src/zira_dashboard/app.py tests/test_company_holidays.py tests/test_page_warmer.py
git commit -m "feat: mirror Odoo company holidays"
```

---

## Task 3: Move closure calendars to the local mirror

**Files:**

- Modify: `src/zira_dashboard/time_off_context.py`
- Modify: `src/zira_dashboard/routes/timeclock_time_off.py`
- Modify: `tests/test_time_off_context.py`
- Modify: `tests/test_time_off_routes.py`
- Modify: `tests/test_time_off_local_backfill.py`

- [ ] **Step 1: Change tests at the I/O seams**

Replace calendar/coverage monkeypatches of `odoo_client.fetch_public_holidays`
with `company_holidays.for_range`. Assert mirrored holidays still fan out with
`source="holiday"` and `label="Plant Closed"`. Add a backfill regression proving
calendar-scoped checks still call the live range facade.

- [ ] **Step 2: Run focused tests and confirm failure**

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_time_off_context.py tests/test_time_off_routes.py tests/test_time_off_local_backfill.py -q
```

Expected: FAIL because render paths still call live Odoo.

- [ ] **Step 3: Switch only plant-wide render readers**

In `time_off_context._holiday_names` and
`routes.timeclock_time_off._approved_by_day`, call
`company_holidays.for_range(start_d, end_d)`. Retain fail-soft behavior for a
local lookup error. Do not modify `time_off_local_backfill.py`.

- [ ] **Step 4: Run focused tests**

Run Step 2. Expected: PASS, including the scoped backfill regression.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/time_off_context.py src/zira_dashboard/routes/timeclock_time_off.py tests/test_time_off_context.py tests/test_time_off_routes.py tests/test_time_off_local_backfill.py
git commit -m "refactor: render holidays from local mirror"
```

---

## Task 4: Centralize optional-workday and effective-shift rules

**Files:**

- Create: `src/zira_dashboard/optional_workday.py`
- Create: `tests/test_optional_workday.py`
- Modify: `src/zira_dashboard/shift_config.py`
- Modify: `tests/test_shift_config_for.py`
- Modify: `tests/test_shift_config_saturday.py`
- Modify: `src/zira_dashboard/saturday_recruiting.py`
- Modify: `tests/test_saturday_recruiting.py`

- [ ] **Step 1: Write classifier/publication tests**

Test ordinary Saturday, weekday holiday, Saturday-holiday precedence, normal
weekday, schedule-only/recruiting-only/mismatched-id closed states, matching
dual publication, removed-holiday history, and adjacent normal-day searches
that skip consecutive holidays. Monkeypatch mirror/store/staffing seams so no
database is needed.

- [ ] **Step 2: Write shift tests**

```python
def test_weekday_holiday_is_closed_even_with_posted_weekday_schedule(monkeypatch):
    day = date(2026, 11, 27)
    monkeypatch.setattr(optional_workday, "for_day", lambda _day: HOLIDAY)
    monkeypatch.setattr(
        optional_workday, "holiday_is_explicitly_published", lambda _day: False,
    )
    monkeypatch.setattr(shift_config, "work_weekdays", lambda: frozenset(range(5)))

    assert shift_config.is_workday(day) is False
```

Also assert worked holiday uses Saturday hours/breaks, custom wins, editor
proposes Saturday hours while closed, weekday stays unchanged, and legacy
published Saturday stays active.

- [ ] **Step 3: Write deadline tests**

Extend:

```python
def response_deadline(
    day: date,
    work_weekdays: frozenset[int],
    shift_start_for: Callable[[date], time],
    is_holiday: Callable[[date], bool] = lambda _day: False,
) -> datetime:
```

Test Black Friday closes Wednesday because Thursday is a holiday, consecutive
holidays search farther back, and the 14-day bound remains.

- [ ] **Step 4: Run tests and confirm failures**

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_optional_workday.py tests/test_shift_config_for.py tests/test_shift_config_saturday.py tests/test_saturday_recruiting.py -q
```

Expected: FAIL because classifier/precedence do not exist.

- [ ] **Step 5: Implement optional-workday service**

Use lazy lifecycle imports to avoid the existing import cycle:

```python
def for_day(day: date) -> OptionalWorkday | None:
    holiday = company_holidays.for_day(day)
    if holiday is not None:
        return OptionalWorkday(day, "holiday", holiday.name, holiday.odoo_id)
    if day.weekday() == 5:
        return OptionalWorkday(day, "saturday", "Saturday", None)
    return None


def holiday_is_explicitly_published(day: date) -> bool:
    workday = for_day(day)
    if workday is None or workday.kind != "holiday":
        return False
    from . import saturday_recruiting_store, staffing
    bundle = saturday_recruiting_store.get(day)
    schedule = staffing.load_schedule(day)
    return bool(
        bundle
        and bundle.recruitment.day_kind == "holiday"
        and bundle.recruitment.holiday_odoo_id == workday.holiday_odoo_id
        and bundle.recruitment.status == "published"
        and schedule.published
    )
```

- [ ] **Step 6: Update shared shift behavior**

Evaluate holiday before normal weekday:

```python
optional = optional_workday.for_day(day)
if optional is not None and optional.kind == "holiday":
    try:
        return optional_workday.holiday_is_explicitly_published(day)
    except Exception:
        return False
if day.weekday() in work_weekdays():
    return True
```

Generalize `_use_saturday_default` to `_use_optional_default`: true for
Saturday/holiday in editor mode and only for an operational date in hot-path
mode. Keep hours-source values unchanged for CSS/JS compatibility.

- [ ] **Step 7: Implement prior-normal-day logic**

Adjacent normal-day helpers use configured weekdays and skip mirror holidays.
They never count separately published optional dates. Allow deadline creation
for a Saturday or `is_holiday(day)`, then use `previous_normal_workday`.

- [ ] **Step 8: Run focused tests**

Run Step 4. Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/zira_dashboard/optional_workday.py src/zira_dashboard/shift_config.py src/zira_dashboard/saturday_recruiting.py tests/test_optional_workday.py tests/test_shift_config_for.py tests/test_shift_config_saturday.py tests/test_saturday_recruiting.py
git commit -m "feat: centralize optional workday rules"
```

---

## Task 5: Generalize recruiting persistence and safe activation

**Files:**

- Modify: `src/zira_dashboard/saturday_recruiting_store.py`
- Modify: `tests/test_saturday_recruiting_store.py`

- [ ] **Step 1: Add failing metadata tests**

Extend `Recruitment` with `day_kind`, `event_name`, and `holiday_odoo_id`.
Extend employee-facing `Offer`, `HomeBanner`, and `CommitmentStatus` with
`day_kind` and `event_name`. Test serialize/load/offer/banner/commitment round
trips and old-row Saturday defaults.

- [ ] **Step 2: Add activation transaction tests**

Cover Saturday regression, current/matching holiday requirement, stored audit
metadata, idempotence, weekday draft clearing, posted snapshot preservation,
failure atomicity, cancellation, and the date remaining closed after any
failed activation.

- [ ] **Step 3: Run store tests and confirm failures**

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_saturday_recruiting_store.py -q
```

Expected: FAIL on new fields/behavior.

- [ ] **Step 4: Hydrate and serialize metadata**

Select metadata in `_load_bundle`, include it in the existing `"recruitment"`
JSON object, and copy kind/name into employee-facing results. Keep route paths
unchanged.

- [ ] **Step 5: Generalize `activate`**

```python
def activate(
    day: date,
    shift_start: time,
    shift_end: time,
    response_deadline: datetime,
    requested_counts: Mapping[int, int],
    actor: str | None,
    now: datetime,
    *,
    day_kind: Literal["saturday", "holiday"] = "saturday",
    event_name: str | None = None,
    holiday_odoo_id: int | None = None,
) -> RecruitmentBundle:
```

Validate Saturdays for `saturday`. For `holiday`, require current mirror
id/name match. Inside the advisory-lock transaction:

1. validate openings before mutation;
2. lock/load schedule;
3. for holiday, use `staffing.draft_from_posted`, persist its snapshot and
   unpublished state, delete all live assignments, and clear sources/overrides;
4. for Saturday, retain current default-only clearing and manual/published
   rejection;
5. insert metadata/openings; and
6. return the loaded bundle.

Use existing `cur=` support or same-transaction SQL. Invalidate the day cache
only after commit.

- [ ] **Step 6: Preserve audit on cancellation**

Set `published=FALSE` and clear live assignments/sources/overrides, but never
null an existing `published_snapshot`.

- [ ] **Step 7: Run tests**

Run Step 3. Expected: PASS, with DB skips only under the normal fixture rule.

- [ ] **Step 8: Commit**

```bash
git add src/zira_dashboard/saturday_recruiting_store.py tests/test_saturday_recruiting_store.py
git commit -m "feat: persist holiday recruiting lifecycle"
```

---

## Task 6: Generalize the manager recruiting API

**Files:**

- Modify: `src/zira_dashboard/routes/saturday_recruiting.py`
- Modify: `src/zira_dashboard/static/saturday-recruiting.js`
- Modify: `tests/test_saturday_recruiting_manager_routes.py`

- [ ] **Step 1: Add failing manager-route tests**

Test holiday/Saturday activation, ordinary-date rejection, effective minimum
demand, configured holiday hours, holiday-skipping deadline, stored-metadata
openings/cancel, kind-aware notification arguments, and no Odoo
`create`/`write`/`unlink`.

- [ ] **Step 2: Run tests and confirm failures**

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_saturday_recruiting_manager_routes.py -q
```

Expected: FAIL on Saturday-only guards.

- [ ] **Step 3: Classify activation dates**

```python
workday = optional_workday.for_day(day)
if workday is None:
    raise HTTPException(status_code=422, detail="This date is not an optional workday.")
```

Pass kind/name/id to `store.activate`. Supply a mirror predicate to deadline
calculation. For openings/commitment cancellation/whole cancellation, load the
persisted bundle and validate lifecycle rather than weekday, preserving access
to historical rows after Odoo changes.

- [ ] **Step 4: Keep APIs stable and copy neutral**

Keep prefix/body/selectors. Change visible generic errors to “optional workday
recruiting.” Label cancellation warnings from persisted kind/name.

- [ ] **Step 5: Run focused tests**

Run Step 2. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/saturday_recruiting.py src/zira_dashboard/static/saturday-recruiting.js tests/test_saturday_recruiting_manager_routes.py
git commit -m "feat: recruit for optional holiday work"
```

---

## Task 7: Render closed holidays in the existing scheduler

**Files:**

- Modify: `src/zira_dashboard/routes/staffing.py`
- Modify: `src/zira_dashboard/staffing_view.py`
- Create: `tests/test_staffing_holiday_work.py`
- Modify: `tests/test_staffing_view.py`

- [ ] **Step 1: Write failing scheduler tests**

Cover adjacent-day skipping, first-sync future-draft pause, synced-empty normal
seeding, blank holiday draft, old-draft closed rendering, all non-reserves Off,
holiday name/closed/default-hours/Recruit/Auto context, Saturday-holiday
precedence, unchanged scheduler DOM controls, and fail-closed lookup warning.

- [ ] **Step 2: Run tests and confirm failures**

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_holiday_work.py tests/test_staffing_view.py -q
```

Expected: FAIL because context is Saturday-only.

- [ ] **Step 3: Pause unsafe first-sync seeding**

At `_seed_new_future_draft`, return loaded schedule when
`company_holidays.has_synced()` is false. Once synced, use blank assignments
for either optional kind; use normal defaults only for normal dates. Viewing a
holiday does not delete old drafts; activation owns transactional cleanup.

- [ ] **Step 4: Build date-neutral context**

Compute `optional_day` once and use it for preparation, nonstandard styling,
recruiting context, volunteer-only bay input, hours, and publish-lock copy.
Keep existing `saturday_*` keys and add:

```python
{
    "is_optional_workday": optional_day is not None,
    "optional_day_kind": optional_day.kind if optional_day else None,
    "optional_day_name": optional_day.name if optional_day else None,
    "optional_day_label": (
        optional_day.name if optional_day and optional_day.kind == "holiday"
        else "Saturday"
    ),
    "optional_recruiting_label": (
        "Holiday recruiting" if optional_day and optional_day.kind == "holiday"
        else "Saturday recruiting"
    ),
    "holiday_sync_warning": (
        "Odoo holidays have not synced yet. New future drafts are paused."
        if not company_holidays.has_synced() else ""
    ),
}
```

Retain `day_is_saturday` for old browser compatibility, but not decisions.

- [ ] **Step 5: Generalize the view input safely**

Let `build_staffing_bays` accept `optional_commitments`, temporarily accepting
`saturday_commitments` as an alias and rejecting both together.

- [ ] **Step 6: Rebuild holiday bays as volunteer-only**

Pass `{}` for an optional date without active recruiting so everyone is Off.
With recruiting, pass only committed plus manager-corrected available people.
Preserve full-day Time Off exclusion and partial-hours metadata.

- [ ] **Step 7: Run tests**

Run Step 2. Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/zira_dashboard/routes/staffing.py src/zira_dashboard/staffing_view.py tests/test_staffing_holiday_work.py tests/test_staffing_view.py
git commit -m "feat: show closed holidays in scheduler"
```

---

## Task 8: Enforce optional-day save, Auto, and publication rules

**Files:**

- Modify: `src/zira_dashboard/routes/staffing.py`
- Modify: `src/zira_dashboard/routes/rotations.py`
- Modify: `tests/test_staffing_holiday_work.py`
- Modify: `tests/test_staffing_rotations.py`

- [ ] **Step 1: Add failing save/publish tests**

Test ordinary schedule cannot publish a holiday, lookup failure makes no
mutation, recruiting-open cannot schedule, closed admits only effective
volunteers, uncommitted validation is date-aware, recruiting must close,
successful publish marks both records, partial marker failure stays
operationally closed, and cancellation recloses.

- [ ] **Step 2: Add failing Auto/reset tests**

Test holiday uses Saturday volunteer roster/locks, unrecruited/open holiday
cannot Auto, reset does not restore weekday defaults, `minimum_only` is false
for optional kinds, and weekday behavior is unchanged.

- [ ] **Step 3: Run tests and confirm failures**

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_holiday_work.py tests/test_staffing_rotations.py -q
```

Expected: FAIL on weekday-specific branches.

- [ ] **Step 4: Fail closed in staffing save**

Classify before assignments/default writes. A holiday requires a matching
recruiting bundle for save and publish. Lookup error returns 409:

```json
{"ok": false, "error": "Optional workday state could not be verified. No schedule changes were saved."}
```

Use the existing volunteer publish validator for either kind. Run ordinary
weekday shortages only when not optional. Keep post-save lifecycle marker
retry; shared `is_workday` requires both states and is safe during mismatch.

- [ ] **Step 5: Generalize availability correction**

Allow the existing availability endpoint/field for active Saturday or holiday
recruiting. Reject an unrecruited closed holiday.

- [ ] **Step 6: Generalize rotation endpoints**

Replace scheduler `d.weekday() == 5` decisions with optional classification.
Preserve JSON key `"saturday_recruiting"`. Reuse current volunteer roster,
optional defaults, and protected-lock algorithms for either kind.

- [ ] **Step 7: Run tests**

Run Step 3. Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/zira_dashboard/routes/staffing.py src/zira_dashboard/routes/rotations.py tests/test_staffing_holiday_work.py tests/test_staffing_rotations.py
git commit -m "feat: enforce volunteer-only holiday schedules"
```

---

## Task 9: Make existing scheduler controls holiday-aware

**Files:**

- Modify: `src/zira_dashboard/templates/staffing.html`
- Modify: `src/zira_dashboard/static/saturday-recruiting.js`
- Modify: `tests/test_staffing_holiday_work.py`
- Modify: `tests/test_staffing_static.py`

- [ ] **Step 1: Add render/static-contract tests**

Assert named holiday text, current Hours/Auto/Recruit/response/Off/Unassigned/
goal/Publish controls, no new page/modal/navigation, Saturday regression, and
unchanged `/api/staffing/saturday-recruiting/*` calls.

- [ ] **Step 2: Run tests and confirm hard-coded copy**

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_holiday_work.py tests/test_staffing_static.py -q
```

Expected: FAIL on holiday labels.

- [ ] **Step 3: Generalize conditions and labels**

Use `is_optional_workday` instead of `day_is_saturday` for decisions. Keep
classes/ids/fields unchanged. Show `Plant closed by default`; during recruiting
keep current response/publish-lock behavior; after close show existing goal and
Publish. Label Saturday-default proposed hours explicitly for holiday work.
Place `holiday_sync_warning` in the existing warning area.

- [ ] **Step 4: Run tests**

Run Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/staffing.html src/zira_dashboard/static/saturday-recruiting.js tests/test_staffing_holiday_work.py tests/test_staffing_static.py
git commit -m "feat: label holiday work in existing scheduler"
```

---

## Task 10: Make employee offers, reminders, and cancellations date-aware

**Files:**

- Modify: `src/zira_dashboard/routes/timeclock.py`
- Modify: `src/zira_dashboard/routes/timeclock_saturday.py`
- Modify: `src/zira_dashboard/templates/timeclock_home.html`
- Modify: `src/zira_dashboard/templates/timeclock_saturday_offer.html`
- Modify: `src/zira_dashboard/templates/timeclock_saturday_partial.html`
- Modify: `src/zira_dashboard/templates/timeclock_saturday_confirm.html`
- Modify: `src/zira_dashboard/templates/timeclock_success.html`
- Modify: `src/zira_dashboard/timeclock_i18n.py`
- Modify: `src/zira_dashboard/saturday_work_reminder.py`
- Modify: `src/zira_dashboard/employee_notifications.py`
- Modify: `tests/test_timeclock_saturday_recruiting.py`
- Modify: `tests/test_saturday_work_reminder.py`
- Modify: `tests/test_timeclock_notifications_routes.py`

- [ ] **Step 1: Add failing employee-surface tests**

Cover Saturday regression; `Holiday Work Available — Black Friday`; named
holiday question; partial error; commitment sentence; holiday schedule;
holiday reminder; cancellation; English/Spanish-primary; unchanged forms,
DOM hooks, and notification dedupe.

- [ ] **Step 2: Run tests and confirm failures**

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_timeclock_saturday_recruiting.py tests/test_saturday_work_reminder.py tests/test_timeclock_notifications_routes.py -q
```

Expected: FAIL because copy lacks metadata.

- [ ] **Step 3: Carry kind/name through contexts**

Add `day_kind`, `event_name`, and `is_holiday` to offer, shared banner,
commitment, and reminder contexts. Keep existing variable/URL compatibility.

- [ ] **Step 4: Add bilingual glossary entries**

```python
"Holiday Work Available — {name}": "Trabajo disponible en el día festivo — {name}",
"Can you work this holiday, {name}, on {date}?":
    "¿Puedes trabajar este día festivo, {name}, el {date}?",
"By confirming, you commit to work this holiday from {hours}.":
    "Al confirmar, te comprometes a trabajar este día festivo de {hours}.",
"Holiday work reminder": "Recordatorio de trabajo en día festivo",
"Holiday work cancelled": "Trabajo del día festivo cancelado",
"{name} work was cancelled. Do not report to work.":
    "El trabajo de {name} fue cancelado. No te presentes a trabajar.",
"Availability must use 30-minute increments and stay within the optional shift.":
    "La disponibilidad debe usar incrementos de 30 minutos y mantenerse dentro del turno opcional.",
```

Render variables through `t()`/`td()` so names/dates stay escaped.

- [ ] **Step 5: Use persisted metadata**

Select kind/name in `saturday_work_reminder.claim_for_person`. Return
`title_key` and event name.

```python
def create_saturday_cancelled(
    person_odoo_id: int,
    day: date,
    *,
    day_kind: str = "saturday",
    event_name: str | None = None,
) -> None:
```

Keep Saturday copy for Saturday. For holiday store `Holiday work cancelled`
and `{event_name or "Holiday"} work was cancelled. Do not report to work.`
Keep notification `kind` and `saturday_day` for dedupe.

- [ ] **Step 6: Run tests**

Run Step 2. Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/routes/timeclock.py src/zira_dashboard/routes/timeclock_saturday.py src/zira_dashboard/templates/timeclock_home.html src/zira_dashboard/templates/timeclock_saturday_offer.html src/zira_dashboard/templates/timeclock_saturday_partial.html src/zira_dashboard/templates/timeclock_saturday_confirm.html src/zira_dashboard/templates/timeclock_success.html src/zira_dashboard/timeclock_i18n.py src/zira_dashboard/saturday_work_reminder.py src/zira_dashboard/employee_notifications.py tests/test_timeclock_saturday_recruiting.py tests/test_saturday_work_reminder.py tests/test_timeclock_notifications_routes.py
git commit -m "feat: label employee holiday work"
```

---

## Task 11: Make remaining operational day searches honor closures

**Files:**

- Modify: `src/zira_dashboard/time_off_reminder.py`
- Modify: `src/zira_dashboard/exception_inbox.py`
- Modify: `src/zira_dashboard/goat_watch.py`
- Modify: `src/zira_dashboard/rotation_training.py`
- Modify: `tests/test_time_off_reminder.py`
- Modify: `tests/test_exception_inbox.py`
- Create: `tests/test_goat_watch.py`
- Modify: `tests/test_rotation_training.py`

- [ ] **Step 1: Add failing regression tests**

Test Friday reminder skips Monday holiday, published optional holiday can be
next operational day, schedule reminder skips closure, GOAT visibility extends
through closure, and training skips closed holiday but counts worked holiday.

- [ ] **Step 2: Run tests and confirm failures**

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_time_off_reminder.py tests/test_exception_inbox.py tests/test_goat_watch.py tests/test_rotation_training.py -q
```

Expected: FAIL on holiday cases.

- [ ] **Step 3: Use shared operational decision**

Replace weekday-only loops with bounded searches using
`shift_config.is_workday(candidate)`, retaining defensive fallbacks. Replace
both training weekday branches so planning/reconciliation agree. Do not change
`time_off_local_backfill.py`; its calendar-scope repair logic is separate.

- [ ] **Step 4: Audit remaining branches**

```bash
rg -n "weekday\\(\\).*work_weekdays|in work_weekdays\\(|weekday\\(\\) == 5|weekday\\(\\) != 5" src/zira_dashboard
```

Expected matches are limited to optional classification/normal-day search,
Odoo employee-calendar/backfill logic, calendar presentation, and compatibility
comments. Investigate every other match.

- [ ] **Step 5: Run tests**

Run Step 2. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/time_off_reminder.py src/zira_dashboard/exception_inbox.py src/zira_dashboard/goat_watch.py src/zira_dashboard/rotation_training.py tests/test_time_off_reminder.py tests/test_exception_inbox.py tests/test_goat_watch.py tests/test_rotation_training.py
git commit -m "fix: honor holidays across workday reminders"
```

---

## Task 12: Verify acceptance and document the shipped feature

**Files:**

- Modify: `CHANGELOG.md`
- Verify: all files above

- [ ] **Step 1: Run the focused suite**

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest \
  tests/test_company_holidays.py \
  tests/test_optional_workday.py \
  tests/test_odoo_client_leaves.py \
  tests/test_shift_config_for.py \
  tests/test_shift_config_saturday.py \
  tests/test_saturday_recruiting.py \
  tests/test_saturday_recruiting_store.py \
  tests/test_saturday_recruiting_manager_routes.py \
  tests/test_staffing_holiday_work.py \
  tests/test_staffing_rotations.py \
  tests/test_time_off_context.py \
  tests/test_time_off_routes.py \
  tests/test_time_off_local_backfill.py \
  tests/test_timeclock_saturday_recruiting.py \
  tests/test_saturday_work_reminder.py \
  tests/test_timeclock_notifications_routes.py \
  tests/test_time_off_reminder.py \
  tests/test_exception_inbox.py \
  tests/test_goat_watch.py \
  tests/test_rotation_training.py \
  tests/test_page_warmer.py -q
```

Expected: PASS; DB tests skip only under existing no-`DATABASE_URL` rules.

- [ ] **Step 2: Run all tests**

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest -q
```

Expected: PASS with no new feature-related skips/warnings.

- [ ] **Step 3: Run lint**

```bash
.venv/bin/python -m ruff check src tests
```

Expected: `All checks passed!`

- [ ] **Step 4: Audit Odoo access**

```bash
rg -n "resource\\.calendar\\.leaves|fetch_company_holidays|fetch_public_holidays" src/zira_dashboard
```

Expected: mirror reaches only `search_read`; no holiday
`create`/`write`/`unlink`; live range reader remains in local backfill.

- [ ] **Step 5: Audit unchanged UI structure**

```bash
git diff -- src/zira_dashboard/templates/staffing.html src/zira_dashboard/templates/timeclock_home.html src/zira_dashboard/static/saturday-recruiting.js
```

Expected: existing components/actions/hooks remain; changes are conditional
labels/context.

- [ ] **Step 6: Add child-friendly What's New note**

Run `date '+%I:%M %p'` and use its exact output in a new heading titled
`Company holidays now close the plant schedule`. Add this bullet:

```markdown
- **Odoo holidays now show up here on their own.** The plant starts closed on those days. A manager can still ask for volunteers and post a work plan with the same screen used for Saturday work. This does not change holiday pay in Odoo.
```

- [ ] **Step 7: Commit the note**

```bash
git add CHANGELOG.md
git commit -m "docs: explain optional holiday work"
```

- [ ] **Step 8: Rebase safely and push**

```bash
git status --short
git pull --rebase origin main
ZIRA_API_KEY=test .venv/bin/python -m pytest -q
git push origin main
```

Expected: implementation commits and note are on `origin/main`; unrelated
untracked files remain unstaged.

## Acceptance Checklist

- [ ] A mirrored unscoped Odoo holiday appears without local entry.
- [ ] A calendar-scoped Odoo leave stays available to backfill but does not
  close the whole plant.
- [ ] A weekday holiday is closed despite an old saved/posted schedule.
- [ ] No normal defaults seed into a new holiday.
- [ ] Existing Hours, Auto, Recruit, response, Off/Unassigned, goal, and
  Publish controls render with holiday wording.
- [ ] Demand uses enabled-center minimums and deadline uses previous normal
  non-holiday workday.
- [ ] Only volunteers or manager-corrected available people can be scheduled.
- [ ] Worked holiday uses Saturday defaults unless custom hours exist.
- [ ] Schedule and recruiting publication agree before operational consumers
  treat the holiday as active.
- [ ] Cancellation closes the date and notifies committed employees.
- [ ] Saturday and normal weekday regressions pass.
- [ ] Odoo holiday integration is read-only and leaves pay rules alone.
