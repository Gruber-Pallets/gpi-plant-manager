# Odoo Attendance as Live Work-Location Truth

**Date:** 2026-08-28
**Status:** Approved through brainstorming; ready for implementation planning

## Goal

Make Plant Manager reliably show where each person is working right now and credit production to the right people by treating Odoo work-center attendance as the source of truth for work location.

Plant Manager will continue to own each employee's clock-in and clock-out for the day. Luke's plant-floor ERP app will own the first work-center assignment and routine transfers during that day. Both systems will communicate through Odoo attendance records.

## Approved Product Decisions

1. After the feature cutover, only valid Odoo work-center attendance can credit a person with production. Schedules and manual staffing assignments will not be fallback evidence.
2. Plant Manager continues to clock people in and out for the day. It does not create routine work-center transfers.
3. Luke's floor app sets the first specific work center and every routine transfer throughout the day.
4. Plant Manager reads those Odoo records and keeps its live staffing view and production attribution synchronized with them.
5. A manager may correct an unassigned production run by choosing one or more people and an exact start and end time, or an open-ended start if the work is still in progress.
6. A correction writes the exact result to Odoo, verifies it by reading Odoo again, and only then resolves the Plant Manager exception.
7. If a correction overlaps an existing Odoo location, Plant Manager preserves the unaffected portions by splitting the old interval into before/new/after pieces.
8. If the chosen person has no Odoo attendance record, Plant Manager may create the exact interval after showing a preview.
9. When multiple valid people overlap at one work center, production in that overlap is divided equally among them.
10. Departments have a configurable `requires_work_center` rule. Maintenance and Supervisor start as exempt; production departments require a work center.
11. If Odoo's department and mapped work center conflict, the mapped work center wins. Plant Manager repairs the Odoo department and verifies the repair.
12. An unknown Odoo work center is shown immediately by its Odoo name, but its production remains unassigned until the work center is explicitly mapped.
13. If a person has two different Odoo work centers at the same time, the conflicting overlap credits neither work center and creates an urgent exception. Valid time on either side remains usable.
14. Plant Manager aims to reflect Odoo changes within 30 seconds.
15. Historical Odoo edits and deletions trigger targeted recalculation, even when the changed attendance is old.
16. Strict attribution applies only from the feature cutover forward. Existing historical production remains unchanged unless an old attendance record is later edited; that edited day is then recalculated under the strict rules.
17. The staffing screen keeps the planned assignment visible and overlays the person's live Odoo location. A planned seat shows when its person has moved elsewhere.
18. After Plant Manager clocks someone in, Luke has a five-minute grace period to provide the first work center. Production in that gap is still unassigned immediately; the grace only controls when the missing-location warning becomes urgent.
19. One exception represents one distinct unassigned production run, not every meter reading or every polling cycle.

## Current State and Gaps

The application already has several useful pieces:

- Odoo attendance windows can be read for a day, including open attendance.
- Open Odoo attendance is refreshed on a roughly 30-second live cadence.
- Production samples can already be divided equally across multiple active workers.
- Plant Manager work centers can be explicitly mapped to Odoo `mrp.workcenter` records.
- The existing production history can segment active production runs.

The current behavior is still hybrid. When Odoo attendance is missing, schedules or local manual attribution can sometimes identify a worker. Local staffing transfers can also write Odoo. That creates two possible location authorities and can hide missing or incorrect Odoo records.

Machine-breakdown logic also considers whether a worker is present now while measuring no-output time from the station's last output. A worker who just arrived can therefore appear responsible for downtime that began before arrival.

## Ownership Model

### Plant Manager owns the workday boundary

Plant Manager creates and closes the employee's day-level clock attendance. Its timeclock remains the source for whether the employee is on the clock.

Clock-in does not guess a work center. Clock-out closes any still-open work-center attendance according to the Odoo integration contract, so no location remains open beyond the workday.

### Luke's floor app owns routine location changes

Luke's app supplies the first work center and each transfer between work centers. These records are normal operations, not exceptions.

Plant Manager's ordinary staffing controls must not compete with that ownership. Existing transfer actions should either become read-only live-location displays or be moved behind the manager correction flow.

### Plant Manager owns reconciliation and exceptions

Plant Manager mirrors Odoo, validates the timeline, attributes production, raises precise exceptions, and provides audited corrections when Odoo does not describe reality.

## Architecture

Use a canonical local mirror of Odoo attendance rather than directly querying Odoo independently in every page and calculation.

### 1. Attendance mirror synchronizer

The synchronizer imports the Odoo records and fields needed to reconstruct employee location over time. It stores source identifiers and source version information so changes are idempotent and auditable.

