# Training process QA

Status: implementation and local validation finished. Authenticated production pages load; a real Odoo completion write remains unverified.

## Scope and test environment

Review the Plant Scheduler training sidebar, create/edit forms, lifecycle buttons,
Auto scheduling, attendance/progress, skill completion, Skills Matrix, printed
schedule, posted view, and narrow screens. Test invalid input and failed saves.
Use the real app and a separate local Postgres database with synthetic employees.
Disable external services and background workers; simulate only the external Odoo
skill write. Authenticated production page checks and live Odoo delivery remain separate checks.

## Plan

1. Reproduce failures in the browser and add focused regression coverage.
2. Fix eligibility to match configured required skills and preserve higher skills
   on completion, following the existing training-skill-gaps design.
3. Check attendance, pause/resume, scheduling conflicts, and completion retries;
   fix verified progress/lifecycle errors.
4. Fix saved-but-unscheduled handling, stale lifecycle responses, and obscured
   controls. Keep the current sidebar workflow and use clear recovery messages.
5. Exercise every training button, keyboard validation, draft/posted views,
   desktop/mobile layouts, and the matrix/print results. Run focused and broader
   tests, review the diff, push implementation to main, and record limits honestly.

## Initial evidence

- Baseline: 324 focused tests passed; one database test skipped in DB-free run.
- Browser: QA Partial has Tablets 1, New Work Stations 1, Recycled Work Stations 0.
  Start training at Tablets rejects with “Trainee must be level 0 for the target
  skill.” The configured three-skill requirement is not used by training creation.
- Browser: fixed Schedule Goal panel overlaps the training create form at the
  default desktop viewport.

## Fixes verified

- Use Settings requirements for each exact work center. Allow partially trained
  people, preserve higher levels, and require existing certificates separately.
- Count recorded attendance once. Keep the trainer on the first actually
  attended day, and extend reservations after absences, conflicts, and pauses.
- Preserve pause history. Prevent edits from transferring earned days to another
  work center or a different set of skills.
- Serialize lifecycle actions. Recover interrupted completion by checking which
  skills remain missing before finalizing. A failed Odoo write leaves a plan open.
- Share a nonblocking person/skill lock with Matrix edits. A concurrent update
  receives a retryable error instead of overwriting a higher level or waiting
  while consuming all database connections.
- Keep a saved card visible after scheduling fails. Retry scheduling reuses that
  plan; the browser test confirms that no second plan is created.
- Refresh the schedule after lifecycle changes. Explain Complete versus End and
  confirm both actions. Show interrupted completion with a retry action.
- Ignore hidden training fields during Publish validation. Enter saves the
  training form rather than submitting the whole schedule.
- Allow the day-one extra trainer without a false capacity error. Later training
  days do not incorrectly require the automatic day-one partner.
- Make explicit Posted views read-only, both current published schedules and
  stored snapshots. Keep controls below training rather than covering it.
- Constrain the narrow-screen grid so the training panel fits a phone. The wide
  scheduling table can still require horizontal scrolling.

## Browser coverage

Chromium against the real local FastAPI app and disposable Postgres database:

- Sidebar load, Start training, Edit, Save, Cancel, Enter, Pause, Resume,
  Complete, End, Retry scheduling, and confirmation cancellation.
- Missing fields, invalid days, self-training, unqualified trainer, and a
  simulated scheduling outage after a successful plan save.
- Reload persistence, Auto scheduling, day-one pair, Training/Normal schedule
  goals, Publish, both Posted states, and print output with the Training label.
- People Matrix shows the newly earned skill. Editing its level and reloading
  persists the change; the synthetic value was restored afterward.
- Training panel at widths 375, 768, and 1440 pixels. The whole panel, not just
  the submit button, is checked against the viewport width.
- No uncaught JavaScript errors in the tested training flows.

20 browser checks passed, including the Matrix and schedule-goal follow-ups.
Screenshots: [phone training](training-process/phone.png) and
[desktop training](training-process/desktop.png).

## Automated validation

- Final integrated suite, including the concurrent Timeclock access change: **6,748 passed, 516 skipped, 1 pre-existing failure**.
- The sole failure is
  `test_exception_inbox_attendance.py::test_every_rendered_section_is_wired_into_the_reconcile_mirror`.
  It fails on the untouched starting checkout too: the quick-punch inbox section
  is absent from the reconciliation mapping. This training change leaves it alone.
- The opt-in real-Postgres tests use one fresh schema per test, reject nonlocal
  database URLs, and cover creation, updates, pause history, completion, concurrent
  actions, certificates, interrupted writes, and Odoo failure/retry.
- Python lint, JavaScript syntax, and whitespace checks pass.

Run the focused suite with `DATABASE_URL=''` and a disposable, loopback-only
`TRAINING_QA_DATABASE_URL` whose database name ends in `_test`:

```sh
python -m pytest tests/test_training_qa_regressions.py tests/test_training_qa_postgres.py tests/test_rotation_store.py tests/test_rotation_training.py tests/test_staffing_rotations.py tests/test_current_schedule_validation.py tests/test_skills_cell_update.py -q
```

## Limits and remaining verification

The headless browser required Microsoft sign-in, but the existing in-app browser
was authenticated. The live scheduler, expandable training form, and People
Matrix loaded successfully. No real employees, schedules, Odoo skills, or
messages were changed during QA. External services and background
workers were disabled locally; the actual shared skill writer and local mirror
ran with only the external Odoo call simulated. A real training completion and Odoo readback remain outstanding because no
real employee training was started or completed solely for QA. This report does
not claim that live write was tested.

## Deployment and remote CI readback

Implementation commit `0ad3250eb00b8d56c8bf7c20fb5075b2b5290dd6` was pushed to
`origin/main`. Railway deployment `f12f2c1b-3afb-4984-8b82-58070dd5f9ba` returned
`SUCCESS`. The authenticated live scheduler then displayed the new training
help text and opened the updated form successfully.

[Remote CI for the implementation](https://github.com/Gruber-Pallets/gpi-plant-manager/actions/runs/35656375496)
finished with **7,230 passed, 33 skipped, 2 failures**. Both failures also appear
in [CI for the preceding commit](https://github.com/Gruber-Pallets/gpi-plant-manager/actions/runs/35652086125):

- The quick-punch inbox reconciliation mapping test described above.
- `test_skills_cache.py::test_skills_save_invalidates_cache`: its assertion checks
  the old unscoped cache key; the preceding access-control implementation now
  scopes keys by user role. This is an existing test assertion mismatch, not a
  training save failure. The real Matrix save/reload browser check passed.

No new CI failures were introduced by the training changes. The temporary QA
server and local database container were stopped after validation.
