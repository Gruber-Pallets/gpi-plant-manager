# Production Legacy ID Reconciliation Implementation Plan

> **Required sub-skill:** Use `superpowers:executing-plans` to implement this plan task by task.

**Goal:** Show each confirmed person once on production leaderboards, while retaining their full history across the StratusTime-to-Odoo employee-ID handoff.

**Architecture:** Keep `production_daily` immutable. Store the 13 confirmed legacy-ID-to-Odoo-ID links in a small audit table. Resolve IDs only when leaderboard records are read; the existing person/day aggregation then combines complementary cutover fragments into one full day. A restricted, dry-run-by-default script creates only the approved links.

**Tech stack:** Python 3.12, PostgreSQL, psycopg2, pytest.

## Global constraints

- Do not rewrite or delete `production_daily` facts.
- Use exact equality on `people.name`; do not use prefix, fuzzy, or truncated-name matching.
- The reconciliation allow-list is exactly: Adrian Aragon, Alejandro Velazquez, Christian Chanta, Domingo Recinos, Eulogio Mendez, Gerardo Vergara, Jesus Galindo, Jesus Martinez, Jose Cabezas, Jose Luis, Jose Ochoa, Lauro Benitez, and Porfirio Cazares.
- The mapping target is the matching `people.odoo_id`; every non-target production ID for an approved name becomes a legacy alias only after the dry run confirms it.
- Preserve separate people with similar or longer names (for example, Adrian Aragon Olivera and Porfirio Cazares Herrera).
- The June 2 split rows must be combined, not removed: the verified pairs hold complementary 6.75-hour and 0.9-hour portions of one 7.65-hour shift.
- Do not change raw history, ribbons, awards, GOAT results, or player-card computations.

---

## Task 1: Add an auditable legacy-ID alias table

**Files:**
- Modify: `src/zira_dashboard/_schema.py`
- Modify: `tests/test_db.py`

1. Directly after the `production_daily` indexes in `SCHEMA_DDL`, add idempotent DDL for `production_identity_aliases`:
   - `legacy_emp_id TEXT PRIMARY KEY`
   - `canonical_emp_id TEXT NOT NULL`
   - `confirmed_name TEXT NOT NULL`
   - `confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now()`
   - `source TEXT NOT NULL`
   - a `CHECK (legacy_emp_id <> canonical_emp_id)` constraint.
2. Add an index on `canonical_emp_id` only if the reader query needs it; the primary-key lookup is the hot path.
3. Extend `test_bootstrap_creates_tables` in `tests/test_db.py` to expect `production_identity_aliases` alongside the precompute tables.
4. Add a PostgreSQL-marked schema test that bootstraps the schema and asserts the primary key is `legacy_emp_id` and the check constraint exists by inspecting PostgreSQL metadata.

## Task 2: Build a conservative reconciliation service

**Files:**
- Create: `src/zira_dashboard/production_identity_aliases.py`
- Create: `tests/test_production_identity_aliases.py`

1. Define `APPROVED_NAMES` as a `frozenset[str]` containing only the 13 confirmed names, and `SOURCE = "legacy_name_reconciliation"`.
2. Implement `find_confirmed_aliases()` to query only these approved names. For each name, join `people` to its exact production records and return a proposed alias only when all of the following are true:
   - exactly one `people` row has that exact name;
   - its nonempty `odoo_id` appears in `production_daily.emp_id` for that name;
   - there is at least one different historical production ID;
   - every proposed legacy ID is nonempty and differs from the canonical Odoo ID.
   Return structured candidates and structured skipped names/reasons so a dry run is auditable.
3. Implement `upsert_confirmed_aliases(candidates)` with a parameterized `INSERT ... ON CONFLICT (legacy_emp_id) DO UPDATE` that refreshes the canonical ID, confirmed name, confirmation time, and source. It must only receive candidates created by the approved-name function.
4. Unit-test successful extraction with an Odoo ID and multiple legacy IDs, a duplicate exact-name roster match, a missing current Odoo-ID record, blank IDs, and an unapproved name. Assert unsafe cases are skipped and never emitted.
5. Unit-test that all proposed aliases retain their approved display name and that applying an empty candidate list performs no database write.

