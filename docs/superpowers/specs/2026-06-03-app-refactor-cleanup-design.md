# App Refactor & Cleanup — Backlog & Operating Model

**Date:** 2026-06-03
**Goal:** Make GPI Plant Manager cleaner, simpler, and (where free) faster — **without changing functionality or UI.** "Tidy on principle," done incrementally on a live app under heavy daily development (~20 commits/day).

## Operating model

- **Incremental & behavior/UI-preserving.** Every change is a refactor: identical behavior, identical rendered output.
- **Approval-gated.** Each candidate is brought to Dale as **what it improves → why → the risk**, then executed once OK'd. Tier 0/1 may be batched; Tier 2 items go one at a time.
- **Verified & logged.** Each change is verified (tests and/or running the app) and gets a `CHANGELOG.md` entry before it ships.
- **Plans for the big ones.** Tier 2 decompositions get their own mini-plan + characterization tests first.

## Important correction (2026-06-03)

The JSON→Postgres migration is **complete**. Postgres is the single source of truth. The root JSON files (`roster.json`, `work_centers.json`, `schedule.json`, `layouts.json`, `settings.json`, `widget_customizations.json`, `schedules/`) are **gitignored, untracked, local-only leftovers** — not read by the running app, never shipped to Railway. There is **no split-brain and no ephemeral-FS data-loss risk** in persistence. The "JSON" smell is dead constants + stale comments pointing at unused files, not live dual-storage.
(`Plant Scheduler(Plant Scheduler).csv` IS still read — a read-only first-run assignment seed. Keep it.)

---

## Tier 0 — Pure cruft removal · zero/near-zero risk

Batched as one cleanup change. Behavior + UI identical.

- [ ] **Delete `requirements.txt`** — lists dead `weasyprint` (replaced by Playwright), contradicts `pyproject.toml` (the real source; Dockerfile uses `pip install .`), would mislead `pip install -r`. *Risk: Low.*
- [ ] **Drop unused `httpx`** dep from `pyproject.toml:23` — zero imports anywhere. *Risk: Low.*
- [ ] **Rewrite `README.md`** — still describes the old "Zira API Capability Probe" CLI, a different program. *Risk: Low (docs).*
- [ ] **Fix `.env.example`** — documents 2 of the app's ~22 env vars. Replace with the real set (DATABASE_URL, SESSION_SECRET, ODOO_*, ZIRA_*, MS_*, SLACK_*, KIOSK_*, etc.). *Risk: Low (example file).*
- [ ] **Delete dead `ROSTER_PATH`** (`staffing.py:114`) + fix stale docstrings/comments: `staffing.py:1-6` ("flat JSON files"), `staffing.py:281-282` (`schedule_time_off`, a dropped table), `settings.py:681` ("stored in work_centers.json"). *Risk: Low (dead constant + comments).*
- [ ] **Remove committed `kiosk_preview.html`** (134 KB generated artifact) + add to `.gitignore`. Keep `scripts/render_kiosk_preview.py` (its generator). *Risk: Low.*
- [ ] **Delete 3 spent migration scripts** — `scripts/migrate_json_to_postgres.py` (now *broken* vs current schema: references dropped `value_streams`/`schedule_time_off`), `scripts/migrate_schedule_names_to_odoo.py`, `scripts/seed_default_view_from_legacy_filter.py`. Keep `backfill_production_daily.py`, `probe_odoo_auth.py`, `render_kiosk_preview.py`. Not imported by `src/`. *Risk: Low.*
- [ ] **Remove dead cache tier** in `live_cache.py` — delete unused `write_timeoff`/`read_timeoff`/`write_production`/`read_production` (no runtime readers) + the `write_production(day, result)` call in `refresh_production` (keep the load-bearing `precompute_day` → `production_daily` side effect). Keep the (now-empty) tables to avoid a schema migration. Finishes a partially-done decommission (`refresh_timeoff` already gone). *Risk: Low (verified no readers; no tests exercise these fns).*

---

## Tier 1 — Low-risk structural tidies · mechanical, output identical

