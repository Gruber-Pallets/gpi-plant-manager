# Maintenance Attendance Continuity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Treat a closed, provably transitional home-department attendance row between Maintenance assignments as effective Maintenance, keep it out of missing-location blockers, and give managers a proof-fenced one-click repair that changes only that row's Odoo department.

**Architecture:** Add one pure continuity classifier that receives raw attendance rows, employee home departments, and the existing work-center mapper. Every Plant Manager projection path runs that classifier before the canonical timeline projection. The Inbox uses the same classifier's immutable proof to show a non-error suggestion. A dedicated durable repair queue revalidates the exact three Odoo rows before and after changing only the target department, while the existing mirror and recalculation queues propagate the verified result.

**Tech Stack:** Python 3.11, FastAPI, PostgreSQL/psycopg2, Jinja2, browser JavaScript, pytest, existing Odoo XML-RPC facade, existing Railway deployment.

## Global Constraints

- Preserve separate Maintenance work-order attendance records. Never merge, stretch, create, or delete those rows in Plant Manager.
- Never invent a work center for general Maintenance time.
- Do not change GPI Forklift behavior. Its existing pre-lunch/post-lunch carry-over remains unchanged.
- A candidate is inferable only when it is closed, has no work center, exactly touches a preceding Maintenance row and a following Maintenance or mapped-work-center row, has no overlap, and its raw department equals the employee's Odoo home department.
- There is no duration cap. Exact evidence, not elapsed minutes, controls the decision.
- Open fallback rows, missing boundaries, overlaps, unmapped next work centers, or conflicting identity remain normal blocking missing-location exceptions.
- The projection is local and read-only. Only a signed-in manager's explicit one-click action may enqueue an Odoo correction.
- The correction writes only the target attendance department. It must fence attendance IDs, employee ID, timestamps, write versions, work-center values, raw/target department values, and both boundary records.
- The target department ID is copied from the proven preceding Maintenance row. Do not hard-code an Odoo department ID or resolve it by a loose name search.
- Retries must be idempotent. A timeout followed by a successful readback is success, not a second write.
- New `CHANGELOG.md` text must use short, common words that a 10-year-old can understand.
- Preserve unrelated workspace changes and the existing untracked `.cursorignore`, `.python-version`, and `uv.lock` files.
- Keep deployment atomic: do not commit an intermediate task. After each focused test checkpoint, inspect the diff and continue. Commit and push the complete verified implementation once in Task 7.
- This plan implements the independently useful **Plant Manager safety track** from the approved design. The GPI Maintenance source track must be planned and implemented in its owning repository; that repository is not available in this workspace, so this plan does not invent its paths or implementation.

---

## Task 1: Build the pure Maintenance continuity classifier

**Files:**

- Create: `src/zira_dashboard/maintenance_continuity.py`
- Create: `tests/test_maintenance_continuity.py`

- [ ] **Step 1: Write failing tests for the accepted and rejected chains**

Create fixture helpers that always include the canonical mirror fields:

```python
def row(
    attendance_id: int,
    *,
    employee_id: int = 77,
    employee_name: str = "Jose Ochoa",
    start: str,
    end: str | None,
    department_id: int | None,
    department_name: str | None,
    work_center_id: int | None = None,
    work_center_name: str | None = None,
    write_date: str | None = None,
) -> dict[str, object]:
    return {
        "odoo_attendance_id": attendance_id,
        "employee_odoo_id": employee_id,
        "employee_name": employee_name,
        "check_in_utc": datetime.fromisoformat(start),
        "check_out_utc": datetime.fromisoformat(end) if end else None,
        "odoo_work_center_id": work_center_id,
        "odoo_work_center_name": work_center_name,
        "odoo_department_id": department_id,
        "odoo_department_name": department_name,
        "odoo_write_date": datetime.fromisoformat(write_date or end or start),
    }
```

Use Jose's exact September 4 timestamps for the primary case:

```python
rows = (
    row(5425, start="2026-09-04T10:50:52+00:00", end="2026-09-04T11:13:02+00:00",
        department_id=6, department_name="Maintenance"),
    row(5434, start="2026-09-04T11:13:02+00:00", end="2026-09-04T11:15:37+00:00",
        department_id=9, department_name="Recycled"),
    row(5435, start="2026-09-04T11:15:37+00:00", end="2026-09-04T11:48:02+00:00",
        department_id=6, department_name="Maintenance"),
)
projection = classify(
    rows,
    home_department_by_employee={77: "Recycled"},
    map_work_center=lambda work_center_id: None,
)
assert projection.rows[1]["odoo_department_id"] == 6
assert projection.rows[1]["odoo_department_name"] == "Maintenance"
assert projection.rows[1]["maintenance_continuity"] is True
assert projection.proofs[0].target_attendance_id == 5434
assert projection.proofs[0].previous_attendance_id == 5425
assert projection.proofs[0].next_attendance_id == 5435
assert projection.proofs[0].duration == timedelta(seconds=155)
```