### 2. Timeline projector and validator

The projector turns raw mirrored rows into non-overlapping location spans for each person. It maps Odoo work centers to Plant Manager work centers, applies department rules, identifies conflicts, and exposes one shared interpretation for live staffing and historical production.

### 3. Production matcher

The matcher intersects station production samples with valid projected spans. It is the only post-cutover path that assigns production to real people.

### 4. Exception builder

The builder groups invalid or uncovered time into stable, deduplicated exceptions. Production exceptions follow distinct production-run boundaries so one continuous issue produces one actionable item.

### 5. Correction orchestrator

The orchestrator previews, writes, re-reads, verifies, and audits manager corrections. It is designed as a durable multi-step workflow because separate Odoo API calls cannot be one database transaction from Plant Manager.

### 6. Recalculation worker

The worker recalculates only the affected people, work centers, production runs, and dates after an Odoo change. Repeated requests for the same scope coalesce.

### 7. Live staffing presenter

The staffing screen combines the plan with current projected Odoo location without changing the ownership of either data source.

## Data Model

Names may change during implementation, but the model needs these concepts.

### `odoo_attendance_mirror`

Stores each source row with:

- Odoo attendance ID
- employee ID
- clock-in and clock-out
- Odoo work-center ID and display name, when present
- department ID, when present
- Odoo `write_date` or equivalent source version
- first-seen, last-seen, and deleted-at timestamps
- raw source identifiers needed for correction and audit

Open rows have no clock-out and are refreshed every live sync.

### `odoo_attendance_sync_state`

Tracks:

- the incremental cursor
- last successful incremental sync
- last successful complete sweep
- current sweep generation
- current health and error state

### `attendance_location_spans`

Stores or materializes the canonical interpretation for a person and time range:

- person
- start and end, with an open end allowed
- Odoo work-center identity and mapped Plant Manager work center
- department
- validation status
- contributing Odoo record IDs and versions
- projection version

### Department policy

Each department has a configurable `requires_work_center` value. Maintenance and Supervisor are initially false. Production departments are true.

### Correction and recalculation state

Durable records track:

- requested correction and manager
- before/after preview
- expected source versions
- each completed Odoo write phase
- verification result
- affected recalculation scope
- final exception resolution

The system also stores the strict-attribution cutover timestamp.

## Odoo Synchronization

### Incremental polling

Poll Odoo every 30 seconds using `write_date` plus a small overlap window. Upsert by Odoo ID and source version so repeated results are harmless. Refresh every known open row on every cycle, even if its `write_date` has not moved.

### Deletion detection

An incremental `write_date` query cannot reveal deleted rows. Run a paginated full-ID sweep at least hourly. Mark a mirrored row deleted only after a complete successful sweep proves its ID is absent. An interrupted sweep must never mark deletions.

### Freshness

All consumers use the same mirror freshness state. A successful incremental cycle advances the live freshness timestamp. A failed or partial cycle does not.

Historical additions, edits, closures, and confirmed deletions enqueue recalculation for the affected employee and dates.

## Timeline Projection and Validation

### Atomic spans

For each person, collect every start and end boundary and evaluate the records active between adjacent boundaries. This preserves valid shoulders around a smaller invalid overlap.

### Work-center mapping

Use explicit reverse mapping from Odoo work-center ID to Plant Manager work center. Never fuzzy-match on names.

### Span statuses

A projected span is one of:

- `valid`: one mapped work center and no conflict
- `pending_first_location`: on the clock, requires a work center, and still inside the five-minute grace period
- `exempt_no_location`: department does not require a work center
- `missing_required_location`: on the clock and past grace without a work center
- `unmapped_location`: Odoo work center exists but has no explicit mapping
- `conflicting_location`: two or more different work centers overlap

The exact identifiers are implementation details, but consumers need these distinct meanings.

### Duplicate and conflicting rows

Two records for the same person and same work center over the same span collapse to one location for production credit. They create a lower-priority data-quality warning rather than double credit.

Records for different work centers over the same span are invalid. The overlap earns no production credit at either work center and raises an urgent exception. Non-overlapping time on either side remains valid.

### Department conflict repair

When a mapped work center implies a different department than the mirrored Odoo department:

1. Publish the work-center location as the effective location.
2. Create or update a repair job with the observed Odoo version.
3. Re-read the source before writing.
4. Update the department only if the expected version still applies.
5. Re-read and verify.
6. Refresh the mirror and clear the repair exception.

This repair happens outside the projection transaction so an Odoo failure cannot corrupt the local timeline.

## Production Matching

### Strict post-cutover rule

