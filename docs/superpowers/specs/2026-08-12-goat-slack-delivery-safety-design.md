# GOAT Slack Delivery Safety Design

## Problem

A `pytest-goat-...` Slack celebration was posted to the production channel.
The marker, future date, and values exactly match the database integration-test
fixture in `tests/test_goat_notification_store.py`.

That fixture previously inserted a pending outbox row into any configured
`DATABASE_URL`. The production GOAT worker runs once a minute and claimed every
pending delivery without checking whether its category or achieved date could
represent a real, timely GOAT celebration. A later test cleanup only removes
the row it creates; it cannot repair already-leaked rows and it leaves a race
with a concurrently running worker.

## Decision

Use two independent safeguards:

1. The delivery worker validates every claimed row before making a Slack API
   call. A row is publishable only when its category key is one of the current
   production GOAT categories, its achieved day is not in the future, and the
   day is still within the existing through-next-business-day celebration
   window.
2. Tests that write GOAT outbox data run only when the database connection is
   explicitly opted in, points to a loopback host, and uses a database whose
   name ends in `_test`. The database test runs in one transaction that is
   deliberately rolled back.

Rows that fail the delivery validation are retained but moved to a terminal
`suppressed` state with a clear reason. This preserves an audit trail and
prevents retry loops or accidental future posting. The leaked `pytest-goat`
row is therefore made safe by the first production boot after deployment.

## Data Flow

```text
finalize completed workday
  -> insert a real GOAT alert + pending delivery
  -> background worker claims delivery
  -> validate category and achieved-day window
      -> valid: post to Slack and mark sent
      -> invalid/stale: mark suppressed; never call Slack
```

The dashboard alert list applies the same category and non-future checks, so a
bad future-dated record cannot become visible there either.

## Schema

`goat_slack_deliveries.status` gains the terminal value `suppressed`.
Existing databases migrate their status check constraint during the normal
idempotent schema bootstrap. Suppressed rows retain their alert link,
timestamps, and `last_error`; no production data is deleted.

## Testing

Regression tests will prove that:

- a `pytest-goat` or future-dated claim is suppressed without calling Slack;
- a canonical, fresh GOAT remains deliverable;
- an expired GOAT is suppressed;
- the dashboard excludes future and noncanonical alerts; and
- the database integration test refuses non-test database URLs and rolls its
  writes back when it does run.

The focused notification tests, test-database safety tests, lint, and the
relevant schema/store tests will be run before delivery.

## Scope

This change does not alter how a genuine GOAT is calculated, which channel it
uses, or the normal Slack retry behavior within the permitted celebration
window.
