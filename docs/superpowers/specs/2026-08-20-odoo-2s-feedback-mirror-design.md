# Feedback to Odoo 2s Improvements Mirror

**Date:** 2026-08-20

**Status:** Approved design, awaiting implementation plan

**Supersedes for new feedback:** the Odoo-project-task authority described in
`2026-06-24-feedback-modal-redesign-design.md`

## Goal

Make GPI Plant Manager's lightbulb feedback local-first and mirror it safely into
the shared Odoo `x_2s_improvements` table. The local app owns submission and
lifecycle state. Odoo is a one-way reporting copy. A failure or delay in Odoo
must never fail, delay, or roll back a local feedback action.

The integration must be isolated from other applications that use the shared
table and from Plant Manager's existing Odoo attendance, time-off, skills,
payroll, and alert integrations.

## Current state and design consequence

Today `POST /feedback` creates an Odoo `project.task` first and only then inserts
a small local row. An Odoo failure returns an error and loses the local
submission. The local row stores the message, reporter email, page URL, type,
submission time, and legacy project-task ID. The app reads project-task stages
live to show Open, Done, or Rejected. There is no local feedback admin page,
lifecycle state, terminal timestamp, resolver, resolution note, or resolution
image. Images and PDFs are passed through as raw Odoo task attachments and are
not stored locally or normalized.

Therefore, the new design adds a small local admin lifecycle and a dedicated,
durable outbox worker. Existing project tasks remain untouched. They are a
read-only source for a separately approved legacy migration, not the ongoing
authority.

## Isolation contract

The permanent Odoo identity for this app is:

- `x_studio_source = "GPI Plant Manager"`
- `x_studio_source_id = "GPI-PM-FB-<local feedback id>"`

The exact stored Source selection value required in Odoo is
`GPI Plant Manager`. The app will never create or modify that selection. A
read-only preflight will report it as missing if it is not present, and Dale
must add that exact stored value manually before any canary write.

Every lookup uses both Source and Source ID, requests at most three rows, and
accepts exactly zero or one match. More than one exact match is a conflict and
is quarantined without a mutation. Source ID is never searched alone.

The new integration uses a dedicated improvements client, dedicated environment
configuration, and a closed model/method allowlist. It does not tighten,
replace, or otherwise alter the generic `odoo_client` used by existing Plant
Manager features. Existing project-task helpers stay available because other
alerts still use them.

## User and admin experience

### Submission

The existing lightbulb remains the entry point. A submission contains:

- Bug or Feature request
- A required problem or idea description
- The current page URL when it is a safe same-origin or HTTPS URL
- One optional before screenshot, selected or pasted

Submission validates and normalizes the optional screenshot, then commits the
feedback row, image, lifecycle state, desired projection version, and sync row
in one short local database transaction. It immediately returns success. It
does not call Odoo.

The old multiple-file/PDF control becomes a single optional screenshot control.
The shared 2s contract has one Before Image field, and retaining arbitrary local
documents would add a separate document-management feature outside this scope.

### My Feedback

The existing list becomes fully local. It shows Requested, In Progress,
Completed, or Declined from local lifecycle state and remains available during
an Odoo outage. Legacy rows that have not yet been migrated retain their current
read-only project-task status behavior until the approved legacy migration
records a local state; this prevents old completed items from suddenly appearing
Requested merely because the dark integration was deployed.

### Local admin lifecycle

A super-admin-only feedback page uses the existing `SUPER_ADMIN_UPNS` check. It
lists feedback and permits these transitions:

- Requested -> In Progress
- Requested -> Completed or Declined
- In Progress -> Completed or Declined

Completed and Declined are terminal in this scope. A terminal action records,
in the same local transaction:

- the local terminal timestamp,
- the authenticated admin's normalized UPN,
- a required plain-text resolution note,
- one optional normalized after screenshot,
- the next desired projection version.

The note is escaped and converted to simple HTML only when the Odoo projection
is built. Terminal actions use a confirmation step and are not editable or
reopenable through the first version of the UI. This keeps a single durable
terminal event for date, completer, and notes.

The admin page reports sync state but never offers a button that bypasses the
worker, clears ambiguity, changes write gates, or performs a direct Odoo write.

## Local data model

### `feedback`

Retain existing columns and add:

- `status`: `requested`, `in_progress`, `completed`, or `declined`; nullable
  only for unmigrated legacy rows
