# Codebase cleanup refactor — design

**Date:** 2026-07-06
**Branch:** `refactor/codebase-cleanup` (off `origin/main`)
**Goal:** Behavior-preserving cleanup: remove duplication, modernize idioms, apply
small performance fixes. **No functionality or UI changes.** Every step keeps the
full test suite (1531 passed / 31 skipped baseline) green.

## Survey summary

Three parallel read-only audits (duplication, dead code, performance) plus a
manual verification pass found:

- **No dead code.** Modules, templates, static assets, and routes are all
  referenced. Nothing to delete. (Page-level usage pruning is a separate,
  data-gated effort — `/admin/page-usage` needs ~1 week of traffic first.)
- **The claimed "N+1" hotspots in `work_centers_store`/`leaderboards` are
  already fixed** — `effective()` routes through the cached bulk
  `_effective_map()`. Verified by reading the code; no change needed.
- **Real duplication:** seven `*_store` modules hand-roll the same
  RLock + module-global singleton-cache scaffolding; `routes/settings.py`
  re-implements `schedule_store._parse_time`; three modules repeat JSON-blob
  normalization.
- **Mechanical modernization:** 123 ruff-safe fixes (mostly `UP017`
  `timezone.utc` → `datetime.UTC`, quoted annotations, PEP 604 optionals).

## Changes (one commit each, tests after each)

### 1. `CachedSingleton` helper (new `src/zira_dashboard/_singleton.py`)

A small lock-guarded lazy cache class (`current() / set() / reload()`), adopted by:
`schedule_store`, `saturday_schedule_store`, `rounding_store`,
`rounding_system_store`, `auto_lunch_settings`, `forklift_settings`,
`work_schedule_store`.

Each module keeps its **public API byte-identical** (`current()`, `save()`,
`reload()`, `get()`, …) and its own SQL/row-conversion; only the
lock/global-cache boilerplate is replaced. No test reads `._cache` directly
(verified), so behavior and tests are unaffected.

### 2. Utility dedup

- `routes/settings.py::_parse_hhmm` → reuse `schedule_store` time parsing
  (exported as `parse_hhmm`). Same outputs for all valid inputs.
- ~~Consolidate the repeated "JSONB may arrive as str or dict" normalization~~
  — **dropped after inspection**: `settings_store` already delegates to
  `app_settings`, and `layout_store` reads a different table with different
  fallbacks, so a shared helper would couple unrelated stores for ~4 lines.

### 3. Micro performance fixes

- `wc_dashboard_data.slug_for_wc`: precompile the regex at module level.
- `leaderboard.cached_leaderboard`: replace the O(n²) `any(m.meter_id …)`
  membership scan with a set.
- `odoo_client.py:315`: drop an unused `created_id =` assignment (F841).

### 4. Ruff-safe modernization

`ruff check --fix` for `UP017,UP035,UP037,UP045` (all marked safe-fixable;
`requires-python` guarantees 3.11+ so `datetime.UTC` etc. are valid). No
manual edits, no unsafe fixes.

## Explicitly out of scope (considered, rejected)

- **Route-render "framework" / render_page DSL** — over-abstraction; routes are
  readable as-is.
- **RowConverter / field-validation DSL** — per-store null-handling differs
  subtly; a DSL trades clarity for line count.
- **Cache-TTL tuning, broader memoization** — current TTLs are deliberate;
  staleness risk outweighs marginal wins.
- **Deleting "unused" pages** — page-usage data isn't trustworthy yet.
- **PERF401/SIM105 style rewrites** — churn without meaningful gain.

## Safety criteria

- Full suite green after every commit (local pgserver Postgres, same recipe as CI).
- `ruff check src tests scripts` (F rules, per CI) green after every commit.
- No template, static asset, or route signature changes → UI provably unchanged.
- Work isolated in `.worktrees/codebase-refactor`; lands via PR, never direct to main.
