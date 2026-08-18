# Production leaderboard employee identity

**Goal:** Show each employee once on production average leaderboards even
when their stored display name changed over time.

## Problem

`production_daily` stores both a stable `emp_id` and the display `name` that
was used when a production snapshot was written. The normalized production
reader currently returns only `name`, and leaderboard math keys people by that
name. An employee therefore receives separate score rows when historic data
uses a prior label (for example, `Jesus Galindo`) and later snapshots use the
current compact roster label (`Jesus G.`).

The Recycling leaderboard later expands compact labels to full Odoo names for
display. That makes the separate score rows appear as duplicate, identical
names.

## Decision

`emp_id` is the authoritative production identity whenever it is available.
The display name is presentation-only.

Unknown legacy records retain their current name-based fallback identity.
They must not be matched to an employee merely because the text happens to
match; two different people can share a name.

## Design

### Record contract

The normalized production-history read path will include `emp_id` on every
returned record, alongside `person`, `day`, `wc`, and the production metrics.

### Normalized score aggregation

The normalized-score and normalized-average helpers will identify a person by
the stable record identity rather than the display name. Their per-day bucket
will be keyed by `(person_identity, day)`, and their cross-day average by
`person_identity`.

For a record with an `emp_id`, `person_identity` is that ID. For a genuinely
legacy record without one, it is a name-scoped fallback identity. The public
row continues to expose a single `name` field, selected from the current
roster mapping when available, so templates do not need to understand IDs.

### Consumers

The shared normalized helpers power the Recycling leaderboard, the New
leaderboard, and Staffing's average leaderboard sections. Updating the shared
contract fixes all three consistently.

Other views that intentionally look up a person by historical label (such as
the player-card route) will preserve their explicit behavior unless their own
identity contract is separately updated.

### Name resolution

For records with stable IDs, the reader will resolve the display label from
the current active people record. If no current record exists, the most recent
stored historical name remains visible. This does not affect the numeric
calculation.

## Error handling

The existing database failure behavior remains unchanged. A missing or
unresolvable current roster entry must not hide production: the stored name is
used as the label, while the stored `emp_id` still joins that employee's
history.

## Tests

- A person with one employee ID and two historical display labels produces one
  normalized score/average row with combined days and units.
- Two records with the same visible name but different employee IDs remain
  distinct rows.
- A record with no employee ID retains its name-based fallback identity.
- The Recycling leaderboard route displays one full-name row for the renamed
  employee.
- Existing New and Staffing average leaderboard tests remain green, proving
  they continue to use the shared math correctly.

## Out of scope

- Mass-updating historic `production_daily.name` values.
- Guessing that matching name text means matching employees.
- Changing awards, GOAT, ribbons, player cards, or non-average leaderboard
  contracts beyond what the shared normalized record identity requires.