- `finished_at`: timezone-aware terminal timestamp, otherwise null
- `finished_by`: terminal admin UPN, otherwise null
- `resolution_note`: terminal plain text, otherwise null
- `lifecycle_origin`: `local` or `legacy_project_task`; nullable only for
  unmigrated legacy rows
- `updated_at`: last meaningful local change
- `projection_version`: monotonically increasing positive integer
- `legacy_lifecycle_migrated_at`: set only when an approved legacy migration
  establishes local lifecycle authority

Database checks enforce terminal-field consistency for locally authored events:
completed/declined rows whose current lifecycle origin is `local` have
`finished_at`, `finished_by`, and a nonblank note; local nonterminal rows do not.
A migrated legacy terminal state may leave unproven terminal details null, with
its origin making that provenance explicit. Any later admin transition replaces
the current lifecycle origin with `local`. The existing `odoo_task_id` is
retained only as a legacy reference.

### `feedback_images`

Store before and after images separately:

- `feedback_id`
- `role`: `before` or `after`
- sanitized JPEG bytes
- SHA-256 hex digest
- decoded byte length
- width and height
- creation timestamp

The primary key is `(feedback_id, role)`. The app never stores original upload
bytes after successful normalization.

### `feedback_odoo_sync`

One current synchronization row per feedback record stores:

- desired projection version
- last-synced version
- adopted/created Odoo improvement ID, if known
- next due/retry time
- attempt count
- state: `idle`, `in_flight`, or `quarantined`
- local claim owner, random claim token, and claim expiry
- active immutable attempt ID
- last safe error class and summary
- quarantine reason and timestamp
- timestamps

The local claim token never appears in an Odoo payload.

### `feedback_odoo_attempts`

An append-only row per proposed mutation stores the evidence needed to reason
about ambiguity:

- random attempt ID
- feedback ID and exact projection version
- `create` or `update`
- remote ID known before dispatch, if any
- immutable JSON manifest containing every nonbinary value to be written
- manifest SHA-256 digest
- before/after image decoded lengths and SHA-256 digests, but no image bytes
- state: `prepared`, `dispatch_marked`, `rpc_succeeded`, `verified`,
  `definitive_failed`, or `ambiguous`
- dispatch, successful-RPC, readback, and settlement timestamps
- safe outcome detail

A database trigger rejects changes to attempt identity, projection version,
mutation kind, manifest, manifest digest, and binary evidence after insert.
Only outcome state and timestamps may advance.

## Projection mapping

| Local value | Odoo field | Rule |
|---|---|---|
| Feedback message | `x_name` | Full trimmed problem or idea text |
| Local ID | `x_studio_source_id` | `GPI-PM-FB-<id>` |
| Created timestamp | `x_studio_date_start` | Plant-local date for an Odoo `date`; UTC timestamp for an Odoo `datetime` |
| Reporter UPN | `x_studio_submitted_by` | Employee ID only on one exact normalized work-email match |
| Terminal timestamp | `x_studio_date_stop` | Plant-local date or UTC datetime, matching the field type, for terminal rows only |
| Terminal admin UPN | `x_studio_completed_by` | Employee ID only on one exact normalized work-email match |
| Resolution note | `x_studio_notes` | Escaped text in simple paragraph HTML |
| Before image | `x_studio_image` | Base64 JPEG at dispatch only |
| After image | `x_studio_after_image` | Base64 JPEG at dispatch only |
| Local status | `x_studio_status` | Selection mapping below |
| Local type | `x_studio_type` | Selection mapping below |
| App identity | `x_studio_source` | Exact stored value `GPI Plant Manager` |

Status mapping:

- `requested` -> `Requested`
- `in_progress` -> `In-Progress`
- `completed` -> `Completed`
- `declined` -> `Declined`

Type mapping:

- `bug` -> `Digital`
- `feature` -> `Digital - New Feature`
- missing legacy type -> `Digital`
- `Physical` is never emitted because Plant Manager accepts only digital
  feedback

Required identity, name, date, status, type, and source fields are always
present in a projection. Optional employee, terminal, note, and image fields
are omitted when local authoritative data is absent. They are never written as
false or null. Consequently the integration cannot erase an optional value
that a person added directly in Odoo. The UI does not support deleting a local
image after it has become authoritative; a later image may replace it through a
new local projection version.

