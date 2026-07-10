# Test debt — DB-gated tests skipped in CI

When CI started running the `DATABASE_URL`-gated test suite against a fresh
Postgres (2026-06-03), **28 pre-existing tests** failed. They had **never run
in CI** and skip locally without `DATABASE_URL`, so this is latent debt the
safety-net surfaced — **none of it was caused by the 2026-06-03 refactoring**
(the new `recycling_data` / `staffing_view` / `time_off_*` tests all pass).

They're skipped via the `pytest_collection_modifyitems` hook in
`tests/conftest.py` (keyed on the node IDs below) so CI can gate the passing
suite + ruff on every push. **Burn-down:** fix a test, then remove its node ID
from the set in `conftest.py`. **17 remain** — the 11 TV / operator-dashboard
render entries were burned down on 2026-07-10 (see "Burned down" below). The
categories:

## 1. Hit the live Zira API (need Zira mocked) — 5
These render dashboards that call `leaderboard()` → Zira; with no real key they
get a 403. They were never written to mock Zira (because they never ran in CI).
- `tests/test_dashboards_polish.py::test_all_three_dashboard_pages_render_200`
- `tests/test_dashboards_polish.py::test_recycling_past_day_view_shows_assigned_names`
- `tests/test_dashboards_polish.py::test_recycling_renders_edit_controls_after_partial_extraction`
- `tests/test_dashboards_polish.py::test_top_nav_renamed_and_work_centers_dropped`
- `tests/test_dashboards_polish.py::test_work_centers_subnav_active_on_work_centers_page`

Likely fix: a fixture that seeds `zira_daily_cache`/`production_daily` or
monkeypatches `leaderboard` — as the burned-down TV/operator render tests now do
(patch `zira_dashboard.routes.departments.leaderboard`).

## 2. Need Odoo env / mocks — 3
`sync()` returns `error='Missing env vars: ODOO_URL, ODOO_DB, ODOO_LOGIN,
ODOO_API_KEY'`. Likely fix: set dummy `ODOO_*` in CI env (if the tests mock the
client) — verify their mocking, then un-skip.
- `tests/test_odoo_sync.py::test_sync_deactivates_employees_missing_from_odoo_response`
- `tests/test_odoo_sync.py::test_sync_force_refreshes_even_within_ttl`
- `tests/test_odoo_sync.py::test_sync_inserts_certification_at_level_3_regardless_of_bucket`

## 3. Stale signature — 3
Call `daily_records(client=…)` / `attribution_range(client=…)` / `attribution_per_day(client=…)`
— a `client` kwarg the function no longer accepts. Fix: update the test calls.
- `tests/test_production_history.py::test_daily_records_reads_from_production_daily`
- `tests/test_production_history.py::test_attribution_range_reads_from_production_daily`
- `tests/test_production_history.py::test_attribution_per_day_reads_from_production_daily`

## 4. Stale seed expectations — 0 (done)
The `tests/test_tv_displays_store.py::test_seed_defaults_*` pair now matches the
current seed defaults and was un-skipped on 2026-07-10 (see "Burned down").

## 5. Need seeded fixtures — 5
Insert/read data assuming seeded skills/people/work-centers. Fix: add fixtures.
- `tests/test_cert_lookup.py::test_load_person_certs_groups_certs_by_person`
- `tests/test_cert_lookup.py::test_load_person_certs_excludes_non_certification_skill_types`
- `tests/test_cert_lookup.py::test_load_person_certs_returns_alphabetical_within_person`
- `tests/test_cert_lookup.py::test_load_person_certs_ignores_level`
- `tests/test_work_centers_store_required_skills.py::test_row_present_with_required_skill_rows_returns_db_list`

## 6. Misc — 1
- `tests/test_settings_auto_lunch.py::test_post_clamps_flex_values` — `assert 30 == 0`; investigate clamp expectation against a fresh DB.

## Burned down

**2026-07-10 — TV / operator-dashboard render path (11 tests).** Every
per-work-center operator TV (`/tv/{slug}`, `kind='wc'`) 500'd plant-wide because
the dispatcher dropped the required `day` arg (fixed in #19). The render path
was uncovered precisely because these DB-gated tests were skipped here. They
were un-skipped after fixing stale assertions/stubs against the current
dashboard:
- `tests/test_wc_dashboard.py` (6) — stale `wc-*` widget IDs → current shared
  IDs; the 4-way KPI split → the single `kpi-pph` tile (Units / Up Time /
  Downtime were folded into the Pallets banner + Downtime widget); `_stub_wc`
  now stubs the render's real calls (`kpi_tiles` / `fifteen_min_progress_buckets`,
  not the retired `daily_progress` / `fifteen_min_increments`); and one
  `widget_customizer.load_all` stub returned the page string instead of a dict
  (the template does `customs.get(...)`). `test_operator_dashboard_has_four_split_kpi_widgets`
  was renamed to `test_operator_dashboard_has_single_pallets_per_hour_kpi`.
- `tests/test_tv_displays_routes.py` (2) — mock `departments.leaderboard` so the
  `vs_recycling` dispatch renders without a live Zira key.
- `tests/test_tv_dashboards_vs.py` (1) — dashboard title `"Recycling VS"` →
  `"Recycling"`.
- `tests/test_tv_displays_store.py` (2) — seed-default expectations already
  matched the current defaults; un-skipped as-is.
