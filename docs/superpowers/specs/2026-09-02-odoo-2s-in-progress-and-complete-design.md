# Odoo 2s In-Progress and Complete tracking

**Date:** 2026-09-02
**Status:** Approved design, pending implementation plan

## Summary

When Dale pastes a 2s Improvement coding request into Codex, Claude, or Cursor,
the matching **2s Improvement Reference Data** row must move to **In-Progress**
as soon as that paste is recognized, including planning. After the scoped work
is finished, checked, and pushed to `origin/main`, the same row must move to
**Completed**, with Dale Gruber as **Completed By**.

This extends `docs/superpowers/specs/2026-08-31-cross-agent-odoo-completion-design.md`.
That spec remains the completion gate. This spec adds the missing start-work
status, puts the job in the copied prompt so every AI sees it, and gives each
app a mechanical start/finish path. It **supersedes** the 2026-08-31 sentence
that forbids any Odoo status change until implementation is fully delivered.
In-Progress now happens at start. Completed still waits for delivery.

The copied prompt names **what** and **when**. Each repository's `AGENTS.md`
names **how**. App-owned rows still update through that app's local feedback
lifecycle. The existing 2s mirror writes Odoo. Agents do not guess that the
paste's Task ID is the 2s record number.

## Goals

- Make In-Progress and Completed happen for every pasted 2s coding request in
  Sales Manager, Plant Manager, and OS Manager.
- Fire In-Progress as soon as the paste is recognized, including planning.
- Keep Completed behind full delivery, validation, and a push to `origin/main`.
- Put the status job in the Sales Manager copy-for-coding text so Codex, Claude,
  and Cursor all see it without relying on `AGENTS.md` alone.
- Keep one exact-match, fail-closed write path per app. Never create or guess
  a 2s row.
- Record **Completed By** as Dale Gruber, matched by work email
  `dale@gruberpallets.com`, when Dale's AI sessions close the work.

## Non-goals

- Auto-marking In-Progress when Dale clicks Copy, including copies into Slack,
  notes, or the wrong repo.
- Housecraft or any app that does not use the Sales Manager coding-copy format.
- New Odoo Studio fields, a background service, or a new Odoo write API.
- Credentials, API keys, or private URLs in git.
- Changing Plant Manager, Sales Manager, or OS Manager 2s mirror gates,
  rollout, or ownership rules.
- Backfilling older tasks that were worked without this workflow.
- Reopening Completed or Declined rows.
- Direct Odoo edits of app-owned rows when the local lifecycle is unavailable.

## Chosen approach

Put a short **ODOO 2s STATUS** block in the Sales Manager copy-for-coding
payload, and keep each app's existing local start/finish workflow as the only
write path.

This is preferred over rules-only `AGENTS.md` edits because those already exist
for completion and are easy to skip when the paste is long. It is preferred
over marking In-Progress on Copy because a copy is not proof that work started
in the correct repository.

Direct Odoo status writes for app-owned rows are forbidden. Local status is
source of truth. The existing mirror sets `x_studio_status` to `In-Progress`
then `Completed`.

## Copied prompt

`gpi-sales-manager` `src/lib/tasks/coding-copy.ts` keeps the existing target
and safety header. Immediately after `Task ID`, both the plain-text and HTML
payloads add:

```text
ODOO 2s STATUS:
This Task ID is the 2s Improvement Reference Data row.
1. Before any other work, including planning, set Status to In-Progress using this repository's existing start-work workflow. Do not edit the Odoo row directly when this app owns it.
2. After the work is finished, checked, and pushed to origin/main, set Status to Completed using this repository's existing finish-work workflow.
If TARGET REPOSITORY does not match this repository, stop. Do not change files or Odoo.
If there is no single match, access fails, or the row is already Completed or Declined, leave it unchanged and tell Dale.
```

The prompt does not add credentials, URLs, or extra IDs. The paste already
carries `TARGET REPOSITORY`, `Task ID`, and, for Plant Manager, `GPI-PM-FB-<id>`
plus `Feedback ID`.

The Task ID in the paste is the work-item key, usually the Odoo project task
id used by Sales Manager and OS Manager `process-feedback --task-id`. It is
**not** the `x_2s_improvements` record id. Each repo maps the paste to the
local feedback row, and the mirror updates the 2s table.

## Per-app start and finish

Agents run these steps only after `TARGET REPOSITORY` matches the current
repository. Wrong-repo pastes stop with no file changes and no status changes.

### Sales Manager (`gpi-sales-manager`)

Keep the existing scripts. `AGENTS.md` must say they fire as soon as the paste
is recognized, including planning.

- Start: `npm run process-feedback -- --task-id <Task ID> --yes`
- Finish: `npm run resolve-feedback -- --task-id <Task ID> --note "<what changed>" --yes`
- Closer: keep the existing default `--by dale@gruberpallets.com`. Agents must
  not omit the closer or point it at a service user or AI name.

### OS Manager (`gpi-os-manager`)

Same timing in `AGENTS.md`. Prefer `FB-<id>` when the paste includes it,
otherwise `--task-id <Task ID>`.

- Start: `npm run process-feedback -- --feedback "FB-<id>" --yes`
- Finish: `npm run resolve-feedback -- --feedback "FB-<id>" --note "<what changed>" --yes`
- Closer: default `resolve-feedback --by` to `dale@gruberpallets.com`. `--by`
  remains an override when someone else is actually closing the row.

### Plant Manager (`gpi-plant-manager`)

Plant Manager today can complete through `/admin/feedback`, and `AGENTS.md`
forbids any Odoo update until the work is fully delivered. That start gap is
why pasted Plant Manager requests stay **Requested**.