## Employee resolution

Normalize a UPN or `work_email` with whitespace trimming and Unicode-safe
lowercasing only. Do not infer aliases or match by name. Search active and
archived `hr.employee` rows by case-insensitive email equality, request at most
three results, then apply exact normalized equality locally:

- one exact employee: write its ID
- zero: omit the many2one and record a safe warning keyed by local feedback ID
- more than one: omit the many2one and record an ambiguous-identity warning

Employee ambiguity does not block the rest of the projection because the
relation is optional.

## Image safety

A shared image normalizer handles before and after screenshots identically:

- accept JPEG, PNG, or WebP based on decoded content, not the claimed MIME type
- reject source files over 10 MiB
- reject dimensions over 8,192 pixels on either side or 25 million total pixels
- honor decoded-image/decompression-bomb checks
- apply orientation, flatten animation to the first frame, and composite
  transparency onto white
- resize proportionally to at most 2,048 pixels on the longest side
- convert to RGB JPEG at quality 85
- strip all metadata by creating a fresh output image
- reject normalized output over 5 MiB

Only sanitized JPEG bytes are saved locally. Base64 is generated immediately
before dispatch and is never stored in the manifest. Readback requests full
binary values with Odoo bin-size summaries disabled, decodes them strictly, and
compares decoded length plus SHA-256.

## Dedicated Odoo client and security boundary

The integration reads dedicated environment values without logging them:

- `ODOO_IMPROVEMENTS_URL`
- `ODOO_IMPROVEMENTS_DB`
- `ODOO_IMPROVEMENTS_LOGIN`
- `ODOO_IMPROVEMENTS_API_KEY`
- `ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID`
- `ODOO_IMPROVEMENTS_EXPECTED_COMPANY`
- `ODOO_SHARED_REPORTING_WRITE_ENABLED`
- `ODOO_IMPROVEMENTS_WRITE_ENABLED`
- `ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID`

The implementation adds support for these variables but does not create,
change, or inspect production credentials or gates.

Both gates must equal the exact lowercase string `true`. Missing, uppercase,
mixed-case, whitespace-padded, or any other values are closed. The improvements
gate ships unset/off. Closed gates prevent both worker claims and mutation
calls. The mutation function checks the gates again immediately before every
create/write to protect direct or stale callers.

When `ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID` is set to one positive local ID,
claims and mutations are additionally limited to that exact record. Invalid
values fail closed. Canary rollout sets this fence before opening the gates.
General live rollout requires separately approved removal of the canary fence.

The permanent allowlist permits only:

- authentication
- database UUID read through `ir.config_parameter`
- current-user company reads through `res.users` and `res.company`
- target contract/selection reads through `x_2s_improvements.fields_get`
- exact target lookup/read through `x_2s_improvements.search_read` and `read`
- exact employee lookup through `hr.employee.search_read`
- legacy migration reads through `project.task.read`, restricted to locally
  stored legacy task IDs and fields `id` and `stage_id`
- `x_2s_improvements.create` and `x_2s_improvements.write`

All other model/method pairs fail locally before an RPC. In particular, the
client permanently denies `unlink`, archive-style writes, Studio or field
metadata mutations, and every unrelated model mutation. `write` also rejects
unknown target fields and any attempt to set `active`.

The legacy `project.task.read` permission is used only by the explicitly invoked
read-only migration tools. If the dedicated service user lacks that permission,
the dry run reports legacy status as unresolved and does not guess. It is never
required for new submissions or live synchronization.

Immediately before each mutation, fresh reads must prove:

- `database.uuid` exactly matches the configured expected UUID
- the authenticated service user's current company name exactly matches the
  configured expected company
- the Source selection still contains stored value `GPI Plant Manager`

Any mismatch prevents dispatch and is quarantined as a target-identity or
contract failure. Identity results are not cached across mutations.

Dedicated credentials are recommended so Odoo itself can restrict this client
to the same narrow permissions. Creating or changing that service user remains
an explicitly approved rollout action and is not part of implementation.

## Lookup, adoption, and mutation flow

For every claimed version:

1. Build the projection entirely from committed local data.
2. Resolve optional employees and read image evidence.
3. Search with both exact Source fields and `limit=3`.
4. If more than one row matches, quarantine without writing.
5. If exactly one row exists, adopt it. If a saved Odoo ID also exists, require
   it to equal the exact match and require the row at that ID to retain both
   identity fields.
