# App Performance: 3x Speedup via Precompute + Warm Cache

**Date:** 2026-05-11
**Status:** Draft — pending user review
**Goal:** Make the GPI Plant Manager dashboard ~3x faster on first-page-load, page-switching, and in-page interactions, without sacrificing the freshness needed for the scheduler and late/absent report.

## Problem

The app feels slow across the board. The slowness shows up in three places: cold first page load, switching between sub-tabs, and in-page interactions. Backend profiling has not been done, but the architectural shape suggests two dominant costs:

1. **External-API tax on every request.** Routes for leaderboards, player cards, trophies, value streams, and the scheduler all call `production_history.attribution_for(day, client)` (or its range form) in the request path. That function runs the per-day attribution computation by combining published schedules with raw Zira leaderboard output — re-doing the same work for the same historical day on every page hit. The raw Zira payload is already cached in `zira_daily_cache`, but the per-person attribution layer is recomputed every time.
2. **External-API tax on the live pages.** The plant scheduler and late/absent report block on StratusTime + Odoo API calls in the request path. These calls dominate cold first-paint on the scheduler.

## Freshness budget (user-defined)

- **Must be live:** Plant Scheduler (today's view) and the Late/Absent Report.
- **Daily refresh is fine:** Leaderboards, Player Cards, Value Streams (Recycling / New VS), Trophies + awards, Skills Matrix. Edits to prior days' schedules show up on the next nightly run; the editor UIs surface a "updates tomorrow" hint where relevant.

## Strategy

Two independent subsystems, both background, both in this spec:

1. **Nightly precompute job** writes a per-day-per-operator-per-WC fact table that every daily-OK page reads from. Routes drop their per-day attribution loop and become `SUM … GROUP BY` queries against a single indexed Postgres table.
2. **Live warmer daemon** refreshes today's StratusTime and Odoo data every ~45 seconds into small cache tables. Live routes always read from the cache tables and never block on external APIs.

The unifying principle: **the request path never calls Odoo or StratusTime.** Yesterday-and-earlier data is pre-aggregated; today's data was refreshed in the background within the last minute.

## Components

### New tables

**`production_daily` — the fact table for every daily-OK page.**

```sql
CREATE TABLE production_daily (
  day         DATE   NOT NULL,
  emp_id      TEXT   NOT NULL,   -- StratusTime EmpIdentifier
  name        TEXT   NOT NULL,   -- denormalized so reads avoid joins
  wc_name     TEXT   NOT NULL,
  units       NUMERIC NOT NULL DEFAULT 0,
  downtime    NUMERIC NOT NULL DEFAULT 0,
  hours       NUMERIC NOT NULL DEFAULT 0,
  days_worked NUMERIC NOT NULL DEFAULT 0,   -- fractional, for multi-person WCs
  computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, emp_id, wc_name)
);
CREATE INDEX ON production_daily (name, day);
CREATE INDEX ON production_daily (wc_name, day);
```

Atomic granularity (per day × person × WC) so any date range is a SUM over a small number of rows. Award overrides keep flowing through `award_overrides` at read time.

**Three live cache tables for the scheduler + late report.**

```sql
CREATE TABLE today_attendance_cache (
  day          DATE PRIMARY KEY,
  payload      JSONB NOT NULL,
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE today_timeoff_cache (
  day          DATE PRIMARY KEY,
  payload      JSONB NOT NULL,
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE today_production_cache (
  day          DATE PRIMARY KEY,
  payload      JSONB NOT NULL,
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Single row per table (keyed by today's date) — the warmer overwrites it on every loop iteration. `refreshed_at` lets routes detect staleness and trigger an inline emergency refresh if the warmer has been silent for >3 minutes.

### Nightly precompute job

**HTTP endpoint:** `POST /api/admin/precompute-nightly`

- Auth: `X-Admin-Secret` header compared against `ZIRA_ADMIN_SECRET` env var. Returns 401 on mismatch.
- Default behavior: precompute yesterday's date.
- Optional query params: `from`, `to` for a range (used by manual re-runs and the deploy-time backfill).
- Logic: for each day in window:
  1. Call `production_history.attribution_for(day, client)` (existing function, already hits `zira_daily_cache`).
  2. Flatten the `{person → {wc → totals}}` structure into per-(day, emp_id, wc_name) rows. Look up `emp_id` from the people roster keyed by name (existing `people` table).
  3. UPSERT into `production_daily` keyed on the PK. PK conflict = update all columns + bump `computed_at`.
- Response: `{days_processed, rows_written, duration_ms, errors: [...]}`. Errors are per-day and don't abort the whole run.
- Idempotent: re-running the same day overwrites cleanly.

**Trigger:** Windows Task Scheduler runs `curl -X POST -H "X-Admin-Secret: $ZIRA_ADMIN_SECRET" http://localhost:8000/api/admin/precompute-nightly` at 3:30 AM daily. Task Scheduler logs the response body for human review.

**Backfill:** one-shot manual `curl` with `?from=2025-05-11&to=2026-05-10` covers the historical year needed for trophies and annual leaderboards.

### Live warmer daemon

**Where:** in-process asyncio task started from the FastAPI lifespan handler.

```
async def warmer_loop():
    while not shutdown:
        today = current_local_date()
        try: refresh_attendance(today)
        except Exception as e: log(e)
        try: refresh_timeoff(today)
        except Exception as e: log(e)
        try: refresh_production(today)
        except Exception as e: log(e)
        await asyncio.sleep(45)
```

Each refresh fetches from StratusTime / Odoo and UPSERTs the JSONB payload into the corresponding `today_*_cache` table with `refreshed_at = now()`. Independent try/except per source so a StratusTime outage doesn't block the Odoo refresh.

**The Odoo refresh also writes today's `production_daily` rows.** After pulling today's Zira production, the warmer runs the same `attribution_for(today, client)` flatten-and-upsert step the nightly job uses, keyed at `day = today`. This is the critical piece that keeps ranges including today (MTD, "last 7 days", "today only") consistent — every `production_daily` query, no matter the range, sees today's partial-day data refreshed within the last 45 seconds. Past days arrive via the nightly job; today's row is continuously refreshed by the warmer. Both write to the same table via the same UPSERT.

**Cold-start safety valve:** when a route reads from a `today_*_cache` table and finds `refreshed_at > now() - INTERVAL '3 minutes'` is false, it triggers an inline one-shot refresh before returning. Covers the case where the app just booted and the warmer hasn't run yet.

### Route changes

| Route family | Old hot path | New hot path |
|---|---|---|
| `/leaderboards` | `production_history.daily_records(start, end, client)` → N day attributions | `SELECT name, SUM(units), SUM(hours), SUM(days_worked) FROM production_daily WHERE day BETWEEN ? AND ? AND wc_name = ANY(?) GROUP BY name` |
| `/staffing/people/{name}` | `attribution_range(start, end, …)` | `SELECT wc_name, SUM(units), SUM(hours), SUM(downtime), SUM(days_worked) FROM production_daily WHERE name = ? AND day BETWEEN ? AND ? GROUP BY wc_name` |
| `/trophies`, player-card Trophy Case | `awards.person_days_in_group/wc` internals call `daily_records` | Same public API; internals now read from `production_daily` |
| `/recycling`, `/new-vs` (Value Streams) | per-day Zira + attribution loop | `SELECT wc_name, SUM(units), SUM(hours) FROM production_daily WHERE day BETWEEN ? AND ? AND wc_name = ANY(?) GROUP BY wc_name` |
| `/staffing/{day}` where `day = today` | `stratustime_client.attendance_for_day` + `time_off_entries_for_day` + Odoo today calls | `SELECT payload FROM today_*_cache WHERE day = ?` (with cold-start safety valve) |
| `/staffing/{day}` where `day < today` | same as today | unchanged (historical days hit the existing past-schedule path; `production_daily` is also available for any aggregates) |
| `/api/late-report` | StratusTime direct | `SELECT payload FROM today_attendance_cache WHERE day = ?` |

The shape of return data does not change. Templates are untouched. Existing tests for ranking, attribution, and award logic continue to apply because we keep the existing pure-functional cores (`rank_by_category`, `apply_overrides`, `_rank_single_day`, etc.) — only the data-fetch layer underneath them is swapped.

## Error handling

- **Nightly job:** per-day try/except; one bad day doesn't tank the run. Failed days are listed in the response. Re-run by re-hitting the endpoint with `?from=…&to=…`. If the whole run fails (e.g., DB down), Task Scheduler shows a non-2xx response and the run is retriable.
- **Warmer:** per-source try/except inside the loop; last-good payload remains in the cache table when a refresh fails. The loop itself is wrapped in an outer try/except so an unexpected exception logs and the loop continues. If the loop ever dies for real, routes fall back to the cold-start inline refresh (slower, but correct).
- **Roster / emp_id lookups in nightly job:** if a person in attribution output isn't in the `people` table, log + skip that row rather than failing the whole day. The next sync pulls the person from Odoo and tomorrow's run captures them.

## Invalidation

- **Edits to prior days' schedules, WC time attributions, or roster filter:** wait for next nightly run. Editor UI surfaces a small "updates tomorrow on history pages" hint where the edit is happening. Manual re-run is always available via the admin endpoint with `?from=<day>&to=<day>`.
- **Edits to today:** the live warmer picks them up automatically on its next 45-second tick — both the `today_*_cache` JSONB blobs and today's `production_daily` rows. No special invalidation needed.
- **Award overrides:** unchanged. `award_overrides` still applies at read time on top of the precomputed slot list.

## Testing

- **Schema migration:** add tables to `db.bootstrap_schema()`; existing migrations style.
- **Nightly endpoint:** integration test that seeds a mock `zira_daily_cache` + schedule, calls the endpoint, asserts `production_daily` rows.
- **Cutover correctness:** for each route family, side-by-side diff test — run the old path and the new path against a real backfilled DB, assert identical output. Keep these tests in the codebase as regression guards. Once green, remove the old path.
- **Warmer daemon:** unit-test the refresh functions; integration-test the loop with a short sleep override and an injected failing source to verify per-source isolation.
- **Cold-start safety valve:** test that a route hit with an empty / >3-min-stale cache triggers an inline refresh and still returns correct data.

## Rollout order (within this single spec)

Both sub-projects ship in this spec / plan. Implementation order:

1. Schema migrations (all four new tables).
2. Nightly precompute endpoint + tests.
3. Backfill the year of history.
4. Cut over leaderboards to read from `production_daily`. Verify correctness against the old path. Remove old path.
5. Cut over player card stats. Verify. Remove old path.
6. Cut over trophies/awards. Verify. Remove old path.
7. Cut over Value Streams / Recycling. Verify. Remove old path.
8. Add live warmer daemon + cache reads on `/staffing/{today}`. Verify. Remove old path.
9. Cut over `/api/late-report`. Verify. Remove old path.

Each cut-over step is independently revertible (revert the route file, leave the table in place). Verification between steps is a side-by-side diff against the old path on a real backfilled DB.

## Out of scope

- HTMX / SPA-style navigation between sub-tabs. Page rendering is no longer the dominant cost once data fetch is free; revisit only if needed.
- Sub-minute freshness on the daily-OK pages. Editor UIs surface the "updates tomorrow" hint; a manual `?from=…&to=…` re-run is the escape hatch.
- DB query indexing beyond the indexes specified above. If a query plan is slow after cutover, add indexes then.
- Replacing the existing in-process `_cache.py` and `_http_cache.py`. They remain in place and continue to serve any path not covered here.
