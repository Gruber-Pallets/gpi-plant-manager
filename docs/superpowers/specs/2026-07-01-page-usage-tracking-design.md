# Page-Usage Tracking — Design

**Date:** 2026-07-01
**Goal:** Measure which pages of the app actually get used over time, so dead
pages can be identified and retired. Answers: *what's used, how often, by how
many distinct people, and what is never hit at all.*

## Why not just read logs

Uvicorn access logging is on in prod, but `railway logs` only returns ~500
recent lines (no history), and it's dominated by TV-display polling. A 500-line
window can show what's active *right now* but cannot prove a page is unused.
This feature records usage into the app's own Postgres so the picture
accumulates over days/weeks.

## Data model

New table `page_views`, one row per (day, route, method, user), upserted:

| column        | type    | notes                                            |
|---------------|---------|--------------------------------------------------|
| `day`         | DATE    | plant-local day the views occurred               |
| `route`       | TEXT    | matched route **pattern**, e.g. `/staffing/people/{name}` — never the concrete URL |
| `method`      | TEXT    | `GET`, `POST`, …                                 |
| `user_email`  | TEXT    | logged-in user; `''` when unauthenticated        |
| `views`       | INTEGER | count for that (day, route, method, user)        |

Primary key: `(day, route, method, user_email)`.

- Storing the route **pattern** (not `/staffing/people/Juan`) is what makes
  aggregation meaningful.
- A row per user gives both **total views** (SUM) and **distinct users** (COUNT)
  exactly, and survives process restarts (upsert adds to the existing count).
- Volume is small: ~33 routes × ~15 users × 7 days ≈ a few thousand rows/week.
- Table self-creates on boot, mirroring `employee_notifications`.

## Capture

Fold the recording into the **existing** `_security_and_cache_headers`
middleware in `app.py` — no new middleware layer (the code comment there
explicitly warns against per-layer overhead).

After `call_next` returns:

1. If tracking disabled → return.
2. Resolve the matched route pattern from `request.scope["route"].path`.
   If no route matched (404) → skip.
3. Skip noise: `/static/*`, `/tv/ping`, `/healthz`, `/robots.txt`,
   `/favicon.ico`, `/auth/*`.
4. Read `user_email` from `request.session` (empty string if none).
5. Increment an **in-memory** counter keyed `(day, route, method, user_email)`.

No DB work happens on the request path.

Kill-switch: env `PAGE_VIEW_TRACKING_ENABLED` (default enabled; any of
`0/false/no/off` disables).

## Flush

The in-memory counter is drained to the DB in one batched upsert on the
**existing 60s warmer tick** in `app.py` — no new background job.

```sql
INSERT INTO page_views (day, route, method, user_email, views)
VALUES (...)
ON CONFLICT (day, route, method, user_email)
DO UPDATE SET views = page_views.views + EXCLUDED.views;
```

The drain swaps out the current counter dict atomically, then writes. If the
process crashes between flushes, up to ~60s of counts are lost — acceptable for
usage statistics.

## Performance

- Per request: one dict lookup + one counter increment. No I/O, no DB, no
  regex. Sub-microsecond; smaller than the header-setting already done per
  request. Zero new middleware layers.
- Memory: at most (routes × methods × active users) small entries between
  flushes, cleared every 60s.
- DB: one batched upsert per 60s on the warmer thread (never on a user
  request), borrowing a pooled connection briefly. The warmer already does
  heavier DB work each tick.
- **Explicitly avoids** the past connection-pool-exhaustion outage, whose cause
  was per-request DB fan-out. This design does zero DB work per request.

## Reporting — `/admin/page-usage`

New HTML page under the existing `/admin/*` diagnostics group.

- Window selectable; default last 7 days.
- **Used pages** table: route, total views, distinct users, last-seen day —
  sorted views-descending.
- **Never hit in this window** section: built by diffing observed routes
  against the app's live route table (`request.app.routes`), filtered to
  GET routes that render HTML pages (the user-facing pages). This is the
  dead-page list.

## Testing (TDD)

Pure, DB-free units (unit-tested directly):
- Counter: `record()` increments; `drain()` returns rows and empties.
- Route-pattern resolution from a request scope.
- Noise-exclusion predicate.
- Never-hit bucketing: given observed routes + full route inventory → correct
  "never hit" set.

DB-gated (skip locally without `DATABASE_URL`, run in CI):
- Upsert accumulates across two flushes.
- Query aggregation (views, distinct users, last-seen) over seeded rows.

## Files

- `src/zira_dashboard/page_views.py` — counter, table DDL/self-create, `record`,
  `drain`/`flush`, query helpers, route-inventory + never-hit helpers.
- `src/zira_dashboard/app.py` — call `page_views.record(...)` in the existing
  middleware; call `page_views.flush()` on the warmer tick.
- `src/zira_dashboard/routes/admin.py` — add `GET /admin/page-usage`.
- `src/zira_dashboard/templates/admin_page_usage.html` — report page.
- `tests/test_page_views.py` — the unit + DB-gated tests above.

## Out of scope (YAGNI)

- Per-visit event trail / exact timestamps (chose daily counts).
- Charts/graphs — a ranked table is enough to decide what to retire.
- Tracking static-asset or TV-ping traffic.