6. If zero rows exist and no saved Odoo ID exists, prepare a create.
7. If zero rows exist but a saved Odoo ID exists, quarantine the ownership
   conflict; never silently create a replacement.
8. Persist the exact immutable manifest and binary evidence in a short local
   transaction.
9. Recheck gates and fresh target identity.
10. Mark dispatch durably, commit, and perform one Odoo mutation with no local
    database lock held.
11. Validate the mutation response: create must return a positive integer ID;
    write must return exactly `True`.
12. Persist successful-RPC evidence before attempting verification.
13. Fresh-read the remote row and compare every field written by the manifest.
14. Settle the local version only after every comparison passes.

Adoption never deletes or clears remote values. The first adoption update sends
the same current projection that a create would send, while omitting absent
optional fields.

## Concurrency and version settlement

The worker claims a small due batch with `FOR UPDATE SKIP LOCKED`, records a
random local claim owner/token and lease, then commits. It processes at most 10
records sequentially per tick. No row lock or transaction remains open during
an Odoo call.

A local edit increments `desired_version` in the same transaction as the domain
change, even if an older version is in flight. Verification settles only the
attempt's exact version. If the desired version advanced, the row returns to
idle and due immediately for the newer projection. Settlement predicates on
the attempt ID, claim token, and expected version, so an older worker cannot
overwrite newer local truth or another worker's claim.

An expired claim that never reached `dispatch_marked` is known not to have
mutated Odoo and may be safely released. An expired claim at or after
`dispatch_marked` is quarantined as ambiguous.

## Failures, retries, and quarantine

Failures known to precede dispatch and server-returned XML-RPC faults that prove
the transaction rolled back are definitive. They retry with bounded exponential
backoff: 1, 2, 4, 8, 16, 32, then 60 minutes, capped at eight total mutation
attempts. Exhaustion quarantines the record.

A timeout, connection loss, malformed success response, worker shutdown after
the durable dispatch marker, or failure to persist a successful response is
ambiguous. It is quarantined immediately and never auto-retried.

Matching current Odoo values alone can never clear an ambiguous mutation. The
stale request could still complete later. Automated readback settlement is
allowed only when the local attempt row already proves the mutation RPC returned
successfully and only readback or local settlement failed afterward. That
recovery still performs a fresh exact readback before settlement.

Quarantine is a fail-closed operator state. The first release mechanism is a
documented local administrative command requiring an explicit attempt ID and a
human-chosen disposition. It never writes automatically. Designing a richer
quarantine-resolution UI is outside this scope.

Logs and reports contain local feedback IDs, attempt IDs, safe error classes,
and counts. They never contain credentials, API keys, raw images, base64,
resolution text, or reporter/admin email addresses.

## Background worker and backfill

Add one existing-style app warmer that wakes every 60 seconds. It exits before
claiming when either write gate is closed. When open, it processes at most 10
due records sequentially. Normal live changes and historical backfill use the
same durable versions, manifests, verification, retry, and quarantine path.

Historical enqueue is bounded and restart-safe. A command selects at most 100
local IDs after a saved cursor, inserts or advances their desired sync rows
idempotently, saves the cursor, and exits or continues only when explicitly
asked for another batch. It does not call Odoo itself.

The read-only full-history dry run uses the same projection builder and exact
compound lookup but never claims, creates, or writes. For legacy rows it may
read the existing Plant Manager project task only to propose an initial local
status:

- New/other -> Requested
- In Progress -> In Progress
- Done -> Completed
- Rejected -> Declined

It does not treat project-task `write_date`, `write_uid`, or chatter as proof of
the exact terminal event. Unknown legacy completion date, completer, note, or
after image remains absent and is omitted from Odoo. Applying the proposed
legacy local states is a separate bounded, restart-safe local migration that
also requires explicit approval.

## Read-only tools and operational reporting

Implementation provides commands that are inert unless called explicitly:

- contract/identity preflight
- full-history dry run
- one-record canary projection and verification report
- bounded historical enqueue
- reconciliation counts
- explicit quarantine inspection/disposition

The preflight verifies field names, field types, writable flags, exact stored
selection values, database UUID, company, and service-user access. It reports
the exact missing Source value as `GPI Plant Manager` and never attempts a
Studio change. The two date fields may be Odoo `date` or `datetime`; all other
unexpected contract types fail preflight before a canary mutation.

