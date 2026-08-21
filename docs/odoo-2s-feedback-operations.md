# Plant Manager feedback to Odoo 2s operations

This runbook is an approval checklist, not permission to run a rollout. Every
live checkpoint needs Dale's separate approval. The connection ships dark:
both write gates are blank or off, and this document does not change them.

## 1. Architecture and shared-table namespace

Plant Manager keeps feedback, pictures, lifecycle state, versions, attempts,
and warnings in its local PostgreSQL database. Local feedback is authoritative.
Odoo is a one-way reporting mirror into `x_2s_improvements`.

Plant Manager owns only rows whose Source is exactly `GPI Plant Manager` and
whose Source ID is `GPI-PM-FB-<positive local feedback id>`. Every lookup uses
both values and a permanent limit of three. Zero matches can create a row only
through the guarded worker, one match can be adopted, and two or more matches
stop in quarantine. This namespace is what keeps other apps' rows separate.

## 2. Environment names and exact gate semantics

The dedicated connection reads only these names:

```text
ODOO_IMPROVEMENTS_URL
ODOO_IMPROVEMENTS_DB
ODOO_IMPROVEMENTS_LOGIN
ODOO_IMPROVEMENTS_API_KEY
ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID
ODOO_IMPROVEMENTS_EXPECTED_COMPANY
ODOO_SHARED_REPORTING_WRITE_ENABLED
ODOO_IMPROVEMENTS_WRITE_ENABLED
ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID
```

Both write gates must contain the exact lowercase word `true`. Case changes,
spaces, missing values, and every other value mean closed. The canary ID, when
present, must be one positive signed-64-bit local feedback ID. An invalid
canary ID fails closed. Never pass credentials, identities, gates, field names,
Source values, or remote IDs as command-line arguments.

## 3. Permanent allowlist and denied operations

The dedicated client may read the target identity, current company, approved
field metadata, exact compound identity, exact improvement readback, employee
email matches, and legacy task stages. It may create or update only allowlisted
fields on `x_2s_improvements`, and only after all gates and identity checks.

It cannot call delete, unlink, archive, Studio field changes, unrelated model
writes, or generic Odoo helpers. It never writes `active`, `Physical`, a claim
token, or an unknown field. Optional missing values are omitted instead of
being cleared. Operators must never delete or archive shared improvements.

## 4. Dark deployment with the improvements gate off

Deploy code and schema with both write gates closed. A safe shell preparation
for a dark deployment is:

```sh
unset ODOO_SHARED_REPORTING_WRITE_ENABLED
unset ODOO_IMPROVEMENTS_WRITE_ENABLED
unset ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID
```

Restart or redeploy, then confirm normal Plant Manager feedback submission and
admin lifecycle work. Closed gates stop before client construction, expired
claim recovery, database claiming, authentication, or an Odoo call. Do not run
any rollout command merely because the dark deployment is healthy.

## 5. Approval checkpoint for read-only preflight

After separate approval to read the live target, configure the six dedicated
connection and expected-identity variables outside shell history. Keep both
write gates closed. Run only:

```sh
.venv/bin/python -m scripts.feedback_odoo_rollout preflight --confirm-read-only
```

The JSON report contains fixed field names and booleans only. Require database
and company matches, no missing/wrong/readonly fields or relations, the exact
selection contract, and the required Source value. Stop on any safe failure;
do not paste credentials or remote metadata into an issue or chat.

## 6. Missing Source response

If preflight says the Source selection lacks `GPI Plant Manager`, stop. Dale
must separately add that exact stored selection value in Odoo Studio. The app
never creates or edits Studio fields or selections. After Dale confirms the
manual change, obtain a new read-only preflight approval and rerun preflight.

## 7. Approval checkpoint for bounded dry-run batches

Dry-run is a separately approved live read. It does not migrate local rows,
save warnings, enqueue work, advance a cursor, or mutate Odoo. Start with a
small approved page:

```sh
.venv/bin/python -m scripts.feedback_odoo_rollout dry-run --confirm-read-only --after-id "$AFTER_ID" --batch-size "$BATCH_SIZE"
```

Use only nonnegative `$AFTER_ID` and a `$BATCH_SIZE` from 1 through 100. Record
the returned `next_after_id` for the next separately approved page. Do not
silently clamp, guess, or skip around a failed page.

## 8. Dry-run report interpretation

The report contains local feedback IDs and bounded counts, never messages,
people, pictures, remote IDs, URLs, or remote exception text. Interpret it as:

- target fields/selections/relations: all must remain safe from preflight;
- create, adopt, and update IDs: projected exact compound outcomes;
- duplicate IDs: multiple exact Source + Source ID matches; stop and review;
- ownership-conflict IDs: saved association and exact lookup disagree; stop;
- employee missing/ambiguous counts: relation will be omitted, never guessed;
- before/after image counts: bounded validated local image evidence;
- projected/skipped counts: unknown or absent legacy stages stay skipped.

Dry-run does not prove permissions to write, a live binary round trip, or
concurrent worker behavior.

## 9. Approval checkpoint and setup for one named canary

Choose one positive local feedback ID only after preflight and dry-run reviews.
Record a separate approval naming that ID. Keep both write gates closed while
installing the fence:

```sh
export ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID="${APPROVED_CANARY_FEEDBACK_ID}"
```

Restart or redeploy and verify the configured ID using the read-only report
guard. Absent, malformed, or mismatched fence configuration stops before any
local or remote read:

```sh
.venv/bin/python -m scripts.feedback_odoo_rollout canary-report --confirm-read-only --feedback-id "$APPROVED_CANARY_FEEDBACK_ID"
```

