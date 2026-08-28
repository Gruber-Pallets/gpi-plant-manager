# Odoo Attendance Live Location Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Odoo attendance the single live source for each production worker's work-center location while Plant Manager continues to own only the start and end of the workday, with accurate production matching and verified manager corrections for bad source data.

**Architecture:** Add a durable local mirror of raw Odoo attendance, project that mirror into one atomic location timeline, and make every live staffing, production, exception, and breakdown decision consume that timeline. Roll out behind an `off`/`shadow`/`live` setting: mirror and compare first, then enable strict matching at a clean workday boundary. Mutations use durable jobs, version-aware Odoo reads, post-write verification, targeted day recalculation, and existing inbox audit events.

**Tech Stack:** Python 3.11, FastAPI, Jinja2, PostgreSQL/psycopg2, Odoo XML-RPC, vanilla JavaScript/CSS, pytest, Ruff.

## Global Constraints

- Preserve Plant Manager clock-in and clock-out as the only day-boundary actions. Clock-in must create or adopt an Odoo attendance without choosing a work center; clock-out must close the current Odoo attendance.
- Treat Luke's ERP plant-floor app as the owner of first work-center selection and routine work-center transfers. Plant Manager must not create routine transfers from Timeclock, Staffing, or breakdown rows after live mode is enabled.
- Use raw Odoo attendance identity and `write_date` as the concurrency boundary. Never repair, split, delete, or overwrite a row from a stale preview.
- Retain the raw Odoo work-center ID/name and department ID/name even when Plant Manager cannot map them. An unknown Odoo work center is visible but receives no meter credit.
- Department policy is configurable. Seed Maintenance and Supervisor as not requiring a work center; all other departments require one.
- The five-minute first-location grace changes warning urgency only. It never creates a location, credits production, or fills a time gap.
- A person assigned to two distinct work centers for the same instant has invalid coverage at both during the overlap. Duplicate rows for the same work center collapse into one valid location.
- Multiple valid workers at the same work center split each production sample equally. Every positive production sample must be conserved exactly: named worker credit plus unassigned credit equals the source sample.
- Keep existing testing offsets and machine-breakdown production exclusions. Stop using schedules or legacy manual staffing attributions as real-person fallback on strict days.
- Preserve already-computed history at cutover. A historical Odoo attendance creation, edit, or deletion after mirror baseline marks the touched local day strict and recalculates it from Odoo.
- Enable `live` mode before the first production of a workday. Schedule rollback to `shadow` at a later clean workday boundary as well. Do not switch a partly computed day between fallback and strict matching.
- Keep every partial implementation push operationally `off` or `shadow`. A `live` save requires Task 13's fresh readiness gate, so an early deployment cannot activate half-built strict behavior.
- Each push to `main` must include a short, child-readable `CHANGELOG.md` note that says what the pushed slice changes and how it helps.
- Preserve the user's existing changes in `.superpowers/sdd/task-7-report.md`, `.cursorignore`, `.python-version`, and `uv.lock`; do not stage them unless a later user request explicitly includes them.

---

### Task 1: Add rollout configuration and department location policy

**Files:**
- Modify: `src/zira_dashboard/_schema.py`
- Create: `src/zira_dashboard/attendance_location_policy.py`
- Modify: `src/zira_dashboard/routes/settings.py`
- Modify: `src/zira_dashboard/templates/settings.html`
- Test: `tests/test_attendance_location_schema.py`
- Test: `tests/test_attendance_location_policy.py`
- Test: `tests/test_settings_timeclock_layout.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write schema tests for the new columns and durable tables**

Add static assertions that `SCHEMA_DDL` contains:

```python
def test_attendance_location_schema_is_idempotent_and_versioned():
    ddl = SCHEMA_DDL
    assert "ADD COLUMN IF NOT EXISTS requires_work_center" in ddl
    assert "ADD COLUMN IF NOT EXISTS requires_work_center_explicit" in ddl
    assert "CREATE TABLE IF NOT EXISTS odoo_attendance_mirror" in ddl
    assert "odoo_attendance_id BIGINT PRIMARY KEY" in ddl
    assert "CREATE TABLE IF NOT EXISTS odoo_attendance_sync_state" in ddl
    assert "CREATE TABLE IF NOT EXISTS attendance_recalc_queue" in ddl
    assert "CREATE TABLE IF NOT EXISTS attendance_strict_days" in ddl
    assert "CREATE TABLE IF NOT EXISTS attendance_correction_jobs" in ddl
    assert "CREATE TABLE IF NOT EXISTS attendance_correction_job_events" in ddl
    assert "CREATE TABLE IF NOT EXISTS attendance_department_repairs" in ddl
```

- [ ] **Step 2: Run the schema test and verify it fails**

Run: `uv run pytest tests/test_attendance_location_schema.py -q`

Expected: FAIL because none of the new attendance-location schema exists.

- [ ] **Step 3: Add the idempotent schema**

Add `departments.requires_work_center BOOLEAN NOT NULL DEFAULT TRUE` and `departments.requires_work_center_explicit BOOLEAN NOT NULL DEFAULT FALSE`. Seed normalized Maintenance and Supervisor names to `FALSE` only where `requires_work_center_explicit = FALSE`; the Settings save sets both the chosen value and `requires_work_center_explicit = TRUE`, so a later bootstrap never overwrites an administrator's choice. Add these tables and indexes:

```sql
ALTER TABLE departments
  ADD COLUMN IF NOT EXISTS requires_work_center BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE departments
  ADD COLUMN IF NOT EXISTS requires_work_center_explicit BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE departments
   SET requires_work_center = FALSE
 WHERE requires_work_center_explicit = FALSE
   AND lower(regexp_replace(name, '^\\s*[0-9]+\\s*', ''))
       IN ('maintenance', 'supervisor');

CREATE TABLE IF NOT EXISTS odoo_attendance_mirror (
  odoo_attendance_id BIGINT PRIMARY KEY,
  employee_odoo_id BIGINT NOT NULL,
  employee_name TEXT,
  check_in_utc TIMESTAMPTZ NOT NULL,
  check_out_utc TIMESTAMPTZ,
  odoo_work_center_id BIGINT,
  odoo_work_center_name TEXT,
  odoo_department_id BIGINT,
  odoo_department_name TEXT,
  odoo_write_date TIMESTAMPTZ NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_sweep_generation BIGINT,
  deleted_at TIMESTAMPTZ,
  CHECK (check_out_utc IS NULL OR check_out_utc >= check_in_utc)
);