The dry run reports missing fields/selections, duplicate compound identities,
saved-ID ownership conflicts, employee match/missing/ambiguous counts, image
readiness, status/type counts, and projected create/adopt/update totals. Reports
identify problem records by local feedback ID without printing email or note
content.

Reconciliation reports synchronized, due, deferred-by-closed-gate, in-flight,
and quarantined counts, plus last-synced versus desired version lag.

## Testing strategy

All automated tests use fake executors and local test data. They never use live
Odoo credentials or production URLs.

Required tests cover:

- exact field, status, and type mapping, including legacy missing type
- exact Source value and namespaced Source ID
- compound lookup zero/one/duplicate behavior and saved-ID conflicts
- one exact employee, missing employee, and ambiguous employee outcomes
- optional before/after images and omission of absent fields
- decoder validation, limits, metadata stripping, orientation, resize, and JPEG
  output
- create, update, and exact orphan adoption
- exact full-field readback before settlement
- full Odoo binary request plus decoded hash/length verification
- no remote sync-token field in any payload or manifest
- both closed write gates prevent claims and mutation RPCs
- fresh database UUID/company/Source verification before every mutation
- allowlist rejection of unlink, archive, Studio, and unrelated mutations
- ambiguous mutations quarantine without retry
- matching values alone cannot clear ambiguous quarantine
- RPC-success evidence permits later exact-readback settlement
- definitive failures use the exact bounded retry schedule
- concurrent workers cannot duplicate a create or settle over newer local truth
- submission and lifecycle changes succeed when Odoo is unavailable
- terminal-event consistency and HTML-safe resolution notes
- legacy status migration omits unproven terminal details
- historical dry run and enqueue are bounded, idempotent, and restart-safe
- reconciliation state counts
- existing feedback UI/status compatibility during the unmigrated legacy period
- existing Odoo attendance, time-off, skills, payroll, and project-task tests
  remain unchanged and passing

Verification before rollout includes the complete project test suite and lint.
Production contract behavior, service-user permissions, real Odoo image
round-tripping, and live concurrency remain explicitly unproven until the
approved preflight and canary stages.

## Operational documentation and patch notes

Implementation updates the README or a dedicated operations document with:

- every new environment variable without values
- gate semantics and the shipped-off default
- allowlisted operations
- preflight, dry-run, canary, backfill, reconciliation, and quarantine commands
- rollback behavior
- which steps require Dale's explicit approval

New What's New notes use plain language and state that feedback remains saved
when Odoo is unavailable. They must not imply production Odoo syncing is active
until the rollout reaches that stage.

## Rollout and approval gates

Implementation does not perform any production Odoo read or write, Studio
change, credential change, gate change, or backfill. Rollout is:

1. Deploy code and schema dark with `ODOO_IMPROVEMENTS_WRITE_ENABLED` absent/off.
2. Obtain approval, then run the read-only contract/identity preflight.
3. Obtain approval, then run the read-only full-history dry run.
4. Report missing fields/selections, duplicates, ownership conflicts, employee
   resolution issues, image readiness, and projected counts.
5. If Source is missing, Dale adds exact stored value `GPI Plant Manager`.
6. Obtain approval for one named local feedback ID as the canary.
7. Set the canary fence to that local ID, enable both gates, write only that one
   record, and verify every mapped value through fresh readback.
8. Close the improvements gate again and report the canary evidence.
9. Obtain separate approval to remove the canary fence and enable the
   improvements gate for live work.
10. Obtain separate approval to apply the bounded legacy lifecycle migration
    and enqueue historical feedback.
11. Backfill through bounded batches and reconcile synchronized, due, deferred,
    in-flight, and quarantined counts after each batch.

Rollback closes either write gate. Local submission, status views, and admin
lifecycle remain functional. Rollback never deletes, archives, clears, or
otherwise mutates an Odoo improvement.

## Out of scope

- Odoo Studio or selection changes
- Automatic deletion or archiving of Odoo improvements
- Reading changes back from the shared table
- Physical improvements
- Reusing or changing other apps' Source values or rows
- Retrofitting the new gates or dedicated credentials onto existing Plant
  Manager Odoo workflows
- A general-purpose Odoo job framework
- Arbitrary local document/PDF storage
- Automatic resolution of ambiguous mutations
