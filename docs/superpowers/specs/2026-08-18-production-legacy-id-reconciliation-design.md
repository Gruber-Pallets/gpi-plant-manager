# Production legacy-ID reconciliation

**Goal:** Show one production leaderboard row for each confirmed employee when
pre-June 2026 StratusTime IDs and current Odoo IDs refer to the same person.

## Problem

The first identity correction groups records that already share an ID. The
remaining duplicate rows have two nonempty, different IDs: a legacy
StratusTime ID before the June 2026 attendance migration and the current Odoo
employee ID afterward. The date ranges in the production database prove the
split occurs across that source-system changeover.

Matching only the visible name at leaderboard-render time is unsafe. Two
people can share a name, and a roster label can change.

## Approved reconciliation list

These exact, unique current-roster-name matches are approved for
reconciliation:

- Adrian Aragon
- Alejandro Velazquez
- Christian Chanta
- Domingo Recinos
- Eulogio Mendez
- Gerardo Vergara
- Jesus Galindo
- Jesus Martinez
- Jose Cabezas
- Jose Luis
- Jose Ochoa
- Lauro Benitez
- Porfirio Cazares

No unmatched or ambiguous name is approved automatically.

## Decision

Keep `production_daily` immutable. Add an audited crosswalk from each approved
legacy ID to its current Odoo employee ID, and use the crosswalk only when
reading normalized records for average leaderboards.

The reconciliation is persistent and explicit: a later roster rename cannot
silently change a previously approved identity mapping.

## Design

### Mapping table

Create `production_identity_aliases`:

| Column | Meaning |
| --- | --- |
| `legacy_emp_id` | The historical StratusTime ID; primary key. |
| `canonical_emp_id` | The current Odoo employee ID used for grouping. |
| `confirmed_name` | The exact roster name used when Dale approved the mapping. |
| `confirmed_at` | When the mapping was written. |
| `source` | Fixed provenance label, `legacy_name_reconciliation`. |

The table is additive. It never updates or deletes rows in
`production_daily`.

### Candidate and apply command

Add an idempotent command that:

1. Reads production rows and the current `people` roster.
2. Finds legacy/current ID pairs only when the stored production name exactly
   matches one and only one Odoo roster name.
3. Prints a dry-run review list with name, legacy ID, current ID, day count,
   and date range.
4. With `--apply` and the approved-name list, inserts the corresponding alias
   rows using conflict-safe upserts.
5. Reports any requested name that is absent, ambiguous, or has no
   legacy/current ID pair; it makes no mapping for those cases.

The command must not accept an unreviewed catch-all "apply all" mode.

### Read-path identity

`normalized_daily_records_in_range()` will left-join
`production_identity_aliases` and return the alias's `canonical_emp_id` when
one exists; otherwise it returns the original production ID. It retains the
stored `person` label and all production metrics.

The existing `person_identity()` helper then groups the approved legacy and
current rows together without any new display-name matching in metric code.

This affects Recycling, New, and Staffing average leaderboards. Ribbons,
awards, GOATs, raw production records, and player-card name-scoped totals are
unchanged.

### Rollout

After deployment creates the table, run the command in dry-run mode against
production and compare it with the approved list above. Then run its
name-restricted `--apply` mode. Reload the recycling leaderboard to verify one
line per approved person.

## Error handling

- Missing aliases preserve the original ID and current behavior.
- Ambiguous or missing names are reported and skipped.
- Re-running the approved apply command is safe and leaves the same mapping.
- Database failure leaves the leaderboard unavailable through its existing
  error behavior; no production data is rewritten.

## Tests

- Schema creates the alias table with the required key and metadata fields.
- The candidate builder accepts an exact unique roster-name match and rejects
  missing or ambiguous matches.
- Applying the approved list writes one alias per legacy ID and is idempotent.
- A normalized-record reader substitutes an alias canonical ID without
  changing its name or metrics; an unmapped row retains its original ID.
- An approved legacy/current pair produces one recycling leaderboard row.
- Ribbons, awards, GOATs, and player-card totals retain their existing
  name-scoped or raw-data contracts.

## Out of scope

- Automatic reconciliation beyond the approved list.
- Rewriting or deleting historical production records.
- Merging records based only on name text at render time.