Add individual rejection tests for:

- candidate is open;
- blank work center condition is violated;
- candidate raw department differs from the employee home department;
- previous department is not Maintenance;
- candidate starts one microsecond after the previous row ends;
- candidate ends one microsecond before the next row starts;
- next row is neither Maintenance nor a mapped work center;
- an additional row overlaps any part of the candidate;
- employee identity changes across a boundary;
- previous Maintenance department ID is absent;
- duplicate attendance IDs or malformed timestamps fail closed;
- input mappings remain byte-for-byte/equality unchanged;
- a long exact interval is accepted, proving no maximum duration exists;
- a mapped next work center is accepted;
- an unmapped next work center is rejected;
- rows for different employees do not become each other's boundaries.

- [ ] **Step 2: Run the tests and confirm the module is missing**

Run:

```bash
pytest -q tests/test_maintenance_continuity.py
```

Expected: FAIL because `zira_dashboard.maintenance_continuity` does not exist.

- [ ] **Step 3: Implement immutable proof and projection values**

In `maintenance_continuity.py`, define:

```python
@dataclass(frozen=True)
class ContinuityProof:
    employee_odoo_id: int
    employee_name: str
    previous_attendance_id: int
    previous_write_date: datetime
    target_attendance_id: int
    target_write_date: datetime
    next_attendance_id: int
    next_write_date: datetime
    start_utc: datetime
    end_utc: datetime
    raw_department_id: int
    raw_department_name: str
    target_department_id: int
    target_department_name: str

    @property
    def duration(self) -> timedelta:
        return self.end_utc - self.start_utc


@dataclass(frozen=True)
class ContinuityProjection:
    rows: tuple[Mapping[str, object], ...]
    proofs: tuple[ContinuityProof, ...]
```

Implement the public pure function:

```python
def classify(
    rows: Sequence[Mapping[str, object]],
    *,
    home_department_by_employee: Mapping[int, str | None],
    map_work_center: Callable[[int], str | None],
) -> ContinuityProjection:
    """Return cloned effective rows plus proof for exact legacy chains."""
```

Required implementation details:

- normalize names with `attendance_location_policy._normalized_department_name()` so numbered Odoo labels compare consistently;
- validate and sort by `(employee_odoo_id, check_in_utc, odoo_attendance_id)` without mutating caller data;
- evaluate each candidate against its immediate same-employee predecessor and successor;
- scan the same employee's complete interval set for any overlap with the candidate;
- accept the successor only if it is Maintenance or `map_work_center(successor_wc_id)` returns a non-empty app work-center name;
- clone only accepted candidate mappings and replace `odoo_department_id`/`odoo_department_name` with the preceding Maintenance values;
- add local-only keys `maintenance_continuity=True` and the positive target attendance ID as `maintenance_continuity_attendance_id` to accepted clones;
- preserve the original row order in `ContinuityProjection.rows` so callers cannot accidentally change display ordering;
- return proofs sorted by target attendance ID for stable snapshots;
- export only `ContinuityProof`, `ContinuityProjection`, and `classify`.

- [ ] **Step 4: Run the focused classifier tests**

Run:

```bash
pytest -q tests/test_maintenance_continuity.py
```

Expected: PASS.

- [ ] **Step 5: Inspect the classifier checkpoint**

Run:

```bash
git diff --check
git status --short
```

Expected: the classifier and its tests are uncommitted, `git diff --check` is silent, and unrelated files remain untouched.

---

## Task 2: Route every timeline and readiness projection through one continuity boundary

**Files:**

- Modify: `src/zira_dashboard/attendance_timeline.py`
- Modify: `src/zira_dashboard/attendance_readiness.py`
- Modify: `tests/test_attendance_timeline.py`
- Modify: `tests/test_attendance_readiness.py`
- Modify: `tests/test_attendance_location_end_to_end.py`

- [ ] **Step 1: Add failing end-to-end projection tests**

Add tests proving:

1. `timeline_for_range()` projects Jose's 155-second row as `exempt_no_location`, not `missing_required_location`.
2. Direct detached Shadow projection in `_project_shadow_snapshot()` produces the same result.
3. `_timeline_metrics_cur()` reports zero missing seconds for the chain.
4. A superficially similar open or overlapping row remains `missing_required_location`.
5. Salaried exemptions, work-center mappings, and department-repair metadata remain unchanged.

The acceptance assertion must include:

```python
missing_seconds = sum(
    (span.end_utc - span.start_utc).total_seconds()
    for span in spans
    if span.status == "missing_required_location"
)
assert missing_seconds == 0
assert any(
    span.start_utc == datetime(2026, 9, 4, 11, 13, 2, tzinfo=UTC)
    and span.end_utc == datetime(2026, 9, 4, 11, 15, 37, tzinfo=UTC)
    and span.status == "exempt_no_location"
    for span in spans
)
```

- [ ] **Step 2: Run the focused tests and observe the missing-location failure**

Run:

```bash
pytest -q tests/test_attendance_timeline.py tests/test_attendance_readiness.py tests/test_attendance_location_end_to_end.py
```

Expected: new assertions FAIL because the raw Recycled row still reaches `project_rows()` unchanged.

- [ ] **Step 3: Add a shared projection wrapper in `attendance_timeline.py`**

Introduce a public wrapper that performs continuity classification and then invokes the existing canonical projector:

```python
@dataclass(frozen=True)
class TimelineProjection:
    spans: tuple[LocationSpan, ...]
    maintenance_continuity: tuple[maintenance_continuity.ContinuityProof, ...]


def project_effective_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    home_department_by_employee: Mapping[int, str | None],
    as_of_utc: datetime,
    verified_through_utc: datetime,
    map_work_center: Callable[[int], str | None],
    requires_work_center: Callable[[str | None], bool],
    expected_department_id: Callable[[str], int | None],
    grace: timedelta = timedelta(minutes=5),
    stale_after: timedelta = timedelta(seconds=90),
) -> TimelineProjection:
    continuity = maintenance_continuity.classify(
        rows,
        home_department_by_employee=home_department_by_employee,
        map_work_center=map_work_center,
    )
    enriched = _rows_with_employee_department_fallback(
        continuity.rows,
        home_department_by_employee=home_department_by_employee,
    )
    return TimelineProjection(
        spans=project_rows(
            enriched,
            as_of_utc=as_of_utc,
            verified_through_utc=verified_through_utc,
            map_work_center=map_work_center,
            requires_work_center=requires_work_center,
            expected_department_id=expected_department_id,
            grace=grace,
            stale_after=stale_after,
        ),
        maintenance_continuity=continuity.proofs,
    )
```

Refactor `_rows_with_employee_department_fallback()` so callers pass the already loaded home-department mapping. Add one local loader that queries every employee in the raw row set, not only employees whose raw department is blank:

```python
def _employee_profiles_for_rows(
    rows: Sequence[Mapping[str, object]],
) -> dict[int, Mapping[str, object]]:
    """Load home department and wage type once for every employee in rows."""
```

This ordering is mandatory: classify against the raw row and explicit home-department mapping first; only then fill blank display departments and wage types. Otherwise a local fallback could masquerade as raw Odoo evidence.

- [ ] **Step 4: Update all three projection entry points**

Change:

- `timeline_for_range()` to use `project_effective_rows()` and return `.spans` after clipping;
- `_project_shadow_snapshot()` to pass `config.employee_departments` and use `.spans`;
- `_timeline_metrics_cur()` to derive `home_department_by_employee` from its existing `home_by_id` rows and use `.spans`.

Do not leave any direct `attendance_timeline.project_rows()` call in `attendance_readiness.py`:

```bash
rg -n "attendance_timeline\.project_rows\(" src/zira_dashboard/attendance_readiness.py
```

Expected: no output.

Keep `project_rows()` public for pure low-level unit tests and callers that intentionally test raw canonical projection. Production paths use `project_effective_rows()`.

- [ ] **Step 5: Run projection and regression tests**

Run:

```bash
pytest -q tests/test_maintenance_continuity.py tests/test_attendance_timeline.py tests/test_attendance_readiness.py tests/test_attendance_location_end_to_end.py tests/test_attendance_location_failure_modes.py tests/test_attendance_location_policy.py
```

Expected: PASS.

- [ ] **Step 6: Inspect the shared projection checkpoint**

Run:

```bash
git diff --check
git status --short
```

Expected: all Task 1-2 edits remain uncommitted for the atomic release.

---

## Task 3: Replace the false error with a non-error Maintenance suggestion

**Files:**

