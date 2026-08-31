# Cross-agent Odoo completion tracking

**Date:** 2026-08-31
**Status:** Implemented

## Summary

Codex, Claude Code, and Cursor will share one project rule for closing completed
work in Odoo's **2s Improvement Reference Data** table. `AGENTS.md` will be the
canonical source. Codex and Cursor will read it directly, while `CLAUDE.md`
will import it.

The Odoo row is a completion record, not a substitute for finishing the work.
An agent may mark a row **Completed** only after the full requested scope is
implemented, required validation passes, and the implementation commits are
pushed to `origin/main`. The agent must then find exactly one matching existing
row, update it through the authoritative lifecycle path, and read it back in
Odoo. If access, matching, writing, synchronization, or verification fails, the
task remains active or partially complete.

## Goals

- Give Codex, Claude Code, and Cursor the same Odoo completion workflow.
- Keep one canonical rule so the three tools cannot drift apart.
- Update only the existing improvement that belongs to the completed request,
  using Plant Manager first when Plant Manager owns the row.
- Prevent plans, partial work, failed checks, or unpushed changes from being
  reported as complete.
- Make an Odoo access or matching failure visible to Dale.

## Non-goals

- Creating a new Odoo improvement when no matching row exists.
- Guessing between similar Odoo rows.
- Backfilling older completed Codex, Claude, or Cursor tasks.
- Changing the Plant Manager feedback mirror, its write gates, or its rollout
  approvals.
- Adding Odoo credentials, API keys, or private URLs to the repository.
- Building a new background service or automatic Odoo integration.

## Chosen approach

Use `AGENTS.md` as the shared source of truth and import it from `CLAUDE.md`.

This is preferred over copying the same prose into several files because a
copied rule can be fixed in one tool and remain stale in another. A dedicated
Cursor rule is unnecessary for this repository-wide workflow because Cursor
can read the root `AGENTS.md` directly. Claude-specific handoff content stays
in `CLAUDE.md` after the import.

## Completion workflow

For a user-requested task that has a corresponding row in **2s Improvement
Reference Data**, every supported agent must follow this order:

1. Finish every scoped implementation item.
2. Run the required validation and resolve failures that are in scope.
3. Commit and push the implementation to `origin/main` under the repository's
   existing delivery rules.
4. Locate the matching existing Odoo improvement. Prefer an explicit Odoo ID
   or Plant Manager Source ID supplied by the task. Otherwise use the request's
   title and description, and proceed only when exactly one row clearly
   matches.
5. Inspect the row's Source and Source ID. When Source is **GPI Plant Manager**
   and the Source ID is `GPI-PM-FB-<id>`, complete that local feedback through
   Plant Manager's authenticated admin lifecycle with the current actor and a
   short, plain-language resolution note. Plant Manager remains authoritative,
   and its existing worker mirrors the completion to Odoo. Do not edit only the
   Odoo mirror.
6. For an existing row not owned by Plant Manager, use an authenticated Odoo
   interface available to the agent to set Status to **Completed**. Fill a
   completion date, completed-by value, or resolution note only when the Odoo
   completion workflow requires it, using the current date, authenticated
   actor, and a short plain-language result. Do not change unrelated fields.
7. Read the same Odoo row back after any required synchronization and verify
   that its status is **Completed**.
8. State in the final handoff whether the Odoo completion was verified.

The agent must not create, delete, archive, or merge Odoo improvement rows as
part of this workflow.

## Failure handling

- No matching row: leave Odoo unchanged and tell Dale no match was found.
- More than one plausible row: leave all rows unchanged and report the
  ambiguity.
- Plant Manager or Odoo credentials, login, connector, browser session, or
  permissions are unavailable: report the access blocker without exposing
  sensitive details.
- A Plant Manager-owned row cannot be completed through its local lifecycle or
  does not synchronize to Odoo: do not bypass local truth with a direct Odoo
  edit; report the blocker.
- Odoo write fails or the status cannot be read back: do not claim Odoo is up
  to date; keep the task active or partially complete.
- Implementation, tests, commit, or push are incomplete: do not attempt the
  Odoo completion.
- Plan-only delivery: do not mark the improvement complete; a plan records
  intended work only.

## Files and agent behavior

- `AGENTS.md`: add the canonical repository-wide Odoo completion rule.
- `CLAUDE.md`: import `AGENTS.md` with `@AGENTS.md`, then preserve its existing
  Claude-specific handoff material.
- Cursor: use the root `AGENTS.md`; no `.cursor/rules` copy is added.

These are instruction files, so they guide an agent but do not grant Plant
Manager or Odoo access. Each agent still needs the authenticated paths required
by the matching row's source.

## Validation

- Confirm `AGENTS.md` contains one unambiguous completion gate and failure
  policy.
- Confirm `CLAUDE.md` imports `AGENTS.md` outside a code block.
- Confirm the existing Claude-specific handoff remains intact.
- Confirm no duplicate Cursor rule is introduced.
- Confirm no credentials or private Odoo connection details are added.
- Review the final diff for conflicts with the repository's existing delivery
  and Odoo safety rules.