For production at time `t` and work center `w`, eligible workers are exactly the people whose projected span at `t` is `valid` and maps to `w`.

After cutover, schedules, planned staffing, locally selected real people, and stale transfer state are not eligible fallbacks. Testing-mode offsets and the existing expected-minute exclusions for known breakdown periods remain supported because they describe production timing, not worker identity.

### Allocation and conservation

For each production sample interval:

- no eligible workers: all production is unassigned
- one eligible worker: that worker receives all production
- `n` eligible workers: each receives `1/n`

Assigned shares plus the unassigned share must always equal the sample total within the system's rounding tolerance.

### Exception grouping

Use the application's established active-production-run segmentation. Adjacent uncovered samples in the same run produce one exception. A real run stop ends the exception. A later restart creates a new exception.

### Breakdown worker timing

When evaluating whether an assigned worker may be affected by a stopped station, measure that worker's no-output time from the later of:

- the station's last output, or
- the worker's arrival at that work center

This prevents a newly transferred worker from inheriting earlier downtime.

## Exception Inbox

The inbox needs actionable, non-duplicating categories:

- unassigned production run
- required work center missing
- unknown work-center mapping
- conflicting work-center overlap
- failed department repair
- stale Odoo source

An unassigned-production exception shows the work center, run time, production amount, nearby attendance, and the reason matching failed. It offers the correction flow below.

An unknown work center shows the exact Odoo name immediately. It offers mapping, but no meter credit occurs until mapping is explicit and the affected time is recalculated.

The five-minute first-location grace changes urgency, not attribution. Unassigned production is visible immediately. The missing-location exception becomes urgent after the grace expires.

## Verified Odoo Correction Workflow

### Manager input

From an unassigned production run, the manager can select:

- one or more people
- exact start time
- exact end time, or "still working here"

The default range is the production run, but the manager must be able to adjust it.

### Preview

Before any write, Plant Manager re-reads each person's current Odoo attendance and builds a before/after timeline. The preview shows rows to create, shorten, split, or close. It preserves lunch and other gaps unless the manager explicitly includes them.

If the source changed after the preview was built, the write stops and requires a refreshed preview.

### Interval surgery

For a closed correction interval:

- preserve the old segment before the correction
- write the selected work center for the correction
- preserve the old segment after the correction

For an open correction:

- preserve or close the old location at the new start
- create or update the selected work center from the new start with no end

If no attendance exists, create the exact requested interval.

### Durable execution

Persist the intended result and each completed write before advancing. If an Odoo call times out, re-read Odoo before retrying because the write may already have succeeded. Adopt matching source state instead of duplicating it.

Continue until Odoo matches the intended non-overlapping timeline or the job reaches a recoverable failed state.

### Completion

A correction is complete only after Plant Manager:

1. re-reads Odoo
2. verifies the exact intended timeline
3. refreshes the local mirror and projection
4. recalculates affected production
5. verifies that attribution and remaining exceptions match the result
6. records the audit details
7. resolves the original exception

## Staffing Presentation

Keep planned staffing and actual live location visually distinct.

- The planned seat remains visible.
- A strong live badge shows the current Odoo work center.
- If the person moved, the planned seat indicates that the person is working elsewhere.
- An unknown Odoo work center is displayed by its Odoo name with an unmapped warning.
- Missing, conflicting, or stale state is shown as uncertain rather than pretending the plan is live truth.

The same projected span must power the staffing page, production history, and exception inbox so those screens cannot disagree.

## Historical Recalculation and Cutover

Before cutover, import enough Odoo history to build and validate the mirror, but do not rewrite existing production attribution.

At the cutover timestamp:

- new production uses strict Odoo attribution
- old production remains as recorded
- an Odoo edit or deletion for an old day causes that affected day to be recalculated under the strict rules

Targeted recalculation uses the source change range plus any adjacent open or overlapping records needed to rebuild a correct timeline. It must be idempotent and safe to retry.

## Failure Policy

### Odoo unavailable or stale

Keep showing the last verified Odoo location with a clear stale indicator. Do not silently fall back to the schedule. Disable correction writes while a fresh source re-read cannot be obtained. Production based on already-known intervals may remain visible, but open intervals must be marked as uncertain once freshness exceeds the configured threshold.

### Production source unavailable

Keep verified attendance and previously calculated results. Queue recalculation until production data is available again.

### Partial correction

Show the correction as in progress or recoverable failure. Do not resolve the exception. The durable orchestrator resumes from verified source state.

### Missing mapping

Show the Odoo-only location, create a mapping exception, and withhold production credit. Never guess a mapping.

## Security and Audit