- Modify: `src/zira_dashboard/attendance_exceptions.py`
- Modify: `src/zira_dashboard/exception_inbox.py`
- Modify: `src/zira_dashboard/inbox_reconcile.py`
- Modify: `src/zira_dashboard/inbox_keys.py`
- Modify: `tests/test_attendance_exceptions.py`
- Modify: `tests/test_exception_inbox_attendance.py`
- Modify: `tests/test_inbox_keys_attendance.py`
- Modify: `tests/test_inbox_reconcile.py`

- [ ] **Step 1: Write failing exception and Inbox tests**

Add tests proving Jose's chain produces exactly one issue with:

```python
assert issue.kind == "maintenance_continuity_suggestion"
assert issue.priority == "warn"
assert issue.reason == "legacy_home_department_between_maintenance"
assert issue.attendance_ids == (5434,)
assert issue.target_odoo_department_id == 6
assert issue.comparison_only is False
```

The same snapshot must contain no `attendance_missing_location` for attendance 5434.

Add Inbox row assertions:

```python
assert row["category_label"] == "Maintenance carry-forward"
assert row["label"] == "Time between Maintenance jobs"
assert row["badge"] == "Suggested fix"
assert row["priority"] == "warn"
assert row["action"] == {
    "type": "maintenance_continuity",
    "item_key": issue.item_key,
}
```

The card detail must expose the raw home department and both boundary attendance IDs without labeling the item as an error.

- [ ] **Step 2: Run focused tests and confirm failure**

Run:

```bash
pytest -q tests/test_attendance_exceptions.py tests/test_exception_inbox_attendance.py tests/test_inbox_keys_attendance.py tests/test_inbox_reconcile.py
```

Expected: FAIL because no suggestion kind exists.

- [ ] **Step 3: Carry proof fields on `AttendanceException`**

Add optional, immutable fields with safe defaults:

```python
continuity_proof: maintenance_continuity.ContinuityProof | None = None
raw_department_name: str | None = None
previous_attendance_id: int | None = None
next_attendance_id: int | None = None
```

Add `_maintenance_continuity_issues(proofs)` that creates stable keys from the target attendance ID and target write version. The stable key helper in `inbox_keys.py` should have the form:

```python
def maintenance_continuity(attendance_id: int) -> str:
    return f"maintenance_continuity:{_positive_id(attendance_id)}"
```

In `build_snapshot()`, load raw rows once, load employee profiles once, call `project_effective_rows()` once, use `.spans` for existing issues, and use `.maintenance_continuity` for suggestion issues. Do not independently reimplement the chain rules inside `attendance_exceptions.py`.

- [ ] **Step 4: Shape the non-error section and reconcile lifecycle**

Add:

```python
"maintenance_continuity_suggestion": ("Maintenance carry-forward", "warn"),
```

Set row text to:

- label: `Time between Maintenance jobs`
- badge: `Suggested fix`
- action type: `maintenance_continuity`
- action label, rendered later: `Carry previous Maintenance department forward`

Include the proof's raw department, previous ID, next ID, target department ID, and all three write versions in `_attendance_row_key()` so a changed proof changes the displayed revision.

Register the section in `inbox_reconcile._SECTION_KIND`, `_KIND_SOURCE`, and `_CORRECTION_LINKED_KINDS`. A completed repair should leave the queue only after the normal source-complete grace and should be linked to the manager action rather than logged as an unexplained auto-resolution.

- [ ] **Step 5: Run exception/Inbox tests**

Run:

```bash
pytest -q tests/test_attendance_exceptions.py tests/test_exception_inbox_attendance.py tests/test_inbox_keys_attendance.py tests/test_inbox_reconcile.py
```

Expected: PASS.

- [ ] **Step 6: Inspect the suggestion-model checkpoint**

Run:

```bash
git diff --check
git status --short
```

Expected: all Task 1-3 edits remain uncommitted for the atomic release.

---

## Task 4: Add a durable, proof-fenced one-click repair worker

**Files:**

- Modify: `src/zira_dashboard/_schema.py`
- Create: `src/zira_dashboard/maintenance_continuity_repair.py`
- Modify: `src/zira_dashboard/odoo_client.py`
- Modify: `src/zira_dashboard/app.py`
- Modify: `tests/test_attendance_location_schema.py`
- Create: `tests/test_maintenance_continuity_repair.py`
- Modify: `tests/test_attendance_sync_warmer.py`

- [ ] **Step 1: Write failing schema, queue, worker, and warmer tests**

Cover:

- exact proof creates one `planned` job;
- repeated clicks return the same active job;
- conflicting proof for the same target is rejected;
- claim uses `FOR UPDATE SKIP LOCKED` and a lease so concurrent warmers cannot double-apply;
- worker rereads exactly the previous, target, and next Odoo IDs;
- any changed ID, employee, timestamp, write date, work center, or department rejects with `source_changed` and performs no write;
- readback showing the target already has the expected Maintenance department completes as `already_correct`;
- a write changes only `odoo_department_id`;
- timeout followed by readback of the expected department completes as `adopted_timeout`;
- failed readback retries up to `MAX_ATTEMPTS`, then records `failed`;
- successful verification updates the mirror with the authoritative Odoo row and enqueues recalculation for every touched plant day;
- event rows record planned, applying, verified/rejected, actor identity, and bounded technical details;
- a successful or terminally rejected job is no longer active;
- app warmer advances one repair every 15 seconds.

- [ ] **Step 2: Run tests and confirm missing implementation**

Run:

```bash
pytest -q tests/test_attendance_location_schema.py tests/test_maintenance_continuity_repair.py tests/test_attendance_sync_warmer.py
```

Expected: FAIL because the table and module do not exist.

- [ ] **Step 3: Add the idempotent repair schema**

Append this installation-safe schema shape to `_schema.py`:

```sql
CREATE TABLE IF NOT EXISTS maintenance_continuity_repairs (
  id BIGSERIAL PRIMARY KEY,
  operation_key TEXT NOT NULL UNIQUE,
  item_key TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN
    ('planned', 'applying', 'complete', 'rejected', 'failed')),
  employee_odoo_id BIGINT NOT NULL,
  previous_attendance_id BIGINT NOT NULL,
  previous_write_date TIMESTAMPTZ NOT NULL,
  target_attendance_id BIGINT NOT NULL,
  target_write_date TIMESTAMPTZ NOT NULL,
  next_attendance_id BIGINT NOT NULL,
  next_write_date TIMESTAMPTZ NOT NULL,
  start_utc TIMESTAMPTZ NOT NULL,
  end_utc TIMESTAMPTZ NOT NULL,
  raw_department_id BIGINT NOT NULL,
  raw_department_name TEXT NOT NULL,
  target_department_id BIGINT NOT NULL,
  target_department_name TEXT NOT NULL,
  actor_email TEXT NOT NULL,
  actor_name TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  lease_started_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  outcome TEXT,
  last_error TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS maintenance_continuity_repairs_active_target_idx
  ON maintenance_continuity_repairs (target_attendance_id)
  WHERE status IN ('planned', 'applying');

CREATE TABLE IF NOT EXISTS maintenance_continuity_repair_events (
  id BIGSERIAL PRIMARY KEY,
  repair_id BIGINT NOT NULL REFERENCES maintenance_continuity_repairs(id),
  phase TEXT NOT NULL,
  result TEXT NOT NULL,
  detail JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS maintenance_continuity_repair_events_repair_idx
  ON maintenance_continuity_repair_events (repair_id, id);
```

`operation_key` must be a SHA-256 digest of the immutable proof fields, not employee name or browser state.

- [ ] **Step 4: Implement the repair service around the shared classifier**

Define these public interfaces in `maintenance_continuity_repair.py`:

```python
def create_job(
    proof: maintenance_continuity.ContinuityProof,
    *,
    item_key: str,
    actor_email: str,
    actor_name: str,
) -> int:
    """Persist or reuse one proof-bound manager repair intent."""


def process_next(*, now_utc: datetime | None = None) -> int | None:
    """Claim, apply, verify, and settle at most one durable repair."""


def status(job_id: int) -> Mapping[str, object] | None:
    """Return one safe repair status mapping, or None when absent."""
```

Also define `RepairConflict(RuntimeError)` with a boolean `source_changed`
attribute so routes can distinguish stale evidence from an active-job clash.

Worker sequence:

1. atomically claim one planned/stale-applying job;
2. fetch the exact three rows with `odoo_client.fetch_attendance_rows_by_ids()`;
3. require exactly three returned IDs and normalize them with the mirror's canonical normalizer;
4. reload the employee's current home department from local `people`;
5. rerun `maintenance_continuity.classify()` over the three authoritative rows with the live work-center mapper;
6. require the newly generated proof to equal the stored proof;
7. call `odoo_client.set_attendance_department_id(target_id, target_department_id)` once;
8. fetch the target row again and require the target department plus unchanged employee/times/work center;
9. upsert only the verified authoritative row into the mirror using the existing mirror transaction helper;
10. enqueue attendance recalculation for every local plant day touched by `[start_utc, end_utc)`;
11. write an immutable repair event and mark complete.