## Task 3: Add the controlled dry-run/apply operator command

**Files:**
- Create: `scripts/reconcile_production_identity_aliases.py`
- Modify: `tests/test_production_identity_aliases.py`

1. Follow the existing `scripts/approve_regular_attendance.py` import-path and `argparse` conventions.
2. Provide a single `--apply` flag. With no flag, initialize the connection pool, call `find_confirmed_aliases()`, and print every approved candidate as `name: legacy_id -> canonical_id`, plus skipped names/reasons and a clear `Dry run only` line.
3. With `--apply`, run `db.bootstrap_schema()` first, re-read candidates immediately before writing, call `upsert_confirmed_aliases()`, and print the exact number of written aliases. Do not accept arbitrary names, an `--all` mode, or a raw SQL input option.
4. Return a nonzero status if a confirmed name cannot be reconciled safely, so an operator cannot mistake a partial apply for a complete one.
5. Unit-test CLI dry-run does not call `bootstrap_schema` or `upsert_confirmed_aliases`; test `--apply` bootstraps then writes exactly the safe candidate set; test an unsafe/skipped confirmed name exits nonzero without writing it.

## Task 4: Resolve aliases only in leaderboard record reads

**Files:**
- Modify: `src/zira_dashboard/precompute.py`
- Modify: `tests/test_precompute_breakdown.py`
- Modify: `tests/test_production_metrics.py`

1. Update `normalized_daily_records_in_range()` to alias `production_daily` as `pd` and left join `production_identity_aliases` as `pia` on `pia.legacy_emp_id = pd.emp_id`.
2. Select `COALESCE(pia.canonical_emp_id, pd.emp_id) AS emp_id`; continue selecting the fact-table display name and all existing numeric fields from `pd` unchanged. Keep the `manual_absences` anti-join scoped to the original `pd.day` and `pd.name`.
3. Do not alter `daily_records_in_range()` or any raw fact-table writer. This keeps player-card, ribbon, award, and GOAT behavior out of scope.
4. Extend the normalized-reader mock test to verify it returns an aliased canonical ID and assert the query contains the alias-table left join and `COALESCE` expression.
5. Add a metrics regression test with two same-name, same-day records carrying different source IDs but the same canonical ID: a 6.75-hour/units fragment plus a 0.9-hour/units fragment must produce exactly one 7.65-hour daily score. Assert one person row and one qualifying day, proving the June 2 handoff is combined rather than double-counted as two days.

## Task 5: Validate, document, and release safely

**Files:**
- Modify: `CHANGELOG.md`

1. Run focused tests:
   ```bash
   uv run --extra dev pytest tests/test_db.py tests/test_precompute_breakdown.py tests/test_production_metrics.py tests/test_production_identity_aliases.py -q
   ```
2. Run the full non-network suite and lint/check commands documented by the repository; report any environment-skipped PostgreSQL tests separately.
3. Run the command without `--apply` against production and compare its exact candidates with the approved 13-name list. Do not run `--apply` until that output has no skipped or unexpected entries.
4. After the dry-run is clean, run `scripts/reconcile_production_identity_aliases.py --apply` once against production, reload the dashboard, and confirm each approved person appears on one leaderboard line while longer/different names remain separate.
5. Add a short, child-friendly `CHANGELOG.md` entry explaining that old and new employee records now stay together on one scoreboard line.
6. Commit the implementation and `CHANGELOG.md`, push to `origin/main`, and record the validation results in the handoff.

## Review checklist

- The runtime reader never matches identities by display name.
- The operator script cannot reconcile a name outside the literal approved allow-list.
- A missing, duplicate, or changed roster identity causes a safe skip and a failing apply, not a guess.
- Exact June 2 fragments aggregate to one person/day with 7.65 hours.
- Re-running the apply command is idempotent and does not mutate `production_daily`.
- Ribbons, awards, GOATs, and player cards have no code-path changes.
