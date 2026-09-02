# Odoo-Authoritative Feedback Types Design

## Goal

Make Plant Manager offer every feedback type allowed by Odoo's 2s Improvements
reference table, keep the two systems on the same exact contract, provide a
safe local start/finish workflow, and recover feedback 44 after the contract is
repaired.

## Authority and Type Mapping

Odoo's `x_2s_improvements.x_studio_type` selection is authoritative. Plant
Manager will support these four user-facing choices in the same order as the
reference experience:

| Plant Manager choice | Local value | Odoo stored value |
| --- | --- | --- |
| Bug | `bug` | `Digital` |
| New Feature | `feature` | `Digital - New Feature` |
| Floor Issue | `floor_issue` | `Physical - Issue` |
| Floor Suggestion | `floor_suggestion` | `Physical - Suggestion` |

The Odoo stored values are distinct from display copy where necessary. The
shared type registry must drive route validation, projection, task titles,
admin labels, and My Requests labels so the mappings cannot drift apart.

The Maintenance-only Repair and work-order actions shown below the divider in
the reference screenshot are not feedback Type values and will not be added to
Plant Manager.

## Feedback Experience

Replace the current two-button Bug/Feature switch with a first-step four-choice
list modeled on the supplied reference: icon, title, short explanation, and a
clear forward affordance for each choice. Choosing an item advances to the
existing description and optional-picture step. The panel retains its current
submission, My Requests, and What's New tabs and remains usable at phone,
tablet, and desktop widths.

Existing feedback rows keep their current local values. New physical rows use
the new stable local values. Admin progress and My Requests show all four names
without treating an unknown value as Bug.

## Odoo Task and Mirror Flow

Owner-task delivery remains independent from the 2s Improvements mirror, but
both paths consume the same canonical type registry. Task titles and tags use
the selected user-facing category. The mirror projection sends the exact Odoo
stored value from the registry.

The mirror contract will require the exact four authoritative Odoo stored
values. A missing or unexpected Type option continues to fail closed before an
Odoo write. Database, company, fields, relations, status values, and Source
checks remain unchanged.

## Plant Manager Lifecycle Commands

Add local-only `scripts/process_feedback.py` and
`scripts/resolve_feedback.py` commands so an agent can use the same
`feedback_store.transition()` path as the authenticated admin UI. The commands
never write Odoo directly; the existing mirror remains the only Odoo writer.

Both commands accept exactly one authoritative identifier:

- `--feedback-id <positive id>`; or
- `--source-id GPI-PM-FB-<positive id>`.

The Odoo Project Task ID is not a Plant Manager feedback ID and must never be
used as one. Both commands default to a read-only preview and require `--yes`
before changing local state.

The start command moves `requested` to `in_progress` as soon as scoped work
begins, including planning. An already-in-progress row is a successful no-op.
Completed and declined rows remain terminal and are never reopened.

The finish command moves only `in_progress` to `completed`, requires a short
resolution note, and defaults the closer to `dale@gruberpallets.com`; `--by`
can identify a different real closer. It runs only after the implementation is
complete, required validation passes, and commits are pushed to `origin/main`.
Requested rows are not allowed to skip the start step, and terminal rows remain
unchanged.

Repository instructions will name these commands as the normal start/finish
workflow. They will continue to require exact matching, fail closed, forbid new
or guessed Odoo rows, and require Odoo readback before completion is reported.

## Safe Recovery for Pre-Attempt Quarantine

Feedback 44 was quarantined before a mutation attempt existed, so the existing
attempt-ID disposition command cannot release it. Add a separate audited local
operation for this exact state.

The operation accepts one feedback ID and reviewer, then succeeds only when:

- the sync row is quarantined with reason
  `target_identity_or_contract_mismatch`;
- `active_attempt_id` and `odoo_improvement_id` are null;
- `attempt_count` is zero and `last_synced_version < desired_version`;
- the local feedback lifecycle and projection version are still authoritative;
- a fresh read-only Odoo target inspection passes the complete contract.

After those checks, one transaction records the reviewer and prior quarantine
evidence, clears only the quarantine fields, and returns the same desired
version to `idle` and due now. It never lowers a version, invents an Odoo ID, or
marks the row synchronized. Repeating the operation after release must fail
safely.

Operator audit storage will support both attempt-backed actions and this
feedback-backed pre-attempt action while preserving all existing audit rows and
foreign-key evidence.

## Feedback 44 Completion

After deployment:

1. Run the production read-only preflight and require every contract check to
   pass.
2. Run the new guarded pre-attempt release for feedback 44 with the authenticated
   reviewer.
3. Let the normal mirror worker create and verify the Odoo row.
4. Read back exactly one row with Source `GPI Plant Manager`, Source ID
   `GPI-PM-FB-44`, Status `Completed`, and the existing completion note.

No direct Odoo row creation or edit is permitted during recovery.

## Error Handling

- Reject unsupported local feedback types with a client-safe validation error;
  never coerce them to Bug.
- Keep contract mismatches quarantined with privacy-safe reasons.
- Abort recovery if target inspection changes, local authority changes, or the
  row no longer matches the exact pre-attempt state.
- Preserve the original quarantine evidence in the operator audit.
- Leave feedback 44 active and report the blocker if preflight, release,
  worker verification, or Odoo readback fails.

## Testing

- Static and route tests cover all four choices and reject unknown types.
- Store and projection tests cover the new local values and exact Odoo mapping.
- Task-delivery tests cover titles and tags for physical feedback.
- Contract tests require the four authoritative Odoo stored values and fail on
  missing or extra values.
- Database tests prove the pre-attempt release's full guard, audit evidence,
  idempotent refusal, and preservation of versions and associations.
- CLI safety tests prove the new command requires a feedback ID, reviewer, and
  fresh read-only contract confirmation.
- Lifecycle command tests cover exact identifiers, source-ID parsing, dry-run,
  `--yes`, already-in-progress behavior, terminal refusal, required notes, and
  Dale's default closer identity.
- Static tests prove the lifecycle commands use the local feedback store and do
  not import a generic Odoo write client.
- Existing task-delivery, mirror, lifecycle, image, and feedback-panel suites
  remain green.

## Out of Scope

- Adding Maintenance Repair or work-order creation to Plant Manager.
- Relaxing the exact database, company, Source, field, status, or Type contract.
- Automatically clearing any ambiguous or attempt-backed quarantine.
- Editing, deleting, merging, or archiving Odoo improvement rows.
- Adding a status block to another application's copied prompt.
- Changing Sales Manager or OS Manager lifecycle commands or repository rules.
