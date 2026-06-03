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

Batched as one cleanup change. Behavior + UI identical. **✅ Shipped 2026-06-03 (commit `0ca0f9e`).**

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

- [x] **Extract `_footer.html`'s inline CSS/JS → `/static`** — shipped as `footer.css` + `footer.js` (cache-busted via `static_v`); 938 → 16 lines, verified byte-identical. Carried 3 global features (changelog modal, Assignments-to-Do, Late/Absence). Kept plain `<script src>` (not `defer`) to preserve execution order + the `window.gpiTransferToast` export. **✅ Shipped 2026-06-03.**
- [x] **Move `db.py`'s ~770-line `_SCHEMA_DDL` → `_schema.py`.** Shipped: db.py 918 → 149 lines (pool/query API unchanged); DDL kept as a Python constant `SCHEMA_DDL` (no packaging config needed); verified byte-for-byte identical to HEAD (34,286 bytes). **✅ Shipped 2026-06-03.**
- [x] **Unify the 10 background warmer loops** → one `_run_warmer(name, tick, interval)` + `_WARMERS` registry. Shipped: app.py 428 → 354 lines, all 10 cadences preserved, 3 structural tests updated. **✅ Shipped 2026-06-03.**
- [x] **Dedupe pure helpers:** `_fmt_hf` → shared `time_format.fmt_decimal_hour`, and `_parse_time` (`work_schedule_store` now imports `schedule_store`'s) — **✅ both shipped 2026-06-03**. `person_id_to_name()` (was rebuilt at 5 sites) → one `attendance.person_id_to_name(name_to_id=None)` helper, no extra queries — **✅ shipped 2026-06-03**. `unwrap_m2o` → one `odoo_client.unwrap_m2o()` (11 inline sites + `time_off_sync._unwrap_many2one`); left `odoo_sync._m2o_id` (distinct None-returning contract) — **✅ shipped 2026-06-03**. *(All dedupes done.)*
- [x] **Add `[tool.ruff]` config** — shipped with the pyflakes `F` ruleset (target py311) + `ruff` in the dev extra. Auto-cleaned 30 safe findings (23 unused imports, 6 redundant test re-imports, 1 unused test var, 1 f-string); reverted 1 (`timeclock.live_cache`, kept via `# noqa: F401` — the reconciliation tests patch it through the module). No CI/pre-push hook (would block the frequent pushes against pre-existing style debt). **F601 resolved:** removed the dead duplicate `"department"` key in `routes/settings.py` (kept `eff["department"]`, behavior-identical); `ruff check` is now clean across the repo. **✅ Shipped 2026-06-03.**
- [x] **Move more inline `<script>`/`<style>` → `/static`:** `index.html` `<style>` → `index.css` (verbatim), `leaderboards.html` 3 JS blocks → `leaderboards.js` (verbatim), `settings.html` ~580 JS lines → `settings.js` (`PROD_MIN` via `window` seam). All compile; behavior-identical. **✅ Shipped 2026-06-03.**
- [x] **Consolidate `settings_store` KV access** → new `app_settings.get_setting`/`set_setting` own the decode + `::jsonb` upsert; `settings_store` (incl. goal-target reads, int-coercion preserved) + `odoo_sync._write_last_sync` delegate. Left `odoo_sync._read_last_sync` (scalar-string decode the generic helper can't safely handle). Goal coercion verified preserved. **✅ Shipped 2026-06-03.**

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
