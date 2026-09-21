# Training process QA

Status: in progress. A pushed plan is not completion.

## Scope and test environment

Review the Plant Scheduler training sidebar, create/edit forms, lifecycle buttons,
Auto scheduling, attendance/progress, skill completion, Skills Matrix, printed
schedule, posted view, and narrow screens. Test invalid input and failed saves.
Use the real app and a separate local Postgres database with synthetic employees.
Disable external services and background workers; simulate only the external Odoo
skill write. Production sign-in and live Odoo delivery remain separate checks.

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

Further findings and final coverage will be recorded here as testing proceeds.