If Odoo raises during the write, perform the same authoritative readback before deciding whether to retry. Never issue another write when readback already matches.

Add a small Odoo facade helper only if needed to avoid `_require_attendance_correction_wc_field()` coupling. The helper must require the configured department field but must not require a work-center field because this repair intentionally has no work center:

```python
def set_attendance_department_id_only(attendance_id: int, department_id: int) -> None:
    department_field = _kiosk_department_field()
    if not department_field:
        raise OdooConfigError("Odoo attendance repair requires a configured department field")
    if not execute(
        "hr.attendance",
        "write",
        [int(attendance_id)],
        {department_field: int(department_id)},
    ):
        raise RuntimeError(f"Odoo did not update attendance department {attendance_id}")
```

- [ ] **Step 5: Wire a dedicated warmer**

In `app.py` add:

```python
async def _tick_maintenance_continuity_repair():
    from . import maintenance_continuity_repair
    await asyncio.to_thread(maintenance_continuity_repair.process_next)
```

Register it next to the other attendance workers:

```python
("maintenance continuity repair", _tick_maintenance_continuity_repair, 15),
```

- [ ] **Step 6: Run queue and worker tests**

Run:

```bash
pytest -q tests/test_attendance_location_schema.py tests/test_maintenance_continuity_repair.py tests/test_attendance_sync_warmer.py tests/test_attendance_mirror.py tests/test_attendance_recalc.py
```

Expected: PASS.

- [ ] **Step 7: Inspect the durable-worker checkpoint**

Run:

```bash
git diff --check
git status --short
```

Expected: all Task 1-4 edits remain uncommitted for the atomic release.

---

## Task 5: Wire the one-click manager action into the Inbox

**Files:**

- Modify: `src/zira_dashboard/routes/exceptions.py`
- Modify: `src/zira_dashboard/templates/exceptions.html`
- Modify: `src/zira_dashboard/static/exceptions.js`
- Modify: `src/zira_dashboard/static/exceptions.css`
- Modify: `tests/test_exceptions_attendance_routes.py`
- Modify: `tests/test_exception_inbox_attendance_template.py`
- Modify: `tests/test_exception_inbox_attendance_js.py`

- [ ] **Step 1: Write failing route and browser-contract tests**

Test:

- anonymous requests receive `401 manager_identity_required`;
- body accepts only `item_key` and `row_key` and rejects unknown fields;
- route rebuilds today's current Inbox snapshot and finds exactly one current suggestion;
- missing, stale, duplicate, or changed row keys return `409 source_changed` and enqueue nothing;
- valid request enqueues one job with authenticated email/name and returns its ID;
- repeated request returns the active job ID;
- route never writes directly to Odoo;
- status endpoint reveals only safe state/outcome text;
- template renders exactly one button labeled `Carry previous Maintenance department forward`;
- JavaScript disables the button while submitting, shows queued/progress state, polls status, refreshes the row after success/rejection, and re-enables after retryable transport failure;
- pressing the button sends only the two allowed binding fields.

- [ ] **Step 2: Run focused tests and confirm missing route/action**

Run:

```bash
pytest -q tests/test_exceptions_attendance_routes.py tests/test_exception_inbox_attendance_template.py tests/test_exception_inbox_attendance_js.py
```

Expected: FAIL.

- [ ] **Step 3: Add one-click apply and status routes**

Add:

```python
@router.post("/api/exceptions/maintenance-continuity/apply")
async def maintenance_continuity_apply(request: Request):
    """Validate the current Inbox proof and enqueue one manager repair."""


@router.get("/api/exceptions/maintenance-continuity/{job_id}")
def maintenance_continuity_status(job_id: int, request: Request):
    """Return safe progress for one Maintenance continuity repair."""
```

Reuse `_correction_manager()`, bounded JSON parsing, and the existing safe error response style. The synchronous apply helper must:

1. rebuild the current day's `attendance_exceptions` snapshot;
2. find one `maintenance_continuity_suggestion` matching `item_key`;
3. reshape it and compare the current `row_key` to the submitted value;
4. require a non-null `continuity_proof`;
5. call `maintenance_continuity_repair.create_job()`;
6. return `{ok: true, job_id, status: "planned"}`.

The server-side rebuild is the click-time preview. This preserves the requested one-click UX without trusting proof fields supplied by the browser.

- [ ] **Step 4: Render the proof and action**

Add `data-maintenance-*` attributes only for this row kind. Display:

- raw department;
- `Previous: Maintenance attendance 5425` for Jose's fixture;
- `Next: Maintenance attendance 5435` for Jose's fixture, or the real mapped work-center name for that suggestion;
- exact source interval;
- one primary action button.

Do not show a work-center picker or label the row `Needs decision`. Use `Suggested fix`.

- [ ] **Step 5: Implement safe asynchronous status handling**

Add a dedicated click handler that posts:

```javascript
{
  item_key: row.dataset.itemKey,
  row_key: row.dataset.rowKey
}
```

Poll only while status is `planned` or `applying`. Show:

- `Carrying Maintenance forward…` while pending;
- `Fixed in Odoo` on complete, then refresh the queue;
- `Attendance changed. Review the refreshed card.` on rejected/source-changed;
- `Could not finish the repair. It is safe to try again.` on failed.

Never optimistically remove the card before the verified worker result.

- [ ] **Step 6: Run route, template, and JavaScript tests**

Run:

```bash
pytest -q tests/test_exceptions_attendance_routes.py tests/test_exception_inbox_attendance_template.py tests/test_exception_inbox_attendance_js.py tests/test_exception_inbox_attendance.py
```

Expected: PASS.

- [ ] **Step 7: Inspect the one-click UI checkpoint**

Run:

```bash
git diff --check
git status --short
```

Expected: all Task 1-5 edits remain uncommitted for the atomic release.

---

## Task 6: Add monitoring, readiness health, lunch regression coverage, and replay tooling

**Files:**

- Modify: `src/zira_dashboard/attendance_readiness.py`
- Modify: `src/zira_dashboard/attendance_exceptions.py`
- Modify: `src/zira_dashboard/exception_inbox.py`
- Create: `scripts/check_maintenance_continuity.py`
- Modify: `tests/test_attendance_readiness.py`
- Modify: `tests/test_attendance_exceptions.py`
- Modify: `tests/test_auto_lunch_worker.py`
- Create: `tests/test_check_maintenance_continuity_script.py`

- [ ] **Step 1: Write failing health and replay tests**

Add tests proving:

- inferred continuity count/minutes are observable but not Live blockers;
- failed repair jobs are urgent blockers and Inbox failures;
- rejected source-changed jobs are counted separately but do not block Live;
- ordinary ambiguous missing-location minutes continue to block Live;
- pre-lunch Maintenance with no work center restores the same Maintenance department ID/name after lunch;
- pre-lunch Forklift continues to restore its work center and department exactly as before;
- replay emits aggregate JSON by default and detailed attendance IDs only with `--details`;
- replay exits `0` when the only inferred record is the expected allow-listed Jose record, `1` for an unrelated inferred fallback, and `2` for source/config failure.

- [ ] **Step 2: Run the tests and confirm missing metrics/script**

Run:

```bash
pytest -q tests/test_attendance_readiness.py tests/test_attendance_exceptions.py tests/test_auto_lunch_worker.py tests/test_check_maintenance_continuity_script.py
```

Expected: FAIL.

- [ ] **Step 3: Extend readiness with separate continuity health**

Add fields to the readiness input/output dataclasses and Settings context:

```python
inferred_maintenance_intervals: int
inferred_maintenance_minutes: float
maintenance_repairs_completed: int
maintenance_repairs_rejected: int
maintenance_repairs_failed: int
```

Only `maintenance_repairs_failed > 0` adds the blocker `maintenance_continuity_repair_failed`. Inferred interval counts are diagnostics and must not feed `clean=False` after their effective spans have removed missing-location time.

Add a failed-repair issue kind with safe operator text and no employee details in logs beyond the Odoo attendance ID.

- [ ] **Step 4: Add a read-only replay command**

Implement:

```text
python -m scripts.check_maintenance_continuity \
  --start 2026-09-01 --end 2026-09-05 \
  --allow-attendance-id 5434
```

The script must:

- read the local mirror and employee home departments only;
- call the shared classifier, never duplicate its rules;
- report `inferred_count`, `inferred_minutes`, `unexpected_attendance_ids`, and ambiguous missing-location aggregates;
- make no Odoo or local writes;
- hide names by default;
- return the documented exit codes.

- [ ] **Step 5: Run focused health and replay tests**

Run:

```bash
pytest -q tests/test_attendance_readiness.py tests/test_attendance_exceptions.py tests/test_auto_lunch_worker.py tests/test_check_maintenance_continuity_script.py
```

Expected: PASS.

- [ ] **Step 6: Inspect the monitoring/replay checkpoint**

Run:

