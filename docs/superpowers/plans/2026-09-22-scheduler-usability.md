# Scheduler usability implementation plan

> Execute the approved items 2, 4, 6, and 7 in this session. A pushed plan is intended work, not completion.

**Goal:** Make warnings actionable, distinguish saved drafts from posted schedules, provide a compact phone layout, and improve measured scheduling responsiveness without changing scheduling rules.

**Architecture:** Keep the existing Jinja table, form inputs, endpoints, and assignment logic authoritative. Add presentation helpers for warnings and responsive layout. Extend the existing autosave state machine. Measure the real local app against synthetic Postgres data before optimizing repeated work.

**Constraints:** Preserve manual/default assignments, training reservations, time off, optional workday rules, print output, posted read-only behavior, and keyboard controls. No new dependencies. Do not implement schedule-goal relabeling, training filtering, or rebuild previews (unselected items 1, 3, 5). Existing known inbox/cache CI failures must be distinguished from regressions.

## 1. Baseline and measurements

- [ ] Run `DATABASE_URL='' python -m pytest tests/test_staffing_static.py tests/test_staffing_rotations.py tests/test_staffing_delivery.py -q` with the existing project venv.
- [ ] Use the local QA server and synthetic Postgres database with external calls blocked. Capture browser day-load, assignment-picker, and rebuild durations over repeated trials, plus request counts.
- [ ] Record baseline and comparable final measurements in `docs/qa/2026-09-22-scheduler-usability.md`; do not promise a wall-clock improvement unsupported by measurements.

## 2. Clickable warnings

Files: new `static/staffing-warnings.js`, existing `static/staffing.js`, a separate CSS file if needed, and focused warning tests.

- [ ] Add tests for grouping duplicate issues without losing distinct reasons, literal rendering of unsafe-looking messages, multi-center warnings, and missing targets.
- [ ] Implement `window.SchedulerWarnings.render(list, warnings, issues)` using textContent and real button elements. Group matching issue identity/message/centers; retain rejection details. Render one action per known affected center using `issue.centers` rather than guessing names from text.
- [ ] Clicking a center scrolls its row into view and focuses/opens its existing picker. Off or read-only centers get focus without enabling or editing anything. Plain/general warnings remain visible. Person-only issues point to the existing unscheduled list where possible.
- [ ] Route initial and refreshed warnings through the same renderer. Avoid moving focus during background validation.

## 3. Save and publication status

Files: existing autosave controller in `static/staffing.js`, new template partial/CSS for its persistent status, focused tests.

- [ ] Test pending, saving, successful, failed, retry, repeated edits during a save, and explicit Posted states.
- [ ] Display persistent text for `Saved draft`, `Changes not saved`, `Saving…`, and `Save failed — Retry`; keep an accessible live region and a type=button retry action.
- [ ] Use server `published`/`has_snapshot` state plus local edits to distinguish a saved draft from the posted schedule. Never imply an autosave publishes. Clear error only after successful persistence. Preserve autosave flush/error propagation and revision handling.
- [ ] Prevent presentation-only controls (layout selection) from triggering saves. Ensure a queued save is awaited before navigation/publishing.

## 4. Compact phone layout

Files: new `static/staffing-layout.js` and CSS, small additions to `templates/staffing.html`, focused responsive tests.

- [ ] Add Cards/Table choice visible on narrow screens. Default to cards on phones and remember an explicit preference safely when storage is unavailable.
- [ ] Style the same table rows as cards under a screen-only media query; never clone named inputs. Include center, department, bay, assignment control, notes, and existing off/on state. Preserve all bay metadata and forklift information.
- [ ] Keep table mode available, preserve desktop layout, and isolate print styles from card layout. Preserve posted read-only behavior and keyboard picker use.
- [ ] Verify 375, 768, and 1440 widths, assignment persistence after toggling layouts, and print output.

## 5. Measured performance and integration

- [ ] Use baseline evidence to select a bounded optimization. Prefer eliminating repeated validation requests or repeated DOM scans while retaining fresh validation after meaningful edits and external changes.
- [ ] Add regression coverage showing equivalent assignment/validation results and reduced work for the measured case. Avoid speculative caching of authoritative roster or skill data.
- [ ] Run relevant Python/Node tests, browser workflows with save/network failure injection, full tests, lint, and independent review. Fix introduced failures.
- [ ] Add plain-language What's New notes, record results and limits, commit and push implementation to origin/main, verify deployment and read-only live screens. Keep the task active if any scoped work remains incomplete.
