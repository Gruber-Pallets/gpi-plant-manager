# StratusTime Decommission — Route Everything Through Odoo — Design

## Context

StratusTime (the old time-clock vendor) has been **fully turned off**. Clock in/out, WC transfers, and time-off requests already run through the Odoo-backed timeclock/kiosk stack. But several signals still call the now-dead `stratustime_client`, so they silently degrade to empty (errors are swallowed) — and one of them is actively losing data.

**Live StratusTime call sites still in the code:**

| Surface | Call site | What it feeds |
|---|---|---|
| Late/Absence report | `routes/staffing.py` `_safe_attendance` → `name_to_emp_id_map()`, `_attendance_with_fallback` → `attendance_for_day()` (via `live_cache.refresh_attendance`) | who's late / no-show, punch status |
| Recycling/New TV dashboards + admin dashboard | `routes/departments.py:122,748`, `routes/admin.py:161` → `full_day_absent_names_for_day()` | who's absent → pulled out of pph/man-hours |
| Staffing man-hours math | `staffing.py:472` → `partial_off_intervals_for_day()` | subtract partial-day off time |
| **Production write path** | `precompute.py:101` → `name_to_emp_id_map()`, `precompute.py:43` drops rows with no StratusTime id | writes `production_daily` (MTD/range totals, player-card stats, trophies) |
| Settings → Integrations | `routes/settings.py:108` → `health_check()`, `templates/settings.html:257` | "StratusTime" status panel |
| Background warmers | `app.py` `_warm_stratustime_loop`, `_prewarm_stratustime`, `live_cache.refresh_timeoff` | now-dead warm loops |
| Debug/util endpoints | `routes/staffing.py` `/api/stratustime/refresh`, the `stratustime_*` debug branch (~1242–1305) | manual cache bust / diagnostics |

**Active data loss (highest priority):** `precompute.flatten_attribution` ([precompute.py:43](../../../src/zira_dashboard/precompute.py)) drops every production row whose person isn't in the StratusTime employee directory. With StratusTime off that directory is empty, so **no new `production_daily` rows have been written since the cutover.** Pre-cutover history is intact (rows already written, keyed by name); the live Zira-direct Recycling/New leaderboard is unaffected; but the precomputed store (MTD/range totals, player-card cumulative stats, trophy aggregates) is missing every day since the cutover. Recoverable by backfill.

**Already on Odoo, not touched by this work:** kiosk clock/transfer, time-off requests, the staffing scheduler's time-off bucket (`scheduler_time_off`), the admin `/staffing/time-off` calendar (Odoo-only; only stale comments remain), roster/skills/departments/balances (`odoo_sync`). Trophies/leaderboards/player cards/awards/GOAT do **not** import `stratustime_client` (verified) — they read Zira + local tables keyed by **name**.

## Goals

1. Restore every StratusTime-fed signal on Odoo with **no day-to-day behavior change**: late/absence report, dashboard "who's absent" + man-hours exclusion, partial-day off math.
2. Fix the production write path so it no longer depends on StratusTime, and **backfill** the missing `production_daily` days.
3. Re-key the absence/late subsystem from StratusTime `emp_id` to the Odoo person identity; **migrate existing history by name** so employee cards keep their pre-cutover records.
4. Delete `stratustime_client.py` and all dead StratusTime code, warmers, endpoints, env references, and the Settings panel.
5. Preserve the late-report thresholds exactly: **7-min** grace (on-time vs late), **15-min** late-report threshold, **30-min** no-show buffer.

## Non-goals

- **Punch-rounding integration with the late report.** "Late" is judged on the **actual** clock-in time, not the payroll-rounded time. (Consistent with the deferral in the punch-rounding plan.)
- Redesigning the late-report UI, the scheduler, or the dashboards. Same screens, same flows — only the data source changes.
- New time-off / attendance features. Time-off is already shipped.
- Changing `production_daily`'s schema or read paths (all reads stay by name).

## Architecture

**New Odoo attendance source** replaces four StratusTime functions:

- `odoo_client.fetch_attendances_for_day(day)` → queries `hr.attendance` for punches whose `check_in` falls within `day` (site-local day → UTC bounds), open **and** closed. Returns `{person_odoo_id: earliest_check_in_iso}`. The one Odoo read that doesn't exist yet.
- New module **`attendance.py`** — the Odoo-era home for the attendance/absence logic that lived in `stratustime_client`. Pure cores take injected punch dicts + a fixed clock so they're testable without mocking time:
  - `compute_status(punches, ids, now_local, shift_start_local, grace=7)` → `{str(person_id): {status, minutes_late}}` where `status ∈ {no_punch, late, on_time}` — **the exact shape `late_report.py` already consumes.** `no_punch` = no check-in; `late` = check-in after `shift_start + grace`; else `on_time`.
  - `derived_absent_ids(punches, scheduled_ids, now_local, shift_start_local, buffer=30)` → scheduled ids with no punch once `now > shift_start + buffer`.
  - `status_for_day(day, ids, now_local, shift_start_local)` / `punches_for_day(day)` → cache-backed wrappers over `compute_status` / `fetch_attendances_for_day`.
  - `full_day_absent_names(day)` → `scheduler_time_off.full_day_off_names(day)` ∪ `late_report.absent_names_for_day(day)` ∪ {name for derived no-shows}. Returns **names** (dashboards key on name).
  - `partial_off_intervals(day)` → `{roster_name: [(start, end), …]}` from `time_off_requests` partial shapes, matching the shape `staffing.effective_minutes_worked` already expects (implementer matches the existing consumer around `staffing.py:440–490`).

**Identity:** the live late-report flow keys on `str(person_odoo_id)` end-to-end (status dict, `absent_ids`, `snoozed_ids`, `scheduled_ids`). Because `late_report.py`'s pure functions already string-coerce their ids (`{str(e) for e in …}`), feeding them `str(person_odoo_id)` requires **no logic change** to those functions — only the data sources change. Name↔id resolution uses the `people` table (`people.name`, `people.odoo_id`), the same join `scheduler_time_off` and `time_off.py` already use.

**Performance:** unchanged pattern. The 45 s `live_cache` warmer keeps populating `today_attendance_cache`, just from Odoo instead of StratusTime. The route reads the cached punches and computes status against fresh `now`/`shift_start` (so `minutes_late` stays current), exactly as today.

## Part 1 — Odoo attendance source

- Add `fetch_attendances_for_day(day)` to `odoo_client.py` (search_read on `hr.attendance`, `check_in` within UTC day bounds, fields `employee_id`, `check_in`; reduce to earliest per employee). Stubbed-XML-RPC test like the other `odoo_client` reads.
- Add `attendance.py` with the functions above. Unit-test the pure cores with injected punch dicts + fixed clock, mirroring `tests/` coverage of `late_report`.

## Part 2 — Repoint consumers (Deploy 1)

- `live_cache.refresh_attendance` → pull from `attendance.punches_for_day` (Odoo), keyed by `person_odoo_id`. **Delete** `refresh_timeoff` (function + its warmer call in `_warm_live_cache_loop`) — `today_timeoff_cache` has no readers.
- `routes/staffing.py`:
  - `_attendance_with_fallback` → Odoo source; drop the StratusTime fallback lambda.
  - `_safe_attendance` → build `scheduled_ids`/`unscheduled_ids` as Odoo ids via `people` name→odoo_id (replaces `name_to_emp_id_map()`); `by_name` via odoo_id→name.
  - `_late_emp_ids` and the `"stratustime"` timing phase → re-label, same logic on the new dict.
- `routes/departments.py:122,748`, `routes/admin.py:161` → `attendance.full_day_absent_names(day)`.
- `staffing.py:472` → `attendance.partial_off_intervals(day)`.

## Part 3 — Production write-path fix + backfill (Deploy 1)

- `precompute.flatten_attribution` / `precompute_day`: resolve the person key from the **Odoo `people` table** (name→`odoo_id`), and **never drop a row** for a missing key — fall back to the person's name as the `production_daily.emp_id` value (the column is TEXT and every read is by name, so the value only needs to be a stable per-person string). This removes the StratusTime dependency without touching `production_daily`'s schema or reads.
- **Backfill:** for each day in the gap window, `DELETE FROM production_daily WHERE day = d` then `precompute_day(d, client)` — idempotent regardless of pre-existing rows, no double-count. Window covers cutover→today (Dale confirms the cutover date; a wider window is safe). Run as a one-off admin script/endpoint.
- Assumption: Zira retains production data for the backfill window.

## Part 4 — Re-key absence/late tables + history migration (Deploy 2)

Tables keyed by StratusTime `emp_id`: `manual_absences`, `late_snoozes`, `late_arrivals`, `cleared_non_work_shifts`.