```bash
git diff --check
git status --short
```

Expected: all Task 1-6 edits remain uncommitted for the atomic release.

---

## Task 7: Validate the complete Plant Manager track and ship the patch note

**Files:**

- Modify: `CHANGELOG.md`
- Verify: all files changed by Tasks 1-6

- [ ] **Step 1: Run the deterministic September replay locally**

Run:

```bash
python -m scripts.check_maintenance_continuity --start 2026-09-01 --end 2026-09-05 --allow-attendance-id 5434 --details
```

Expected:

- inferred attendance IDs: only `5434`;
- inferred duration: `155` seconds;
- no unexpected inferred rows;
- Jose's interval contributes no missing-location minutes after effective projection.

If the local mirror does not contain production data, run the same command through the production one-off command facility after deployment and retain its aggregate output in the task handoff. Do not loosen the allow-list to make a failing replay pass.

- [ ] **Step 2: Run the complete attendance and Inbox regression suite**

Run:

```bash
pytest -q tests/test_maintenance_continuity.py tests/test_maintenance_continuity_repair.py tests/test_attendance_timeline.py tests/test_attendance_exceptions.py tests/test_attendance_readiness.py tests/test_attendance_location_end_to_end.py tests/test_attendance_location_failure_modes.py tests/test_attendance_location_policy.py tests/test_attendance_department_repair.py tests/test_attendance_correction_jobs.py tests/test_attendance_correction_recovery.py tests/test_auto_lunch_worker.py tests/test_exception_inbox_attendance.py tests/test_exception_inbox_attendance_template.py tests/test_exception_inbox_attendance_js.py tests/test_exceptions_attendance_routes.py tests/test_inbox_reconcile.py tests/test_check_maintenance_continuity_script.py
```

Expected: PASS.

- [ ] **Step 3: Run the full repository verification**

Run:

```bash
pytest -q
ruff check src tests scripts
git diff --check
```

Expected: all tests pass, Ruff reports no errors, and `git diff --check` prints nothing.

- [ ] **Step 4: Add the child-readable What's New note**

Under `## 2026-09-04` in `CHANGELOG.md`, add:

```markdown
- Time between Maintenance jobs now stays in Maintenance. It no longer looks like missing plant work. Managers can also fix an old row with one safe click.
```

- [ ] **Step 5: Commit and push the complete verified implementation**

Run:

```bash
git add CHANGELOG.md scripts/check_maintenance_continuity.py src/zira_dashboard tests
git diff --cached --check
git commit -m "feat: keep between-job time in maintenance"
git push origin main
```

Expected: one implementation commit contains every Task 1-7 code, test, and release-note change; the existing unrelated untracked files are absent from the commit.

- [ ] **Step 6: Verify production deployment without changing Live mode**

Use the existing Railway deployment/status workflow to wait for the pushed `main` build. Verify:

- health endpoint succeeds;
- deployed commit matches local `main`;
- September replay finds only attendance `5434`;
- Jose's Inbox row is `Maintenance carry-forward`, not `Odoo Location Missing`;
- readiness missing minutes exclude the 155-second interval;
- production mode remains whatever it was before this deployment unless a separate, explicit cutover request is executed.

- [ ] **Step 7: Exercise one current Maintenance boundary end to end**

After the GPI Maintenance source owner supplies a current finish/start cycle, verify in order:

1. separate work-order rows remain separate in Odoo;
2. between-job time has Maintenance and no work center;
3. the local mirror matches Odoo;
4. Plant Manager reports no missing location;
5. the Inbox has no false error;
6. readiness remains clean for that interval.

If the source app still creates a home-department fallback, Plant Manager should safely infer it and show the repair suggestion; record that as a source-track regression, not a Plant Manager failure.

---

## External Source-Track Handoff (Not Implemented in This Repository)

The GPI Maintenance owner must implement the approved source contract in its own repository:

```text
finish work order
  -> close distinct work-order attendance
  -> idempotently open Maintenance/no-work-center holding attendance
  -> persist reason = maintenance:between_jobs
  -> persist unique source event ID
```

Starting a Maintenance work order, receiving another app's explicit assignment, or clocking out closes the holding row. The source implementation must include durable retry after partial failure and must never restore the employee home department merely because a work order ended.

Before Plant Manager relies on explicit source metadata, the Maintenance repository and Plant Manager mirror must agree on the exact Odoo field or audit-model names for source, reason, and event ID. Until then, Plant Manager's conservative closed-chain inference remains the safety net, and properly created Maintenance holding rows already project correctly because Maintenance does not require a work center.