Only authorized managers may correct Odoo attendance, map work centers, or override department configuration.

Every correction records:

- manager identity and timestamp
- originating exception
- selected people and work center
- requested interval
- before and verified-after Odoo state
- source record IDs and versions
- production recalculation result
- failures and retries

Routine mirror reads and projection do not change Odoo. Department repairs and manager corrections are the only Plant Manager flows that alter location details, aside from timeclock-owned clock-in and clock-out boundaries.

## Observability

Track and alert on:

- age of the last successful Odoo sync
- open records not refreshed
- full-sweep completion and deletion count
- mirror-to-projection lag
- invalid overlap duration
- missing and unmapped location duration
- unassigned production amount and age
- correction success, retry, and verification failure rates
- recalculation queue depth and age
- department repair failures

Logs should carry employee, Odoo record, work-center, exception, correction, and recalculation identifiers without exposing more personal data than needed.

## Rollout

1. Add the mirror, sync state, department rule, correction state, and cutover configuration behind feature flags.
2. Import attendance history and run live synchronization without changing attribution.
3. Run the new timeline projector in shadow mode and compare it with current Odoo window logic.
4. Run the strict matcher in shadow mode and measure changes in credited and unassigned production.
5. Meet readiness gates for mapping coverage, sync freshness, overlap rate, and unexplained unassigned production.
6. Release the plan-plus-live staffing display and new exceptions while legacy attribution remains active.
7. Set the cutover timestamp and enable strict attribution for new production.
8. Monitor closely and keep a controlled rollback switch that changes the matcher for new data without deleting the mirror or audit records.
9. Remove legacy routine transfer ownership only after Luke's first-location and transfer feed has proven reliable in production.

## Test Plan

### Unit tests

- atomic span construction
- open and closed intervals
- same-work-center duplicate collapse
- different-work-center conflict isolation
- five-minute grace states
- exempt and required departments
- explicit mapping and unmapped behavior
- equal production split and conservation
- distinct production-run grouping
- interval surgery for create, shorten, split, close, and open-ended corrections
- cutover and old-day edit rules

### Integration tests

- incremental sync overlap and idempotency
- open-row refresh
- completed versus interrupted deletion sweeps
- source version conflicts
- department repair with reread verification
- correction timeout followed by source adoption
- mirror change to targeted recalculation
- live staffing, production history, and exception inbox reading the same projection

### Odoo contract tests

Use a mocked Odoo facade for search, read, create, write, delete visibility, `write_date`, pagination, open records, and ambiguous timeout outcomes. Confirm that retry behavior never creates duplicate intervals.

### End-to-end scenarios

- clock in, receive first work center from Luke, transfer, and clock out
- first work center arrives during and after grace
- worker arrives after a station has already stopped
- two people share production at one work center
- one person accidentally overlaps two work centers
- manager repairs an unassigned closed run
- manager assigns a person who had no attendance
- manager opens a still-working interval
- a historical Odoo row is edited or deleted
- Odoo becomes stale and later recovers

## Acceptance Criteria

The design is implemented successfully when:

- Plant Manager timeclock still controls the day's clock-in and clock-out.
- Luke's Odoo updates appear as live Plant Manager work-center locations within 30 seconds under normal conditions.
- Post-cutover production is credited only through valid mapped Odoo location spans.
- Shared-worker production divides equally and conserves total production.
- Conflicting overlaps credit neither location during the conflict.
- Exempt departments do not receive missing-work-center warnings.
- Unknown work centers are visible but receive no production credit until mapped.
- Managers can preview and apply exact Odoo-backed corrections, including people with no prior attendance.
- Exceptions resolve only after verified Odoo state and completed recalculation.
- Historical source edits trigger targeted, idempotent recalculation.
- Staffing, production, and exceptions agree on the same live timeline.
- Newly arrived workers do not inherit downtime from before their arrival.

## Out of Scope

- Replacing Plant Manager's timeclock
- Making Plant Manager the normal transfer tool
- Guessing work centers from schedules, production, proximity, or names
- Automatically deciding which worker produced an unassigned run without manager input
- Rewriting all historical production at cutover
- Changing Luke's floor application beyond honoring the shared Odoo attendance contract

## Likely Affected Areas

Implementation planning should trace at least:

- Odoo attendance ingestion and caching
- staffing assignment and transfer services
- live staffing cache and UI
- attendance-window and assignment-window resolution
- production history attribution and recalculation
- exception generation and resolution
- machine-breakdown worker timing
- work-center mapping administration
- department configuration
- timeclock clock-in and clock-out integration
- audit, permissions, health checks, and operational dashboards
