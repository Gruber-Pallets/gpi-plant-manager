# Time-Off Approval Email Implementation Plan

**Goal:** Email employees when current or future time off is fully approved.

**Architecture:** Transactional PostgreSQL outbox, captured by a leave-state
trigger; one-minute background delivery through Odoo's existing mail queue.
**Tech stack:** Python, PostgreSQL, XML-RPC, pytest.

## Constraints

- Follow the adjacent design for feedback GPI-PM-FB-38; no historical mail blast.
- Do not send reasons, leave types, or private notes. No live employee test mail.
- Preserve existing kiosk behavior and unrelated workspace files.
- Commit and push plans and implementation to origin/main as authorized by AGENTS.md.

## 1. Capture approvals durably

Files: `src/zira_dashboard/_time_off_email_schema.py`, `_schema.py`,
`_odoo_time_off.py`, `time_off_sync.py`, `tests/test_time_off_email_store.py`.

- [ ] Write PostgreSQL tests for approval transitions, first/second approval,
  transaction rollback, duplicate updates, old versus fresh imports, and replay.
- [ ] Run tests and confirm failure because the outbox does not exist.
- [ ] Add `approval_source_updated_at` to the leave mirror and request `write_date`
  from Odoo. Pass it on imported inserts. Add a singleton installation timestamp,
  approval snapshot outbox, and trigger. Use one row per request and UUID delivery
  keys. Trigger inserts only final approval, excluding old imports and past leave.
- [ ] Run the store tests against disposable PostgreSQL and existing sync tests.

## 2. Deliver and monitor approval emails

Files: `src/zira_dashboard/time_off_email.py`, `app.py`,
`tests/test_time_off_email.py`, `.env.example`, `README.md`.

- [ ] Write tests for bilingual full/partial-day content, recipient rules,
  cancelled/changed/deleted leave, disabled mode, missing email retry, duplicate
  worker lock, remote timeout recovery, and queued/sent/failure readback.
- [ ] Confirm tests fail before adding the worker.
- [ ] Implement `run_once()` and a one-minute warmer. Read exact employee
  addresses, use Odoo's sender default, and retain mail records. Commit sending
  intent before mail creation and reconcile uncertain sends by Message-ID.
- [ ] Document the recipient policy and kill switch plus attention handling.
- [ ] Run focused tests and review the complete diff.

## 3. Ship and verify completion

- [ ] Add short, plain-language What's New notes.
- [ ] Run `ruff check src tests scripts` and the full pytest suite on disposable
  PostgreSQL. Resolve regressions and report any existing unrelated failures.
- [ ] Commit and push implementation to origin/main; verify CI and Railway health.
- [ ] Verify installed capture trigger, worker configuration, and email access
  without sending fabricated employee mail.
- [ ] Preview and apply `scripts.resolve_feedback --feedback-id 38` with Dale's
  email and a plain-language result. Wait for normal workers. Read improvement
  210 and the task from the durable feedback_task_delivery relation, verifying
  Completed / Done / Done. Keep the task partial if any required check fails.