- Migration (idempotent, in `db.bootstrap_schema`): `ALTER TABLE … RENAME COLUMN emp_id TO person_id` (PK follows the rename — no constraint rebuild), guarded via `information_schema` so re-running boot is a no-op. Then `UPDATE … SET person_id = p.odoo_id::text FROM people p WHERE p.name = <table>.name`. Rows whose name no longer matches keep their old value (history preserved, just not live-matched). Column stays TEXT and now holds `person_id = str(person_odoo_id)` — the same identity the rest of the live flow keys on.
- `late_report.py` data accessors (`absences_for_day`, `absent_emp_ids_for_day`, `active_snoozes`, `late_arrivals_for_day`, `declare_absent`, `snooze`, `save_late_arrival`, …) read/write `person_id`. **History reads (`absences_history_for_name`, `late_arrivals_history_for_name`) already query by name — unchanged.**
- Staffing JS + the declare-absent / snooze / save-late / clear endpoints pass `person_id` (= Odoo id) instead of `emp_id`.
- **Consolidate the "clear partial" mechanism:** remove the StratusTime-specific clear paths in `routes/staffing.py` (request-id `cleared_time_off` and emp-id `cleared_non_work_shifts`, ~lines 414–442) in favor of the name-based `cleared_partials_by_name` the Odoo scheduler already uses. Leave the legacy tables in place (no data loss); just stop reading/writing them from live paths.

## Part 5 — Decommission (Deploy 3)

- Delete `src/zira_dashboard/stratustime_client.py` and `tests/test_stratustime_client.py`.
- Remove `app.py` `_warm_stratustime_loop`, `_prewarm_stratustime`, and their task wiring. (`refresh_timeoff` already removed in Deploy 1.)
- Remove `routes/staffing.py` `/api/stratustime/refresh` and the StratusTime branch of the staffing debug endpoint (~1242–1305).
- Remove the Settings → Integrations "StratusTime" panel (`templates/settings.html:257`) and `health_check()` plumbing in `routes/settings.py`. No replacement — the Timeclock and Time-Off sync-health panels already cover Odoo.
- Remove `STRATUSTIME_*` env-var references and stale StratusTime comments across `db.py`, `staffing.py`, `late_report.py`, `routes/time_off.py`, `routes/timeclock.py`, `static/settings.css`, `templates/staffing.html`.
- Move any still-used pure helpers off the deleted client (`_fmt_time_range` / `_fmt_time_short` → a small format util or `attendance.py`).

## Edge cases

- **Odoo outage:** every Odoo read stays wrapped — the late report and dashboards degrade to empty panels, never a 500 (same pattern as `_safe_attendance` / `_safe_time_off_entries` today).
- **No Odoo punch but approved partial-day off:** person is excused, not late — preserved via the existing time-off exclusion before the no-punch check.
- **Name with no `people.odoo_id` match** (departed/renamed): re-key leaves the old value; live views skip them; name-keyed history still shows. Production backfill falls back to name as the key so the row is still written.
- **Overnight punch** (check-in yesterday, still open): out of scope — plant is day-shift; the day-bounded `check_in` filter is correct for the supported case.
- **Backfill double-count:** prevented by delete-then-recompute per day.

## Testing strategy

- `attendance.py` pure cores: unit tests with injected punch dicts + fixed `now`/`shift_start` covering no_punch / late / on_time, derived-absent buffer, and the full-day-absent union.
- `fetch_attendances_for_day`: stubbed-XML-RPC test (earliest-per-employee reduction, UTC day bounds).
- Re-key migration: test that name-join backfill sets `person_id` and that history-by-name is unchanged.
- `precompute`: test that a person missing from Odoo still produces a row (no silent drop).
- Suite can't run locally (Python 3.9, no FastAPI) — each deploy is scoped so the result is verifiable on the live dashboards.

## Done criteria

- No `import stratustime_client` anywhere; file and its test deleted.
- Late/Absence report shows real late + no-show people from Odoo punches.
- Recycling/New + admin dashboards exclude absent people from pph again.
- `production_daily` receives new rows daily; gap days backfilled.
- Employee cards show pre-cutover absence/late history (matched by name).
- Settings → Integrations no longer shows a StratusTime panel.

## Staging (three deploys)

1. **Deploy 1 — stop the bleeding + restore signals:** Part 1, Part 2, Part 3 (precompute fix + backfill).
2. **Deploy 2 — re-key + history:** Part 4.
3. **Deploy 3 — delete:** Part 5.

Each lands and auto-deploys independently so the numbers can be eyeballed on the live dashboards before the next stage.

## Open Questions

- **Cutover date** for the production backfill window (Dale to confirm; a wider window is safe).