CREATE INDEX IF NOT EXISTS odoo_attendance_mirror_employee_time_idx
  ON odoo_attendance_mirror (employee_odoo_id, check_in_utc, check_out_utc)
  WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS odoo_attendance_sync_state (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  cursor_write_date TIMESTAMPTZ,
  cursor_id BIGINT,
  last_incremental_started_at TIMESTAMPTZ,
  last_incremental_completed_at TIMESTAMPTZ,
  last_full_sweep_completed_at TIMESTAMPTZ,
  last_full_sweep_deletion_count INTEGER NOT NULL DEFAULT 0,
  full_sweep_generation BIGINT NOT NULL DEFAULT 0,
  baseline_completed_at TIMESTAMPTZ,
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS attendance_recalc_queue (
  day DATE PRIMARY KEY,
  reason TEXT NOT NULL,
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS attendance_strict_days (
  day DATE PRIMARY KEY,
  reason TEXT NOT NULL,
  source_changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS attendance_correction_jobs (
  id BIGSERIAL PRIMARY KEY,
  item_key TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN
    ('planned', 'applying', 'verifying', 'recalculating', 'complete', 'failed')),
  target_work_center_name TEXT NOT NULL,
  target_odoo_work_center_id BIGINT NOT NULL,
  start_utc TIMESTAMPTZ NOT NULL,
  end_utc TIMESTAMPTZ,
  employee_odoo_ids JSONB NOT NULL,
  source_snapshot JSONB NOT NULL,
  operations JSONB NOT NULL,
  completed_operations JSONB NOT NULL DEFAULT '[]'::jsonb,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  verification_failure_count INTEGER NOT NULL DEFAULT 0,
  actor_email TEXT,
  actor_name TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  last_error TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS attendance_correction_jobs_active_item_idx
  ON attendance_correction_jobs (item_key)
  WHERE status IN ('planned', 'applying', 'verifying', 'recalculating');

CREATE TABLE IF NOT EXISTS attendance_correction_job_events (
  id BIGSERIAL PRIMARY KEY,
  correction_job_id BIGINT NOT NULL REFERENCES attendance_correction_jobs(id),
  phase TEXT NOT NULL,
  result TEXT NOT NULL,
  detail JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS attendance_correction_job_events_job_idx
  ON attendance_correction_job_events (correction_job_id, id);

CREATE TABLE IF NOT EXISTS attendance_department_repairs (
  odoo_attendance_id BIGINT PRIMARY KEY,
  expected_write_date TIMESTAMPTZ NOT NULL,
  target_odoo_department_id BIGINT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'applying', 'complete', 'failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error TEXT
);
```

Insert the singleton sync row with `ON CONFLICT DO NOTHING`.

- [ ] **Step 4: Write policy tests before the implementation**

Cover default `off` mode, invalid setting fallback, `shadow`, `live`, clean-boundary cutover, strict historical day, and department requirements:

```python
def test_day_is_strict_for_live_cutover_or_historical_override(monkeypatch):
    cutover_day = date(2026, 8, 31)
    cutover_utc = datetime.combine(
        cutover_day,
        shift_config.shift_start_for(cutover_day),
        tzinfo=shift_config.SITE_TZ,
    ).astimezone(UTC)
    monkeypatch.setattr(policy.app_settings, "get_setting", lambda key: {
        "mode": "live",
        "cutover_at": cutover_utc.isoformat(),
        "live_gate": {
            "checked_at": (cutover_utc - timedelta(minutes=1)).isoformat(),
            "report_digest": "b617a1c0" * 8,
            "activated_at": cutover_utc.isoformat(),
        },
    })
    monkeypatch.setattr(policy, "strict_days", lambda: {date(2026, 8, 20)})
    assert policy.day_is_strict(date(2026, 8, 31)) is True
    assert policy.day_is_strict(date(2026, 8, 20)) is True
    assert policy.day_is_strict(date(2026, 8, 19)) is False
```

- [ ] **Step 5: Run the policy tests and verify they fail**

Run: `uv run pytest tests/test_attendance_location_policy.py -q`

Expected: FAIL because `attendance_location_policy` does not exist.

- [ ] **Step 6: Implement the typed policy boundary**

Create:

```python
Mode = Literal["off", "shadow", "live"]
MatchState = Literal["legacy", "pending", "strict"]

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

get_rollout_config() -> RolloutConfig
set_rollout_config(config: RolloutConfig, *, cur=None) -> None
day_is_strict(day: date) -> bool
match_state_for_day(day: date, *, now_utc: datetime | None = None) -> MatchState
live_is_active(*, now_utc: datetime | None = None) -> bool
department_requires_work_center(department_name: str | None) -> bool
set_department_requirement(department_name: str, required: bool) -> None
```

Store one dict under `app_settings` key `odoo_attendance_location`:

```json
{"mode": "off", "cutover_at": null, "live_gate": null}
```

Reject `live` without a timezone-aware cutover and a non-expired `live_gate` created by Task 13's readiness check. Until Task 13 exists, Settings may save only `off` or `shadow`; a posted `live` value returns `live_readiness_required`. Require the cutover's local time to equal the configured workday boundary, and surface a validation error rather than silently shifting it.

`live_is_active()` returns true only after `live_gate.activated_at`. `match_state_for_day` is `pending` after a scheduled cutover becomes due but before the boundary readiness recheck decides; pending production recomputation leaves the saved snapshot unchanged instead of using either matcher. `day_is_strict(day)` returns true when the day is already recorded in `attendance_strict_days`, or when live is active and the day is on/after the cutover's local date. Task 5 records each live-computed day in `attendance_strict_days` before replacing its production rows, so a later rollback can affect future workdays without silently reinterpreting days that were already strict.

- [ ] **Step 7: Add Settings controls and route validation**

Add a “Work-center attendance” section that lists every synced department with a “Work center required” checkbox and shows rollout mode, cutover, mirror freshness, and last full sweep. Only the existing super-admin settings action may change rollout mode; normal signed-in managers can view health.

- [ ] **Step 8: Run focused tests**

Run: `uv run pytest tests/test_attendance_location_schema.py tests/test_attendance_location_policy.py tests/test_settings_timeclock_layout.py -q`

Expected: PASS.

- [ ] **Step 9: Add the child-readable patch note**

Under `## 2026-08-28`, add:

```markdown
### Set which teams need a work area

- **Settings can now remember which teams must have a work area in Odoo.** Maintenance and supervisors can stay clocked in without one. The new Odoo location rules are still turned off.
```

- [ ] **Step 10: Commit and push Task 1**

Run:

```bash
git add src/zira_dashboard/_schema.py src/zira_dashboard/attendance_location_policy.py src/zira_dashboard/routes/settings.py src/zira_dashboard/templates/settings.html tests/test_attendance_location_schema.py tests/test_attendance_location_policy.py tests/test_settings_timeclock_layout.py CHANGELOG.md
git commit -m "feat: add attendance location policy"
git push origin main
```

### Task 2: Add raw, paginated Odoo attendance read/write contracts

**Files:**
- Modify: `src/zira_dashboard/_odoo_attendance.py`
- Modify: `src/zira_dashboard/odoo_client.py`
- Test: `tests/test_odoo_attendance_raw.py`
- Test: `tests/test_odoo_facade_contract.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write contract tests for lossless raw rows and stable paging**

Test that the adapter requests `id`, `employee_id`, `check_in`, `check_out`, the configured work-center field, the configured department field, and `write_date`. Cover paging ordered by `write_date,id`, a two-minute overlap, open attendance refresh, all-ID sweep paging, direct reads by ID, and unknown many-to-one values retained as both ID and label.

Expected normalized shape:

```python
{
    "odoo_attendance_id": 901,
    "employee_odoo_id": 44,
    "employee_name": "Adrian A.",
    "check_in_utc": datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
    "check_out_utc": None,
    "odoo_work_center_id": 72,
    "odoo_work_center_name": "Luke Floor / Dismantler 1",
    "odoo_department_id": 8,
    "odoo_department_name": "01 Recycled",
    "odoo_write_date": datetime(2026, 8, 28, 13, 1, tzinfo=UTC),
}
```

- [ ] **Step 2: Run the raw adapter tests and verify they fail**

Run: `uv run pytest tests/test_odoo_attendance_raw.py tests/test_odoo_facade_contract.py -q`

Expected: FAIL because the raw methods are absent.

- [ ] **Step 3: Implement normalized raw reads**

Add private adapter functions and public facade wrappers with these signatures:

```python
fetch_attendance_changes(
    *, after_write_date: datetime | None, after_id: int | None,
    overlap: timedelta = timedelta(minutes=2), page_size: int = 250,
) -> list[dict]

fetch_open_attendance_rows(*, page_size: int = 250) -> list[dict]
fetch_all_attendance_ids(*, page_size: int = 500) -> list[int]
fetch_attendance_rows_by_ids(ids: Sequence[int]) -> list[dict]
fetch_employee_attendance_rows(
    employee_odoo_id: int, start_utc: datetime, end_utc: datetime | None
) -> list[dict]
```

Use keyset paging. A page boundary must include `("write_date", ">", cursor_date)` OR matching `write_date` with `id > cursor_id`; never rely on offset paging. Parse every Odoo datetime as aware UTC.

- [ ] **Step 4: Add correction-safe mutation wrappers**

Expose:

```python
create_attendance_interval(*, employee_odoo_id: int, check_in_utc: datetime,
    check_out_utc: datetime | None, odoo_work_center_id: int,
    odoo_department_id: int | None) -> int
update_attendance_interval(attendance_id: int, *, values: Mapping[str, object]) -> None
delete_attendance_interval(attendance_id: int) -> None
set_attendance_department_id(attendance_id: int, department_id: int) -> None
close_all_open_attendance_rows(employee_odoo_id: int,
                               check_out_utc: datetime) -> tuple[int, ...]
```

Keep all field-name knowledge inside `odoo_client.py`. `close_all_open_attendance_rows` reads every open row for the employee, closes each at the same day-boundary timestamp, re-reads, and returns the verified closed IDs; a partial failure remains retryable through the existing timeclock sync log. Reject correction writes when the configured work-center field is unavailable.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest tests/test_odoo_attendance_raw.py tests/test_odoo_facade_contract.py tests/test_odoo_attendance_for_day.py -q`

Expected: PASS, including existing facade behavior.

- [ ] **Step 6: Add the patch note**

Add:

```markdown
### Keep every Odoo work-area detail

- **Plant Manager can now read the full work-area record from Odoo without losing names it does not know yet.** This prepares the app to show the real source clearly. The new records are not driving production yet.
```

- [ ] **Step 7: Commit and push Task 2**

Run:

```bash
git add src/zira_dashboard/_odoo_attendance.py src/zira_dashboard/odoo_client.py tests/test_odoo_attendance_raw.py tests/test_odoo_facade_contract.py CHANGELOG.md
git commit -m "feat: add raw Odoo attendance contract"
git push origin main
```

### Task 3: Build the canonical mirror, incremental sync, and safe deletion sweep

**Files:**
- Create: `src/zira_dashboard/attendance_mirror.py`
- Create: `src/zira_dashboard/attendance_sync.py`
- Modify: `src/zira_dashboard/app.py`
- Test: `tests/test_attendance_mirror.py`
- Test: `tests/test_attendance_sync.py`
- Test: `tests/test_attendance_sync_warmer.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write mirror store tests**

Cover idempotent upsert, reopening/closing rows, retaining raw unknown work-center labels, excluding tombstones from active range reads, sync cursor updates in the same transaction as rows, coalescing recalculation requests by local day, and marking old changed days strict only after `baseline_completed_at` exists.

- [ ] **Step 2: Run mirror tests and verify they fail**

Run: `uv run pytest tests/test_attendance_mirror.py -q`

Expected: FAIL because the mirror store does not exist.

- [ ] **Step 3: Implement the mirror store boundary**

Create:

```python
@dataclass(frozen=True)
class MirrorHealth:
    last_incremental_completed_at: datetime | None
    last_full_sweep_completed_at: datetime | None
    baseline_completed_at: datetime | None
    oldest_recalc_requested_at: datetime | None
    last_error: str | None

upsert_rows(rows: Sequence[dict], *, sync_completed_at: datetime) -> set[date]
rows_overlapping(start_utc: datetime, end_utc: datetime) -> tuple[dict, ...]
rows_for_employee(employee_odoo_id: int, start_utc: datetime,
                  end_utc: datetime | None) -> tuple[dict, ...]
mark_deleted_after_successful_sweep(ids: set[int], generation: int) -> set[date]
enqueue_recalc(days: Iterable[date], reason: str, *, mark_strict: bool) -> None
health_snapshot() -> MirrorHealth
```

Compare material source fields before enqueueing. A repeated overlapping poll with identical data must not recalculate a day.

- [ ] **Step 4: Write sync tests for partial failure safety**

Cover:

- incremental changes and open rows merge into one transaction;
- cursor advances only after the page is stored;
- a failed page leaves the prior cursor usable;
- baseline import stores old rows without recalculating old production;
- hourly sweep marks missing IDs deleted only after all pages succeed;
- interrupted or malformed sweeps mark nothing deleted;
- post-baseline additions, edits, and deletions enqueue every touched local day, including both the old and new days when an edit moves a row;
- a row crossing midnight enqueues both local days.

- [ ] **Step 5: Run sync tests and verify they fail**

Run: `uv run pytest tests/test_attendance_sync.py -q`

Expected: FAIL because sync orchestration is absent.

- [ ] **Step 6: Implement the pollers**

Expose:

```python
run_incremental_sync(*, now_utc: datetime | None = None) -> SyncResult
run_full_sweep(*, now_utc: datetime | None = None) -> SyncResult
tick(*, now_utc: datetime | None = None) -> SyncResult
```

`tick()` performs the incremental change poll and open-row refresh every call. It performs a full ID sweep when no successful sweep exists in the last hour. Set `baseline_completed_at` only after the first complete incremental import, open refresh, and full ID sweep have all succeeded.

- [ ] **Step 7: Register a 30-second warmer**

Add `_tick_attendance_mirror()` to `_WARMERS` at 30 seconds. Do not remove `live_cache` yet; later tasks migrate its consumers before retirement.

- [ ] **Step 8: Run focused tests**

Run: `uv run pytest tests/test_attendance_mirror.py tests/test_attendance_sync.py tests/test_attendance_sync_warmer.py -q`

Expected: PASS.

- [ ] **Step 9: Add the patch note**

Add:

```markdown
### Keep a safe copy of Odoo work times

- **Plant Manager now keeps a fresh copy of Odoo work times and checks for changed or removed records.** A failed check cannot erase good records. The copy is still running in the background only.
```

- [ ] **Step 10: Commit and push Task 3**

Run:

```bash
git add src/zira_dashboard/attendance_mirror.py src/zira_dashboard/attendance_sync.py src/zira_dashboard/app.py tests/test_attendance_mirror.py tests/test_attendance_sync.py tests/test_attendance_sync_warmer.py CHANGELOG.md
git commit -m "feat: mirror Odoo attendance safely"
git push origin main
```

### Task 4: Project mirrored rows into one atomic location timeline

**Files:**
- Create: `src/zira_dashboard/attendance_timeline.py`
- Test: `tests/test_attendance_timeline.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write exhaustive pure timeline tests**

Use fixed aware UTC datetimes. Cover valid mapped location, exempt no-location attendance, required missing location, five-minute pending first location, a later WC-less gap that is immediately missing rather than pending, unknown raw Odoo location, duplicate same-work-center overlap, conflicting distinct-work-center overlap, adjacent rows, gaps, fresh open rows capped at `as_of_utc`, stale open rows split at the last verified source time, cross-midnight rows, and department mismatch that leaves the mapped work center valid while requesting repair.

Assert exact atomic boundaries, not only totals.

- [ ] **Step 2: Run timeline tests and verify they fail**

Run: `uv run pytest tests/test_attendance_timeline.py -q`

Expected: FAIL because the projection module does not exist.

- [ ] **Step 3: Implement typed timeline values**

Create:

```python
LocationStatus = Literal[
    "valid", "pending_first_location", "exempt_no_location",
    "missing_required_location", "unmapped_location", "conflicting_location",
    "stale_open_location",
]

@dataclass(frozen=True)
class LocationSpan:
    employee_odoo_id: int
    employee_name: str
    start_utc: datetime
    end_utc: datetime
    status: LocationStatus
    app_work_center_name: str | None
    odoo_work_center_id: int | None
    odoo_work_center_name: str | None
    attendance_ids: tuple[int, ...]
    department_repair: tuple[int, int, datetime] | None

project_rows(rows: Sequence[Mapping[str, object]], *, as_of_utc: datetime,
             verified_through_utc: datetime,
             map_work_center: Callable[[int], str | None],
             requires_work_center: Callable[[str | None], bool],
             expected_department_id: Callable[[str], int | None],
             grace: timedelta = timedelta(minutes=5),
             stale_after: timedelta = timedelta(seconds=90)) -> tuple[LocationSpan, ...]

timeline_for_range(start_utc: datetime, end_utc: datetime,
                   *, as_of_utc: datetime | None = None) -> tuple[LocationSpan, ...]
```

Algorithm:

```python
for employee_odoo_id, employee_rows in group_rows_by_employee(rows).items():
    boundaries = {range_start_utc, range_end_utc}
    boundaries.update(row["check_in_utc"] for row in employee_rows)
    boundaries.update(
        row["check_out_utc"]
        for row in employee_rows
        if row["check_out_utc"] is not None
    )
    for left, right in pairwise(sorted(boundaries)):
        active = [
            row for row in employee_rows
            if row["check_in_utc"] < right
            and (row["check_out_utc"] or as_of_utc) > left
        ]
        distinct_wc_ids = {
            row["odoo_work_center_id"]
            for row in active
            if row["odoo_work_center_id"] is not None
        }
        # zero IDs: exempt, first-location pending, or missing
        # one ID: mapped or unmapped; more than one: conflict
```

The grace applies only from the employee's day clock-in until the first valid or unmapped work-center value. A later WC-less gap is `missing_required_location` immediately. If the last successful mirror sync is older than 90 seconds, project an open row only through `verified_through_utc` and emit `stale_open_location` afterward. Merge adjacent spans only when all identity, status, work-center, attendance-ID, and repair fields match.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_attendance_timeline.py -q`

Expected: PASS.

- [ ] **Step 5: Add the patch note**

Add:

```markdown
### One clear answer for each worker's location

- **Plant Manager can now turn Odoo records into one clear work-area timeline.** It keeps gaps, unknown areas, and mixed-up overlaps visible instead of guessing.
```

- [ ] **Step 6: Commit and push Task 4**

Run:

```bash
git add src/zira_dashboard/attendance_timeline.py tests/test_attendance_timeline.py CHANGELOG.md
git commit -m "feat: project Odoo attendance timelines"
git push origin main
```

### Task 5: Make strict production attribution consume only valid Odoo locations

**Files:**
- Modify: `src/zira_dashboard/assignment_windows.py`
- Modify: `src/zira_dashboard/production_history.py`
- Modify: `src/zira_dashboard/production_segments.py`
- Modify: `src/zira_dashboard/precompute.py`
- Create: `src/zira_dashboard/attendance_recalc.py`
- Modify: `src/zira_dashboard/app.py`
- Test: `tests/test_production_history_odoo_strict.py`
- Test: `tests/test_production_segments.py`
- Test: `tests/test_attendance_recalc.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write strict matcher tests before changing production**

Cover:

- only `LocationSpan(status="valid")` becomes `WorkSegment(source="odoo")`;
- schedule and manual attribution are ignored on strict days;
- pre-cutover days retain existing behavior unless present in `attendance_strict_days`;
- a due but not yet activated cutover returns `ProductionSourceUnavailable` and never writes a fallback snapshot for that day;
- two valid people split a sample exactly 50/50;
- two different Odoo employee IDs with the same display name remain two workers and split rather than collapsing;
- a conflicting, missing, unmapped, or gap sample is fully unassigned;
- a stale open suffix is unassigned while verified closed/earlier spans remain usable;
- a positive work-center total that is not represented by timestamped samples fails strict recomputation instead of being divided by worked minutes;
- named plus unassigned units equal every input sample and work-center total;
- testing offsets and breakdown exclusions still apply;
- `ProductionSourceUnavailable` is raised only when a strict day has no completed mirror baseline or no verified snapshot at all; ordinary staleness keeps prior results and withholds new credit from the stale open suffix instead of falling back.

- [ ] **Step 2: Run strict production tests and verify they fail**

Run: `uv run pytest tests/test_production_history_odoo_strict.py tests/test_production_segments.py -q`

Expected: FAIL because strict timeline matching is not wired.

- [ ] **Step 3: Add strict timeline conversion**

Add:

```python
@dataclass(frozen=True)
class WorkSegment:
    wc_name: str
    person_name: str
    start_utc: datetime
    end_utc: datetime
    source: str
    person_odoo_id: int | None = None

def work_segments_from_timeline(
    spans: Sequence[LocationSpan], *, window_start_utc: datetime,
    window_end_utc: datetime,
) -> tuple[WorkSegment, ...]:
    return tuple(
        WorkSegment(
            person_name=span.employee_name,
            wc_name=span.app_work_center_name,
            start_utc=max(span.start_utc, window_start_utc),
            end_utc=min(span.end_utc, window_end_utc),
            source="odoo",
            person_odoo_id=span.employee_odoo_id,
        )
        for span in spans
        if span.status == "valid" and span.app_work_center_name
    )
```

Branch once at the top of `production_history.attribution_for`: strict days use timeline work segments; non-strict days retain the current hybrid resolver. Before replacing a live strict day's rows, insert that day into `attendance_strict_days` in the same database transaction. Do not add fallback inside the strict branch.

Before strict credit, validate per work center that positive timestamped sample units equal the adjusted source total within the existing numeric tolerance. If they do not, raise `ProductionSourceUnavailable`, leave the prior `production_daily` snapshot untouched, enqueue the day, and surface the source problem. Retain the current worked-minute total-only fallback only in the pre-cutover branch.

Extend `SegmentCredit` with `person_odoo_id`. In `credit_work_segments`, identify active people by `person_odoo_id` when present and by the legacy display name only for pre-cutover sources. Keep the new `WorkSegment` field last with a default so existing non-strict positional callers remain compatible.

Use an identity-bearing key only when strict credits have an Odoo ID:

```python
PersonAttributionKey = str | tuple[int, str]

def attribution_key(credit: SegmentCredit) -> PersonAttributionKey:
    if credit.person_odoo_id is None:
        return credit.person_name
    return (credit.person_odoo_id, credit.person_name)
```

`attribute_for_segments` groups strict rows by `(employee_odoo_id, display_name)` and retains plain string keys for legacy rows. `precompute.flatten_attribution` recognizes the tuple, writes its ID directly to `production_daily.emp_id`, and writes the display name to `production_daily.name`. This avoids merging two Odoo employees who share a display name without changing pre-cutover behavior.

- [ ] **Step 4: Preserve distinct unassigned production runs**

Extend production credit output with an `UnassignedRun` rather than one min/max bucket:

```python
@dataclass(frozen=True)
class UnassignedRun:
    wc_name: str
    start_utc: datetime
    end_utc: datetime
    units: float
    sample_count: int

unassigned_runs_for_samples(
    samples: Sequence[tuple[datetime, float]],
    assigned_sample_times: set[datetime],
    active_intervals: Sequence[tuple[datetime, datetime]],
) -> tuple[UnassignedRun, ...]

unassigned_runs_for_day(day: date, client,
                        *, now_utc: datetime | None = None) -> tuple[UnassignedRun, ...]
```

`production_history.unassigned_runs_for_day` must obtain the same samples, active intervals, testing offsets, breakdown exclusions, and strict timeline used by attribution, then delegate to the pure grouping function. The Exception Inbox calls this read path from its existing warmed Zira data; it must not infer a run from aggregate daily totals. Join consecutive positive samples only while the meter's `active_intervals` say they are in the same production run. Do not bridge a stopped interval, lunch gap, or breakdown exclusion.

- [ ] **Step 5: Write recalculation queue tests**

Cover oldest-first claim, `FOR UPDATE SKIP LOCKED`, retry after failure, successful removal/completion, one job per local day, and a historical changed day remaining strict after recalculation.

- [ ] **Step 6: Run recalculation tests and verify they fail**

Run: `uv run pytest tests/test_attendance_recalc.py -q`

Expected: FAIL because the worker does not exist.

- [ ] **Step 7: Implement and register the recalculation worker**

Expose:

```python
process_next(*, production_client=None,
             now_utc: datetime | None = None) -> RecalcResult | None
```

Claim one day. When `production_client` is `None`, lazily import `deps.client`; tests pass a fake Zira client directly. Call `precompute.precompute_day(day, production_client)`, refresh attribution-dependent caches, and mark complete. Register a 15-second warmer tick. A failed job records the error and remains eligible with bounded backoff.

- [ ] **Step 8: Run focused and regression tests**

Run:

```bash
uv run pytest tests/test_production_history_odoo_strict.py tests/test_production_segments.py tests/test_attendance_recalc.py tests/test_production_history_testing.py tests/test_production_history_breakdown.py tests/test_precompute.py -q
```

Expected: PASS.

- [ ] **Step 9: Add the patch note**

Add:

```markdown
### Match production only to clear Odoo locations

- **Plant Manager can now match production to workers only when Odoo clearly shows the same work area.** Missing or mixed-up time stays unassigned, and shared work is split evenly. The strict rule is still not live.
```

- [ ] **Step 10: Commit and push Task 5**

Run:

```bash
git add src/zira_dashboard/assignment_windows.py src/zira_dashboard/production_history.py src/zira_dashboard/production_segments.py src/zira_dashboard/precompute.py src/zira_dashboard/attendance_recalc.py src/zira_dashboard/app.py tests/test_production_history_odoo_strict.py tests/test_production_segments.py tests/test_attendance_recalc.py CHANGELOG.md
git commit -m "feat: match production from Odoo locations"
git push origin main
```

### Task 6: Create stable attendance and production-run exceptions

**Files:**
- Create: `src/zira_dashboard/attendance_exceptions.py`
- Modify: `src/zira_dashboard/exception_inbox.py`
- Modify: `src/zira_dashboard/inbox_keys.py`
- Modify: `src/zira_dashboard/inbox_reconcile.py`
- Modify: `src/zira_dashboard/app.py`
- Modify: `src/zira_dashboard/wc_attributions.py`
- Test: `tests/test_attendance_exceptions.py`
- Test: `tests/test_exception_inbox_attendance.py`
- Test: `tests/test_inbox_keys_attendance.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write exception projection tests**

Cover these categories and urgency rules:

| Kind | Starts | Urgent |
|---|---:|---:|
| `attendance_missing_location` | first required WC-less span | after five minutes |
| `attendance_unmapped_location` | first unknown raw WC span | immediately |
| `attendance_conflicting_location` | start of overlap | immediately |
| `attendance_duplicate_location` | start of same-WC duplicate overlap | follow-up, not urgent |
| `attendance_department_repair_failed` | repair failure | immediately |
| `attendance_source_stale` | mirror crosses the 90-second limit | immediately |
| `production_source_unavailable` | strict total lacks matching timestamped samples | immediately |
| `production_unassigned_run` | first positive sample in distinct run | immediately |

Assert exact raw Odoo labels, attendance IDs, UTC boundaries, affected workers, work center, units, and stable keys. An open span/run key must remain the same as its end time advances. The first WC-less span starts non-urgent and becomes urgent at five minutes without changing key. A later WC-less gap after a real work center is immediately urgent.

- [ ] **Step 2: Run exception tests and verify they fail**

Run: `uv run pytest tests/test_attendance_exceptions.py tests/test_inbox_keys_attendance.py -q`

Expected: FAIL because the new categories do not exist.

- [ ] **Step 3: Implement stable keys and exception values**

Use immutable source identity:

```python
def attendance_issue_key(kind: str, employee_odoo_id: int,
                         attendance_ids: Sequence[int], start_utc: datetime) -> str:
    ids = ",".join(str(value) for value in sorted(attendance_ids))
    return f"{kind}:{employee_odoo_id}:{ids}:{start_utc.isoformat()}"

def production_run_key(wc_name: str, start_utc: datetime) -> str:
    return f"production_unassigned_run:{wc_name}:{start_utc.isoformat()}"

def attendance_source_stale_key() -> str:
    return "attendance_source_stale:odoo_attendance_mirror"
```

Expose `build_snapshot(day, *, now_utc) -> AttendanceExceptionSnapshot` and merge it into the existing inbox snapshot without removing machine, punch, leave, or equipment categories.

- [ ] **Step 4: Wire durable reconciliation**

Add `_SECTION_KIND` and `_KIND_SOURCE` entries. Mark an attendance item resolved only after it is absent from a complete mirror/timeline snapshot and, for corrections, the linked job is `complete`. Never auto-resolve from an unavailable or stale source.

Once the mirror baseline is complete and rollout mode is `shadow` or `live`, stop adding the legacy `missing_wc` section and make `_tick_missing_wc` a no-op; the new timeline-backed category owns that signal. Keep the old tables, routes, and audit rows readable for history. In `off` mode, retain the legacy warmer work and section so deployment of the mirror alone does not change current operations.

On strict days, replace `wc_attributions.unattributed_for_day` as the inbox source with `production_history.unassigned_runs_for_day`; do not show both the old min/max aggregate and the new run-specific item. In shadow mode, label the new run items as comparison results and keep legacy actions active until live cutover.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest tests/test_attendance_exceptions.py tests/test_exception_inbox_attendance.py tests/test_inbox_keys_attendance.py tests/test_exception_inbox.py -q`

Expected: PASS.

- [ ] **Step 6: Add the patch note**

Add:

```markdown
### Show each Odoo location problem once

- **The Exception Inbox can now keep one clear item for each missing, unknown, or mixed-up Odoo location and each production run with no worker.** Open items keep the same identity while time moves forward.
```

- [ ] **Step 7: Commit and push Task 6**

Run:

```bash
git add src/zira_dashboard/attendance_exceptions.py src/zira_dashboard/exception_inbox.py src/zira_dashboard/inbox_keys.py src/zira_dashboard/inbox_reconcile.py src/zira_dashboard/app.py src/zira_dashboard/wc_attributions.py tests/test_attendance_exceptions.py tests/test_exception_inbox_attendance.py tests/test_inbox_keys_attendance.py CHANGELOG.md
git commit -m "feat: add attendance location exceptions"
git push origin main
```

### Task 7: Build a pure, versioned manager-correction planner

**Files:**
- Create: `src/zira_dashboard/attendance_corrections.py`
- Test: `tests/test_attendance_correction_planner.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write interval-surgery tests first**

Use table-driven cases for:

- no source row: create exact closed or open row;
- exact source row: update only the work center/department;
- correction at left edge: split into corrected prefix plus original suffix;
- correction at right edge: original prefix plus corrected suffix;
- correction in the middle: original prefix, corrected middle, original suffix;
- several source rows with lunch gap: preserve the gap and operate on each overlap only;
- fully covered rows: reuse one row and delete only redundant rows;
- open source and open correction;
- several selected employees: independent plans with the same requested interval;
- invalid start/end, missing work-center mapping, inactive/unknown selected employees, and stale source versions. A valid selected employee with no attendance is not an error; it produces an exact create operation.

Assert that no operation changes time outside the manager's selected interval.

- [ ] **Step 2: Run planner tests and verify they fail**

Run: `uv run pytest tests/test_attendance_correction_planner.py -q`

Expected: FAIL because the planner does not exist.

- [ ] **Step 3: Implement immutable plan values**

Create:

```python
OperationKind = Literal["create", "update", "delete"]

@dataclass(frozen=True)
class SourceVersion:
    attendance_id: int
    write_date: datetime

@dataclass(frozen=True)
class CorrectionOperation:
    key: str
    kind: OperationKind
    attendance_id: int | None
    employee_odoo_id: int
    before: Mapping[str, object] | None
    after: Mapping[str, object] | None

@dataclass(frozen=True)
class CorrectionPlan:
    source_versions: tuple[SourceVersion, ...]
    operations: tuple[CorrectionOperation, ...]
    expected_intervals: tuple[Mapping[str, object], ...]

plan_correction(*, rows: Sequence[Mapping[str, object]],
                employee_odoo_id: int, start_utc: datetime,
                end_utc: datetime | None, odoo_work_center_id: int,
                odoo_department_id: int | None) -> CorrectionPlan
```

Generate deterministic operation keys from employee, interval, source ID/version, and desired values so a retried job can adopt already-applied state.

- [ ] **Step 4: Add serialization round-trip tests and implementation**

Add `plan_to_json(plan)` and `plan_from_json(value)` and assert aware UTC datetimes and integer IDs survive a JSONB round trip exactly.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest tests/test_attendance_correction_planner.py -q`

Expected: PASS.

- [ ] **Step 6: Add the patch note**

Add:

```markdown
### Plan safe fixes for work times

- **Plant Manager can now plan an exact Odoo time fix without covering lunch or changing time outside the chosen window.** The plan can split a record into before, fixed, and after pieces.
```

- [ ] **Step 7: Commit and push Task 7**

Run:

```bash
git add src/zira_dashboard/attendance_corrections.py tests/test_attendance_correction_planner.py CHANGELOG.md
git commit -m "feat: plan safe attendance corrections"
git push origin main
```

### Task 8: Execute corrections durably and verify Odoo before resolving

**Files:**
- Modify: `src/zira_dashboard/attendance_corrections.py`
- Modify: `src/zira_dashboard/app.py`
- Test: `tests/test_attendance_correction_jobs.py`
- Test: `tests/test_attendance_correction_recovery.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write job lifecycle tests**

Cover create, claim, attempt-count increment, apply, verify, verification-failure count, mirror refresh, recalc wait, completion, audit event, and original inbox resolution. Assert duplicate submissions for the same active `item_key` return the same job. Assert every claim, source-version stop, adopted timeout, Odoo failure, verification result, recalculation result, and completion appends a durable `attendance_correction_job_events` row with phase/result identifiers and no unrelated personal data.

- [ ] **Step 2: Write timeout and stale-preview recovery tests**

Cover:

- Odoo times out after applying an update; retry re-reads and adopts the matching row;
- create times out; retry finds an exact employee/time/WC match rather than creating a duplicate;
- source `write_date` changed before the first write; job fails as `source_changed` with a fresh preview required;
- some operations completed; retry resumes at the first incomplete operation;
- verification mismatch leaves the job failed and the exception open;
- Odoo succeeds but recalculation fails; job remains `recalculating` and later resumes without rewriting Odoo.

- [ ] **Step 3: Run job tests and verify they fail**

Run: `uv run pytest tests/test_attendance_correction_jobs.py tests/test_attendance_correction_recovery.py -q`

Expected: FAIL because durable execution is absent.

- [ ] **Step 4: Implement create/claim/apply/verify transitions**

Expose:

```python
create_job(*, item_key: str, employee_odoo_ids: Sequence[int],
           target_work_center_name: str, start_utc: datetime,
           end_utc: datetime | None, actor_email: str | None,
           actor_name: str | None) -> int
process_job(job_id: int) -> CorrectionJobResult
process_next() -> CorrectionJobResult | None
correction_preview(*, item_key: str, employee_odoo_ids: Sequence[int],
                   target_work_center_name: str, start_utc: datetime,
                   end_utc: datetime | None) -> CorrectionPreview
```

Creation must re-read live Odoo, resolve the saved app-to-Odoo work-center mapping, build the plan, and persist source versions before returning. Apply operations in deterministic order: close or shrink any existing open row, update retained closed rows, create required closed pieces, delete redundant closed rows, then create the one requested open piece last. This order respects Odoo's one-open-attendance rule. Record each completed operation transactionally.

Immediately before every operation, re-read its target source row. Continue only when the saved `write_date` and before-state still match, or when the row already matches that operation's after-state and can be adopted after a timeout. For creates, search the employee's overlapping rows and adopt only one exact time/work-center/department match. Any different intervening state stops the job as `source_changed` and requires a fresh preview.

- [ ] **Step 5: Implement verification and downstream completion**

After writes, re-read the employee intervals from Odoo and compare normalized expected intervals. Then:

1. upsert the verified rows into the mirror;
2. enqueue every touched day as strict;
3. wait for or synchronously run targeted recalculation;
4. rebuild attribution/inbox caches;
5. record one `inbox_log` correction event with before/after/operation detail;
6. mark the job complete.

The original exception resolves only after step 6 and a complete inbox snapshot no longer contains it.

- [ ] **Step 6: Register the correction warmer**

Add a 15-second `_tick_attendance_corrections()` warmer. It processes at most one job per tick to bound Odoo load.

- [ ] **Step 7: Run focused tests**

Run: `uv run pytest tests/test_attendance_correction_jobs.py tests/test_attendance_correction_recovery.py tests/test_attendance_correction_planner.py -q`

Expected: PASS.

- [ ] **Step 8: Add the patch note**

Add:

```markdown
### Check every Odoo time fix

- **Plant Manager can now save a manager's time fix, finish it safely after a connection problem, and check Odoo before calling it done.** Production is counted again only after the fix is confirmed.
```

- [ ] **Step 9: Commit and push Task 8**

Run:

```bash
git add src/zira_dashboard/attendance_corrections.py src/zira_dashboard/app.py tests/test_attendance_correction_jobs.py tests/test_attendance_correction_recovery.py CHANGELOG.md
git commit -m "feat: verify attendance correction jobs"
git push origin main
```

### Task 9: Repair work-center department mismatches with work center winning

**Files:**
- Create: `src/zira_dashboard/attendance_department_repair.py`
- Modify: `src/zira_dashboard/attendance_sync.py`
- Modify: `src/zira_dashboard/app.py`
- Test: `tests/test_attendance_department_repair.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write repair tests**

Cover one queued repair per attendance ID, expected-version deduplication, re-read before write, changed-source abort/requeue, correct department no-op, successful write plus re-read verification, timeout adoption, failed verification, and exception generation after bounded retries.

- [ ] **Step 2: Run repair tests and verify they fail**

Run: `uv run pytest tests/test_attendance_department_repair.py -q`

Expected: FAIL because the repair worker does not exist.

- [ ] **Step 3: Implement repair queue and worker**

Create:

```python
enqueue_from_spans(spans: Sequence[LocationSpan]) -> int
process_next(*, now_utc: datetime | None = None) -> RepairResult | None
```

For a valid mapped work center with the wrong department:

1. store attendance ID, expected `write_date`, and target department ID;
2. re-read that attendance;
3. if its work center changed, discard this repair and let the next projection decide;
4. if only its version changed, refresh the expected version and retry;
5. write the work center's department;
6. re-read and verify both work center and department;
7. upsert the verified row into the mirror.

The work center remains valid for matching during repair.

- [ ] **Step 4: Enqueue repairs after each successful sync and register a 15-second worker**

Projection after mirror sync supplies repair candidates. Process one per tick and expose failures through `attendance_exceptions`.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest tests/test_attendance_department_repair.py tests/test_attendance_sync.py tests/test_attendance_timeline.py -q`

Expected: PASS.

- [ ] **Step 6: Add the patch note**

Add:

```markdown
### Keep Odoo teams lined up with work areas

- **When an Odoo work area and team disagree, Plant Manager can now keep the work area and safely fix the team.** It checks the record again before and after the change.
```

- [ ] **Step 7: Commit and push Task 9**

Run:

```bash
git add src/zira_dashboard/attendance_department_repair.py src/zira_dashboard/attendance_sync.py src/zira_dashboard/app.py tests/test_attendance_department_repair.py CHANGELOG.md
git commit -m "feat: repair attendance departments"
git push origin main
```

### Task 10: Give managers the correction preview and apply flow in Exception Inbox

**Files:**
- Modify: `src/zira_dashboard/routes/exceptions.py`
- Modify: `src/zira_dashboard/templates/exceptions.html`
- Modify: `src/zira_dashboard/static/exceptions.js`
- Modify: `src/zira_dashboard/static/exceptions.css`
- Test: `tests/test_exceptions_attendance_routes.py`
- Test: `tests/test_exception_inbox_attendance_template.py`
- Test: `tests/test_exception_inbox_attendance_js.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write route tests before adding endpoints**

Add protected endpoints:

```text
POST /api/exceptions/attendance-correction/preview
POST /api/exceptions/attendance-correction/apply
GET  /api/exceptions/attendance-correction/{job_id}
```

Test signed-out rejection, missing authenticated manager identity, invalid/stale item key, unmapped target work center, invalid time range, selected employee validation, open interval, closed interval, live Odoo preview, duplicate apply idempotency, and actor identity passed to the job. The existing authenticated Plant Manager session is the manager authorization boundary for corrections; work-center mapping, department policy, and rollout changes remain super-admin-only Settings actions.

- [ ] **Step 2: Run route tests and verify they fail**

Run: `uv run pytest tests/test_exceptions_attendance_routes.py -q`

Expected: FAIL because the endpoints are absent.

- [ ] **Step 3: Implement JSON route contracts**

Preview request:

```json
{
  "item_key": "production_unassigned_run:Dismantler 1:2026-08-28T16:55:00+00:00",
  "employee_odoo_ids": [44],
  "work_center_name": "Dismantler 1",
  "start_utc": "2026-08-28T16:55:00+00:00",
  "end_utc": null
}
```

Preview response includes the selected people, exact local display times, open/closed state, source attendance rows, before/after intervals, operation summary, and a server-generated preview token tied to source versions. Apply accepts that token; it must rebuild/revalidate the plan rather than trusting client-supplied operations.

- [ ] **Step 4: Write template and JavaScript tests**

Assert the exception card shows exact Odoo-only location names, status, work-center production run, units, source time, and “Choose workers and times” action. An unmapped-location card also shows “Map this Odoo work center,” linking the super admin to the existing Settings mapping control with the raw Odoo ID/name preselected for review. Assert modal controls allow one or more people, exact start, optional end/“Still working”, target work center, preview, confirm, progress, stale-preview refresh, and retry-safe status polling.

- [ ] **Step 5: Run UI tests and verify they fail**

Run: `uv run pytest tests/test_exception_inbox_attendance_template.py tests/test_exception_inbox_attendance_js.py -q`

Expected: FAIL because the attendance correction UI is absent.

- [ ] **Step 6: Implement accessible modal behavior**

Use existing dialog/card styles and focus handling. Disable Apply until a successful preview. Show every before/after interval in local plant time and label an absent end as “Still working.” On `source_changed`, keep selections, fetch a new preview, and require a second confirmation.

- [ ] **Step 7: Run focused tests**

Run:

```bash
uv run pytest tests/test_exceptions_attendance_routes.py tests/test_exception_inbox_attendance_template.py tests/test_exception_inbox_attendance_js.py tests/test_exception_inbox.py -q
```

Expected: PASS.

- [ ] **Step 8: Add the patch note**

Add:

```markdown
### Fix unassigned production from the inbox

- **A manager can now choose who did an unassigned production run, set the exact start and end time, and preview the Odoo change before saving.** “Still working” keeps the time open.
```

- [ ] **Step 9: Commit and push Task 10**

Run:

```bash
git add src/zira_dashboard/routes/exceptions.py src/zira_dashboard/templates/exceptions.html src/zira_dashboard/static/exceptions.js src/zira_dashboard/static/exceptions.css tests/test_exceptions_attendance_routes.py tests/test_exception_inbox_attendance_template.py tests/test_exception_inbox_attendance_js.py CHANGELOG.md
git commit -m "feat: add attendance correction inbox flow"
git push origin main
```

### Task 11: Make Timeclock own only clock-in/out and show live Odoo locations in Staffing

**Files:**
- Modify: `src/zira_dashboard/attendance.py`
- Modify: `src/zira_dashboard/attendance_state.py`
- Modify: `src/zira_dashboard/auto_lunch.py`
- Modify: `src/zira_dashboard/live_cache.py`
- Modify: `src/zira_dashboard/timeclock_windows.py`
- Modify: `src/zira_dashboard/staffing_attendance.py`
- Modify: `src/zira_dashboard/app.py`
- Modify: `src/zira_dashboard/routes/timeclock.py`
- Modify: `src/zira_dashboard/templates/timeclock_dashboard.html`
- Modify: `src/zira_dashboard/templates/timeclock_time_off_override_confirm.html`
- Modify: `src/zira_dashboard/timeclock_sync.py`
- Modify: `src/zira_dashboard/routes/staffing.py`
- Modify: `src/zira_dashboard/staffing_view.py`
- Modify: `src/zira_dashboard/templates/staffing.html`
- Modify: `src/zira_dashboard/static/staffing.js`
- Modify: `src/zira_dashboard/static/staffing.css`
- Modify: `src/zira_dashboard/staffing_transfer.py`
- Test: `tests/test_timeclock_day_boundary_only.py`
- Test: `tests/test_attendance_mirror_cutover.py`
- Test: `tests/test_staffing_live_locations.py`
- Test: `tests/test_staffing_attribute_endpoints.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write timeclock ownership tests**

When `live_is_active()` is true, assert:

- clock-in succeeds without `wc_name` and creates/adopts an open Odoo row;
- clock-out closes and verifies every open Odoo row for the employee at the same timestamp, including bad overlapping rows, so no work-center location remains open past the workday;
- normal dashboard has no work-center picker or Transfer action;
- leave override confirmation carries no work-center value;
- a stale POST to pick/transfer returns HTTP 410 with a translated message telling the worker to use the plant-floor app;
- retrying a Plant Manager clock-in never clears a work center Luke's app already set.

In `off`, `shadow`, and pending-live states, assert the existing picker/transfer path remains available until readiness has proven Luke's feed and boundary activation succeeds. Shadow Staffing may display the new live overlay without changing timeclock ownership yet.

- [ ] **Step 2: Run timeclock tests and verify they fail**

Run: `uv run pytest tests/test_timeclock_day_boundary_only.py -q`

Expected: FAIL because timeclock still asks for and transfers work centers.

- [ ] **Step 3: Simplify timeclock actions**

When `live_is_active()` is true, change clock-in to call `clock_in(employee_odoo_id, wc_name=None, ts=clock_time_utc)`. Clock-out and its retry path call `close_all_open_attendance_rows` and do not mark the local sync row complete until Odoo verifies that none remain open. Remove the picker and Transfer controls only after activation. Keep route names as 410 compatibility handlers after activation so an old browser cannot silently perform a routine transfer; retain legacy behavior while mode is `off`, `shadow`, or pending live. In `timeclock_sync`, adopt an existing open Odoo row without calling `set_attendance_wc` when the desired WC is `None`.

- [ ] **Step 4: Migrate attendance readers to the canonical mirror**

Write `tests/test_attendance_mirror_cutover.py` first. Assert that `off` mode keeps the legacy cache path, while a complete mirror baseline in `shadow` or `live` makes `attendance`, `attendance_state`, `timeclock_windows`, `staffing_attendance`, and `auto_lunch` read the mirror and its shared freshness timestamp. No shadow/live reader may make an on-request Odoo attendance fetch or consult `odoo_open_attendance_cache`.

Add mirror queries for day presence and current open attendance, then migrate those modules. In `app.py`, make `_tick_odoo_attendance` and the attendance half of `_tick_live_cache` no-op once the mirror owns reads; retain the production refresh half of `_tick_live_cache`. Keep the old table and functions during rollback, but establish the mirror as the only shadow/live attendance source.

- [ ] **Step 5: Write Staffing live-overlay tests**

Cover planned assignments beside live `LocationSpan`s, “Working elsewhere” on an empty planned seat, unscheduled live worker, exact unknown Odoo location label, pending/missing/conflict badges, exempt workers outside work-center bays, source freshness, and shadow-vs-live labels.

- [ ] **Step 6: Run Staffing tests and verify they fail**

Run: `uv run pytest tests/test_staffing_live_locations.py -q`

Expected: FAIL because Staffing uses the legacy attendance/attribution view.

- [ ] **Step 7: Implement planned-plus-live view data**

Add:

```python
@dataclass(frozen=True)
class StaffingPersonLocation:
    person_name: str
    planned_work_center: str | None
    live_work_center: str | None
    raw_odoo_work_center: str | None
    status: LocationStatus
    since_utc: datetime
    source_fresh_at: datetime | None

build_live_locations(planned_by_wc: Mapping[str, Sequence[str]],
                     spans: Sequence[LocationSpan],
                     *, as_of_utc: datetime) -> tuple[StaffingPersonLocation, ...]
```

Render scheduled position and live position as separate facts. Never move the schedule card because of attendance. A mapped live work center controls production; a raw unknown value is visibly “Odoo only — mapping needed.”

- [ ] **Step 8: Disable Plant Manager's routine Odoo transfer side effects**

After `live_is_active()` becomes true, legacy `/api/staffing/attribute` actions may retain testing and reporting annotations but must not call `staffing_transfer.decide_and_apply`. Return a clear 409/410 response for real-person transfer attempts. Keep undo support for historical audit events; do not delete old data.

- [ ] **Step 9: Run focused regressions**

Run:

```bash
uv run pytest tests/test_timeclock_day_boundary_only.py tests/test_timeclock_bilingual_render.py tests/test_timeclock_state_reconciliation.py tests/test_timeclock_sync_dedup.py tests/test_attendance_mirror_cutover.py tests/test_attendance.py tests/test_attendance_state.py tests/test_timeclock_windows.py tests/test_staffing_attendance_source.py tests/test_auto_lunch_worker.py tests/test_staffing_live_locations.py tests/test_staffing_attribute_endpoints.py tests/test_staffing_transfer.py -q
```

Expected: PASS.

- [ ] **Step 10: Add the patch note**

Add:

```markdown
### Timeclock starts and ends the day

- **When the new Odoo location rule is turned on, workers will use Plant Manager only to clock in and out.** The plant-floor app will choose work areas and move people during the day, while Staffing can already show both the plan and the live Odoo location.
```

- [ ] **Step 11: Commit and push Task 11**

Run:

```bash
git add src/zira_dashboard/attendance.py src/zira_dashboard/attendance_state.py src/zira_dashboard/auto_lunch.py src/zira_dashboard/live_cache.py src/zira_dashboard/timeclock_windows.py src/zira_dashboard/staffing_attendance.py src/zira_dashboard/app.py src/zira_dashboard/routes/timeclock.py src/zira_dashboard/templates/timeclock_dashboard.html src/zira_dashboard/templates/timeclock_time_off_override_confirm.html src/zira_dashboard/timeclock_sync.py src/zira_dashboard/routes/staffing.py src/zira_dashboard/staffing_view.py src/zira_dashboard/templates/staffing.html src/zira_dashboard/static/staffing.js src/zira_dashboard/static/staffing.css src/zira_dashboard/staffing_transfer.py tests/test_timeclock_day_boundary_only.py tests/test_attendance_mirror_cutover.py tests/test_staffing_live_locations.py tests/test_staffing_attribute_endpoints.py CHANGELOG.md
git commit -m "feat: separate day clock from floor locations"
git push origin main
```

### Task 12: Start worker breakdown timing at the worker's own arrival

**Files:**
- Modify: `src/zira_dashboard/machine_breakdown.py`
- Modify: `src/zira_dashboard/breakdown_actions.py`
- Modify: `src/zira_dashboard/exception_inbox.py`
- Modify: `src/zira_dashboard/routes/exceptions.py`
- Modify: `src/zira_dashboard/templates/exceptions.html`
- Test: `tests/test_machine_breakdown_detect.py`
- Test: `tests/test_machine_breakdown_rows.py`
- Test: `tests/test_exception_inbox_breakdown.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write worker-relative timing tests**

Cover:

- worker present before last output: personal stop equals last output;
- worker arrives after last output: personal stop equals arrival;
- worker has not reached the breakdown threshold since arrival: no urgent worker row and no personal production exclusion;
- worker crosses threshold later: row/exclusion starts at arrival;
- transfer away in Odoo closes/auto-resolves the worker row;
- station-level breakdown can remain open independent of a newly arrived worker;
- conflicting/unmapped/missing location never counts as an operator at the station.

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/test_machine_breakdown_detect.py tests/test_machine_breakdown_rows.py tests/test_exception_inbox_breakdown.py -q`

Expected: FAIL for the new arrival-relative cases.

- [ ] **Step 3: Replace operator boolean with valid location arrivals**

Use:

```python
@dataclass(frozen=True)
class OperatorPresence:
    person_name: str
    wc_name: str
    arrival_utc: datetime

def personal_breakdown_start(*, station_stop_utc: datetime,
                             arrival_utc: datetime) -> datetime:
    return max(station_stop_utc, arrival_utc)
```

Build operator presence only from current valid timeline spans. Apply the existing breakdown threshold to `now - personal_start` for worker rows and exclusions.

- [ ] **Step 4: Remove the breakdown Transfer action**

Keep Snooze and explanatory state. Luke's floor app owns the move after activation; a valid Odoo transfer causes the next snapshot to resolve the worker row. Return 410 for stale breakdown-transfer requests only after `live_is_active()` becomes true.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest tests/test_machine_breakdown_detect.py tests/test_machine_breakdown_rows.py tests/test_exception_inbox_breakdown.py tests/test_exceptions_breakdown_routes.py -q`

Expected: PASS.

- [ ] **Step 6: Add the patch note**

Add:

```markdown
### Breakdown time starts when each worker arrives

- **A worker who reaches a stopped machine later will no longer inherit breakdown time from before they arrived.** Their warning and excluded time start from their own arrival.
```

- [ ] **Step 7: Commit and push Task 12**

Run:

```bash
git add src/zira_dashboard/machine_breakdown.py src/zira_dashboard/breakdown_actions.py src/zira_dashboard/exception_inbox.py src/zira_dashboard/routes/exceptions.py src/zira_dashboard/templates/exceptions.html tests/test_machine_breakdown_detect.py tests/test_machine_breakdown_rows.py tests/test_exception_inbox_breakdown.py CHANGELOG.md
git commit -m "fix: use worker arrival for breakdown time"
git push origin main
```

### Task 13: Add shadow comparison, readiness checks, observability, and live cutover validation

**Files:**
- Create: `src/zira_dashboard/attendance_readiness.py`
- Modify: `src/zira_dashboard/attendance_exceptions.py`
- Modify: `src/zira_dashboard/exception_inbox.py`
- Modify: `src/zira_dashboard/routes/settings.py`
- Modify: `src/zira_dashboard/templates/settings.html`
- Modify: `src/zira_dashboard/app.py`
- Create: `scripts/check_attendance_location_readiness.py`
- Test: `tests/test_attendance_readiness.py`
- Test: `tests/test_attendance_location_end_to_end.py`
- Test: `tests/test_attendance_location_failure_modes.py`
- Modify: `docs/superpowers/specs/2026-08-28-odoo-attendance-live-location-truth-design.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write readiness and shadow-comparison tests**

Readiness must report:

```python
@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    mirror_age_seconds: float | None
    last_full_sweep_age_seconds: float | None
    open_rows_not_refreshed: int
    last_sweep_deletion_count: int
    projection_lag_seconds: float | None
    recalc_queue_age_seconds: float | None
    recalc_queue_depth: int
    open_conflicts: int
    conflict_minutes_today: float
    open_unmapped: int
    unmapped_minutes_today: float
    open_missing_required: int
    missing_minutes_today: float
    unassigned_units_today: float
    oldest_unassigned_age_seconds: float | None
    shadow_changed_worker_units: float
    failed_corrections: int
    correction_retries_today: int
    correction_verification_failures_today: int
    failed_department_repairs: int
    blockers: tuple[str, ...]
```

Test hard blockers: incomplete baseline, mirror older than 90 seconds, open rows not refreshed, full sweep older than two hours, stuck recalculation, failed correction/repair, unresolved conflicts, and cutover not at the local workday boundary. Also test ready scheduling, successful boundary activation, failed boundary recheck returning to shadow, stable cutover-blocked exception identity, and an already-strict day staying strict after rollback. Unmapped or missing rows are visible metrics and blockers for affected production work centers, not hidden warnings. Emit structured logs with attendance, employee, work-center, exception, correction, repair, and recalculation IDs, but omit unrelated personal fields.

- [ ] **Step 2: Run readiness tests and verify they fail**

Run: `uv run pytest tests/test_attendance_readiness.py -q`

Expected: FAIL because the readiness module does not exist.

- [ ] **Step 3: Implement shadow comparison and health view**

In `shadow` mode, compute strict Odoo attribution without writing `production_daily`; compare it with current attribution by day/person/work center and store only aggregate health counters in `app_settings` as a dict. Do not retain a second source of attendance truth.

Expose `build_report(now_utc)` and render it in Settings with timestamps, queue ages, anomaly counts, unassigned units, correction status, repair status, and the reason live mode is blocked.

When a super admin submits a future clean-boundary cutover, build a fresh report in the same request. Only a ready report may store a pending gate with `checked_at`, `report_digest`, and `activated_at = null`, together with mode `live`; the save transaction must finish within five minutes of `checked_at`.

Add `activate_due_cutover(now_utc)` to a 30-second warmer. At the cutover boundary it builds another fresh readiness report. While that decision is pending, production precompute refuses to write a fallback snapshot. If ready, activation atomically sets `live_gate.activated_at`, records/enqueues the cutover day as strict, and lets the recalculation worker rebuild it before ordinary live refresh continues. If not ready, it atomically returns mode to `shadow`, clears the pending gate, leaves production on the prior matcher, and creates one urgent `attendance_cutover_blocked` exception keyed by the scheduled cutover with the report's blockers. `day_is_strict` never treats a merely pending gate as live.

- [ ] **Step 4: Add a read-only readiness script**

`scripts/check_attendance_location_readiness.py` must initialize app config, call `attendance_readiness.build_report`, print JSON, and exit 0 only when `ready` is true. It performs no Odoo writes and no mode change.

Run: `uv run python scripts/check_attendance_location_readiness.py`

Expected before a configured baseline: non-zero exit with explicit blockers.

- [ ] **Step 5: Write end-to-end and failure-mode tests**

Build a fake Odoo plus fake production meter scenario that proves:

1. Plant Manager clock-in opens WC-less attendance.
2. The first five minutes are pending and production is unassigned.
3. Luke's app supplies WC A; sync/mirror/timeline show WC A.
4. WC A production credits the worker.
5. Luke transfers the worker to WC B; later production credits WC B.
6. Two workers at WC B split samples equally.
7. Conflicting WC rows credit neither location.
8. An unknown raw WC is shown exactly and credits nothing.
9. A manager correction splits Odoo rows, verifies them, recalculates, and resolves the run.
10. Plant Manager clock-out closes the final attendance.
11. A post-baseline historical edit strictly recalculates the old day.
12. Odoo timeout, partial sweep, stale preview, and failed re-read never fabricate credit or erase mirrored data.
13. A positive meter total without matching timestamped samples leaves the prior computed snapshot unchanged and raises a source exception.

- [ ] **Step 6: Run end-to-end tests and fix only failures within this feature**

Run:

```bash
uv run pytest tests/test_attendance_location_end_to_end.py tests/test_attendance_location_failure_modes.py -q
```

Expected: PASS.

- [ ] **Step 7: Update the design's rollout section with the exact operator runbook**

Record these commands and decisions:

1. deploy with mode `off`; verify schema and mirror worker;
2. wait for a completed baseline and full sweep;
3. set `shadow`; observe at least one complete production day;
4. resolve mappings/conflicts and exercise one correction in a non-production test interval;
5. run `uv run python scripts/check_attendance_location_readiness.py` until exit 0;
6. schedule `live` for the next local workday boundary before production begins;
7. monitor mirror age, unassigned units, queue age, conflicts, corrections, and department repairs;
8. schedule rollback to `shadow` at the next clean workday boundary; keep the mirror, already-strict days, source records, and audit history intact.

- [ ] **Step 8: Run the complete validation suite**

Run:

```bash
uv run pytest -q
uv run ruff check src tests scripts
git diff --check
```

Expected: all tests PASS, Ruff reports no errors, and `git diff --check` prints nothing.

- [ ] **Step 9: Add the final patch note**

Add:

```markdown
### Odoo live locations are ready for a safe start

- **Plant Manager can now check that Odoo locations are fresh and complete before they control production.** The app can compare the new answer first, start on a clean workday, and show clear reasons when it is not safe to start.
```

- [ ] **Step 10: Commit and push Task 13**

Run:

```bash
git add src/zira_dashboard/attendance_readiness.py src/zira_dashboard/attendance_exceptions.py src/zira_dashboard/exception_inbox.py src/zira_dashboard/routes/settings.py src/zira_dashboard/templates/settings.html src/zira_dashboard/app.py scripts/check_attendance_location_readiness.py tests/test_attendance_readiness.py tests/test_attendance_location_end_to_end.py tests/test_attendance_location_failure_modes.py docs/superpowers/specs/2026-08-28-odoo-attendance-live-location-truth-design.md CHANGELOG.md
git commit -m "feat: validate Odoo location cutover"
git push origin main
```

## Final Acceptance Checks

- [ ] Plant Manager clock-in creates or adopts an open attendance without selecting or clearing a work center.
- [ ] Plant Manager clock-out closes the active attendance at the end of the day.
- [ ] Luke's Odoo work-center attendance changes appear in Staffing within 30 seconds under normal service health.
- [ ] Production is credited only inside valid, mapped Odoo location spans on strict days.
- [ ] Missing, unknown, conflicting, and uncovered production intervals remain visibly unassigned.
- [ ] Two or more valid workers on one work center split samples equally and all source units are conserved.
- [ ] Maintenance and Supervisor can remain clocked in without work-center exceptions; department policy remains configurable.
- [ ] Work-center/department mismatches keep the work center, repair the department, and verify Odoo.
- [ ] Manager corrections preserve gaps, use exact start/end or open time, survive retry, verify Odoo, recalculate affected days, and resolve only after verification.
- [ ] Historical source changes after baseline mark the touched days strict without rewriting untouched history.
- [ ] Worker breakdown timing begins at `max(last output, worker arrival)`.
- [ ] Settings shows mirror freshness, last full sweep, queue age, exceptions, unassigned production, correction failures, department repair failures, and live-cutover blockers.
- [ ] `uv run pytest -q`, `uv run ruff check src tests scripts`, and `git diff --check` all pass before live mode is scheduled.