Match **only** from the paste's `GPI-PM-FB-<positive id>` or `Feedback ID:
<positive id>`. Do not use Task ID as the Plant Manager feedback id.

Add `scripts/process_feedback.py` and `scripts/resolve_feedback.py` that call
the existing local feedback lifecycle (`feedback_store.transition`), the same
path as `/admin/feedback`. They must not write Odoo directly.

- Start: `.venv/bin/python -m scripts.process_feedback --feedback-id 43 --yes`
  moves `requested` → `in_progress` immediately, including planning. Also
  accept `--source-id GPI-PM-FB-43`.
- Finish: `.venv/bin/python -m scripts.resolve_feedback --feedback-id 43 --note
  "<what changed>" --yes` moves `in_progress` → `completed` with closer
  `dale@gruberpallets.com`. Also accept `--source-id`. `--by` may override the
  closer.
- Default to dry-run unless `--yes` is passed, matching Sales and OS.
- The existing mirror remains the only Odoo writer for Source `GPI Plant
  Manager` and Source ID `GPI-PM-FB-<id>`.

`AGENTS.md` must replace the "do not update Odoo until complete" rule with:
mark In-Progress at start through the local start-work command; mark Completed
only after delivery through the local finish-work command; never edit the Odoo
mirror directly.

`CLAUDE.md` continues to import `AGENTS.md`. No `.cursor/rules` copy.

## Completed By identity

Odoo **Completed By** is `x_studio_completed_by`, a many2one on `hr.employee`,
filled by matching the local closer email to `work_email`.

When Dale closes work from Cursor, Claude, or Codex, the closer is always
**Dale Gruber** via `dale@gruberpallets.com`. Never use the Odoo API user, a
Cursor/Claude/Codex name, or a blank closer. If that email does not match
exactly one employee, omit the relation, leave local `finished_by` as that
email, and tell Dale the Odoo person field could not be filled.

In-Progress has no person field. This identity applies only to Completed.

## Failure and skip rules

- **Wrong repo:** stop. No files, no In-Progress, no Completed.
- **No single match:** missing identifiers, or more than one plausible row.
  Leave status unchanged and tell Dale.
- **Already Completed or Declined:** leave it. Do not reopen to In-Progress.
- **Already In-Progress:** start-work is a successful no-op. Continue the task.
- **Access or sync fails:** credentials, database, CLI, admin, or mirror
  readback fails. Leave the row as-is where possible, keep the task active or
  partial, and report the blocker without secrets.
- **Plan-only:** still mark In-Progress. Do not mark Completed.
- **Incomplete implementation, failed checks, or unpushed work:** do not mark
  Completed.
- **No new Odoo rows:** never create, delete, archive, merge, or rewrite a 2s
  row to make the status fit.
- **Plant Manager-owned rows:** if local start/finish cannot run, do not edit
  the Odoo mirror directly.

After start-work, the agent should say whether local status is `in_progress`
and whether Odoo readback is `In-Progress`. After finish-work, the same for
`completed` / `Completed`. Do not claim Odoo is current when readback failed
or the mirror has not caught up.

## Files

Implementation is split by repository. This spec lives in Plant Manager
because the design started here. Sales Manager and OS Manager edits happen in
those codebases, not from a Plant Manager session that sees a different
`TARGET REPOSITORY`.

| Repository | File | Responsibility |
|---|---|---|
| `gpi-sales-manager` | `src/lib/tasks/coding-copy.ts` | Add the ODOO 2s STATUS block to text and HTML copy. |
| `gpi-sales-manager` | `src/lib/tasks/coding-copy.test.ts` | Prove the block is present after Task ID. |
| `gpi-sales-manager` | `AGENTS.md` | Fire process/resolve on paste, including planning; keep Dale as closer. |
| `gpi-os-manager` | `scripts/resolve-feedback.ts` | Default `--by` to `dale@gruberpallets.com`. |
| `gpi-os-manager` | `AGENTS.md` | Same start/finish timing; document the default closer. |
| `gpi-plant-manager` | `AGENTS.md` | In-Progress at start, Completed after push, local-only writes, Dale as closer. |
| `gpi-plant-manager` | `scripts/process_feedback.py`, `scripts/resolve_feedback.py` | Local `requested` → `in_progress` and `in_progress` → `completed`. |
| `gpi-plant-manager` | tests for those scripts | Exact-id matching, dry-run, `--yes`, already-in-progress no-op, terminal skip, default closer. |
| `gpi-plant-manager` | `CHANGELOG.md` | Child-readable note for the Plant Manager-visible helper behavior. |

Sales Manager and OS Manager follow their own What's New / release-note rules
when those repos change.

## Validation

- Sales Manager copy text and HTML include the approved status block immediately
  after Task ID, with no credentials or extra identifiers.
- Plant Manager, Sales Manager, and OS Manager `AGENTS.md` files agree on
  timing: In-Progress at paste/planning, Completed after push.
- Plant Manager start/finish commands match only `GPI-PM-FB-<id>` or Feedback
  ID, call `feedback_store.transition`, and do not import a generic Odoo write
  helper for this workflow.
- OS Manager `resolve-feedback` defaults closer to `dale@gruberpallets.com`.
- Wrong-repo, no-match, already-complete, and already-in-progress behaviors
  are covered by tests or static instruction checks as appropriate to each
  repo.
- No credentials or private Odoo URLs are added to any repository.
- The 2026-08-31 completion gate remains: a plan is not Completed.

## Relationship to existing completion tracking

Keep the 2026-08-31 exact-match, Plant-Manager-first, no-create, verify-readback
rules. Change only the start timing and the closer identity for Dale's AI
sessions. Completed still requires scoped implementation, required validation,
and a push to `origin/main`.