- [ ] **Extract `_footer.html`'s ~923 lines of inline changelog CSS/JS → `/static`** (`changelog.css` + `changelog.js`, cache-busted via `static_v`). Footer is 938 lines, 98% inline assets, zero Jinja, included on 6 pages. Biggest single cleanup; also makes it browser-cacheable. *Risk: Low.*
- [ ] **Move `db.py`'s ~770-line `_SCHEMA_DDL` → a `_schema.py`/`schema.sql`.** Shrinks `db.py` (918→~150) to its connection-pool facade. Keep the pool/query API unchanged (207 call sites). If using `.sql`, load via `importlib.resources` so it ships. *Risk: Low.*
- [ ] **Unify the 10 background warmer loops** behind one `_run_warmer(name, fn, interval)` + a registry in `app.py` (~130→~40 lines, no cadence change). *Risk: Low.*
- [ ] **Dedupe pure helpers:** `_fmt_hf` (byte-identical copy: `scheduler_time_off.py` & `routes/timeclock_time_off.py`) → `time_format.py`; `_parse_time` (`work_schedule_store` re-declares `schedule_store`'s); `unwrap_m2o` (~14 Odoo many2one sites in `odoo_client.py` + `odoo_sync._m2o_id` + `time_off_sync._unwrap_many2one`); `person_id_to_name()` (rebuilt at 5 sites). *Risk: Low–Medium (covered by existing tests).*
- [ ] **Add `[tool.ruff]` config** to `pyproject.toml` (target py311, explicit `select` E,F,I,UP,B) + add `ruff` to dev extra + a lint check. Lint-only first (no autofix churn). Would auto-catch future copy-paste/unused imports. *Risk: Low (config only).*
- [ ] **Move more inline `<script>`/`<style>` → `/static`:** `index.html` (161-line `<style>`, zero Jinja), `leaderboards.html` (~145 lines JS, zero Jinja), `settings.html` (~578 lines JS, one Jinja value `PROD_MIN`). *Risk: Low–Medium.*
- [ ] **Consolidate `settings_store` KV access** — `_read/_write` (int-dict) + `_read_raw/_write_raw` (JSON) + `odoo_sync._read_last_sync` all re-handle the JSONB decode quirk + dup the upsert SQL. Centralize `get_setting`/`set_setting`. *Risk: Low–Medium (touches goal-target reads — verify typed coercion).*

---

## Tier 2 — Bigger refactors · own mini-plan + characterization tests first

- [ ] **Decompose `routes/staffing.py` (1284)** — extract attendance/late-report helpers → `staffing_attendance.py`; extract the pure per-WC render-model builder → `build_staffing_bays()`. Leave `_LATE_REPORT_CACHE` in the route. Hottest-editing file → biggest merge-pain reduction. *Risk: Low–Medium.*
- [ ] **Decompose `routes/timeclock_time_off.py` (1310)** — extract the Who's-Out calendar engine → `time_off_calendar.py`; the `time_off_requests`/`leave_types_cache` data-access → `time_off_store.py` (dedupes the leave-types upsert shared with `time_off_sync.py`); optionally wizard validators → `time_off_wizard.py`. *Risk: Low (calendar) / Medium (store dedupe).*
- [ ] **Lift recycling goal-math out of `routes/departments.py` (953)** → `recycling_data.py` (`_recycling_day_data` + aggregation closures), kills the duplicated inline-assign popover block. **This is the goal-calc area behind recent prod fires — characterization tests FIRST.** *Risk: Medium.*
- [ ] **`odoo_client.py` (996) → package split** (`_transport`/`hr`/`attendance`/`leave`). **Medium–High risk:** module-level caches (`_leave_types_cache`, `_uid_cache`, `_wc_dept_id_cache`) are poked by name from `settings.py`/`timeclock_time_off.py` — re-export surface must be exact. Do last / cautiously, if at all. *Risk: Medium–High.*
- [ ] **Centralize Odoo datetime/date string parsing** (`_to_odoo_dt`, `_odoo_dt_to_iso`, date `[:10]` slicing scattered across 4 modules) into the canonical `odoo_client` helpers. *Risk: Medium (live punch/sync path).*
- [ ] **Extract a `SingletonCache` helper** for the 5 stores sharing the RLock+`_cache`+`current()`/`save()`/`reload()` boilerplate. *Risk: Medium (hot-path caching/threading — own PR, tests green).*

---

## Optional perf · opt-in only (app not reported slow)

- [ ] **Widen `_warm_zira_cache_loop` to the metered superset** so the 30s + 45s warmers stop double-fetching the same 7 recycling meters from Zira (also removes redundant `zira_daily_cache` UPSERTs). Conservative variant = widen station set, no loop deletion. *Risk: Low (conservative).*
- [ ] **Batch the per-employee Odoo balance sweep** (`time_off_balances.refresh_stale`) — 2 `search_read`s instead of ~2K per-employee round-trips. *Risk: Medium.*
- [ ] **Use the warm open-attendance cache in `odoo_client.transfer()`** to drop a redundant `get_current_attendance` round-trip on each kiosk transfer (keep live fallback). *Risk: Medium (write path).*

---

## Test safety-net (enabler for Tier 2)

- [ ] **Provision Postgres in CI** so the **35 of 102 DB-gated test files** actually run (currently silently skipped without `DATABASE_URL` → false comfort).
- [ ] **Add goal-pace characterization tests** before touching recycling/`wc_dashboard_data` math — lock today's numbers using the attendance/punch/attribution shapes from the June goal-regression commits.
- [ ] **Add 2 missing fixtures** so `test_time_off_routes.py` & `test_timeclock_dashboard_tile.py` assert instead of permanently skipping.

---

## Checked & deliberately left alone (don't chase)

- **Persistence:** no split-brain; `db.py` connection facade is clean (don't split the pool/query API). `*_store.py` `snapshot()` shims are format adapters over Postgres, not a second store.
- **Caching tiers** `_TODAY_CACHE` / `_RESPONSE_CACHE_TODAY` / `live_cache` are genuinely different layers — not redundant.
- **Per-thread `ServerProxy`/`requests.Session`** in Odoo/Zira clients are required for thread safety — don't "simplify" away.
- **`wc_dashboard_data.py`** is already the extracted, cohesive data layer — model, not split candidate.
- **Deps** `playwright` (Slack-PDF render), `authlib`, `itsdangerous`, `python-multipart`, `psycopg2-binary` are all live. `pyyaml` is needed only because `zira_probe` ships in the same wheel.
- **`zira_probe`** is dormant but its `client.py` is the live Zira client (`deps.py`, `leaderboard.py`). Don't bulk-delete; relocating `client.py` to retire the package is a separate, opt-in Tier 1+ move.