Before the worker has verified the canary, this report is expected to fail
safely because durable verified evidence does not exist yet.

## 10. Open both gates for the named canary and close immediately

This is a separate production-write approval. In the approved deployment
system, set each gate to the exact lowercase enabled value described in section
2; do not remove the canary fence. Restart or redeploy so the sequential worker
can claim only the named local ID. Do not manually invoke a worker batch.

```sh
export ODOO_SHARED_REPORTING_WRITE_ENABLED="${APPROVED_EXACT_GATE_VALUE}"
export ODOO_IMPROVEMENTS_WRITE_ENABLED="${APPROVED_EXACT_GATE_VALUE}"
```

After the worker settles, run the exact canary-report command from section 9.
Require: fresh target identity, local current version equal to synchronized
version, one matching immutable verified attempt, exactly one compound match
equal to the saved association, and full fresh readback including binary
length/hash evidence. The report never prints the remote ID or field values.

Immediately close the improvements gate and restart or redeploy:

```sh
unset ODOO_IMPROVEMENTS_WRITE_ENABLED
```

Keep the canary fence in place while evidence is reviewed.

## 11. Separate approval to remove the canary fence

Removing the fence and enabling live writes is a new decision after canary
evidence review. It is not implied by a successful canary. With both gates
closed, remove the fence and restart:

```sh
unset ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID
```

Only after the separate live-write approval may the deployment system reopen
both exact gates. Watch reconciliation and quarantine counts. Close either gate
at the first unexpected result.

## 12. Separate approvals for local legacy migration and history enqueue

Legacy migration changes local lifecycle provenance only. It reads exact legacy
task stages, maps only the finite approved stage names, and never invents
terminal details. Each page needs its own approved cursor and size:

```sh
.venv/bin/python -m scripts.feedback_odoo_rollout migrate-legacy --confirm-read-only --confirm-local-migration --after-id "$AFTER_ID" --batch-size "$BATCH_SIZE"
```

Historical enqueue is a different local-write approval. It advances a durable
local cursor and never calls Odoo:

```sh
.venv/bin/python -m scripts.feedback_odoo_rollout enqueue-history --confirm-local-backfill --batch-size "$BATCH_SIZE"
```

Do not treat migration approval as enqueue approval, or either one as Odoo
write approval. Stop on a conflict rather than changing a local row by hand.

## 13. Reconciliation counts and version lag

This command reads local state only and needs no Odoo client:

```sh
.venv/bin/python -m scripts.feedback_odoo_rollout reconcile
```

The mutually explainable buckets are synchronized, due, deferred, in-flight,
and quarantined. Version lag is the total positive difference between desired
and synchronized local versions. With either gate closed, unsynchronized idle
work is deferred rather than due. Investigate counts; never fix them by lowering
a desired version or marking a row synchronized manually.

## 14. Quarantine listing and human dispositions

List at most 100 ordered privacy-safe rows:

```sh
.venv/bin/python -m scripts.feedback_odoo_rollout quarantine-list
```

`keep` records human review and leaves the row quarantined:

```sh
.venv/bin/python -m scripts.feedback_odoo_rollout quarantine-disposition --attempt-id "$ATTEMPT_ID" --disposition keep --reviewer "$REVIEWER_NAME"
```

`release-definitive` is allowed only for an exact prepared or definitively
failed active attempt. It retains evidence and makes the same desired version
idle and due:

```sh
.venv/bin/python -m scripts.feedback_odoo_rollout quarantine-disposition --attempt-id "$ATTEMPT_ID" --disposition release-definitive --reviewer "$REVIEWER_NAME"
```

`supersede-and-retry` is allowed only for an exact ambiguous attempt after a
human checks Odoo. It preserves the old attempt, advances local truth by one,
and emits a fixed duplicate-risk warning:

```sh
.venv/bin/python -m scripts.feedback_odoo_rollout quarantine-disposition --attempt-id "$ATTEMPT_ID" --disposition supersede-and-retry --reviewer "$REVIEWER_NAME" --confirm-human-review
```

Both retry dispositions grant a fresh retry budget after the reviewed action.
`keep` does not reset that budget.

Matching values cannot auto-clear ambiguity. They do not prove whether a timed
out mutation succeeded. Never infer success, overwrite a remote association,
delete an attempt, or mark synchronized from current remote values.

## 15. Rollback by closing either gate

Rollback means close either exact write gate and restart or redeploy. Closing
the improvements gate is the narrowest stop:

```sh
unset ODOO_IMPROVEMENTS_WRITE_ENABLED
```

For a wider shared-reporting stop:

```sh
unset ODOO_SHARED_REPORTING_WRITE_ENABLED
```

Do not delete or archive remote improvements. Do not edit immutable attempts,
lower versions, clear quarantine in SQL, or change credentials as rollback.
Local submission and lifecycle work continue while the mirror is stopped.

## 16. Production behavior that remains unproven

Until each approved live stage completes, explicitly treat these as unproven:

- actual database UUID and current-company identity;
- actual field types, relations, selections, and Source presence;
- service-user authentication, read permission, and mutation permission;
- exact zero/one/duplicate compound identity counts in production;
- employee match results and omission counts;
- real image upload and full binary readback length/hash behavior;
- live database concurrency, recovery, retry timing, and quarantine operations;
- legacy stage distribution, historical row counts, and version lag;
- successful named-canary create/adopt/update and immutable readback evidence;
- safe unfenced ongoing writes shared with every other app using the table.

Unit tests and dark deployment prove code contracts only. They are not proof of
any item above and do not authorize the next checkpoint.
