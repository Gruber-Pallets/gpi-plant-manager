# What's New

Latest updates to GPI Plant Manager. Newest first. Each day is split by deployment time so you can tell what shipped together.

## 2026-06-03

### 9:40 AM

- **`db.py` slimmed 918 → 149 lines — its 770-line schema DDL moved to `_schema.py` (Tier 1 refactor, byte-identical)** — `db.py` mixed a tight ~150-line connection-pool/query facade with a giant embedded `_SCHEMA_DDL` string (every `CREATE TABLE IF NOT EXISTS` plus a dozen historical guarded `DO`-block migrations). Moved that DDL verbatim into a new `_schema.py` as `SCHEMA_DDL`, which `db.bootstrap_schema()` now imports and executes exactly as before. Kept it as a Python constant (not a `.sql` file) so it always ships in the Railway build with zero packaging config. Verified **byte-for-byte identical** — the extracted DDL bytes match `HEAD` exactly (34,286 bytes), so the schema applied on every boot is unchanged. `db.py` now reads as the focused Postgres connection facade its docstring describes; the schema is a separate, greppable artifact. Full test suite green (524 passed).

### 9:29 AM

- **Footer partial slimmed 938 → 16 lines — its CSS/JS moved to cacheable `/static` (Tier 1 refactor, byte-identical)** — `_footer.html` is `{% include %}`d on every page (staffing, settings, recycling, the dashboards, and the whole `_staffing_base` family) and carried ~921 lines of inline `<style>` + `<script>` for three global features: the **"What's new" changelog modal**, the **"Assignments to Do" badge/modal**, and the **"Late/Absence Report" badge/modal**. Moved that CSS → `/static/footer.css` and JS → `/static/footer.js` (cache-busted via `static_v`), leaving the partial as just its 13 lines of markup + two asset tags. The extraction was mechanical and verified **byte-identical** (re-embedding the two files reproduces the original exactly), so rendered output and behavior are unchanged; the assets are now fetched once and cached across pages/navigations instead of re-sent inline on every page load. Kept a plain `<script src>` (not `defer`) at the same position so execution order is preserved — including the `window.gpiTransferToast` export that `recycling.html` / `new_dept.html` reuse inside their Save handlers. Full test suite green (524 passed).

### 9:11 AM

- **Repo cleanup pass (Tier 0 of an incremental refactor) — dead files, stale docs, and a dead cache tier removed; zero behavior/UI change** — first batch of a "tidy on principle" refactor (full ranked backlog in `docs/superpowers/specs/2026-06-03-app-refactor-cleanup-design.md`). Cleared cruft that accrued as the app grew: the **134 KB committed `kiosk_preview.html`** (a generated preview — now gitignored; regenerate with `scripts/render_kiosk_preview.py`), three **spent one-shot migration scripts** (`migrate_json_to_postgres.py` was also broken against the current schema), the orphaned **`requirements.txt`** (the build uses `pyproject.toml`; it still listed the long-removed WeasyPrint), and the unused **`httpx`** dependency. Rewrote the stale **`README.md`** (still described the old "Zira probe" CLI) and **`.env.example`** (documented 2 of ~22 env vars). Deleted the dead **`ROSTER_PATH`** constant and fixed comments/docstrings that still claimed state lives in JSON files — it's all Postgres now. Removed a **dead `live_cache` tier** (`today_production_cache` / `today_timeoff_cache` read/write helpers had no callers; the load-bearing `production_daily` UPSERT is untouched). Net ~2,250 fewer lines, no runtime path changed; full test suite green (524 passed, 207 DB-gated skipped locally).

### 8:34 AM

- **Recycling goals now follow the time clock for the whole day — fixes goals reading ~200% on days with auto-lunch** — a work center's goal is driven by when its operator is actually clocked in (per the schedule's productive hours, breaks/lunch removed). The bug: the goal read only the **morning** attendance record, because auto-lunch closes the morning record at lunch and opens a **fresh afternoon record** — and the goal source (the local kiosk punch mirror) was missing that afternoon record, so every scheduled station's goal truncated at lunch (~half a day → boards showed ~200%). Now the goal reads the **complete set of Odoo `hr.attendance` records** for the day — morning, post-lunch, and every mid-shift transfer — so a person clocked in all day gets a full-day goal, and a **mid-shift transfer moves the goal to the new work center automatically**. That also means a mid-day move to an *unscheduled* WC lights up its goal straight from the time clock; the manual **"↪ assign"** attribution stays as the fallback for production where someone worked a WC but forgot to transfer. New `odoo_client.fetch_attendance_intervals_for_day` (all records as `[check_in, check_out, wc]` intervals) + `timeclock_windows.attendance_windows_for_day` (stitches a person's records, inheriting the WC across an untagged split); `_recycling_day_data` now sources work segments from that instead of the punch-log mirror.

## 2026-06-02

### 3:04 PM

- **Fix: work-center goals dropped (everyone reading ~200%) — goals no longer shrink when an operator takes partial time-off** — the 2:07 PM change prorated each station's goal with `effective_minutes_worked`, which nets out an operator's partial leave. That was wrong for a goal: a station's target is a **pace** number (what the WC should have produced by now if it was running) and must not shrink because the person on it took a few hours off. On a day with time-off it dropped every affected Dismantler/Repair goal — e.g. a dismantler reading `584 / 284` (≈206%) when the real pace goal was ~500 (≈115%). Goals now prorate by **breaks-only** productive minutes over each segment's window (new `shift_config.productive_minutes_in_window`), exactly matching the long-standing `shift_elapsed_minutes` pace target — while still accruing a mid-day assignment (e.g. Dismantler 4) only from its own start. Man-hours / pph still net out time-off (that's the right question there); only the per-WC pace goal changed back.

### 2:38 PM

- **Settings → Timeclock reorganized into Schedules / Rules / Activity sub-tabs, with consistent autosave** — the Timeclock settings panel had grown into one long scroll that mixed editing with monitoring and saved inconsistently (the two schedules autosaved silently while Rounding, Auto-Lunch, and per-schedule rounding had explicit Save buttons). It's now three sub-tabs: **Schedules** (Company Schedule + Saturday Default), **Rules** (default + per-schedule **Rounding** shown as one paired IN/OUT card, plus **Auto-Lunch** below), and **Activity** (sync status, recent punches, schedule variances). Every editable field now saves the same way — the 600 ms-debounce "Saved" toast + page-header Undo/Redo the schedules already used — so the per-section Save buttons are gone (the "+ Add break" and per-schedule **Add a schedule** / **Remove** controls stay as buttons, since those add or remove whole rows). Helper text and heading spacing are unified for a cleaner read, and adding or removing a per-schedule override now lands you back on the Rules tab. Pure layout/organization — no field names, save endpoints, or the punch → Odoo `hr.attendance` sync path changed (locked by a new characterization test, `tests/test_settings_timeclock_layout.py`).

### 2:07 PM

- **Mid-day work-center assignments now show a live, prorated goal — fixes the blank goal on stations like Dismantler 4** — when you assign an operator to an unscheduled work center partway through the shift (the `↪ assign` button on `/tv/recycling`), that station used to show production with **no goal denominator** at all (e.g. Dismantler 4 read `113` with no `/ target`). Two reasons: (1) the assignment was saved with a *frozen* end time pinned to the last meter reading at the moment of assignment, and (2) the per-station goal was computed only from the **published schedule's** head-count × whole-shift elapsed hours — so a mid-day attribution, which isn't in the schedule, counted as zero people and produced a zero goal the dashboard then hid. Now a mid-day assignment is **open-ended** (no end time at creation) and stays open until the person clocks out, transfers, or is reassigned; and the goal is built from time-windowed **work segments** prorated from each assignment's **own start** through to its end/transfer/now, using the same productive-minutes math (breaks + partial time-off netted out) the rest of the app uses. Sources are **hybrid — kiosk punches first**: if an operator punched WC transfers on the kiosk those windows win; otherwise the open-ended manual assignment is the fallback, so it works across the floor today while the kiosk is still piloting. Testing-only windows (the no-credit carve-out below) never add to a goal. Side effect worth noting: a *scheduled* station's goal now also nets out that operator's breaks/partial leave, so it reads slightly lower (and more accurate) than the old head-count×elapsed number on days with breaks. New pure, unit-tested modules `assignment_windows.py` (segment resolver + per-WC expected) and `timeclock_windows.py` (punch→window derivation); `wc_time_attributions.end_utc` is now nullable; `/api/staffing/attribute` + the three assign popovers (`recycling.html`, `new_dept.html`, `_footer.html`) create assignments open-ended; `_recycling_day_data` now derives both the operator labels and the goal from segments.

### 1:13 PM

- **Attributing production to a person now moves them into the work center's department in Odoo automatically** — when you credit sensed production to someone in the "Assignments to Do" modal, the app no longer just records the attribution — it reflects a real department move. If that person is clocked into a *different* department (or has no open punch at all), their Odoo attendance is transferred/opened to the work center's department, effective at the **start of the window they worked** — clamped so the move can never be backdated before their actual check-in. You see it confirmed as a toast with an **Undo** if it was the wrong call.
- **"Assignments to Do" modal gains a Testing button for carving out test runs** — next to **Save** there's now a **Testing** button. Use it when part of a sensed run was machine testing rather than real output: it carves a testing time window out of the production, credits those units to **no one** (they're subtracted from the work center's total), and then lets you assign the operator who worked the *remainder* of the window. So a run that was half test, half production gets split correctly — the tester's units drop off the count and only the real production is attributed.

### 12:21 PM

- **GOAT celebration banner moved into the TV header — gives the boards back a full row** — the "NEW GOAT" record-break banner used to sit in its own full-width strip *below* the header on every TV dashboard, pushing the widgets down. It now rides in the **center of the header, between the work-center title and the operator name(s)** (so an operator board reads `Repair 1 · 🏆 NEW GOAT — Dismantlers … · Jesus Galindo`). On operator boards it's pinned to a single tight line, and the least-important detail (the prior record it beat) ellipsizes first if the operator names crowd it; on the department boards (`/tv/recycling`, `/tv/new`) there's no name block, so it shows in full. The screen/editor view is unchanged — the banner stays below the chrome there. Built as an optional center slot on the shared `tv_header` macro, filled via a `{% call %}` block, with a 3-column header grid in `tv-mode.css`; touches `_tv_header.html`, `tv-mode.css`, `wc_dashboard.html`, `recycling.html`, `new_dept.html`.

### 12:20 PM

- **TV dashboards survive deploys & blips — no more black "upstream error" flash** — the four live boards auto-refreshed via an unconditional `<meta http-equiv="refresh">` + `setTimeout(location.reload, 60000)`. A full reload re-requests the whole document, so a momentary backend gap (a deploy swap, a network hiccup) painted the edge's "upstream error" page and blacked the screen until the next reload. Replaced both with a shared guarded refresher (`static/tv-refresh.js`): every 60s it probes the current URL and only reloads on a real 2xx response; on a 5xx / redirect / network error it keeps the last good frame on screen and re-checks every 7s, so a brief outage is invisible on the floor and the board recovers on its own. Belt-and-suspenders with the health-gated deploys. Applied to `recycling.html`, `new_dept.html`, `wc_dashboard.html`, `index.html`; the TV integration tests now assert the meta is gone and `tv-refresh.js` is wired in.
- **Thread-safe Odoo + Zira HTTP clients — kills the `Request-sent` / `Idle` warmer errors** — `odoo_client` shared one module-level `xmlrpc.client.ServerProxy` across every caller, and `ZiraClient` shared one `requests.Session` across `leaderboard()`'s `ThreadPoolExecutor`. Both hold a single persistent connection and are NOT thread-safe, so concurrent warmer (`asyncio.to_thread`) + request threads corrupted the connection's state machine — surfacing as `CannotSendRequest`('Request-sent') / `ResponseNotReady`('Idle') (e.g. `refresh_odoo_open_attendance failed: Request-sent`). Each thread now gets its own proxy / session via `threading.local()` (the Zira session still carries the `X-API-Key` header). New tests assert per-thread isolation for both; full suite green.

### 12:11 PM

- **Late/Absence Report now skips anyone on a flexible schedule** — people whose Odoo **Schedule Type** is flexible have no fixed start time to be measured against, so flagging them as "late" was just noise. The report (and the matching red highlight on the scheduler) now apply **only to hourly people on a fixed schedule** — flexible-schedule folks are dropped from all three sections (scheduled-late, unscheduled-late, needs-reason) and no longer light up on the day view. This builds on the existing salaried-manager exclusion: a person shows up only if they're paid hourly **and** on a fixed schedule. Source of truth stays in Odoo (the same `is_flexible` flag the auto-lunch feature reads off each employee's Working Hours / Schedule Type) — move someone to a flexible schedule in Odoo and they drop off the report automatically, no setting to toggle here. The scheduler highlight and the Late/Absence modal now use one shared eligibility rule, so they can't drift out of sync.

### 10:45 AM

- **Zero-downtime deploys — TVs no longer flash "upstream error" on every push** — root-caused the report that the **Repair 1 + Repair 3** TV dashboards went black with an "upstream error". The string isn't ours (grep confirms the app never emits "upstream" outside one unrelated ERP comment) — it's **Railway's Envoy edge** response when the `web` container isn't answering. The service runs `numReplicas: 1` with `healthcheckPath` / `overlapSeconds` / `drainingSeconds` all unset, so every deploy is a **hard cutover**: the old container stops and the new one boots with a gap in between where no instance serves. During this morning's go-live push cycle (5 deploys between 08:13–10:31) each cutover opened a ~15–30s dead window; the TVs auto-refresh on a stagger, so whichever screens happened to reload *inside* a window (Repair 1 + 3 this time) rendered Envoy's "upstream error" while the rest kept their last-loaded frame. Fix: point Railway at the existing `/healthz` endpoint (`routes/api_layout.py` — returns `{"ok": true}`, dependency-free, already auth-exempt in `auth.py`) via `deploy.healthcheckPath` in `railway.json`. With a health check configured, Railway keeps the **old** deployment serving until the **new** one passes `/healthz`, so traffic never hits a dead window — and a future broken boot can no longer take the site down (the old container stays up if the new one never goes healthy). No application code changed. Known follow-ups (not in this deploy): (1) make the TV auto-refresh resilient — keep the last frame + retry on a failed fetch instead of navigating to the edge's 502 page; (2) `ZiraClient` (and the StratusTime/Odoo clients) share a single non-thread-safe `requests.Session` across `ThreadPoolExecutor` workers, which is the source of the `Request-sent` / `Idle` warmer errors in the logs.

## 2026-06-01

### 12:15 PM

- **Different work schedules can now have their own time-clock rounding** — punch rounding used to be a single plant-wide rule (the "Round To Schedule" window in Settings → Timeclock). Now a specific Odoo work schedule can carry its own rounding windows. Two-step setup: in Odoo, create/confirm the work schedule (e.g. a "Drivers" schedule at 5:45–2:30) and set it as those employees' Working Hours; then in **Settings → Timeclock → Per-schedule rounding**, add that schedule and enter its four windows. The hours come from Odoo (shown read-only); the rounding numbers are yours and behave exactly like the default. Drivers' example — 20 minutes before the start, nothing after, nothing at the end — means a 5:30 clock-in counts as 5:45, a 5:52 clock-in stays 5:52, and clock-out is always as-punched. Anyone not on a configured schedule keeps the plant default, so the rest of the floor is untouched, and the match is automatic from each person's Odoo Working Hours — move someone onto the drivers schedule in Odoo and they pick up the right rounding with no extra step here.

### 9:11 AM

- **"Who's Out" is now one tap from the kiosk home** — added a Who's Out shortcut on the main kiosk screen that opens the month calendar of approved time off without having to pick your name first. It's a public glance (just who's out and when, no personal details), served from a new `/timeclock/whos-out` route that reuses the same month grid as the per-person calendar.

### 9:10 AM

- **Settings → Roster Filter now separates Active and Inactive employees** — the roster filter used to be one flat list. It's now split into an **Active** section and a read-only **Inactive** section sourced automatically from Odoo's archived (`active = false`) flag — so people who've left the company are clearly separated, stay out of every live picker (scheduler, kiosk, assignment dropdowns), but keep all their historical stats and trophies. Every name in both sections now links to that person's employee card.

### 8:49 AM

- **Time clock now reconciles against Odoo's real attendance state — no more duplicate clock-ins** — the kiosk decides "clock-in vs clock-out" from its local punch log, which left a blind spot when attendance was edited directly in Odoo. If someone forgot to punch in and you added it by hand in Odoo, the kiosk used to still show "clock in" — and tapping it created a second open attendance (or a sync error). Now a background process (~every 30s) mirrors every open Odoo attendance into a local cache, and the punch screen reconciles against it, so the screen reflects punches added, closed, deleted, or edited in Odoo — all without adding any Odoo call to the tap, so the screen stays instant. Clock-in is now self-correcting: it adopts an already-open Odoo attendance instead of creating a duplicate.
- **Time off removed in Odoo now disappears here too** — when a leave is deleted in Odoo, the 60-second poller now hard-deletes the local mirror row (it used to soft-cancel and leave a lingering entry), so "My Requests" and the Who's Out calendar stay in step with Odoo.

### 8:22 AM

- **Who's Out calendar is easier to scan — color-coded and tap-to-expand** — entries are now color-coded by type: full-day off shows in blue, partial-day in amber. On a touchscreen you can tap a name to expand its full text in place (tap again to collapse), and days with more than four people out get a "Show all / Show less" toggle so a busy day doesn't overflow the cell.

### 8:18 AM

- **Time-off requests: the end date can't fall behind the start** — in the kiosk time-off wizard the end date now defaults to the start date you pick and is prevented from being set earlier than it, so a multi-day request can't be entered backwards.

## 2026-05-29

### 3:07 PM

- **Skills matrix + player cards now cached (and the matrix pre-warmed)** — these were the two staffing pages with no response cache, so they re-rendered from scratch on every load (the skills matrix is a 40 KB person×skill grid). The skills matrix now caches its render (invalidated on every roster/skill/view write and Odoo force-sync) and is pre-warmed every 5 min by a background loop. Player cards cache per person+range — today-inclusive ranges in the 60s bucket (busted by attribution/attendance/award edits), past-only ranges for 5 min. Second views are now instant; cold first views are cheaper thanks to the 1-hour roster cache.

### 2:43 PM

- **Staffing first-load is now warm, not cold** — the day-view and leaderboards already cache their rendered HTML, but the cache was populated lazily by the first visitor, who paid the full ~1.9s render (and again after every deploy / TTL expiry). A new background loop (`page_warmer.warm_once`, ticking every 45s and on boot) now pre-renders today's `/staffing` and default `/staffing/leaderboards` straight into the response cache, and the today cache TTL was raised 15s → 60s so it stays warm between ticks. First load — including the first after a Railway redeploy — now serves cached bytes in <1ms instead of re-rendering. Mutations still call `invalidate_today_cache()`, so saves appear immediately. The warmer calls the handlers directly (bypassing auth/middleware) via a minimal synthetic request and can never crash the app.

## 2026-05-28

### 11:30 AM

- **Balance panel label fix: stop calling unlimited-paid types "Unpaid"** — `kiosk_time_off.js` was hardcoded to say `"Unpaid · no balance required"` whenever a leave type had `requires_allocation = no` in Odoo. That assumption holds for the unpaid Custom Hours type (the partial-day default), but it's wrong for a paid type configured with no allocation cap — e.g., "Paid Time Off" set up for unlimited PTO. Both cases reach the same code branch since `requires_allocation = no` is the only signal we cache. Changed the copy to `"No allocation tracked"` which is accurate either way; the user already sees the type name in the dropdown above so they know what they picked. (If Dale wants per-employee PTO balance tracking, flip "Requires allocation = Yes" on the type in Odoo: Settings -> Time Off -> Configuration -> Leave Types -> Paid Time Off -> Allocation Requests section -> check the box, save, grant allocations to employees, click Refresh from Odoo in our Settings panel. Then this panel will show the actual remaining balance.)

### 11:15 AM

- **Removed the redundant "Default shift window" panel from Settings -> Time Off** — the Company Schedule (Settings -> Timeclock -> Company Schedule) already stores shift_start / shift_end / work_weekdays / breaks for the whole plant; having a second, partially-overlapping shift window in the Time Off panel was just two places to keep in sync. `_shift_window_for(person_odoo_id)` in `routes/kiosk_time_off.py` now falls back to `schedule_store.current()` instead of `settings_store.get_default_shift_hours()` — same lookup the schedule-warning JS already uses for `work_weekdays`. Removed the form block from `templates/settings.html`, dropped the `/api/settings/time-off/default-shift` POST handler, and dropped the `default_shift_start` / `default_shift_end` from the panel's render context. `settings_store.get_default_shift_hours()` / `set_default_shift_hours()` themselves stay defined (still tested) — they're now dead code but cheap to keep around if a per-feature override is ever wanted again. All 16 route + settings tests still pass; if you change the Company Schedule's shift_start/shift_end, the partial-day request validation picks it up immediately.

### 11:00 AM

- **Kiosk dashboard: content pinned to the top, no more vertical-center gap** — `.k-main` on `kiosk_dashboard.html` had `justify-content: center` which floated the status heading + buttons into the vertical middle of the viewport. On taller screens that left a big empty band between the name banner and the first thing you could tap. Switched to `justify-content: flex-start` + `padding-top: 2rem` so the action area sits right under the header, close to the thumb. Other kiosk pages (landing, mine, calendar) already used `flex-start`-style layouts so this brings the dashboard in line.

### 11:00 AM

- **Odoo public holidays now show on Who's Out + the admin Time Off calendar** — `_approved_by_day(start_d, end_d)` in `routes/kiosk_time_off.py` previously only read approved `hr.leave` rows; now it also merges company-wide public holidays from Odoo's `resource.calendar.leaves` (the rows with `resource_id=False` — "4th of July", "Christmas Day", etc.). New helper `odoo_client.fetch_public_holidays(start_d, end_d)` runs a `search_read` with the datetime-formatted overlap domain (`date_to >= start 00:00:00 AND date_from <= end 23:59:59` — the field is datetime, not date). Each holiday gets fanned out per-day into the existing `by_day` dict with `source: "holiday"` and a sentinel label so templates can style it. `kiosk_time_off_calendar.html` (Who's Out) renders holiday entries as bold red pills; `time_off.html` (admin calendar) gets a new `.off-pill.holiday` style (red background, white bold text) and the day/week/month branches each check `e.source == 'holiday'` first. `_odoo_time_off_by_day` in `routes/time_off.py` now preserves the source flag through so holidays flow into the admin view automatically. Odoo failures inside `_approved_by_day` swallow to an empty list so a transient outage on the holiday fetch doesn't blank the rest of the calendar.
- **Wizard warns when a requested date falls outside the plant's standard schedule** — Saturday on a Mon-Fri plant now triggers a yellow `#fef3c7` banner above the submit button, so accidental weekend picks get caught without hard-blocking. Not a block — an employee scheduled for weekend OT may legitimately need to request Saturday off. `request_details`, `mine_edit`, and both error-rerender paths in `request_submit` / `mine_edit_submit` now pass `work_weekdays = sorted(schedule_store.current().work_weekdays)` to the template; the value embeds into the JS as `window.__WORK_WEEKDAYS__`. `static/kiosk_time_off.js` gains a `dateIsWorkday(iso)` helper (converts JS `Date.getDay()` 0=Sun..6=Sat to Python 0=Mon..6=Sun) and `checkSchedule()` that runs at startup and on every date input change (called at the end of `recalc()`); for full_day shape both dates are checked, for partial-day only `dateFrom`. The recalc function now branches on `hasBalancePanel` (since partial-day shapes drop the balance panel entirely) so the panel-related DOM writes are gated and the schedule check still fires for both branches. Tests stub `schedule_store.current` so they stay DB-free.

- **Shape picker reflow — Full Day spans the top, partial-day cards line up underneath in chronological order** — Dale's call after seeing the live screen: the auto-fit grid was placing the four shape cards in a 2x2 that obscured the visual hierarchy (full-day is the common case; partial-day are the three exceptions). Switched to a fixed 3-column grid where the Full Day(s) Off card uses `grid-column: 1 / -1` to span all three columns of the top row (becomes the big blue anchor), then the three partial-day cards (Arriving Late → Out for Part of the Day → Leaving Early) line up underneath in chronological order — morning to midday to afternoon, matching how a shift reads left-to-right. Template-only change to `kiosk_time_off_request_shape.html`; same card styles, same routes, just a layout reflow.

### 10:30 AM

- **Full-day requests now accept Unpaid Time Off (and any other hour-unit type)** — `_fetch_visible_leave_types("full_day")` previously filtered to `request_unit IN ('day', 'half_day')`, which excluded the "Unpaid Time Off" type Dale has set up with Duration Type=Hours. Now full_day shows ALL active leave types (day, half_day, AND hour); partial-day shapes still filter to hour-unit only (those literally don't make sense against day-unit types). New helper `_type_request_unit(holiday_status_id)` looks up the unit from `leave_types_cache`. Both submit handlers (`request_submit` for new and `mine_edit_submit` for edits) now inject full-shift hour bounds when a full_day shape is used against an hour-unit type — without those, Odoo can't compute a duration for an hour-unit hr.leave. The hour bounds are `(shift_from, shift_to)` covering the entire work shift each day in the date range, so Odoo treats the leave as the full work window times N days. Day-unit types stay at `hour_from=hour_to=None` (existing behavior). JS in `kiosk_time_off.js` now reads `data-unit` from the selected type and switches the request-size display unit accordingly: full_day with day-unit shows "3 days", full_day with hour-unit shows "24 hours" (3 days × 8 shift-hours) — matches whatever Odoo's allocation tracks. Tests stub `_type_request_unit` to return "day" so the submit-handler tests don't need a real DB. All 10 route tests pass.

### 10:15 AM

- **Partial-day requests drop the balance panel entirely** — Dale crossed it out on the live screen. With Available always reading "Unpaid · no balance required", request size and remaining-after both at "—" forever (no allocation to subtract from), and submit always enabled, the panel was visual noise. Wrapped the balance panel in `{% if shape == "full_day" %}` so it only renders when there's actually a paid balance to track. JS in `kiosk_time_off.js` already short-circuits at startup when the balance DOM elements are missing, so no JS changes needed — for partial-day shapes, the recalc function never binds event listeners and the submit button stays enabled per the template's static `disabled` attribute logic. Full-day path unchanged.

### 10:05 AM

- **Two kiosk fixes Dale flagged after seeing the live screens** —

  **(1) Name banner now actually centered on the dashboard.** The header used `display: flex; justify-content: space-between` with three children (Done button, name, brand logo). Space-between puts the middle item between the side items, but only centers it visually when the sides have equal widths — and they don't (Done button ~100px, brand logo wider). Fix: absolute-position the name span with `left: 50%; top: 50%; transform: translate(-50%, -50%)` so it centers in the header independent of the side items' widths. Added `pointer-events: none` so taps fall through to whatever's underneath (irrelevant today since nothing overlaps, but cheap insurance). Added `position: relative` to the .k-header itself so the absolute positioning anchors correctly.

  **(2) Name-pick screen (and every kiosk page) now actually scrolls.** My earlier "fix" added `min-height: 0` + touch properties to `.k-main`, but the real bug was one level up: `body { display: flex; flex-direction: column }` was the flex container, but body's only direct child is `#kiosk-screen` (the htmx swap wrapper), not the header/main. So `.k-main { flex: 1 1 auto }` had no effect — its actual parent (`#kiosk-screen`) was a plain block element with no constrained height, growing to fit all content. The `overflow-y: auto` on `.k-main` couldn't engage because `.k-main` never overflowed its parent. Fix: make `#kiosk-screen` itself a flex column with `flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column`. Now body → #kiosk-screen → .k-header / .k-main is a proper flex chain top-to-bottom and the `.k-main`-level overflow scroll actually fires when content exceeds viewport. Affects every kiosk page since they all extend `kiosk_base.html`.

### 9:50 AM

- **Partial-day requests now auto-use the unpaid hour-unit type (no picker)** — Dale's call after seeing the wizard live: "for arriving late, leaving early, or out for part of the day, remove type and make it always the unpaid type. Only full days allow choosing different types." Implemented in three places: (1) `routes/kiosk_time_off.py` — the GET handlers (new-request `request_details` and edit `mine_edit`, plus both their 422-rerender error paths) now compute `partial_day_type = types[0] if (shape != "full_day" and types) else None` — the first hour-unit type returned by `_fetch_visible_leave_types(shape)` (which already filters to hour-unit for partial-day shapes). It passes through to the template as a dict (`{id, name, request_unit, requires_allocation}`). (2) `templates/kiosk_time_off_request_details.html` — the visible `<select name="holiday_status_id">` now only renders for `shape == "full_day"`. The three partial-day shapes get a hidden `<input name="holiday_status_id" value="{{ partial_day_type.id }}" data-requires-alloc="..." data-unit="...">` (still with `id="holiday-status-select"` so the JS finds it) plus a small read-only "Type: Custom Hours · Unpaid" label so the user knows what they're submitting. The empty-state warning branches too: full-day gets the "No day-unit leave types" copy; partial-day gets a new copy telling the admin to set up an hour-unit type with `requires_allocation=no` (e.g. "Custom Hours") in Odoo. (3) `static/kiosk_time_off.js` — the recalc function now branches on `typeSel.tagName`: SELECT path reads `.options[selectedIndex].dataset.requiresAlloc` (existing behavior); INPUT path reads `.dataset.requiresAlloc` directly (hidden inputs don't expose `.options`). Without this, partial-day requests would have defaulted to `requiresAlloc=true` and the JS would try to enforce a balance check on Unpaid hours — wrong. Submit button disabled-state also branches: full-day disables if no day-unit types loaded; partial-day disables if no hour-unit type loaded. All 10 `tests/test_time_off_routes.py` pass; no test changes needed because the existing happy-path test stubs ride through both branches.

### 9:25 AM

- **Kiosk wizard now falls back to Odoo direct when `leave_types_cache` is empty** — closes a gap that hit Dale right after flipping `KIOSK_TIME_OFF_ENABLED=1`: if the background poller hadn't successfully populated the local cache table yet (because of the auth issue surfaced by the Settings panel banner, or just because the first poller tick hadn't fired), the request wizard rendered with an empty `<select>` and the "No day-unit leave types available yet" warning. Now `_fetch_visible_leave_types(shape)` in `routes/kiosk_time_off.py` reads the cache first; on empty, calls `odoo_client.fetch_leave_types()` directly (busting the in-process 10-min cache first, same as the Settings refresh button) and opportunistically writes the result back into `leave_types_cache` so the next render is fast. If the Odoo call also fails, swallows the error with `_log.warning(..., exc_info=True)` and returns empty — the existing template warning copy still shows. Same defensive pattern the Settings panel uses, applied to the wizard so neither UI surface needs the other to work first. No template or test changes.

### 9:15 AM

- **Kiosk: name-pick screen + every other kiosk page now scrolls properly** — the classic flex+overflow bug. `.k-main` already had `overflow: auto` set, but because `body { display: flex; flex-direction: column }` was its parent and `.k-main` didn't have `min-height: 0`, the flex child's computed height grew to fit its content instead of being clamped to the available viewport — so the content overflowed the viewport without ever engaging the inner scroll. Fix is 5 lines in `kiosk_base.html`: `min-height: 0` (the actual fix); `overflow-y: auto` + `overflow-x: hidden` (more explicit than just `overflow: auto`); `-webkit-overflow-scrolling: touch` (legacy iOS momentum scroll); `touch-action: pan-y` (explicit vertical-swipe affordance on touch devices); `overscroll-behavior: contain` (don't bubble the scroll up to the locked body). Affects every kiosk page since they all extend `kiosk_base.html`: name-pick grid (`/kiosk`), pick-WC screen, dashboard, time-off landing/wizard/mine/calendar. No template changes needed elsewhere.

### 9:00 AM

- **Kiosk dashboard polish — Time Off button label + employee name banner** — three small UX tweaks Dale requested after seeing the live kiosk: (1) "Time Off" button is now "Time Off Request" — clearer what tapping it does. (2) Added two icons to the left of the label: 🏖️ (beach umbrella, for "vacation") + 📅 (calendar), so people can scan-recognize the button without reading. Icons are 1.5rem (24px) and sit inside the existing button, no layout shift. (3) Employee name in the top banner went from 1.5rem to 2.25rem (50% bigger) so it's actually readable across a workshop floor — matches Dale's note that the name should anchor the screen visually. All three are pure template tweaks to `kiosk_dashboard.html`; no route or test changes.

### 8:46 AM

- **fix(timeclock): error-class-specific Odoo error hints + diagnostic probe script** — when Settings -> Time Off can't load leave types, the red banner now picks its hint based on the exception class rather than always blaming `hr.leave.type` permissions, which was misleading for the actual issue Dale was hitting (an `OdooAuthError` raised by `common.authenticate(...)` returning False). Four branches: `OdooConfigError` -> check the four `ODOO_*` env vars in Railway; `OdooAuthError` -> verify `ODOO_API_KEY` matches a current non-revoked key in Odoo (Settings -> Users -> [user] -> Account Security) and `ODOO_LOGIN`/`ODOO_DB` are correct; `Fault` (XML-RPC fault after auth succeeded, the original assumed case) -> permission issue, give the API user the Time Off -> Officer/User group; anything else -> generic "check Railway logs" hint. Implementation captures `type(e).__name__` into `time_off_settings["odoo_error_class"]` in `routes/settings.py`; `templates/settings.html` `{% elif %}` chain picks the matching copy. Also new: `scripts/probe_odoo_auth.py` — a standalone diagnostic that loads `ODOO_*` from `.env`, runs `common.authenticate(...)`, prints the uid (or the False if rejected), then runs `hr.leave.type.search_read(...)` and prints the count + first few records, warning if `requires_allocation` comes back as a bool instead of a string (Odoo version drift). Distinct exit codes per failure stage (1 = missing env, 2 = transport, 3 = auth rejected, 4 = leave-type read failed, 0 = OK) so it's scriptable. Runnable as `python -m scripts.probe_odoo_auth` from the project root. All 44 selected `time_off`/`settings` tests pass.

### 8:30 AM

- **fix(timeclock): Refresh from Odoo now actually refreshes** — the **Refresh from Odoo now** button on Settings → Time Off was a no-op even when there was nothing to refresh — the panel kept saying "No leave types loaded from Odoo yet" because the in-process 10-min cache in `odoo_client._leave_types_cache` held an empty list from an earlier silent XML-RPC failure, and the Refresh button didn't bust it. Four fixes: (1) the refresh handler in `routes/settings.py` now resets `_leave_types_cache = None` before calling `time_off_sync.poll_odoo_leaves()`, guaranteeing the next fetch actually hits Odoo. (2) The Settings panel GET now reads from the `leave_types_cache` table (the canonical source populated by the 60s poller) instead of the in-process cache, so the panel mirrors what the kiosk picker sees and stays usable during an Odoo outage; it falls back to a direct `fetch_leave_types()` call only when the table is empty. (3) Exceptions in both the settings GET and the poller's leave-types refresh now log at `warning` with `exc_info=True` instead of being silently swallowed — Railway logs will actually show *why* the fetch failed (e.g., the API user lacking `hr.leave.type` read perm). (4) A new red error banner on the Settings panel shows the Odoo exception text + a hint about granting the API user the Time Off → Officer/User group. New test in `tests/test_odoo_client_leaves.py` pins the cache-reset contract. All 68 time_off/odoo_client tests pass.

## 2026-05-27

### 11:00 AM

- **Timeclock Time Off — Phase A is shipped (behind `KIOSK_TIME_OFF_ENABLED=1`)** — employees can now request time off from the kiosk without going through StratusTime. Spec: `docs/superpowers/specs/2026-05-27-timeclock-time-off-design.md`. Flip the flag on and your crew gets a Time Off tile on the kiosk with four request shapes (full day, arriving late, leaving early, midday gap), a live balance check that disables submit when you'd go negative, a My Requests list (with cancel for kiosk-originated requests), and a privacy-safe Who's Out month calendar. A Settings → Time Off panel lets you toggle the StratusTime overlay, watch sync health, and force a balances refresh. The admin `/staffing/time-off` calendar now reads from the local Odoo mirror as its source of truth, with StratusTime entries appearing as a fading overlay (badged `ST`) so you can spot drift during the cutover. Flag ships **OFF by default** — flip `KIOSK_TIME_OFF_ENABLED=1` in Railway when you're ready to pilot.

### 10:39 AM

- **Timeclock Time Off: `/staffing/time-off` reads Odoo, StratusTime is now the overlay (Task 21)** — admin calendar's source of truth flipped from StratusTime to the local Odoo mirror. `_time_off_by_day(start_d, end_d)` now reads `time_off_requests` (state='validate', joined to `people` by `odoo_id`) via `kiosk_time_off._approved_by_day` — same SQL the kiosk "Who's Out" calendar already uses, so the two surfaces can never drift. If `time_off.show_stratustime_overlay` is on (default TRUE during the pilot), StratusTime entries appear alongside Odoo entries with a fade + `ST` source badge so the admin can spot drift; toggle the overlay off in Settings → Time Off once Odoo is the system of record. Each entry carries a `source` flag (`'odoo'` or `'stratustime'`) so the template renders them distinctly: Odoo entries use the new privacy-safe `{name, label, source}` shape with a green "odoo" pill (and a shorter label like "arrives 9:00am" / "leaves 2:00pm" / "10:00am–12:00pm" for partial-day shapes); StratusTime entries keep their existing rich shape (`hours`, `pay_type`, `derived`, `manual_absent`, `time_range`) so the existing partial/absent pill logic still works for them. New helper `_odoo_time_off_by_day` wraps `_approved_by_day` and tags entries; new helper `_stratustime_overlay_by_day` fetches StratusTime + tags entries + falls back to the per-day path on bulk-fetch failure. New header banner replaces the "All time-off entries are live from StratusTime" copy with a source indicator (`Showing: [Odoo pill] + [StratusTime overlay pill]` when overlay is on) and points at the Settings → Time Off panel for the toggle. The StratusTime deeplink + Refresh buttons only render when the overlay is on (no point opening StratusTime when we're not reading from it anymore). Footer hint updated to explain the new sourcing. Full test suite stays green (350 passed, 140 skipped).

### 10:34 AM

- **Timeclock Time Off: Who's Out calendar (Task 20)** — the third action on the time-off landing (**Who's Out**) now lands somewhere instead of a 404. `GET /kiosk/time-off/calendar/{token}` renders a month-grid calendar of approved leaves for the current month, padded to full Monday-first weeks so the layout stays a clean 7-column grid (leading/trailing days from adjacent months are flagged `outside` and fade to a lighter shade). Each cell shows the day number, a list of up to 4 people out that day with a privacy-safe timing label (no leave type — coworkers see *who* and *when*, not *why*), and a "+N more" footer when a day has more than 4 entries. Today's cell gets a blue outline + blue background so it's the first thing the eye lands on. Three new helpers on `routes/kiosk_time_off.py` keep the route lean: `_approved_by_day(start_d, end_d)` queries `time_off_requests` joined to `people` for `state='validate'` rows overlapping the date range and fans out each request to its individual day cells (so the template loop doesn't have to do date math); `_label_for(r)` renders the timing label per shape — `"full day"` for full_day, `"arrives 9:00am"` for late_arrival (arrival = hour_to), `"leaves 2:00pm"` for early_leave (leave = hour_from), `"10:00am–12:00pm"` for midday_gap; `_fmt_hf(h)` is the 12-hour clock formatter shared across labels (`6.5 -> "6:30am"`, `14.0 -> "2:00pm"`, `0.0 -> "12:00am"`). Bad/expired token bounces to `/kiosk` per the same auth pattern; the header back button returns to the time-off landing with a fresh-minted token. CSS adaptation: kept the table simple per the task spec — minimal inline styles on `<table>`/`<thead>`/`<tbody>` matching the existing kiosk visual weight (rounded corners, light-slate borders, light-amber pills for the leave entries), no new CSS classes invented. New template: `templates/kiosk_time_off_calendar.html`. Tests: `tests/test_time_off_routes.py` — added the happy-path test (stubs `_verify_token`/`_person_by_id`/`_approved_by_day`, asserts 200 + "Who" or "calendar" in body). Full time-off suite stays green (41 passed, 5 skipped where DATABASE_URL is required).

### 10:30 AM

- **Timeclock Time Off: My Requests list, detail, and cancel handler (Task 19)** — the **My Requests** action on the time-off landing now lands somewhere (was a 404). Three new routes on `routes/kiosk_time_off.py`: `GET /kiosk/time-off/mine/{token}` lists the newest 100 requests for the calling employee (joined to `leave_types_cache` for friendly type names, bucketed Pending/Approved/Rejected with color-coded pills); `GET /kiosk/time-off/mine/{token}/{rid}` shows the detail view (dates, hours when partial-day, shape, note, sync error if any, with a big red **Cancel This Request** button when the row is kiosk-originated and non-terminal); `POST /kiosk/time-off/mine/{token}/{rid}/cancel` is the cancel handler. Four new helpers keep the routes lean: `_load_request(rid, person_odoo_id)` scopes the SELECT to the caller so a leaked URL with someone else's request id can't fall through to a render; `_set_row_state(rid, state)` flips a row's state and marks it `synced_to_odoo=FALSE` so the sweep picks it up; `_list_my_requests(person_odoo_id)` is the newest-first query with the type-name join; `_state_to_bucket(state)` maps raw `hr.leave.state` (plus our local `draft_*` states) to the one-word bucket the UI color-codes. Cancel logic branches on `odoo_leave_id`: when it's NULL (row never made it to Odoo, still in the initial draft/retry queue) we DELETE the local row outright — nothing on the Odoo side to refuse, and a stale draft would just be noise. When it's set, we flip the row to `draft_cancel` and queue a background `_queue_push` — the existing push routes through `time_off_sync._push_cancel` which calls `odoo_client.refuse_leave` (same action Odoo uses for both pending-cancel and approved-cancel). The local row stays in place so the sweep retries on failure and the user sees the row land in the Rejected bucket once the push completes. Bad/expired token bounces to `/kiosk` per the same auth pattern; a row id that doesn't exist (or belongs to a different person) bounces back to the list with a fresh token. CSS adaptation: the plan's invented `kiosk-time-off-mine` / `kiosk-request-list` / `kiosk-action-cancel` classes were dropped in favor of the existing `k-header`, `k-main`, `k-btn`, `k-btn.danger`, `k-warning` vocabulary from `kiosk_base.html`, with the list rows getting an inline-styled grid card layout (date+type on the left, bucket pill on the right) and the detail page getting an inline-styled `<dl>` for the field rows. New templates: `templates/kiosk_time_off_mine.html`, `templates/kiosk_time_off_mine_detail.html`. Tests: `tests/test_time_off_routes.py` — cancel-handler test (POST with stubbed `_verify_token`/`_person_by_id`/`_load_request`/`_set_row_state`/`_queue_push`, asserts `(42, "draft_cancel")` lands in updates and `[42]` lands in queued). Full suite stays green (349 passed, 153 skipped).

### 10:26 AM

- **Timeclock Time Off: request wizard submit handler (Task 18)** — the details form on step 2 now actually goes somewhere. `POST /kiosk/time-off/request/{token}/submit` validates the inputs against the employee's shift window, persists a draft `time_off_requests` row (state='draft', synced_to_odoo=FALSE), schedules an immediate background `time_off_sync.push_one(request_id)` for the first sync attempt, and renders a new success page. If the 60s sync fails, the existing `retry_unsynced_requests` sweep keeps retrying. Five helper functions on `routes/kiosk_time_off.py` keep the route lean: `_parse_time_to_float("HH:MM")` converts `<input type="time">` values to decimal-hour floats; `_shape_to_hour_bounds(shape, time_a, time_b, shift_from, shift_to)` does all the shape-specific validation and returns `(hour_from, hour_to, error_message)` — late_arrival validates the arrival is after shift start + within shift; early_leave validates the leave is within shift + before shift end; midday_gap validates both times are inside the shift with end > start; full_day skips time validation entirely; `_compute_working_hours_json(...)` returns the COMPLEMENT of the leave window (the ranges the employee is still working) as a list of `{from, to}` dicts for the JSONB column — None for full_day, used later by the scheduler cascade + kiosk calendar; `_insert_request_row(...)` does the actual INSERT inside a `db.cursor()` transaction with RETURNING id; `_queue_push(request_id)` is a thin pass-through to `time_off_sync.push_one` that exists as a standalone function so tests can monkeypatch it to capture queued ids. On validation failure the form re-renders with a 422 status and the error message in the existing `k-error` banner so the user sees what to fix without losing their place. New file: `templates/kiosk_time_off_success.html` — uses existing kiosk classes (`k-btn`, `k-header`, `k-main`) with a green checkmark, "Request Submitted" headline, the date range, and two big-touch buttons (Done → landing, My Requests → mine list). CSS adaptation: no new classes invented, matches Task 15-17's style. Tests: `tests/test_time_off_routes.py` — happy-path test (POST with full_day data, asserts inserted dict + queued list); error-path test (midday_gap with times outside the shift, asserts 422-or-redirect). Full suite stays green (348 passed, 153 skipped).

### 10:42 AM

- **Timeclock Time Off: request wizard step 2 — details form with live balance calc (Task 17)** — picking a shape on step 1 now lands on the details form instead of a 404. `GET /kiosk/time-off/request/{token}/details?shape=<shape>` renders a touch-friendly form that branches by shape: **full_day** asks for start/end dates, **late_arrival** asks for an arrival time within the shift window, **early_leave** asks for a leave time within the shift window, **midday_gap** asks for a leave + return time pair. A balance panel at the top of the form shows Available / This request / Remaining after, and a new `static/kiosk_time_off.js` keeps those numbers in sync as the user changes type, date(s), or time(s) — the submit button auto-disables when the request exceeds practical balance (skipped for `requires_allocation='no'` types like Custom Hours). Three new helpers on `routes/kiosk_time_off.py` keep the route lean: `_fetch_visible_leave_types(shape)` reads from the `leave_types_cache` table (populated by the Task 10/23 poller) and filters to the right unit per shape; `_refresh_and_load_balances(person_odoo_id)` does a synchronous ~200-500ms blocking refresh from Odoo before render so the panel starts fresh; `_shift_window_for(person_odoo_id)` reads the employee's `resource.calendar` shift bounds (falls back to `settings_store.get_default_shift_hours()`). Bad token bounces to `/kiosk`, bad shape value bounces back to the shape picker — never renders the form with an invalid shape. CSS adaptation: the plan's invented `kiosk-time-off-details` / `kiosk-field` / `kiosk-balance-panel` classes were dropped in favor of the existing `k-btn`, `k-header`, `k-main` patterns from `kiosk_base.html`, with the form fields and balance grid getting minimal inline styling that matches the existing kiosk visual weight (rounded corners, light-slate borders, 56px min-height for touch). If the cache is empty (poller hasn't run yet), the form renders an empty `<select>` with a yellow `k-warning` explaining the poller cadence, and the submit button starts disabled. Submit endpoint (`POST /kiosk/time-off/request/{token}/submit`) is not yet wired — that's Task 18. New files: `templates/kiosk_time_off_request_details.html`, `static/kiosk_time_off.js`. Tests: `tests/test_time_off_routes.py` — bad-token redirect, bad-shape redirect, and happy-path render (with stubbed helpers, no DB needed) all pass; full suite stays green (346 passed).

### 10:13 AM

- **Timeclock Time Off: request wizard step 1 — shape picker (Task 16)** — tap **Request Time Off** from the landing now lands on a four-card shape picker instead of a 404. `GET /kiosk/time-off/request/{token}` mints a fresh 60s HMAC token and renders four big-touch cards — **Full Day(s) Off**, **Arriving Late**, **Leaving Early**, **Out for Part of the Day** — each linking to `/kiosk/time-off/request/{token}/details?shape=<value>` (step 2, not yet wired). Bad/expired token bounces to `/kiosk` per the same auth pattern. The header back button reads **‹ Cancel** and returns to the time-off landing. Cards use the existing `k-btn` styling (primary for the most-common Full Day case, `.secondary` for the other three) — the plan's invented `kiosk-shape-card` class was dropped in favor of the existing kiosk button vocabulary so the visuals match the landing. New template: `templates/kiosk_time_off_request_shape.html`. Route appended to `routes/kiosk_time_off.py`. Tests: `tests/test_time_off_routes.py` — added bad-token redirect test for the picker (passes).

### 10:01 AM

- **Timeclock Time Off: landing route (Task 15)** — kiosk Time Off tile now lands somewhere. `GET /kiosk/time-off/{token}` (gated by the same 60s HMAC token as the rest of `/kiosk`) renders a touch-friendly page with three big actions: **Request Time Off**, **My Requests** (with an orange pill badge of total request count), and **Who's Out**. If any of this employee's recent submissions are stuck (`synced_to_odoo=FALSE AND sync_error IS NOT NULL`), a yellow `k-warning` banner explains and points at the 60s retry sweep. Reuses `_mint_token` / `_verify_token` / `_person_by_id` from `routes/kiosk.py` (no auth-helper drift). Subsequent links (`/kiosk/time-off/request/…`, `…/mine/…`, `…/calendar/…`) are placeholders today — the request wizard, mine list/detail, and calendar pages get appended by subsequent plan tasks. New file: `routes/kiosk_time_off.py`. New template: `templates/kiosk_time_off_landing.html`. Tests: `tests/test_time_off_routes.py` — bad-token redirect test passes; happy-path test stays skipped until the kiosk route suite gets a seeded-person fixture (Task 16 in the plan promises to wire it).

### 9:31 AM

- **Settings: Company Schedule folded into Timeclock too** — same move as the rounding regroup five minutes ago, now for the schedule form. Shift start/end, work days, breaks/cleanup all live inside the Timeclock section, between the "Open Kiosk Portal" buttons and the Rounding inputs. Sidebar no longer has a separate "Company Schedule" item. Autosave still works (the form keeps its `data-section="schedule"` attribute so it saves on every edit without a button). Save endpoint redirects to `?section=kiosk` for the no-JS fallback. Roster Filter and Integrations stay in the sidebar — they're used by other features too, not just the timeclock.

### 9:26 AM

- **Settings: rounding now lives under Timeclock** — moved the four rounding window inputs (IN-before/after, OUT-before/after) into the existing Timeclock section on `/settings` instead of being its own top-level sidebar item. Same form, same `POST /settings/rounding`, same behavior — Save Rounding now bounces back to `?section=kiosk&saved=1` and the "Saved." flash appears next to the button. Cleaner sidebar; rounding is a Timeclock setting so it belongs there.

### 9:11 AM

- **Timeclock: configurable punch rounding** — new "Rounding" section on `/settings` lets Dale set four window values (IN-before, IN-after, OUT-before, OUT-after, each clamped 0..60 minutes), modeled on StratusTime's "Round To Schedule" UI. Clock-in / clock-out punches that fall inside the window round to the scheduled `shift_start` / `shift_end` before being written to Odoo `hr.attendance`. Raw and rounded timestamps are both persisted on `kiosk_punches_log` for audit (new `rounded_at` column, idempotent ALTER at boot; rounding settings live in a new `rounding_settings` singleton table). Transfers are never rounded — only true day-boundary in/out punches. Ships disabled (all four values = 0, which is a no-op). Plumbing: pure `rounding.apply_rounding(...)` + `RoundingSettings` dataclass (14 unit tests covering DST + naive-datetime guard), `rounding_store` singleton with in-process cache (4 DB-gated tests), kiosk write path `_open_log_row` computes + persists `rounded_at` (wrapped in try/except so a rounding failure can never lose a punch), kiosk dashboard reads `COALESCE(rounded_at, occurred_at)` so "Clocked in at HH:MM" matches what Odoo shows, and `kiosk_sync` ships the rounded value through to `hr.attendance`. Server-side clamping in the settings POST handler ensures out-of-range values are coerced to 0..60 even if the client tries something silly.
- **Deferred:** late-report integration with rounded time. Today's `late_report.late_people_for_day` operates on a StratusTime-sourced attendance dict that doesn't know about kiosk punches. Plumbing rounded values through that path requires deciding whether to merge `kiosk_punches_log` into the attendance dict — that's a separate design conversation, to revisit closer to the StratusTime cutover.

### 8:54 AM

- fix(sync): treat Odoo Certifications as binary — single-level cert types in Odoo were bucketing to 0 and getting dropped by the sync, so cert lookups returned nothing. DOT mechanics now get the wrench badge; CDL drivers scheduled to Truck Driver now color green.
- fix(work-centers): allow a work center to have a blank required-skill list. Settings → uncheck all skills → save now persists empty, and the staffing page renders people there at a neutral pill (lvl-2) instead of red. Useful for WCs where skill matters less than presence.

### 8:30 AM

- **Timeclock rename — remaining files** — yesterday's 7:15 PM entry announced the Plant Kiosk → Timeclock rename, but only `settings.html` got pushed at the time (it rode along with the CSS-extraction commit). The other user-visible spots — the kiosk's browser tab title (`kiosk_base.html`), the "your punches haven't synced" warning pointer on the kiosk dashboard (`kiosk_dashboard.html`), the module docstring in `routes/kiosk.py`, the schema comment in `db.py`, and the standalone `kiosk_preview.html` + its generator script — are now in HEAD too. Pure label change, no behavior. Internal naming (`/kiosk` route, `kiosk_punches_log` table, `kiosk_*.html` templates) still untouched on purpose.

## 2026-05-26

### 8:00 PM

- **Refactor pass — code organization cleanup, no behavior change** — three targeted improvements that make the codebase easier to navigate without touching any user-visible behavior. **(1) Extracted 472 lines of inline `<style>` CSS from `templates/settings.html` to a new `static/settings.css`** — the Settings page was carrying its entire stylesheet inline (1438 → 966 lines in the template). The `_save_toast.html` shared-CSS partial that was Jinja-included inside the style block is now inlined directly into `settings.css` since it's pure CSS with no template vars. Cache-busting via the existing `static_v()` Jinja global means browsers re-fetch on edits. **(2) Extracted the `.autosave` indicator styles (~50 lines) from `templates/staffing.html` into the existing `static/staffing.css`** — the Jinja-rendered cert/goat badge CSS and `_save_toast` include have to stay in the inline `<style>` block (they reference template variables), but the pure-CSS autosave block belonged in the stylesheet. **(3) Deduplicated three near-identical "read live cache, refresh if stale, fall back to StratusTime" helpers in `routes/staffing.py`** — `_timeoff_entries_with_fallback`, `_attendance_with_fallback`, and `_timeoff_names_with_fallback` were each ~17 lines of the same cold-start safety valve with different read/refresh/fallback functions. Pulled the cache-or-fallback flow into a single `_live_or_fallback(day, *, read, refresh, fallback, transform)` helper; each caller now passes its three live_cache/stratustime functions plus a `transform` for the cached payload. The fallback returns the caller's final shape directly so it doesn't get transformed. ~30 lines removed; intent is clearer because the three calls now visibly do the same thing. **(4) Small dead-code cleanup**: removed an unused `goat = awards.goat(...)` assignment and its now-orphan `awards` import in `goat_watch.py:_finalize_yesterday_goats` (kept the explanatory comment about why `prior_record` is used instead), removed unused `cached_leaderboard` import in `routes/admin.py`, removed unused `typing.Iterable` import in `goat_watch.py`. Ruff F401/F811/F841 now clean across `src/`. **(5) Test suite fix**: `responses` (used by `tests/test_client.py`) was declared in `pyproject.toml` `[project.optional-dependencies].dev` but never installed in the `.venv`, so pytest couldn't even *collect* `test_client.py` — the suite was running with one whole test module silently broken. Installed it; full suite now collects + passes (284 passed, 138 skipped due to no DB/Odoo env). No commands or paths to update — drop-in.

### 7:15 PM

- **"Plant Kiosk" is now "Timeclock" in the UI** — Settings sidebar entry, Settings panel heading, the kiosk's browser tab title, and the "punches haven't synced" warning pointer all say "Timeclock" now. Internal naming kept as-is (route `/kiosk`, file `routes/kiosk.py`, template prefix `kiosk_*.html`, DB tables `kiosk_punches_log` / `kiosk_schedule_variances`) — those are internal identifiers, never user-visible, and renaming them would create churn without benefit. Older CHANGELOG entries that reference "Settings → Plant Kiosk" are left alone as historical record (that's what shipped at the time); going forward it's "Settings → Timeclock". Source-file docstring and the standalone `kiosk_preview.html` artifact (+ its generator script) updated to match.

### 6:50 PM

- **Truck Driver WC now requires the "CDL (Automatics) Certified" certification from Odoo** — was requiring a "Truck Driver" skill that didn't exist anywhere. Switched to an existing Odoo certification name so the staffing scheduler finds anyone with the cert and offers them as assignable. The `Odoo Certifications` skill type is already synced by `odoo_client.fetch_skill_columns_with_types()` alongside Production + Supervisor skills, so no new sync wiring needed. Make sure your drivers have the "CDL (Automatics) Certified" cert in Odoo Employees → their Skills tab, then run an Odoo sync (or wait for the hourly tick).

### 6:45 PM

- **New work center: "Truck Driver"** — added a 23rd WC (was 22) under a new "Transportation" bay, in the "Transportation" department. No Zira meter (drivers don't have per-pallet telemetry). `min_ops=1`, `max_ops=None` (unlimited drivers). The kiosk's WC→Odoo-department resolver matches "Transportation" against Odoo's "06 Transportation" via the existing ILIKE substring search, so drivers who punch in via the kiosk land in the right department bucket on Dale's hours-by-department report. `DEPARTMENT_ORDER` extended to include "Transportation" so it sorts correctly across staffing screens. **One follow-up if you want drivers assignable from the scheduler:** the WC's default required-skill is "Truck Driver" — that skill doesn't exist in Odoo yet, so the staffing/scheduler view won't find any operators with the skill. Either (1) add a "Truck Driver" skill in Odoo Settings → Employees → Skills → New Skill (under Production Skills) and assign it to your drivers, then sync; or (2) edit the WC in Settings → Work Centers and clear the required-skills checkbox so anyone can be assigned. The kiosk itself doesn't care — drivers can tap their name and pick Truck Driver regardless of skill assignment.

### 6:30 PM

- **Deep rename: "value stream" is fully gone — internal code, DB schema, route file, URLs, and TV brand names all now say "department"** — follow-up to the UI-only rename earlier today. Every internal identifier matches the user-facing language now. **DB migration:** idempotent `DO $$ ... $$` block in `bootstrap_schema()` runs once at boot, does `ALTER TABLE value_streams RENAME TO departments` and `ALTER TABLE work_centers RENAME COLUMN value_stream TO department`, then is a no-op on subsequent boots. Fresh installs land directly on the new schema. **Python rename:** `VALUE_STREAMS` → `DEPARTMENTS_FALLBACK`, `synced_value_streams()` → `synced_departments()`, `work_centers_store.value_stream(loc)` → `.department(loc)`, the `GROUP_KINDS` second element `"value_stream"` → `"department"`, and every SQL query referencing the old table/column names is updated. Effective() / snapshot() dict keys are now `"department"`. **Route file rename:** `routes/value_streams.py` → `routes/departments.py` (via `git mv` so history follows). app.py import + tv_displays.py imports updated. **URL rename:** `/new-vs` → `/new`, `/tv/new-vs` → `/tv/new`. The old paths stay mounted as 301 redirects (preserving the query string) so existing TV bookmarks and any external links keep working. **Template + static asset renames:** `templates/new_vs.html` → `new_dept.html`, `static/new_vs.css` → `new_dept.css`. **TV brand names:** the "Recycling VS" / "New VS" labels on TV headers and the dashboards subnav are now "Recycling" / "New" — they read as proper department names, not category abbreviations. The seed list for new installs (`tv_displays_store._SEED_LIST`) is updated to seed "Recycling" + "New"; existing seeded rows in Dale's DB still have the old names and can be renamed inline via Settings → TVs if desired. **Tests:** patch paths updated from `zira_dashboard.routes.value_streams.*` to `zira_dashboard.routes.departments.*`; URL assertions updated for the rename. Full test suite passes (277 / 415 — the 138 skipped require DB/Odoo env). **Form field names:** the Settings → Work Centers table's Department dropdown now POSTs `wc__{key}__department` (was `__value_stream`); the per-department goal-override field is now `group_override__department__{name}` (was `__value_stream__{name}`). **What's intentionally NOT renamed:** the `vs_recycling` / `vs_new` `kind` values in `tv_dashboard_templates` and `tv_displays` tables — those are stored data values, renaming them would require an UPDATE migration with possible breakage if any TVs are mid-fetch during the rollout. They're internal identifiers, never user-visible. The cache_key inside `_render_new_dept()` was renamed to `"new_dept"` to match.

### 5:45 PM

- **"Value Stream" is now called "Department" everywhere in the UI, and the list comes from Odoo** — the legacy hardcoded value streams (`New / Recycled / Transportation`) are replaced by a sync that pulls `hr.department` names from Odoo every hour via the existing `odoo_sync.sync()` cycle. Names have their leading numeric prefix stripped (`01 Recycled` → `Recycled`) so the dropdowns stay clean. New `odoo_client.fetch_departments()` does the fetch + clean; `odoo_sync.sync()` now upserts the cleaned names into the existing `value_streams` table (additive only — never deletes, so any WC pointing at a since-archived department still resolves in the picker). New `work_centers_store.synced_value_streams()` is the live read function — falls back to the hardcoded tuple on a fresh DB before first sync. Validation in `save_one()` now accepts any synced name instead of only the hardcoded three. UI labels updated across Settings (column header, sidebar heading, footnote), the Recycling / New TV dashboards (page titles, the "VALUE STREAMS · TODAY" crumb is now "DEPARTMENTS · TODAY", panel captions), and `_tv_header.html`. **Internal naming kept as `value_stream`** for now — DB columns, table names, Python variables, route URLs (`/api/value-streams/*`), and route file names are unchanged so existing tests, bookmarks, and stored data all continue to work. A deeper rename pass is a separate follow-up if it ever matters. **TV brand names "Recycling VS" and "New VS" are kept as-is** — they read as product names, not categorization, so renaming felt forced; happy to revisit if Dale wants them as "Recycling Department" / "New Department".

### 5:15 PM

- **Kiosk now tags each Odoo `hr.attendance` with a `department_id`** — without this, Dale's "Scorecard - Weekly Hours by Department" report bucketed kiosk punches as "unassigned" (or attributed them to the employee's home department, which is wrong when an employee transfers across departments mid-shift). New `odoo_client._department_id_for_wc(wc_name)` resolves a kiosk WC name → its `staffing.LOCATIONS.department` value ("Recycled" / "New" / "Supervisor" / "Maintenance") → an Odoo `hr.department.id` via a case-insensitive substring search on `hr.department.name` (so "Recycled" matches "01 Recycled" etc.). Resolved IDs are cached in-process keyed by WC name; cache survives only until the next restart, which is fine — Odoo department names don't change often and the lookup is O(1) after the first hit. `clock_in()` includes the resolved department in its payload whenever the new `ODOO_KIOSK_DEPARTMENT_FIELD` env var is set; default unset = no department write (safe for ongoing pilots that haven't added the field yet). Transfers naturally flow through this too since `transfer()` calls `clock_in()` for the new attendance row. **Required setup before this takes effect:** (1) add a Many2one field `x_kiosk_department_id` → `hr.department` on `hr.attendance` in Odoo Studio (Odoo's native `department_id` is read-only/related in most versions), (2) set `ODOO_KIOSK_DEPARTMENT_FIELD=x_kiosk_department_id` in Railway. Without these the kiosk still works exactly as today; new env var is opt-in. Memory + setup checklist updated.

### 4:45 PM

- **Kiosk dashboard now warns when your punches haven't synced to Odoo** — the perf refactor moved Odoo writes to the background, so if Odoo rejects a punch (bad credentials, employee not found, etc.) the user no longer saw the error in the moment. New `_sync_error_warning(person_odoo_id)` query on the dashboard render path counts unsynced rows for the current person that have a `sync_error` set (i.e. already tried at least once and failed). If any, a yellow banner at the top of the dashboard says "Heads up — N of your recent punches haven't synced to Odoo" with the latest error message, the timestamp of the last attempt, and a pointer to Settings → Plant Kiosk for the manager-side detail view. Pending-sync rows (no error yet, just queued) intentionally don't trigger the banner — those resolve within a second of the next request, so warning about them would be noise. Singular/plural grammar handled inline ("punch hasn't" vs "punches haven't"). Cross-platform-safe time format helper (`_fmt_short_dt`) added since Windows uses `%#` where POSIX uses `%-`.

### 4:30 PM

- **Plant Kiosk is ~4x faster — three changes that compound** — the kiosk was paying a ~200-500ms Odoo XML-RPC round-trip on every dashboard render plus another 200-1000ms blocking the user during clock-in / clock-out / transfer writes. Three changes remove all of that from the hot path: **(1) Read state from local DB, not Odoo.** New `_current_state(person_odoo_id)` helper in `routes/kiosk.py` reads the most recent `kiosk_punches_log` row — a ~5ms local SELECT — to determine is-clocked-in / current-WC / check-in time. The dashboard route no longer calls `odoo_client.get_current_attendance()`. Safe because the local DB is authoritative for kiosk users (Phase 0 = Dale only, Phase 1 = plant cutover with StratusTime gone). **(2) Move Odoo writes to FastAPI BackgroundTasks.** Clock-in / clock-out / transfer handlers now write the local `kiosk_punches_log` row, render the success page, and queue a background task that runs the Odoo XML-RPC write off the request path. The existing 60s sweep worker (`kiosk_sync.retry_unsynced_punches`) stays as a safety net. New `kiosk_sync.sync_one_by_id(log_id)` lets the route handlers trigger an immediate sync for the specific row instead of waiting up to 60s. FastAPI runs sync `def` background tasks in a threadpool so the XML-RPC call doesn't block the event loop. **(3) Drop full-page reloads — added htmx 1.9.12 (self-hosted, SRI pinned).** `kiosk_base.html` loads htmx from `/static/htmx-1.9.12.min.js`, wraps the content block in `#kiosk-screen`, and sets `hx-boost="true" hx-target="#kiosk-screen" hx-select="#kiosk-screen" hx-swap="outerHTML"` on the body. Every link/form submission now does an AJAX swap of just the screen content — no white flash, no base-layout re-parse. Subtle 120ms opacity fade smooths the swap so it feels intentional rather than choppy. Idle-timeout JS sits outside `#kiosk-screen` so swaps don't tear it down; it also re-arms on `htmx:afterSwap`. **Dead-code cleanup:** removed inline-Odoo helpers `_mark_log_synced` / `_mark_log_failed` from `routes/kiosk.py` (no longer used — the background sync path owns those updates now) and the `odoo_client` import from the route module. Updated the kiosk docstring + dashboard template (now reads `check_in_display` instead of `current_attendance.check_in`, dropped the unreachable `odoo_error` warning block since reads no longer touch Odoo).

### 4:00 PM

- **Plant Kiosk admin panel in Settings** — new sidebar entry `Settings → Plant Kiosk` that's the operator entry point for the kiosk pilot: a prominent "Open Kiosk Portal" button (opens `/kiosk` in a new tab) plus a live status panel: 7-day sync status (total punches, unsynced count, last successful sync timestamp, error count), recent punches table (last 50 with synced ✓ / pending ⏳ status), and schedule variances table showing every "I'm somewhere else" override (scheduled WC vs actual WC). Also lands the missing DB schema from Thursday's session — `kiosk_punches_log` and `kiosk_schedule_variances` tables — which were referenced by the kiosk routes but never made it into `db.py`, so the kiosk would have crashed on first punch. The PIN columns from that earlier stash were intentionally dropped: **the kiosk is name-pick only, no PIN, by design** (Dale's call — PINs add friction without meaningfully changing the trust model on the shop floor, and `/kiosk` is already gated behind the plant-manager session login). Kiosk source-file docstring and project memory both updated to remove the "PIN deferred to Phase 1" language.

### 11:41 AM

- **`(N-D4)` side-metric drops to the baseline + fades more** — was vertically centered against the headline number (looked like it was floating), now uses `vertical-align: baseline` so the small "(52.3-D4)" sits flush with the bottom of the big number. Also bumped `opacity: 0.55` to make it more clearly a footnote.

### 11:32 AM

- **pallets/hr/person KPI tweaks** — three small visual changes to the recycling KPI: (1) the main number now renders in the page's standard text color (`var(--fg)`) instead of green, so it stops fighting the colored progress bars below it; (2) the side-metric shrunk from `0.42em` → `0.32em` so it reads more clearly as a footnote next to the headline; (3) format changed from "(52.3 ex D4)" to the more compact "(52.3-D4)". Tooltip on the side-metric still explains what it means on hover.

### 11:18 AM

- **Widget title edits actually take effect on save** — editing a KPI/bar/progress widget's title (or color / alignment / etc.) wrote to Postgres correctly, but the reload that fires right after saving was getting served from the HTTP response cache (15s TTL on today, 5min on past ranges). The page came back with the old title and looked like the edit had been swallowed. `widget_customizer.save_one` invalidates its own per-page cache but didn't know about the response-bytes cache one layer up. New `_http_cache.invalidate_all_cache()` clears both today and past response buckets in one shot, and the `/api/widget/{page}/{widget_id}` save + delete handlers call it. Title/color/alignment edits and Resets now reflect immediately on the next reload.

### 11:05 AM

- **`pallets/hr/person` now shows an "ex D4" side-metric** — Dale's ERP-derived pph/person ran ~30% below the dashboard because Dismantler 4 reprocesses reject material that the ERP doesn't count as new throughput. The recycling dashboard's KPI now renders the standard number plus a smaller, dimmer "(52.3 ex D4)" beside it — same denominator (operator labor is still real) but the numerator drops Dismantler 4's pallets. Tooltip on the side-metric explains the rationale. Works across all ranges (today, week, month, custom) and on both `/recycling` and `/tv/recycling`.

### 10:38 AM

- **Time-off flash in Unscheduled — kill it at the deepest layer + sweep on load** — even after the prior fix, a brief flash of the unchecked name in Unscheduled could still happen if the page was loaded against stale data (the StratusTime cache hadn't picked up the new PTO yet at render time, so `time_off_names` came back without that person, but the Time Off section caught up before the next uncheck). Two more belts: (1) the `__timeOffNames.has(name)` guard moved one layer deeper — `addToUnscheduled` and `addToReserves` themselves now bail out for time-off people, so no caller can sneak past (current or future). (2) On page load, sweep the server-rendered Unscheduled and Reserves lists and remove anyone whose name appears in `__timeOffNames` — keeps the left rail consistent with the Time Off section without forcing a full reload when caches diverge.

### 10:20 AM

- **Time-off people stop appearing in Unscheduled (and stop being scheduleable)** — when you removed Jose Luis from a station today, his name showed up in BOTH the Time Off list AND the Unscheduled list. Two underlying issues, both fixed: (1) the JS time-off check (`removeFromCorrectList` and friends) was querying `.section.timeoff .pill[data-name="…"]`, but modern StratusTime-driven Time Off rows render as `<li class="time-off-row">` without a reliable `data-name`, so the selector always missed and the person fell through into Unscheduled. (2) `refreshPickerVisibility` only marked someone as taken-elsewhere when they were already in another WC's checked checkboxes or in the legacy `loc____time_off` form inputs (which we no longer write), so PTO people stayed visible in every WC's dropdown and could be re-checked. New `__timeOffNames` Set is rendered into the page from the existing `time_off_names` template var and is now the single source of truth for "is this person on time off today?" — used by `addBackToCorrectList` (belt-and-suspenders), both broken selectors, and `refreshPickerVisibility` (which now hides time-off people from every dropdown except a WC where they're currently selected, so you can still uncheck them).

### 9:48 AM

- **× on the Testing Day pill — clear the flag without rebuilding the schedule** — once `testing_day` got flipped to `1` (via the "Override (Training + Testing Day)" or "Override (Testing Day)" buttons on overstaffed/understaffed warnings) there was no UI to flip it back. The regular save path on a Posted schedule goes through `save_notes`, which deliberately preserves `testing_day`. Now the pill has a small × inside it that hits a dedicated `POST /api/staffing/clear-testing-day` endpoint — JSON-only, idempotent, doesn't touch assignments, notes, published state, or `custom_hours`. The handler also syncs the hidden form input so the next autosave doesn't re-add the flag.

### 9:12 AM

- **Time Off section now finds people with compound first names (Jose Luis fix)** — Jose Luis had approved PTO in StratusTime but didn't appear in the Time Off section. Root cause: Odoo's `_short_name` truncates "Jose Luis Hernandez Alvarez" to the roster name "Jose Luis", but StratusTime stores Hispanic compound given names entirely in `FirstName` (`FirstName="Jose Luis"`, `LastName="Hernandez Alvarez"`). `name_to_emp_id_map` split the roster name on whitespace and looked up `by_first["jose"]`, missing the `"jose luis"` key. The lookup failed, the entry fell back to the full StratusTime name ("Jose Luis Hernandez Alvarez"), and the active-roster final filter added by commit `e719b4f` (2026-05-13) silently dropped it because that string isn't in the local roster set. Added a new matching rule between the exact full-name check and the first-name+initial logic: if the whole roster name (lowercased) matches a StratusTime `FirstName` verbatim with a unique active candidate, take that. Existing rules (`Jesus M` → Martinez, `Porfirio Cazares` → `Cazares Herrera` via prefix) are untouched. Two regression tests cover the round-trip and the end-to-end time-off render.

### 7:37 AM

- **Late/Absence report: filter to hourly employees only** — was flagging Dale, Wendy, Ian, and other salaried/management folks every morning because they're active non-reserve people who don't have scheduled stations. The report now reads `wage_type` from Odoo `hr.employee` (selection: `hourly` / `monthly`) and only considers `hourly` people across all three sections (scheduled-late, unscheduled-late, needs-reason). Unknown wage_type is treated as non-hourly (safer default — better to miss flagging than to nag salaried folks). Plumbing: new `people.wage_type` column (idempotent ALTER); `odoo_client.fetch_employees` now requests `wage_type`; the Odoo→people sync writes it on every refresh; `staffing.Person` carries it through `load_roster`; the filter is applied at the `late_people_for_day_v2` call site in `routes/staffing.py` (not in `_safe_attendance`) so the rest of the staffing page is unaffected. If a salaried person's wage_type is wrong, fix it in Odoo — there's no per-person UI override.

## 2026-05-21

### 4:55 PM

- **NEW GOAT alerts now show on every dashboard (and every TV)** — celebration goes plant-wide. The banner that previously lived only on `/recycling` now also renders on `/new-vs`, `/wc/{slug}`, and their `/tv/*` counterparts (so every operator TV and every TV display lights up when someone breaks a record). Dismiss is global: hitting it on any TV clears the row from every screen. Live contenders (per-group projection) stay on `/recycling` only — they don't fit per-WC displays. Three pieces of plumbing: (1) banner CSS extracted to its own `static/goat_watch.css` so non-Recycling pages don't have to pull in `recycling.css`; (2) dismiss JS moved from `recycling.html` into the partial itself so any template including `_goat_watch_banner.html` gets the click handler for free; (3) auth middleware extended — `_tv_accessible_path()` now allows IP-allowlisted shop-floor TVs and device-token TVs to POST `/api/goat-alerts/{id}/dismiss` without a human session (dismissing a celebration row is benign, no security implication).

### 4:25 PM

- **GOAT Watch — fix gate timing + flatten NEW GOAT banner** — two fixes to last week's GOAT Watch banner on `/recycling`. (1) **Live contenders now appear after the *real* final break (~13:45), not at end-of-shift.** The previous logic took `max(b.end for b in breaks)` which picked the Cleanup window (15:15–15:30) — and Cleanup ends *at* shift_end (15:30), so the banner was gated until shift was already over. Cleanup is wind-down, not a break operators come back from, so `_final_break_passed()` now ignores breaks whose end equals shift_end. (2) **NEW GOAT alert collapsed to a single horizontal row.** Trophy, "NEW GOAT — GROUP", `Person · Units pallets at WC · Date`, the "Beat 318 (prior holder, date)" subtext, and Dismiss button all sit inline on one line via `display: flex; flex-wrap: wrap`. Was 3 stacked rows eating ~80px of vertical space; now ~24px on a wide viewport. Wraps gracefully on narrow widths.

### 4:00 PM

- **Plant kiosk — Phase 0 (Dale-only pilot)** — new `/kiosk` route ships a touch-first clock in/out + work-center transfer flow that writes directly to Odoo `hr.attendance`. **Replaces StratusTime for the pilot user only** (Dale); the rest of the plant continues on StratusTime until pilot validates. **Auth = name-pick only** — no PIN. The home screen is an alphabetical scrollable list with sticky search; tapping a name mints a 60-second HMAC session token and bounces straight to the dashboard. Scheduler-aware: at clock-in the kiosk surfaces today's scheduled WC ("Today you're on Repair 1 — Confirm?") with a smaller "I'm somewhere else" override; overrides log to `kiosk_schedule_variances` for later supervisor review. Mid-shift transfers split into 2 `hr.attendance` records via close-then-reopen. Offline-tolerant: every punch writes to `kiosk_punches_log` first then attempts the Odoo write; a 60s background worker (`kiosk_sync.py`) retries unsynced rows so an Odoo outage never blocks the kiosk. Branded header on every kiosk screen (Gruber Pallets, Inc. logo + wordmark). New tables: `kiosk_punches_log`, `kiosk_schedule_variances`. **Odoo prerequisites before this can actually punch**: (1) write-capable Odoo API user on `hr.attendance`, (2) custom `x_kiosk_workcenter_name` Char field added to `hr.attendance` (via Studio or a small module) — when present, set `ODOO_KIOSK_WC_FIELD=x_kiosk_workcenter_name`; without it the kiosk still writes attendance, just without the WC attribution. Phase 1 (plant-wide cutover, variance review UI) and Phase 2 (time-off requests via kiosk) ship later. The `people.pin_hash` / `failed_pin_count` / `pin_locked_until` columns are added by the schema bootstrap but unused — left in place so PIN auth can be re-enabled later without a migration.

## 2026-05-20

### 7:29 AM

- **Late/Absence report: Snooze now auto-closes the modal** — was leaving the modal open with the snoozed person dropped from the list but other late/absent rows still visible. The intent of Snooze is "out of my face for 30 min" so forcing the user to also dismiss the modal defeats the purpose. Other actions (Save reason, Declare Absent) still keep the modal open so you can deal with remaining people in one sitting; only Snooze closes immediately. `doAction()` gained a small `opts.alwaysClose` flag; the Snooze button passes it.

## 2026-05-18

### 2:45 PM

- **Auth: IP allowlist for `/tv/*` paths so shop-floor TVs don't need typed tokens** — the original device-token-in-URL design was unworkable for TVs operated by remote control (typing a 100-char signed token is a non-starter). Added a `TV_ALLOWED_IPS` env var (comma-separated single IPs or CIDR ranges); requests from a matching IP bypass auth on `/tv/*` paths only — `/recycling` and other editor views still require Microsoft sign-in regardless. The shop's public IP is stable enough for this to be zero-config: set the env var once in Railway, every TV on the shop network just works. Device tokens are still supported as a fallback for off-network displays. Six new tests cover happy path, CIDR matching, non-tv paths NOT bypassed by IP, env-var unset, and malformed entries.

### 2:30 PM

- **Fix for Internal Server Error on `/auth/login`** — two missing pieces blocking the OIDC handshake from working in production. (1) `httpx` was an unpinned optional dep of Authlib's Starlette integration — `ModuleNotFoundError: httpx` fired the moment `/auth/login` tried to construct the OAuth client; pinned it explicitly in `requirements.txt` + `pyproject.toml`. (2) Added Starlette's `SessionMiddleware` to `app.py` — Authlib's `authorize_redirect()` stores the OIDC state nonce in `request.session` for CSRF validation on the callback, and without SessionMiddleware that access raises `AssertionError`. Re-uses `SESSION_SECRET` for cookie signing rather than introducing another env var.

### 2:21 PM

- **Auth: production login fixes + minor hardening before Sub-phase 2C cutover** — three issues from final cross-task review. (1) **Critical**: Dockerfile uvicorn launch now passes `--proxy-headers --forwarded-allow-ips="*"` so the app trusts Railway's `X-Forwarded-Proto: https` header. Without this, `request.url_for("auth_callback")` returned `http://gpiplantmanager.com/auth/callback` (uvicorn sees plain HTTP from the proxy) and Microsoft rejected the redirect URI mismatch against the Entra ID app registration's `https://` URI — login would have 100%-failed on first attempt after the `AUTH_DISABLED` flip. (2) FastAPI's auto-mounted `/docs`, `/redoc`, `/openapi.json` are now disabled (`docs_url=None` etc.) — they'd 302 to login per the auth middleware once enforced, but disabling outright is cleaner than gating and removes the route-surface enumeration vector. (3) `/auth/logout` now clears `gpi_auth_next` alongside `gpi_session` — a lingering signed next-cookie from an abandoned login attempt could otherwise survive a logout/login cycle and redirect post-login. No test count change (274 passed).

### 2:17 PM

- **Auth Sub-phase 2B: device tokens for shop-floor TVs** — new Postgres `device_tokens` table (idempotently bootstrapped at boot); admin UI at `/admin/devices` to mint, list, and revoke tokens (revoke is instant — TVs stop loading on the next refresh); `RequireAuthMiddleware` now accepts a `?device=<signed-token>` URL param ONLY on `/tv/*` paths (a valid token on `/recycling` or any non-TV path still bounces to login). Tokens are 32-byte random + HMAC-SHA256 of `SESSION_SECRET`; the random half lives in Postgres for instant revocation, the signature is re-derived at validate time so a leaked DB column alone can't forge a working URL — and rotating `SESSION_SECRET` invalidates every token at once as a panic button. Authed requests now stash `user_upn` + `user_name` on `request.state` so route handlers can see who's signed in (device-token requests get `device:<name>` to distinguish TVs from humans). Still gated by `AUTH_DISABLED=1` in Railway — nothing user-visible changes until Sub-phase 2C flips the env var off.

### 2:07 PM

- **Auth Sub-phase 2A: Microsoft Entra ID OIDC plumbing landed (NOT YET ENFORCED)** — `/auth/login`, `/auth/callback`, `/auth/logout` routes wired up; `RequireAuthMiddleware` registered in `app.py`; session JWT cookies via `authlib.jose` (HS256, 7-day sliding refresh); `@gruberpallets.com` domain check; `tests/conftest.py` keeps the existing TestClient suite happy. While `AUTH_DISABLED=1` is set in Railway, every route still serves anonymously — users see zero change. The middleware re-logs at ERROR level every 500 requests when `AUTH_DISABLED` is on, so accidental production bypass is detectable from request logs (boot-time log alone would scroll out of the buffer within hours). Next: Sub-phase 2B adds device tokens for TV displays; Sub-phase 2C is the cutover where Dale unsets `AUTH_DISABLED` and the door closes.

### 12:15 PM

- **Security: site is now marked non-indexable to search engines** — incident response for employee names + production data showing up in Google. Two changes: (1) every HTTP response now carries `X-Robots-Tag: noindex, nofollow, noarchive, nosnippet` (the authoritative signal Google honors across header + meta tag) via the existing `_security_headers` middleware in `app.py`. (2) New `/robots.txt` route returns `User-agent: *\nDisallow: /` as the upstream backstop — crawlers fetch this BEFORE crawling, so they bounce off before requesting anything else. This is the bleeding-stop pass; full Microsoft Entra ID auth (the door-lock that stops direct-link access too) is being designed separately and will ship over the next 1-3 days. Dale's manual TODO: file URL-removal requests for already-indexed pages via Google Search Console (https://search.google.com/search-console).

### 10:00 AM

- **Perf: parallelize the per-day loop on `/recycling` range views** — the per-day data computation (`_recycling_day_data`) ran sequentially over every day in the range, paying the full I/O cost (Zira leaderboard, schedule load, StratusTime absent-names) per day on a cold cache. A week view = 7 sequential round-trips, a month view = ~30, a quarter ≈ 90, a year ≈ 365. Now fanned out across a 4-worker `ThreadPoolExecutor` — cap chosen so we don't starve the DB pool (`maxconn=20`) or hammer the Zira API on long ranges. The helper is read-only and the caches it touches (leaderboard TTL, per-day schedule cache, `work_centers_store._EFFECTIVE_CACHE`, settings) are all thread-safe. Single-day views (the default `/recycling`) stay inline so we don't pay pool-spinup cost on the hot path. Order is preserved (pool.map returns in input order) so the downstream `zip(per_day, days)` aggregation is unchanged. Realistic impact: week views ~4×, month ~4×, quarter ~4× on cold renders (the 4-worker cap is the lower bound). Combined with the response cache shipped at 9:54 AM, warm-load impact is multiplicative.

### 9:54 AM

- **Perf: server-side HTML response cache extended to `/wc/{slug}`, `/tv/wc/{slug}`, and `/staffing/leaderboards`** — these were the last three heavy GET routes still re-rendering on every request. They now use the same `_http_cache` pattern `/recycling` and `/new-vs` already use: 15 s TTL on data that includes today, 5 min on past-only ranges, invalidated alongside every other today-cache via the existing `invalidate_today_cache()` calls in the staffing/schedule write paths (publish schedule, declare absent, save attribution, etc.). Operator dashboards reload every 30-60 s on the shop-floor TVs — before this, every refresh rebuilt the page from scratch (5 calls to `cached_leaderboard`, schedule load, KPI computation, GOAT race, monthly ribbons, full template render). Now refreshes inside the 15 s window serve cached bytes from RAM. `/staffing/leaderboards` was even heavier on range views — a 22-WC × per-person-day computation loop over the date range — and is now likewise short-circuited inside the TTL. Realistic impact: ~2× on warm/multi-viewer load; cold-path latency unchanged (still need to render once per 15 s per cache key).

### 9:24 AM

- **Operator dashboard Monthly Ribbons: each ribbon line now shows the date** — was "🥇 Carlos · 47" with no hint of when the medal was earned. Now "🥇 Carlos · May 12 · 47" — same medal/name/units, with the date sitting between name and units in muted gray. Uses `%b ` + `%d`-without-leading-zero so dates render as "May 2" / "May 12" cross-platform (Windows strftime doesn't support `%-d`). Sized one notch smaller than units in operator (TV) mode so long names don't get pushed off-screen on narrow widgets.

### 8:32 AM

- **Recycling VS: per-WC goals now scale with scheduled people × shift hours** — was `target/hr × productive_minutes_from_meter_active_intervals`, which collapsed goals to single-digit hours on Saturday whenever meters were idle for chunks of the shift (Saturday Dismantler bars showed 72 expected vs. 241/251/341 actual = 335%+ "achievement"). New formula: `target/hr × scheduled_people × elapsed_shift_hours`. Past single-day views (e.g. Saturday) use the full shift; today midshift scales linearly via `shift_elapsed_minutes`. WCs that produced units without anyone scheduled get expected=0 (no target line) — those are the same WCs that prompt the "↪ assign" inline-attribute button. Pairs with the `shift_elapsed_minutes` fix below — without it, Saturday's new goal denominator would still be zero.
- **`shift_elapsed_minutes` honors published schedules on non-standard weekdays** — was hard-returning 0 whenever `day.weekday() not in work_weekdays()`, which zeroed out elapsed/uptime math and the new goal denominator on every Saturday view. Now it only zeros out when the day has NO published schedule; a published Saturday (with or without custom_hours) is treated as a workday and the existing `shift_start_for`/`shift_end_for`/`breaks_for` overrides do the rest. Two regression tests pin the behavior.

### 8:24 AM

- **Recycling VS: past-day single-day views now show assigned names (and people counts)** — fix for the bug where `/recycling?start=2026-05-16&end=2026-05-16` (or any other single past day) rendered every WC as "(no assignment)" even when the scheduler had names published for that day. Root cause: the aggregation loop in `value_streams.py` only captured `agg_who_today` and `schedule_today_assignments` when `d == today`, so past-day single-day views fell through to empty dicts. Bars (`_bars()`) and downtime rows (`_downtime_rows()`) then read `agg_who_today.get(name)` → `None` and dropped into the "(no assignment)" template branch; the people-count footer also reported 0 for the same reason. Condition is now `not is_range`, which captures the viewed day's who-labels regardless of whether it's today or a past day. Range views still get `who=None` (gated on `is_range` downstream). Regression test added: `test_recycling_past_day_view_shows_assigned_names`.

## 2026-05-15

### 2:01 PM

- **GOAT Watch banner on Recycling VS** — surfaces operators on pace to beat their group's single-day GOAT record. Two stacked sections at the top of `/recycling`: (1) **Live contenders** show after the final break of the day, one row per group (Juniors / Repairs / Dismantlers) whose leading WC projects ≥ 98% of the group's all-time GOAT record at current pace — names the primary scheduled operator and their projected end-of-day total. (2) **NEW GOAT alerts** persist once someone actually beats the record (strict `>`), one card per group with trophy icon, prior-record context, and a Dismiss button. Alerts auto-clear after the next business day. New `goat_watch.py` module + `goat_alerts` table + `/api/goat-alerts/{id}/dismiss` endpoint. End-of-shift finalize is lazy and idempotent (in-process memo + DB UNIQUE on `(achieved_day, group_name, wc_name)`).

### 1:46 PM

- **Downtime Report per-WC uptime label: drop "up", 50% bigger** — was "82% up" at `clamp(0.7rem, 3cqh, 1.05rem)`. Now just "82%" at `clamp(1.05rem, 4.5cqh, 1.6rem)` — same percentage info, bolder presence inside the green bar.

### 1:42 PM

- **Per-WC uptime % positioned absolute inside the green bar; widget-total slid left of ⋮ edit button** — two visual fixes. (1) The per-WC uptime label is now absolutely positioned inside `.good` at `bottom: 6px` so it sits unambiguously inside the green fill (instead of flex-bottom-anchored, which was rendering at the boundary between the green and the name section). (2) `.widget-total` was overlapping the `⋮` widget-edit-btn — moved its `right` offset from `clamp(6px, 1.5cqw, 18px)` to a fixed `2.5rem` (clears the 28px-wide edit button + a small gap). h3 right-padding bumped to `clamp(100px, 32%, 240px)` accordingly. Widgets with both a `.people-count` pill AND `.widget-total` (Daily Progress + 15-min progress) get `right: 7rem` so the total sits left of the pill, which is left of the edit button.

### 1:39 PM

- **Downtime Report: per-WC uptime % moves inside the green bar (bottom)** — was rendered below the operator/WC name as a separate line. Now it sits at the bottom of each column's green "working" segment in white text, anchored via `align-items: flex-end` on the `.good` div. Reads as part of the visual bar instead of an extra footer line. Falls back to silent clip if the green segment is too short (high-downtime case) — `.good` already had `overflow: hidden`.

### 1:36 PM

- **Downtime Report now shows per-WC uptime % + total uptime moves into widget header; Up Time KPI tile dropped** — three changes on `/recycling`. (1) Each vertical column in the Downtime Report gets an uptime % rendered in green below the operator/WC name (e.g. "Eulogio Mendez / Dismantler 1 / **82% up**"). (2) The widget-total at the top-right of the Downtime widget now shows total uptime and downtime side by side ("76% up · 594m down" instead of just "594m"). (3) Removed the standalone `kpi-uptime` KPI tile since the same number now lives at the top-right of the Downtime widget. Three KPI tiles → two (Total Pallets Processed, pallets/hr/person).

### 1:01 PM

- **Scheduler: removed placeholder example notes** — the "Notes for the day" textarea had a placeholder reading "e.g. Rush order on Bay 4. Isidro to train Jose O on Repair 1 after lunch." which was showing up on shared PDFs and Slack posts when the day's notes were empty (the PDF/Slack share renders the scheduler page directly). Cleared the placeholder. Empty notes now render as a blank textarea everywhere.

### 12:37 PM

- **StratusTime: log every `TimeGetPunchesByEmpIdentifier` request + response** — temporary diagnostic logging added at StratusTime's request to capture the exact request bodies we send. INFO-level logs go to Railway with the AuthToken redacted and the response status / first 200 bytes of body. Will remove once their dev team has the trace they need.

### 12:07 PM

- **Downtime calculation now excludes lunch/break time even when an event bleeds into a break** — the upstream `in_shift_on` filter only drops downtime events whose START is inside a break window. But if a machine reports "Stopped at 11:25 for 50 min" (event runs 11:25→12:15) and pallets bracket a 30-min lunch within `TRANSFER_GAP`, the active-interval (11:20–12:15) used to span the break and credit the full 50 min as downtime — including the 30 min of lunch. New `_minutes_in_breaks(start_utc, end_utc)` helper in `leaderboard.py` computes the break-overlap minutes, and `_adjusted_downtime` subtracts that off the active-interval overlap before summing. 7 new unit tests pin the behavior, including the bleeds-into-lunch scenario.
- **Shift hours / breaks now come from PUBLISHED scheduler only** — `shift_start_for(day)`, `shift_end_for(day)`, and `breaks_for(day)` previously honored per-day `custom_hours` on any saved schedule (draft or published). Now they only apply the override when the day's schedule is published — drafts fall back to the global settings defaults (the Settings → Shift hours panel). Means draft "what-if" hours edits don't bleed into the downtime/uptime/Pallets-banner math until the supervisor actually publishes. Three test fixtures updated to set `published=True` explicitly where they intended the custom hours to apply.

### 11:01 AM

- **Scheduler: edit notes after publishing without dropping to draft** — supervisors couldn't update the day's notes (or per-WC notes) after publishing because the `.locked` CSS disabled the textareas, and any save while published auto-flipped the schedule to Draft and required a re-publish. Three changes: (1) CSS now exempts `textarea[name="notes"]` and `textarea[name^="wc_note__"]` from the locked-form pointer-events block, so the notes fields stay editable on a published schedule. (2) Autosave JS detects the "published-and-still-locked" state and routes the save through `action=save_notes` (new) instead of plain `action=save`. (3) Backend handles `save_notes` by updating only the `notes` and `wc_notes` fields and preserving assignments, `published_snapshot`, `testing_day`, and `custom_hours` — the schedule stays Published, no snapshot of the prior state is taken, no re-publish needed.

### 8:40 AM

- **On-goal bar color: white → neutral gray** — `_progress_color()` returned `#ffffff` for bars within 1% of goal. Looked fine on dark TVs but went invisible against the light-mode panel background. Switched to `#9ca3af` (neutral cool gray) which reads well against both light and dark backgrounds. Off-goal bars (anything outside ±1%) keep their HSL-ramped red/green colors unchanged.

### 8:01 AM

- **Goal info slides left of the people-count pill** — when a widget has both a `.people-count` badge (top-right at `right: 2.5rem`) AND a `.widget-total` (was also `right: ~0`), they overlapped. New CSS: `.grid-stack-item-content:has(.people-count) .widget-total { right: 6.5rem }` slides the goal info to the LEFT of the people-count so they sit side by side. The `h3` padding-right is also bumped via `:has(.people-count):has(.widget-total)` so the title text clears both elements.

### 7:53 AM

- **15-minute progress widgets get the people-count icon** — the Dismantlers + Repairs 15-min progress widgets on `/recycling` now show the same person/people-day counter that the Daily Progress widgets have always shown. Uses the existing `dismantler_people` / `repair_people` context vars (already populated by the route) and the `.people-count` CSS class. `new_vs.html` only has Daily Progress widgets so no change needed there.

## 2026-05-14

### 3:14 PM

- **Total + Goal now on the title row in both screen and TV mode; all four progress legends removed** — three related changes. (1) The `.widget-total` element (Pallets-by-WC, Downtime Report) moves to absolute top-right of its widget content area in **both** screen mode and TV mode — was previously bottom-right in screen mode, absolute top-right only in TV. Dropped the "Total" word from the rendered text so it just shows the number. CSS scoping moved from `tv-mode.css` to `recycling.css`. (2) Removed the legend (swatches + Goal text) from the `progress_chart` macro and the `cumulative_progress_chart` macro — affects all four progress widgets (Dismantlers/Repairs × 15-min/Daily). (3) Goal info ("Goal X/hr · Y/15 min") relocated to a `.widget-total` div on the two 15-min progress widgets — pinned top-right by the same CSS rule, on the same line as the title. The freed vertical space from the removed legend is automatically taken up by the chart plot via the existing `flex: 1 1 auto` on `.plot`.

### 3:11 PM

- **Up Time KPI: no decimals, never wraps** — `kpi-uptime` template now formats as `uptime_pct|round(0)|int ~ ' %'` so "76.3 %" becomes "76 %". CSS adds `white-space: nowrap` to `.grid-stack-item-content .val` so the value never breaks to a second line on narrow widget widths.

### 3:08 PM

- **Downtime Report rotated to vertical bars** — was horizontal rows (operator name on left, stacked working/down bar in middle, downtime minutes on right). Now each work-center is a vertical column: downtime minutes label on top, vertical stacked track in the middle (red on top of green so the red "down" portion is what you see growing), operator/WC name at the bottom. New `.downtime-vbars` flex layout in `recycling.css` with cqh/cqw-scaled fonts so columns adapt to widget size. Reads more like a bar chart and packs more WCs into the same widget width.

### 3:05 PM

- **Recycling VS: Downtime Report shows total at top-right; standalone Total Downtime KPI dropped** — `recycling.html`'s downtime-report widget now sums `downtime_rows | sum(attribute='down')` and renders a `.widget-total` div ("Total Xm") that the TV-mode CSS from the prior commit pins to the top-right corner of the widget. In screen mode it falls through to the existing `.widget-total` styling at the bottom of `widget-body`. The redundant `kpi-downtime` ("Total Downtime") KPI tile is removed from `kpi_defs` since the same number now lives on the Downtime widget itself. Saved layouts referencing `kpi-downtime` become orphaned — Gridstack ignores them silently.

### 3:01 PM

- **Recycling VS TV: bigger names on Pallets by WC; total moves to top-right** — two changes scoped to TV mode. (1) Bar-row name font floor raised from `0.35rem` to `0.75rem` (`clamp(0.75rem, 5cqh, 1.5rem)`), line-height `1.05`. Operator names like "Eulogio Mendez" / "Dismantler 1" now render at a comfortable ~16px instead of squinting-required ~7px. (2) `.widget-total` (the `Total X / Y (Z%)` line) moves from the bottom of the bar widget to the **top-right corner**, absolute-positioned. Frees the bottom of the widget for the bar-rows so the bars fill more vertical space. Title `h3` gets `padding-right: clamp(80px, 28%, 220px)` (scoped via `:has(.widget-total)`) so its text doesn't run under the relocated total.

### 2:04 PM

- **Today Pallets banner: number color tracks bar color** — Dismantler 4 showed a green number above a red bar. Same `.ahead` / `.behind` class now also tags the big units number so the number color matches the bar (green ahead, red behind). User's custom widget color (via the `⋮` edit panel) still wins via inline-style specificity.

### 1:57 PM

- **Today Pallets banner bar now colors by goal status** — was hardcoded green. Now green when `units_today >= target_today` (ahead/on pace) and red when behind the prorated target at this moment. Template tags `.bar-fill` with `.ahead` or `.behind`; CSS applies `var(--accent)` / `var(--bad)` accordingly.

### 1:39 PM

- **Recycling VS TV: tame the giant "Total X / Y" line; tighter bar-rows** — screenshot showed the `Total 940 / 1236 (76.1%)` summary rendering at ~34px (a hardcoded `1.6rem` in `tv-mode.css`) and dominating the Pallets-by-WC + Downtime widgets, which forced the bar-rows above it to collapse so tight their two-line operator names overflowed into the neighboring row's space and visually "stacked on top of each other". Two CSS tweaks: (1) `widget-total` font dropped from `1.6rem` to `clamp(0.7rem, 3cqh, 1.2rem)` — small base, scales gently with widget height instead of dominating. (2) bar-row name floor dropped from `0.5rem` to `0.35rem` and `line-height` tightened from `1.05` to `1` so names fit in their row slice even when the widget is tall and packed with 6+ work-centers.

### 1:18 PM

- **Monthly Ribbons text now scales down to fit — no cutoff** — the prior "tight name+number with width-shrinking gap" pass dropped the `cqw` component from the font formula, so narrowing the widget didn't shrink the text; long names overflowed the row and got clipped/cut off. Restored width-aware scaling with a much lower floor: `clamp(0.55rem, min(18cqh, 4.5cqw), 2.6rem)`. Text now shrinks with whichever widget dimension is smaller, so long names like "Christian Galindo Mendez" stay fully visible at any widget width by scaling down to fit. Medals and units use matching formulas (lower floor, cqh+cqw scaling). Removed the `text-overflow: ellipsis` since the aggressive scaling should prevent overflow at reasonable widget sizes; `overflow: hidden` stays as a silent fallback.

### 1:07 PM

- **Daily Progress target now agrees with the Pallets banner — break buckets get target=0** — Pallets banner said "ahead of goal" while Daily Progress said "behind goal" on the same screen. Root cause: Pallets banner target = `goal_per_day × productive_elapsed/total_productive` (breaks excluded), but the cumulative-target line on the Daily Progress chart summed a constant per-bucket target across *every* 15-min bucket — *including* lunch-break buckets where no work is expected. That inflated the cumulative target by `break_minutes/15 × per_bucket_target` worth of "expected" output, so the chart said you're behind when the Pallets banner said you're ahead. Fix: `fifteen_min_increments` now looks up `shift_config.breaks_for(day)` and sets `target=0` for any bucket whose window overlaps a break. Cumulative target line now stays flat during break time and lines up with the Pallets banner's productive-elapsed math. Added a regression test that pins the lunch-break-target=0 behavior with a 7am/8hr/11:30-lunch scenario.

### 1:00 PM

- **GOAT widget: 🐐 back on the same row as the number** — reverted the stacked layout. Delta line is `+5 AHEAD 🐐` on one row again. Both number and icon size matched at `clamp(1.8rem, min(35cqh, 14cqw), 6.5rem)`.

### 12:59 PM

- **Monthly Ribbons: name and number sit tight; gap shrinks first when narrowing** — the base `.ribbons-list .name { flex: 1 }` was pushing the units to the far-right edge of every row, leaving a huge empty gap between (e.g.) "Jose Galindo" and "1462". On the operator dashboard the name now sits at its natural width with a small flexible `clamp(0px, 1.5cqw, 0.6rem)` gap between items. The font-size also dropped its `cqw` component (now `clamp(1.1rem, 20cqh, 3rem)`) so it doesn't shrink with widget width — narrowing the widget collapses the gap first and only starts to ellipsis the name once the gap is fully consumed.

### 12:55 PM

- **GOAT Race widget — goat stacks under the number, zero horizontal padding** — the delta line was `+5 AHEAD 🐐` on one row; widget didn't fill vertical space well. Now `+5 AHEAD` sits on top with the 🐐 stacked below, and both scale much bigger (`min(38cqh, 18cqw)` for the number, `min(45cqh, 22cqw)` for the goat). Widget padding zeroed on left/right (title gets a small 8px inset so it doesn't touch the edge). The goat now reads as the focal point of the widget at any height.

### 12:51 PM

- **Progress charts now stay in sync with the Pallets banner** — the operator dashboard's 15-min progress chart and Cumulative Daily chart appeared "really behind" the live Pallets banner. Root cause: the truncation filter compared **wall-clock bucket offsets** (`i * 15` from shift start) against **productive elapsed minutes** (`shift_elapsed_minutes` subtracts breaks). After every break, the chart appeared frozen for `break_minutes` of wall-clock time because the productive elapsed lagged behind. Typical 30-min lunch → chart looks ~30 min behind for the rest of the day. Fix: new `_elapsed_wall_clock_minutes(day)` helper computes minutes since `shift_start_for(day)` directly from `datetime.now(SITE_TZ)`, no break subtraction. `fifteen_min_progress_buckets` now uses this for both the `in_progress` flag and the truncation filter. Pallets banner data path unchanged (still uses productive elapsed for GOAT-pace prorating). Added a regression test that pins the wall-clock semantics with a 30-min break scenario.

### 12:45 PM

- **GOAT Race widget cleanup; unified widget titles on operator dashboard** — three changes. (1) Removed the bar graph from the GOAT Race widget; just the big +/- delta number with `🐐` remains. (2) Moved the `<units> of <pace> on pace` ratio line down next to the `🐐 Beat N to top Name` footer line — both at the bottom of the widget. (3) Pallets/hr, GOAT Race, and Monthly Ribbons titles now use the same `clamp(0.9rem, 6.5cqh, 1.3rem)` formula the Downtime Report's h3 uses, so all four widget headers read at the same size and scale with widget height the same way.

### 12:43 PM

- **Pallets banner: "start · HH:MM" now sits at the actual bar's left edge** — the axis row was using recycling's `.bar-row.numpos-widget` grid template (`name | track | val` with the name column ~6-11rem wide), so the axis-track was offset rightward by the name-column width while the bar above it spanned the full banner. Result: "start · 07:00" rendered in the middle of the visible bar instead of at its left edge. Replaced with a plain `.pallets-axis > .axis-track` that spans the full banner width — start/now ticks now align with the bar above them.

### 12:26 PM

- **TV dashboards: dead space at the bottom fixed** — the `maxRows` calculation in the fit-to-viewport JS initialized to the *fallback default* (30 for recycling, 25 for operator) and only ever raised the value as it iterated. So if the actual saved layout fit in 18 rows, `maxRows` stayed at 30 — cellHeight got computed for a 30-row layout, widgets filled only 18/30 of the screen, leaving 40% empty at the bottom. Fix is one line per file: initialize `maxRows = 0`, raise it from items, fall back to 30 only when the grid is empty. Now the widget grid expands to fill the full TV viewport regardless of saved layout extent.

### 12:18 PM

- **TV view: per-row labels actually fit, footer + resize handles hidden** — screenshot from a 1080p TV showed three concrete problems my prior scaling pass introduced: (1) bar-row labels (operator names) in the Pallets-by-WC and Downtime widgets were stacking on top of each other because the screen-mode CSS floor of `0.9rem` on `.bar-row .name` was bigger than the per-row pixel slice when a widget has 6 dismantlers and only ~100px of vertical space. (2) The in-page footer ("Refreshed … · What's new ↗") rendered at the bottom — that should be gated to screen mode only. (3) Gridstack's resize-handle chevrons still painted despite `staticGrid: true`. Fixes: TV-mode CSS drops the bar-row name font floor to `0.5rem`, hides every `.ui-resizable-*` handle, and stops the `What's new` + Refreshed footer from rendering in TV mode. Also removed the `max-height: 100vh` clamp on `.grid-stack` and `main` since it was clipping chart content visible at the bottom of the dashboard.

### 11:25 AM

- **TV scaling — measure actual header, fit on every resize** — the prior cellHeight formula reserved a hardcoded 80px for the TV header, but the header scales with root font, so on a 1440p+ TV it's actually 100-120px and the layout still overflowed. The JS now `getBoundingClientRect`s the `.tv-header` element to get its real rendered height. The fit also runs twice (once on init + once on `requestAnimationFrame`) in case the first call mistimed against font loading, plus on every `resize` event. CSS strengthened: `html`, `body`, `main`, `.grid-stack`, and `.grid-stack-item` all get `overflow: hidden` (with `!important` on the latter two) so nothing can render outside its widget bounds. `.grid-stack` and `main` get `max-height: 100vh`. Body padding/margin zeroed in TV mode so chrome can't sneak in extra space.

### 11:16 AM

- **TV scaling fixes — no more overflow or overlapping widgets** — the prior viewport-scaling pass left two problems on Recycling VS TVs: widgets ran off the bottom of the screen, and content from one widget appeared to "double up" onto the next. Two root causes, both fixed: (1) the cellHeight formula was based on a hardcoded `nRows=30` but didn't subtract the gridstack margin (8px × 29 gaps = 232px extra). Now the JS queries the *actual* rendered layout extent via `grid.save(false)` after init and computes `cellHeight = (innerHeight - 80 - (maxRows - 1) * 2) / maxRows`. Works correctly with any saved layout, not just the template defaults. (2) Gridstack `margin` is reduced to `2` in TV mode (from `8`) so margin overhead doesn't eat into available height. Plus `html` and `body` now get `height: 100%; overflow: hidden` in TV mode — TVs can't scroll anyway, and the hard-clip prevents any small miscalc from producing the "doubled" overlap effect. Belt-and-suspenders `.grid-stack-item { overflow: hidden }` so widget content can't bleed past its widget bounds. Operator dashboard (`/tv/wc/{slug}`) got the same fix.

### 11:07 AM

- **TV dashboards now scale to any screen size** — root font-size and GridStack row height both scale with TV viewport, so a 1080p, 1440p, and 4K TV all render the same layout, just bigger/smaller proportionally. Three changes: (1) `tv-mode.css` now sets `html[data-tv-theme] { font-size: clamp(16px, 1.1vw, 40px) }` — every rem-based size (operator strip, widget titles, KPI labels, ribbons rows, GOAT stats) scales with viewport width. ~21px on 1080p, ~28px on 1440p, 40px on 4K. (2) The `_tv_header` element converted from px to rem so its title + crumb scale with the same root. (3) GridStack `cellHeight` is computed per page load from `window.innerHeight`: `/recycling` uses `(innerHeight - 80) / 30` (its layout is ~30 rows tall), `/wc/{slug}` uses `(innerHeight - 80) / 25`, both with a 28px floor — so the full layout fills the TV without scrolling regardless of screen size. Screen-mode pages (`/recycling`, `/wc/{slug}` on a desktop) are unchanged — `data-tv-theme` is only set in TV mode and `cellHeight` stays at the original 60px.

### 11:00 AM

- **Recycling VS TV view is now chrome-free like the operator TV view** — `/tv/{slug}` for Recycling VS was still rendering the regular page header (logo + Dashboards / Trophy Case / Staffing / Settings nav), the date-range toolbar, the dashboards sub-nav tabs, the auto-save edit-bar, and the per-widget `⋮` edit buttons. Gated all that chrome behind `{% if not tv_mode %}` in `recycling.html` so the TV view shows just the `_tv_header` + the widget grid. Also passed `staticGrid: true` to `GridStack.init` in TV mode so touchscreen TVs can't accidentally drag widgets around.

### 10:29 AM

- **TV display URLs are shorter: `/tv/{slug}` (no more `/d/`)** — saves 3 chars per URL (`/tv/d/junior-2` → `/tv/junior-2`). The Settings → TVs panel now renders the new short URL. Old `/tv/d/{slug}` URLs still work — they 302-redirect to `/tv/{slug}` and preserve the `?theme=…` query string — so already-deployed TVs keep loading without any manual reconfiguration. Two new tests lock in the redirect behavior.

### 10:25 AM

- **Reverted: pallets counter inside the bar** — the inside-the-bar overlay didn't read well. Restored the prior layout: separate `.pallets-numbers` row above the bar (number + `/ N full day`), then the rectangular fill bar (45-180px tall), then the start/now axis ticks below. Downtime widget's left-aligned `%` change from the prior commit stays.

### 10:19 AM

- **Downtime % moves to the LEFT of the green bar; pallets counter moves INSIDE the bar** — two related layout cleanups. (1) The downtime widget's up-time label was right-aligned at the green/red boundary; now it's left-aligned at the left edge of the green portion. The downtime minutes (`12m`) stay right-aligned inside the red portion. (2) The pallets banner's big units number moves *inside* the bar at the left edge — the separate numbers row above the bar is gone. The bar now fills the available vertical space (grid row 1fr) with the number overlaid in white at the left, target denominator (`/ 240`) right after it. Frees ~30px of vertical space and makes the banner read as a single dense element.

### 10:16 AM

- **GOAT widget delta line gets a big 🐐 next to the counter** — added a goat emoji after the AHEAD / BEHIND label, sized to match the number itself (`clamp(1.8rem, min(30cqh, 12cqw), 5.5rem)`). The delta line now reads `+5 AHEAD 🐐` / `-3 BEHIND 🐐`, doubling down on the GOAT theme. Replaces the earlier 🔥. ON PACE case omits it.

### 10:11 AM

- **Vs. GOAT Pace widget redesigned** — the old "Today: X / GOAT pace now: Y / 🐐 …" three-line list didn't communicate the race. New layout, three sections stacked top-to-bottom: (1) ratio line `<units> of <pace_now> on pace` plus a chunky horizontal bar that fills to `units / pace_now` and is green when ahead, red when behind. The bar's max IS the GOAT's prorated pace at this moment — *not* the full-day record — so you immediately see "am I keeping up?" at a glance. (2) Big delta number front and center: `+5 AHEAD 🔥` or `-3 BEHIND` (or `ON PACE` when within ±5 of pace) in matching green/red. (3) Footer names the record to beat: `🐐 Beat 180 to top Dale Smith`. Designed to read well at `gs-w=3` so the widget doesn't need to be wider than three tiles. Pre-shift fallback (pace_now=0) shows current units centered with the record line below. No-record-yet fallback shows units + "set the bar!".

### 10:04 AM

- **Downtime widget bar spans the whole widget; both labels live inside it** — dropped the name column and the right-side val column (the operator-strip at the top of the dashboard already shows the WC + operator, so the name was redundant). The stacked working/down bar now fills the full widget width. Up-time % sits at the right edge of the green portion (without the word "up" — just `95.0%`); downtime minutes (`12m`) sit at the right edge of the red portion. Both labels are white, bold, and scale with widget size (`clamp(1.1rem, min(35cqh, 4cqw), 3.2rem)`). Bar height also grows with widget height — `clamp(40px, 55cqh, 220px)`.
### 10:02 AM

- **Pallets/hr, GOAT Pace, Monthly Ribbons — bigger titles top-left, less padding, body fills widget** — three operator-dashboard polish items grouped. (1) Widget titles for those three are now `0.95rem` (was 11px ~0.7rem) and pinned top-left regardless of the widget's align-X class. (2) Padding cut from the default `10px 12px` to `2px 8px` (Pallets/hr) or `4px 10px` (GOAT Pace, Monthly Ribbons) so the number and body content reach the widget edges. (3) Number and body sizing bumped much higher: Pallets/hr number scales as `clamp(2rem, min(75cqh, 28cqw), 8rem)` so it fills the area after the title. GOAT Pace stats scale `clamp(1.1rem, min(18cqh, 7cqw), 4.5rem)` with the inner content flex-centered to fill vertically. Monthly Ribbons rows scale `clamp(1.1rem, min(20cqh, 5cqw), 3rem)` with `justify-content: space-around` so the three rows spread out to fill the widget height.

### 9:58 AM

- **Pallets banner bar is now rectangular and 50% taller** — dropped the pill rounding (`border-radius: 0`) and bumped the height clamp from `clamp(30px, 30cqh, 120px)` to `clamp(45px, 45cqh, 180px)`. Squared-off corners, chunkier vertical presence.

### 9:53 AM

- **Pallets banner bar is chunkier and scales with widget height** — the progress bar was capped at 22px regardless of widget size. New rule: `clamp(30px, 30cqh, 120px)` — chunky 30px floor, scales at 30% of widget height, up to a 120px ceiling. Border-radius set to a full pill so the bar reads as a single thick stripe at any size. Drag the banner taller and the bar grows with it.

### 9:50 AM

- **Operator dashboard top chrome collapsed into one strip** — the WC picker bar, the big operator-name band, and the edit-bar were three separate rows stacking ~150px of chrome above the widget grid. They're now one row: the work-center dropdown sits on the left as the page heading (the WC name *is* the dropdown trigger), the scheduled operator name(s) follow next to it, and the "auto-saves" indicator + Reset Layout button are pinned to the right. Widgets get ~150px more of vertical screen space. Function-equivalent — nothing hidden, just packed tighter. Tests renamed `operator-band` → `operator-strip` to match.

### 9:45 AM

- **Pallets banner layout fixed; Pallets/hr title matches other widgets** — two operator-dashboard polish items. (1) The Pallets banner's big number was overlapping the widget title and the start/now axis labels were getting clipped. Root cause: the widget's container query was `container-type: inline-size` so `cqh` (container-query height) silently fell back to 0, breaking every `min(cqh, cqw)` rule. Switched the widget container type to `size` so `cqh` actually tracks the widget's pixel height. Banner now uses a CSS grid (`grid-template-rows: 1fr auto auto`) — the number row flexes to fill, while the bar and axis row hold their natural height and stay glued to the bottom, no clipping. The big number sits in its own nested container so its font scales by the row's height, not the widget's full height. (2) The Pallets/hr widget's `.label` was using a custom KPI style different from the `<h3>` other widgets use — now styled identically (11px, uppercase, muted, 8px bottom margin) so all widget titles read as one family.

### 9:30 AM

- **Downtime widget: up-time % moves inside the green bar; right side shows just `Xm`** — the up-time percent now renders as a white label at the right edge of the green "working" portion of the stacked bar, sitting right where the bar transitions to red. The right-side value column drops the word "down" — just shows `Xm` (the bar's red color makes "down" redundant). If the green bar is too narrow to fit the label (very high downtime), the parent's `overflow: hidden` clips it cleanly.

### 9:27 AM

- **Pallets banner no longer overflows or shows a scrollbar** — the big number was scaling by `9cqw` (9% of widget width) with no height cap, so on a full 12-col banner it tried to render at ~126px tall, overflowed the default `gs-h=2` widget, and triggered a scroll affordance. Switched to `min(40cqh, 8cqw)` scaling (whichever dimension is smaller wins), added `overflow: hidden` on the banner as a safety net, and capped the max at 3.6rem. The number now scales smoothly with widget size in both axes and never pushes content past the widget edge.

### 9:24 AM

- **Operator dashboard: 3 KPI tiles removed, Up Time % folded into Downtime widget** — `Units today` / `Up Time` / `Downtime` are gone as standalone widgets. Units is already prominent in the Pallets banner; Up Time + Downtime both live inside the Downtime widget now (red `Xm down` over green `Y% up` on the right side of the bar). Only `Pallets / hr` remains as a standalone KPI tile.

### 9:22 AM

- **Downtime row shows operator name like /recycling; GOAT + Ribbons titles fully customizable; body text scales aggressively on small widgets** — three operator-dashboard polish items: (1) the **downtime widget row** now puts the scheduled operator(s) as the primary label and the work-center name as the small secondary line below — matches how `/recycling`'s bar rows render. (2) The **Vs. GOAT Pace** and **Monthly Ribbons** widget titles no longer auto-append the group/month suffix after a custom title — the group name and month/year are now baked into the *default* title, so typing your own title in the `⋮` edit panel fully replaces what's shown (no more `Custom Title — Repairs · May 2026`). (3) The GOAT race and Monthly Ribbons **body text now scales by the smaller of widget height or width** (`min(cqh, cqw)`) with bigger floors, so the content fills the widget at small sizes instead of bottoming out at a tiny font that leaves the widget half-empty.

### 9:19 AM

- **KPI text now shrinks to fit instead of wrapping** — the operator dashboard's four KPI widgets had loose padding and a fixed font-size ceiling, so a long number like "1,234" would wrap onto two lines or get cut off when you shrunk the widget. Padding dropped to `4px 6px`, label and value both get `white-space: nowrap`, and the value font now scales by `min(40cqh, 16cqw)` — the smaller of widget height vs width — so the number stays on one line and uses the full widget area at any size. Aside: the prior `.wc-dashboard .kpi .val` CSS rules were dead code (the operator template doesn't wrap KPIs in a `.kpi` div) — removed.

### 9:11 AM

- **Hotfix — / and /recycling were 500'ing** — the 9:07 AM "Operator dashboard polish" deploy broke every page that imports the shared `edit_controls` Jinja partial. Jinja's `{% from … import macro %}` does NOT pass the calling template's context to the imported macro by default, and the macro reads `customs.get(id, {})` — so every render raised `UndefinedError: 'customs' is undefined`. Fix: add `with context` to the import line on `recycling.html` and `wc_dashboard.html` so the macro sees the page's `customs` dict.

### 9:07 AM

- **Operator dashboard polish** — `/wc/{slug}` now mirrors `/recycling`'s editor features. The KPI row is split into four resizable widgets (`Units today`, `Up Time`, `Downtime`, `Pallets / hr`) so you can size each one to taste. Every widget gets a `⋮` edit panel — change the title, color, alignment, legend/target toggles where they apply. Layout **auto-saves on drag/resize** with a "Saved" indicator, plus a Reset Layout button to restore defaults. **Customize once → applies to every WC** — layout + widget customizations share a single `page="operator"` key in the database, so dragging KPIs around on Repair 1's dashboard reshapes Dismantler 2's too. A new band under the WC picker shows the work-center name and the scheduled operator(s) from the Plant Scheduler (falls back to `(unassigned)` when no one's assigned). The pallets banner now has the same **start/now axis ticks** `/recycling` uses on its bar rows — a tick where you should be right now plus `start · HH:MM` / `now · HH:MM` labels. GOAT Pace, Monthly Ribbons, KPI value text, and the pallets banner number all **scale with widget size** via CSS container queries — make the widget bigger, the text gets bigger. KPI text is **black on the light theme, white on the dark theme**. The 15-minute progress and cumulative daily charts now **stop at "now"** instead of running the whole shift. Legacy per-WC layout rows (`wc:repair-1`, …) are dropped on the next boot.

### 8:01 AM

- **Deploy crash fix — finish the workshop column drop** — the 7:41 AM deploy boot-looped because the schema bootstrap re-created the `tv_displays_custom_dashboard_id_fkey` FK on every start, then `DROP TABLE custom_dashboards` couldn't drop the table the FK still referenced. The teardown DDL now drops the FK + column first, in FK-safe order. Downstream cleanup followed: `tv_displays_store.save()` lost its `custom_dashboard_id` kwarg and the column is gone from every INSERT/UPDATE/SELECT; `POST /api/tv-displays` no longer accepts the field; the Settings → TVs picker JS stopped sending it. Old workshop tests trimmed.

### 7:41 AM

- **Workshop tear-down + new Operator dashboard** — the widget workshop / custom dashboards / pinned dashboards / layout templates experiments are removed entirely. Roughly 30 files deleted, 5 DB tables dropped (`widget_definitions`, `custom_dashboards`, `dashboard_widgets`, `tv_dashboard_templates`, `pinned_dashboards`); any TV display rows with `kind = 'custom'` are also deleted. Sub-nav is now a fixed 4-tab strip: **Recycling VS · New VS · Operator · Work Centers**. The top-nav "My Dashboards" link is gone (page removed). The new **Operator dashboard** lives at `/wc/{slug}` (TV: `/tv/wc/{slug}`) and mirrors `/recycling`'s visual style scoped to a single work center: KPI tiles row (Units / Up Time / Downtime / Pallets/hr), Pallets banner, 15-min progress chart, Cumulative Daily Progress, Downtime stacked bar, Vs. GOAT Pace, Monthly Ribbons. A WC dropdown at the top lets you switch which work center the page shows. `/operator` redirects to the first WC. The Settings → TVs panel drops the Custom optgroup and the Layout Templates section.

## 2026-05-13

### 4:09 PM

- **Per-WC dashboards (`/wc/{slug}`) now match /recycling's visual style** — the per-WC pages used to render their own custom widget styles that looked nothing like the Recycling VS dashboard. Rewritten to use the workshop's widget partials (same partials a custom dashboard uses), so every widget renders with the same CSS classes /recycling uses. New layout, all 12 cols wide stacked vertically: (1) Pallets banner (this WC), (2) **15-min progress chart** for this WC (slot the recycling dismantler-progress occupies), (3) **Cumulative Daily Progress** for this WC (dismantler-cumulative slot), (4) **Vs. GOAT Pace** for the WC's group (in the repair-progress slot), (5) **Monthly Ribbons** for the WC's group (in the repair-cumulative slot), (6) **Downtime Report** for this WC. The "All Repairs / All Dismantlers" doubling-up is gone — those slots now hold GOAT race + ribbons. Cumulative widget resolver was also unified with daily_progress (both produce the same buckets shape, the partials render them differently — bar vs cumulative line). `recycling.css` is loaded on the per-WC page so all the `.bar-row`, `.progress`, `.cum-progress`, `.stacked-track` rules apply.

### 3:55 PM

- **Workshop and My Dashboards now show the standard Plant Manager header** — both pages used to render their own bare header (`<h1>Widget Workshop</h1>` and a plain `<h1>Plant Manager</h1>` without the logo), which made it harder to jump back to the main dashboards. Both pages now show the same clickable logo + "Plant Manager" title + Dashboards / Trophy Case / Staffing / Settings top nav as every other page, so clicking the logo or "Dashboards" gets you back to `/recycling` in one click. Sub-nav strip is unchanged.

### 3:52 PM

- **Pallets, Downtime, and 15-min Progress widgets now render identically to /recycling** — the workshop's `pallets_by_wc`, `downtime`, and `daily_progress` widget partials were rewritten to use the same markup as /recycling's inline widgets (same CSS classes, same DOM structure), so a custom dashboard or workshop preview now shows them with the same look and feel as the Recycling VS dashboard. Pallets-by-WC: dropped the extra `.pallets-by-wc` wrapper that was breaking the flex layout, added the target line and the `Total / total_e (pct%)` footer. Downtime: switched from a single-WC event list to per-WC stacked working/down bars (green/red) — the standard /recycling rendering. New resolver shape `{rows: [...], total_elapsed}`. 15-min Progress: replaced the loose color-bar grid with the proper `progress` chart (one column per 15-min bucket, target tick on each column, in-progress highlighting, time-of-day x-ticks) — new resolver shape `{buckets: [{label, actual, target, in_progress}, ...], bucket_target}`. **All three widgets now accept multi-WC + multi-group selection** (just like Pallets-by-WC's earlier ship): pick any combination of WCs or groups; the data is summed across the deduplicated WC set. Old single-`{wc_name: ...}` presets still work via resolver back-compat.

### 3:44 PM

- **Pallets by Work Center widget now supports multi-WC + multi-group selection** — the workshop's data scope for the `pallets_by_wc` widget swaps its single Group dropdown for two scrollable checkbox lists: **Work Centers** (pick any combination of WCs by name) and **Groups** (pick any combination of groups; each expands to its members). The final WC set is the deduplicated union of both selections, so you can pick "Repair 1" + "Repair 2" + the entire "Dismantlers" group on the same widget. The old `{group: "Repairs"}` shape on previously saved presets still works (resolver back-compat). To create your own Pallets-by-WC preset: go to /widgets, click **+ Create / edit**, pick type "Pallets by Work Center", give it a name (e.g. "All Repairs Pallets"), check whichever WCs and/or groups you want, save. Preview pane on the right shows it live as you build.

### 3:38 PM

- **Widget Workshop live preview** — `/widgets` gets a third panel on the right that renders the widget you're working on as you type. Click **Edit** on any saved widget → the form fills in AND the preview renders the current saved config. Change the color, sort, group, WC, KPI metric, etc. → the preview re-renders ~250 ms after you stop typing. New endpoint `POST /api/widgets/preview` resolves the widget's data via its registered resolver and returns the rendered partial; the workshop loads `wc_dashboard.css` + `recycling.css` so the preview matches what the widget looks like on a dashboard. Panel layout switches to a single column below 1100 px wide.

### 3:35 PM

- **Removed third-tier VS sub-nav; Work Centers promoted to the dashboards sub-nav** — `/recycling`, `/new-vs`, and `/work-centers` no longer render the redundant **Recycling VS · New VS · Work Centers** sub-tabs (the first two were already pinned in the dashboards sub-nav). Work Centers is now a built-in dashboard kind (`vs_work_centers`) — pinned by default alongside Recycling VS and New VS, listed in the My Dashboards Built-in section with a star pin toggle, and reachable in one click from any dashboard-family page's sub-nav. No TV variant for Work Centers (it's a status board), so its row hides the "Open as TV" action. Existing /work-centers URL unchanged.

### 3:20 PM

- **Dashboards sub-nav + pinning + unified index + simpler TVs picker** — major restructure of the dashboards family. (1) A new sub-nav strip under the top "Dashboards" tab shows your **pinned dashboards** left-aligned (Recycling VS + New VS pinned by default), with **My Dashboards** and **Workshop** anchored on the right. The strip renders on every dashboard-family page (`/recycling`, `/new-vs`, `/wc/{slug}`, `/dashboards/{slug}`, `/dashboards`, `/widgets`) so you can hop between favorites in one click. TV-mode pages stay chrome-stripped (no sub-nav there). (2) The redesigned `/dashboards` index lists **every dashboard in the system** — Built-in (Recycling VS, New VS, one per WC) and My custom dashboards — with a star/unstar pin toggle per row that saves to a new `pinned_dashboards` table. (3) **Top nav + Settings sidebar cleanup**: the "My Dashboards" top-nav link and the Settings sidebar entries for Widget Workshop / My Dashboards are gone (now reachable via the sub-nav). The four top tabs are back to Dashboards, Trophy Case, Staffing, Settings. (4) **TVs settings flat picker**: the kind / wc / custom-dashboard cascading selects collapse into one **Dashboard** picker listing everything with Built-in and Custom optgroups. Existing TV display rows render correctly with the new picker auto-selected to their target. Schema unchanged.

### 2:35 PM

- **Widget Workshop seeded with starters + Duplicate button + Edit-warning popup** — three workshop improvements plus one small fix. (1) **10 starter widgets** auto-seed on first boot mirroring the configs on `/recycling` (Pallets by WC + Total Pallets KPI, per group) and `/wc/{slug}` (Pallets Banner, Daily Progress, Cumulative, Downtime, GOAT Race, Monthly Ribbons — default to Repair 1, duplicate and swap WCs for others). Deleted seeds stay deleted across redeploys. (2) **Duplicate button** on every workshop row — POST `/api/widget-defs/{id}/duplicate` clones the row with name "<original> (copy)" / "(copy 2)" / etc., reloads the editor pre-filled on the new row. (3) **Edit-warning popup** fires when you click Edit on a widget that's placed on N dashboards — three buttons: Cancel / Edit anyway (changes affect all placements) / Duplicate and edit (clones the row, edits go to the copy). Rows with no placements skip the modal. (4) Settings → TVs panel: the per-row URL is now a clickable hyperlink that opens in a new tab; the separate Copy button is gone (right-click → copy on the link works).

### 2:12 PM

- **Top-nav "My Dashboards" link + cleaner pallets-by-WC widget rendering** — two small follow-ons after the widget-workshop closeout. (1) The main top nav on every page (`/`, `/recycling`, `/new-vs`, `/staffing`, `/trophies`, `/settings`) gains a **My Dashboards** entry pointing at `/dashboards` so the custom-dashboards surface isn't buried under Settings. (2) The Pallets-by-Work-Center widgets render cleaner at any size: the name column scales with widget width (`clamp(6rem, 22cqw, 11rem)`) so longer WC names like "Hand Build #1" don't get squeezed, long names ellipsis-truncate instead of wrapping into the bar, and a small `margin-block` between rows gives a visible gap. Addresses the "too tight, names jumbled" issue end-to-end without needing to resize the widget manually.

### 2:07 PM

- **Widget Workshop Phase 3 — closeout** — three polish items finish off the workshop spec. (1) **Custom dashboards can be added as TVs**: the Settings → TVs panel gains a "Custom Dashboard" kind with a cascading picker; the resulting `/tv/d/{slug}` URL renders the chosen custom dashboard with the row's saved theme. Deleting a custom dashboard nulls out any TV displays that referenced it (FK ON DELETE SET NULL) and shows a "dashboard removed" page when visited. (2) **Per-placement edit popover** on the dashboard editor — a small ⋮ button on each widget opens a schema-driven form to override that placement's data scope; ✕ deletes the widget from the dashboard. (3) **"In use by N" badge** in the Widget Workshop list, with the Delete button disabled for widgets referenced by any dashboard so it's obvious why a delete would fail. The widget-workshop master spec (sub-project 5) is now fully shipped: Workshop + custom dashboards + 8 widget types + TVs integration + per-placement overrides.

### 1:49 PM

- **Widget Workshop Phase 2 — 5 more widget types** — Pallets Banner (single-WC), Daily Progress (15-min color-coded bars), Cumulative Progress (cumulative SVG line chart with optional goal line), KPI Tile (units-today by WC or group, or downtime minutes), and Downtime Report (event list + total). All five join the Workshop alongside the Phase 1 trio (Pallets by WC, Vs Goat Pace, Monthly Ribbons), so any custom dashboard at `/dashboards/{slug}` can now drop in 8 different widget types. KPI metrics in Phase 2 are limited to today's units (per WC or group sum) and today's downtime minutes; more metrics can be added later by appending to the resolver's metric list. Phase 3 (TV Displays integration so a custom dashboard can be saved as a TV, plus per-placement data-override UI) still to ship.

### 1:35 PM

- **Widget Workshop & Custom Dashboards (Phase 1)** — two new surfaces let you build your own dashboards from a catalog of reusable widgets. **Widget Workshop** at `/widgets` is where you create named widget presets: pick a type, set a default data scope, choose a color/sort/etc., save. Three types ship in Phase 1: **Pallets by Work Center**, **Vs. Goat Pace**, **Monthly Ribbons** — covering the most-asked widgets from `/recycling` and `/wc/{slug}`. **My Dashboards** at `/dashboards` is where you assemble custom dashboards: name it, pick a scope (a WC or a group — drives the TV header), then drag widgets from the right-side palette and resize. Each placement can override its widget's default data scope, so the same "Pallets by WC" preset can show Repairs on one dashboard and Dismantlers on another. Flip any dashboard to a TV view at `/tv/dashboards/{slug}` — strips chrome, swaps in the TV header (scope name top-left + assigned operators top-right). Existing `/recycling`, `/new-vs`, `/wc/{slug}` dashboards are untouched — this is a sibling system, not a replacement. Links to both surfaces live in the Settings left rail. Phase 2 (KPI / daily progress / cumulative / downtime / pallets-banner widgets) and Phase 3 (TV Displays integration so custom dashboards can be saved as TVs, plus palette UX polish) ship later.

### 12:33 PM

- **TV dashboards are now fully editable, same as the screen versions** — open `/tv/recycling`, `/tv/wc/{slug}`, or any `/tv/d/{slug}` on the TV browser and drag/resize widgets right there. The per-widget edit button (⋮) is also visible on TV mode now, so axis / colors / numbers position can be tweaked without leaving the kiosk. Layout changes auto-save to the same `widget_layouts` row the editor URL uses, so the next 60 s refresh on every other TV showing the same dashboard picks up the new arrangement. Top nav, range chips, sub-nav, and the layout-save toolbar stay hidden on TV — only the edit chrome that's *useful at the kiosk* is back. Fixes the "Pallets by Work Center widget is too tight, names jumbled" issue by letting you make the widget bigger directly from the TV.

### 12:24 PM

- **Settings → TVs section** — central registry for every TV in the plant. Each row is a friendly name + which dashboard it shows + a light/dark toggle + a bookmarkable URL (`/tv/d/{slug}`). The first deploy seeds 10 default rows (Recycling VS, New VS, Junior 2, Repair 1/2/3, Dismantler 1/2/3/4) all in dark mode — toggle any to light and the TV picks up the change on its next 60 s refresh. The seed only runs on an empty table, so deleting a seeded row stays deleted across redeploys. Renaming a row regenerates the slug; a small note under the table warns that old URLs will break on rename. Also adds a Layout Templates table below Displays with a delete button for cleanup of templates saved via the WC editor. The existing `/tv/recycling`, `/tv/new-vs`, and `/tv/wc/{wc_slug}` URLs continue to work as default-dark fallbacks — no bookmarks shipped earlier today break. This is the final sub-project (4 of 4) in the TV dashboards spec.

### 10:39 AM

- **Per-WC dashboard layout templates** — arrange Repair 1's six widgets once, then fan that arrangement out to every other WC with a click. New `tv_dashboard_templates` table stores named layout snapshots. The `/wc/{slug}` editor now has **Save as template…** and **Apply template…** popovers above the widget grid: pick a template, choose "this WC only" / "every WC in this group" / "every WC", click Apply. Underlying API: `POST /api/tv-templates` (save), `GET /api/tv-templates` (list), `DELETE /api/tv-templates/{id}`, `POST /api/tv-templates/{id}/apply` with `targets` accepting an explicit page list, `group:<name>`, or `"all"`. Theme is stored per template per the spec but theme propagation to target WCs waits for sub-project 4 (Settings panel + tv_displays table).

### 10:07 AM

- **Per-work-center TV dashboards** — every WC can now have its own URL on a TV mounted at the workstation. Editor at `/wc/{slug}` (drag/resize the six widgets, layout auto-saves); TV view at `/tv/wc/{slug}` (read-only, no chrome, `?theme=light` for a bright-area TV, 60 s auto-refresh). Widgets: pallets banner (today's count vs prorated goal for THIS WC), daily progress chart (cumulative 15-min buckets), GOAT race (status pill + WC group's all-time GOAT pace), monthly ribbons (group's top-3 person-days), 15-min increments (color-coded green/amber/red), downtime report (events derived from active-interval gaps + total minutes). Header shows WC name top-left + assigned operator names top-right (only the people scheduled for THIS WC, not the whole group). Sub-project 2 of 4 in the TV-dashboards spec; layout templates + the Settings panel still to follow.

### 9:19 AM

- **TV mode for the Recycling + New value-stream dashboards** — two new permanent URLs designed to live on a TV browser: `/tv/recycling` and `/tv/new-vs`. No top nav, no range chips, no per-widget edit buttons, no sub-nav — just the data with bigger fonts. Dashboard title sits top-left ("Recycling VS" / "New VS") so anyone walking by knows what's on screen. Page auto-refreshes every 60 s. Dark theme by default; pass `?theme=light` for a bright-area TV. Gridstack drag is disabled on the recycling TV variant so a stray touch can't reshuffle the widgets. The screen versions (`/recycling`, `/new-vs`) are byte-identical to before — TV mode is gated entirely on a `tv_mode` context flag the new routes set. First of four sub-projects in the TV-dashboards spec; per-WC dashboards + dashboard templates + Settings panel to follow.

### 8:11 AM

- **Time Off section now hides Roster-Filter-excluded people for real** — Isaac Miller kept showing as "Absent" on the scheduler even after the prior fix because `stratustime_client.derived_absences_for_day` independently flagged him as a scheduled-but-no-punch derived absence, using the StratusTime full-name map as a fallback when our roster map didn't carry his emp_id (which is exactly what happens for an excluded person). That bypassed the late_report-level filter entirely. Added a final pass at the end of `time_off_entries_for_day` (and its bulk-range sibling) that drops every entry whose name isn't in the active non-excluded roster — so derived absences, StratusTime time-off requests, non-work shifts, and manual absences all flow through the same gate now. New `/admin/person-state?name=...` admin endpoint kept around since it was the only way to spot the underlying issue.

### 8:01 AM

- **Top nav fixes: drop "Leaderboards" from `/settings` and home** — earlier nav restructure removed the "Leaderboards" top-tab from the staffing-family templates, `/staffing`, `/recycling`, and `/new-vs`, but missed `settings.html` (the Settings page kept showing 5 tabs) and `index.html` (the very first page if anyone hits `/`). Now the top nav reads **Dashboards · Trophy Case · Staffing · Settings** consistently across every page in the system, with **Trophies** and **Leaderboards** as sub-tabs only under Trophy Case.

### 7:39 AM

- **Archived-in-Odoo people now auto-drop from the scheduler** — Isaac Miller (and any other employee you archive in Odoo) used to keep `people.active = TRUE` locally forever because the sync only upserted what Odoo returned and never marked the disappeared rows inactive. Two fixes: (1) the Odoo sync now flips `active = FALSE` for any local person whose `odoo_id` is missing from the response (guarded so an empty response doesn't wipe everyone out); (2) `late_report.absences_for_day` / `absences_for_range` / `late_arrivals_for_day` now LEFT JOIN `people` and skip rows whose person is archived or roster-filter-excluded, so even a stale `manual_absences` record from before the archive stops surfacing as "Absent". The historical row is preserved in Postgres for audit.

## 2026-05-12

### 3:35 PM

- **Internal refactor pass — no user-visible changes** — four cleanups while the trail was fresh: (1) gate `test_shift_config_for.py` + `test_dashboards_polish.py` on `DATABASE_URL` so the local pytest suite is now 199 pass / 74 skip / **0 fail** (was 11 fail); (2) drop the vestigial `client` arg from `production_history.daily_records` / `attribution_range` / `attribution_per_day` after the precompute cutover left it unused, plus the matching unused `client` imports in two route files; (3) extract `static/staffing-print.css` (212 lines of @media-print rules) from staffing.css so the next "the PDF looks wrong" lands in a dedicated file; (4) split the four late-report mutation endpoints into a new `routes/late_report.py` module, shrinking the 1280-line `routes/staffing.py` by ~115 lines. URL routes and behavior unchanged.

### 7:43 AM

- **Leaderboards moves under Trophy Case as a sub-tab** — top nav drops "Leaderboards" as its own entry and the Trophy Case top-tab now stays active when you're on either `/trophies` or `/staffing/leaderboards`. A new `_trophies_subnav.html` shows two sub-tabs underneath: **Trophies** (the existing trophy case home) and **Leaderboards**. The staffing sub-tabs (Plant Scheduler, Time Off, Skills Matrix, People, Past Schedules) no longer appear on the Trophy Case page — that was bleed-through from the shared base template's sub-nav logic. URL for leaderboards is unchanged (`/staffing/leaderboards`) so existing bookmarks and the share-to-Slack permalinks still work.

### 7:37 AM

- **Printed schedule no longer clips multi-line WC notes** — the per-WC notes column is a `<textarea rows="1">` on screen, which Chromium honored verbatim in the PDF so long notes were cut after one line. Each cell now also emits a `<div class="wc-note-print">` carrying the same text; screen CSS hides it, print CSS hides the textarea and shows the div instead. The div auto-grows with content (word-wrap + pre-wrap), so the row stretches to fit the full note. Adjacent cells in the row top-align so the long note no longer appears to float in space.

## 2026-05-11

### 3:48 PM

- **Month display switches from `YYYY-MM` to "Month YYYY"** — the Trophy Case Monthly Ribbons section header now reads "Monthly Ribbons (April 2026)" instead of "Monthly Ribbons (2026-04)", and the month-picker dropdown options follow the same format. Same change applied to the player-card trophy-case tooltip text for monthly ribbon icons. New `month_name(m)` Jinja global wraps `calendar.month_name` so future templates can reach for it.

### 3:05 PM

- **Hover tooltips no longer clip off the left/right edge of the screen** — both the small 🐐 GOAT badge tooltips and the bigger player-card trophy icon tooltips were rendering past the viewport when the icon sat near the left or right edge (centered popups + `transform: translateX(-50%)`). A tiny shared script measures the icon's viewport position on hover and toggles `.tip-anchor-left` / `.tip-anchor-right` so the tooltip's left or right edge clamps to the icon instead of overflowing. Wired into the shared base template so every staffing-family page picks it up; the three standalone templates (scheduler, recycling, new-vs) include it explicitly.

### 2:06 PM

- **Trophy Case → Player Card jumps + Player Card trophy-icon redesign** — every operator name on the Trophy Case page (GOAT cards, Annual top-days, Annual best-avg, per-WC best-avg, Monthly ribbons) is now a hyperlink to that person's player card. Subtle hover-only dotted underline keeps the page readable. On the player card itself, the "Trophy case" section is now a row of icon-only trophies at ~3x the previous size; hovering any icon pops a dark pill tooltip with the full detail (group, year, units, pph, day, etc.), and clicking jumps straight to the matching anchor on `/trophies` (e.g. `#annual-repairs`) so you land in context.

### 2:03 PM

- **GOAT badges on the Recycling + New value-stream dashboards + better hover** — the 🐐 icons now also appear next to operator names on the dashboard widgets (both today's per-WC bars where a person is assigned, and the per-person bars in range views). The hover tooltip was rebuilt: no more question-mark cursor, no more half-second browser delay, no more tiny black-on-black text — hovering any 🐐 instantly pops a bigger pill-shaped tooltip with the group name. Same visual treatment everywhere the badge appears (scheduler, leaderboards, skills matrix, past schedules, player cards, and now dashboards).

### 1:57 PM

- **Trophy Case Annual + Monthly sections now use a side-by-side grid** — the per-group blocks in Annual Trophies and Monthly Ribbons no longer stack as full-width rows; they flow into the same kind of responsive 4-up grid the GOATs section already uses. New shared `.tc-grid` class wraps both loops with `repeat(auto-fit, minmax(280px, 1fr))` so the layout adapts to viewport width while keeping at least 280px per card for readable per-WC rows in the Annual blocks.

### 1:55 PM

- **Trophy Case section relabel + Annual icons re-skinned as trophies** — the "Annual" section is now **Annual Trophies** and its top-3 person-days render as 🏆 trophies tinted gold (1st), silver (2nd), bronze (3rd) via CSS filter. The "Monthly" section is now **Monthly Ribbons**; its 🥇🥈🥉 ribbon icons are unchanged. The Annual section's best-avg and per-WC rows already used 🏆 — no change there.

### 1:48 PM

- **🐐 GOAT badges next to every employee name** — anywhere an operator's name appears (Plant Scheduler, Leaderboards, People Matrix, Past Schedules, player-card headline), a 🐐 emoji now sits next to it for each group they currently hold the all-time GOAT record in. Hover over the icon to see which group ("GOAT — Repairs", etc.). People who hold GOAT in multiple groups get one icon per group, stacked. Updates within ~5 min of any change (in-process cache TTL) and recomputes from `production_daily` each render — so if anyone takes the GOAT record away on a given day, the badge moves overnight after the nightly precompute. Trophies page itself unchanged — names there are already grouped under their GOAT scope.

### 12:29 PM

- **Live warmer + nightly precompute job both running** — third in-process asyncio task ticks every 45 s and refreshes today's StratusTime attendance, today's time-off entries, and today's `production_daily` rows (so MTD leaderboards include today's partial-day data). The scheduler day-view and `/api/late-report` both now read from the live cache instead of blocking on StratusTime in the request path — measured median is ~285 ms for `/api/late-report`, ~1.9 s for `/staffing` (includes full template render). Nightly `/admin/precompute-run` is scheduled in Windows Task Scheduler at 3:30 AM daily, hitting yesterday by default; logs land at `%USERPROFILE%\Logs\zira-precompute-YYYY-MM-DD.log`. Backfill for 2026-02-05 → 2026-05-10 already complete (61 rows from 10 scheduled days; the rest of the year had no published schedules to attribute). Going forward every scheduled day shows up in `production_daily` overnight.

### 10:39 AM

- **Backend speedup — daily-OK pages now read from a precomputed fact table** — leaderboards, player cards, trophies/awards, and value-stream production views previously recomputed per-person attribution from raw Zira on every page hit. They now read from a new `production_daily` table populated by `POST /admin/precompute-run` (default = yesterday; with `from`/`to` query params = backfill). Three core history functions (`daily_records`, `attribution_range`, `attribution_per_day`) keep their existing signatures but now run a single SUM/GROUP BY against `production_daily` instead of parallel-fetching per-day attribution. The user-visible speedup lands once the table is backfilled and the live warmer (next deploy) keeps today's row fresh. Award overrides flow unchanged.

### 9:12 AM

- **Late/absence report: modal now auto-clears after a save** — declaring someone absent and picking a reason no longer leaves the row stuck showing "Saving…". The report re-pulls fresh data on save; the saved row drops out, and if nothing actionable is left the popup closes itself automatically. Multiple late/absent people still keep the popup open until you've worked through them.

## 2026-05-07

### 1:07 PM

- **Trophy system — badges, trophies, GOAT awards** — three tiers of recognition derived from production data: **monthly badges** (Gold/Silver/Bronze for top single-day units in each group's WCs), **annual trophies** (top-3 days per group + best avg pph per group + best avg per individual WC, both with a 30-day floor), and all-time **🐐 GOAT awards** (best single-day units in each group, holder displaced only by a strictly better day). New **Trophy Case** sub-tab between Leaderboards and Staffing with year/month pickers; player cards now show a **Trophy case** section listing every award the operator currently holds. Manual ✏️ button on every awarded slot lets you reassign, delete, or reset to computed (corrections persist in `award_overrides`).

### 12:20 PM

- **Player card stats redesign** — at the top of `/staffing/people/{name}`, the **Total units** bubble is gone, replaced by a row of **group-average pph** tiles (Repairs, Dismantlers, Juniors, etc.). One tile per registered group; tiles auto-hide when the operator has no hours in any of that group's WCs. The per-WC table below now has an **Avg (pph)** column alongside Units, and the table headers right-align with their numbers (the old `th.num` was left-aligned, putting headers offset from their numeric cells).

### 10:08 AM

- **Roster Filter — exclude specific Odoo employees from current views** — new sub-tab in Settings (between Work Centers & Goals and Company Schedule). Renders one row per Odoo-synced person with a checkbox; uncheck to hide that person from the People Matrix, scheduler pickers, player-card picklist, and late/absence report. The exclusion flag is local-only — Odoo's hourly sync preserves it across runs the same way it preserves the `reserve` flag. Historical data (past schedules, leaderboards, attendance records) is unaffected — old assignment data still references excluded people, by design.

### 9:33 AM

- **Skills Matrix rename + new People tab + clickable names on the scheduler** — three small staffing-nav improvements: (1) The Staffing sub-tab labeled "People Matrix" is now **Skills Matrix** — the matrix has always been about skills, so the label finally matches. (2) New **People** sub-tab (between Skills Matrix and Past Schedules) lands you straight on the first active roster member's player card; from there the new name picklist takes you anywhere else. (3) On the Plant Scheduler, every name in **Unscheduled**, **Time Off**, and **Reserves** is now a hyperlink to that person's player card. Click-to-clear on partial Time-Off entries still works — clicking a name navigates, clicking anywhere else on the row clears the partial.

### 9:21 AM

- **Player card name is now a dropdown** — at the top of `/staffing/people/{name}`, the operator's name is a picklist of every active roster member. Pick a name to jump straight to that person's card without going back to the People Matrix. The current date range (From/To) carries through the navigation, so switching cards keeps your selected timeframe.

### 8:55 AM

- **Quick-pick reasons now save instantly — one click, done** — clicking **Sick / Car issues / Overslept** in the late/absence popup no longer requires a follow-up Save click. The button fills the reason input and immediately fires the save. **Other** still clears the input and waits for you to type a custom reason, then Save records it.

### 8:44 AM

- **Late/absence report: reason required before any record posts** — Snooze still files instantly with no reason (it's transient by design). But Declare Absent and the auto-detected Late Arrival entries now refuse to post until a non-empty reason is captured. Backend: both endpoints return 400 if the reason is empty/missing. UI: the Save button on each reason editor stays disabled until the input has content; clicking a quick-pick (Sick / Car issues / Overslept) enables Save instantly because it fills the input. The "Other" quick-pick clears the input and waits for you to type, so Save stays disabled until you do. Net effect: nothing lands in `manual_absences` or `late_arrivals` without a reason attached.

### 8:36 AM

- **Late/absence report no longer flags people on StratusTime time off** — operators with an active approved time-off entry today (full-day or partial) are officially excused; the report shouldn't be calling them out as late or no-punch. `_safe_attendance` now drops them from both the scheduled and unscheduled id lists before fetching attendance, so they never enter the report at all. The Time Off section on the scheduler is still the single source of truth for who's out.

### 8:26 AM

- **Late/Absence report covers unscheduled people, captures reasons, and the Player card grows an Attendance section** — three coupled improvements: (1) The popup now lists active non-reserve operators who didn't punch in even if they weren't on today's schedule (Gerardo Vergara would now show up alongside Isaac Miller). Same Snooze and Declare Absent buttons. (2) When someone clocks in past the late threshold, the popup auto-surfaces a "Late arrivals — reason needed" entry. Quick-pick buttons (Sick / Car issues / Overslept / Other) populate a short text field; click Save to record. Declare Absent now also has an inline reason editor (optional). (3) Each player's card at `/staffing/people/{name}` gains an Attendance table showing per-day Absent / Late history with reasons in the active range, plus two new tiles (Days Absent, Days Late). Reason cells are inline-editable so today's "(no reason)" entries can be filled in later from the card.

## 2026-05-06

### 3:04 PM

- **pallets/hr/person now reads correctly — fixes the bad denominator from earlier today** — Dale's `pph_debug` dump showed two compounding bugs that together pushed pph to 28.1 vs the expected ~63. (1) The man-hours filter used `work_centers_store.value_stream(loc) == "Recycled"`, the user-editable DB-backed setting. Loading/Jockeying, Tablets, and Work Orders were configured there as "Recycled" (so they show up under the Recycled value-stream rollup) but their actual `loc.department` is "Supervisor"/"Maintenance" — they're forklift and mechanic support roles, not production-line labor. They were adding 8 ghost operators × full window of hours to the man-hours total. Switched the filter to `loc.department == "Recycled"`, the static schema field that's the right source of truth for "is this a recycling-line WC?". (2) `effective_minutes_worked` was returning gross minutes — it didn't subtract scheduled lunch + cleanup breaks. Other parts of the dashboard (e.g., `pallets_per_hour`) use the break-adjusted `shift_elapsed_minutes`, so the two denominators were inconsistent. `effective_minutes_worked` now also subtracts breaks that overlap the requested window. The two fixes together drop man-hours from 128.5 h to roughly 8 × 7 = 56 h, putting pph back near the hand-calc.

### 2:41 PM

- **pallets/hr/person no longer counts absent operators as labor** — Dale reported the headline stat reading 28.1 vs an expected ~65 mid-shift. Root cause: `effective_minutes_worked` only subtracted *partial-day* StratusTime time-off from per-person hours; full-day absences (8+ hour off requests, manual "Declare Absent" entries, and derived no-punch absences) were silently treated as full-shift labor. With ~half the recycling crew out today, total man-hours roughly doubled and the pph denominator inflated by ~2x. Now both the recycling and new-vs man-hours computations skip anyone whose name is in today's full-day-absent set before counting them. Partial-day off entries still get prorated the same way as before via `partial_off_intervals_for_day`.

### 10:34 AM

- **Today's production data now persists to Postgres on every fetch** — root cause of yesterday's data being blank. The previous flow saved past-day results to `zira_daily_cache` (Postgres) but kept today's results in-process only. When today rolled over (or Railway redeployed mid-shift), the in-process cache evaporated. The next-day lookup found nothing and had to lazy-load from Zira on first view — so days that nobody happened to browse before-or-after the rollover stayed blank. Now the persist call fires for any day with results, today included; `save_day` is idempotent (ON CONFLICT DO UPDATE), so the most recent today-fetch becomes the durable past-day record automatically. No more day-rollover gaps. To recover 5/5/26 specifically (and any other historical day already gone), run `/admin/zira-backfill?start=2026-05-05&end=2026-05-05` after this deploys.

### 9:47 AM

- **Admin endpoints for inspecting and backfilling historical Zira data** — past-day production has been lazy-loaded since the Postgres migration: the first time anyone views a past day, Zira gets called and the result is saved to `zira_daily_cache`. Days never browsed had no cached data. New endpoints:
  - **`GET /admin/data-status?start=YYYY-MM-DD&end=YYYY-MM-DD`** — inspects the DB and reports per-day: how many stations are cached in `zira_daily_cache`, whether a schedule row exists and is published, how many `schedule_assignments` rows exist. Read-only. No fetching. Use this first to see what's actually in the DB.
  - **`GET /admin/zira-backfill?start=YYYY-MM-DD&end=YYYY-MM-DD`** — proactively pulls Zira readings for every work-day in the range and saves them to `zira_daily_cache`. Idempotent (already-cached days skip via the Postgres-first lookup in `cached_leaderboard`). Capped at 90 days per request to stay under typical browser timeouts; for longer windows, invoke multiple times with non-overlapping ranges. Returns counts + a list of dates that came back with zero units.

### 9:13 AM

- **Historical production data now shows for past days regardless of publish status** — leaderboards, player cards, and the new operator drill-down popup were all empty for any past day where the schedule had never been formally Published. The hard gate was `attribution_for(d, client)` returning `{}` when `sched.published` was False — but most past days have a saved draft (assignments + people) without anyone ever clicking Publish, and by the time a day is in the past, the saved draft is the closest available record of what actually happened. Now: **today's** drafts still gate on Publish (so a supervisor mid-edit doesn't pollute live leaderboards with partial assignments), but **past** days use whatever's saved. Days with no saved schedule at all still show no per-person attribution — there's literally nothing to attribute against — but Zira-meter unit totals on the recycling/new-vs dashboards aren't affected (those don't depend on schedules). Existing pph-per-person fallback on the recycling dashboard (1 person per active WC) still kicks in for those no-schedule days.

### 8:51 AM

- **Leaderboards is now a top-level tab** — moved out of the Staffing sub-nav and into the main top nav between Dashboards and Staffing. Visible from every page in the app. When you're on the leaderboards page, that tab is highlighted (and Staffing is not), and the Plant-Scheduler/Time-Off/People-Matrix/Past-Schedules sub-nav strip is hidden — Leaderboards isn't a Staffing sub-page anymore. The previous spot at the right end of the Staffing sub-nav is gone; existing `/staffing/leaderboards` URLs still work.

## 2026-05-05

### 1:30 PM

- **Leaderboards range bar gets an "All Time" preset** — the range chip toolbar on `/staffing/leaderboards` was `Today | Week | Month | Quarter | Year | Custom`. Now adds **All Time** at the end (between Year and Custom) — start date pinned to 2024-01-01 (well before the plant's earliest production data), end date today. If older data ever shows up the constant in `deps._ALLTIME_START` is the only thing that needs nudging back. First click on All Time may take a moment on cold cache (the range fans out per-day to attribution data); subsequent clicks within the hour are cached.

### 1:22 PM

- **Fix: leaderboards drill-down popup now finds days for per-group widgets** — the popup said "No production days for this person in the selected range" when you clicked an operator name on any per-group averages widget (Repair, Dismantler, Junior, etc.) even when production clearly happened. The endpoint was resolving the group name by `loc.skill == group` (the work-center's category column from `staffing.LOCATIONS`), but leaderboards "groups" are actually user-defined memberships from the Settings → Work Centers → Groups list — different concept, different names. Now resolved via `work_centers_store.members("group", group_name)`, the same way the leaderboards page itself builds those widgets. Per-WC popups (Repair-1, Dismantler-3, etc.) were unaffected by the bug — those used the WC name directly.

### 1:10 PM

- **Drill-down popups on leaderboards averages widgets + per-day rows on the player card** — clicking any operator name on any averages widget on `/staffing/leaderboards` (per-WC and per-group, active and inactive) now opens a modal showing that person's days contributing to the widget's average over the page's selected range. Each row's date hyperlinks to the `/recycling` dashboard for that single day, and a primary "Open full player card" button opens the full player card with the same timeframe carried through. The player card itself now has a per-day-per-WC breakdown table below the existing per-WC summary, with date hyperlinks into the recycling dashboard. Speed-first: the leaderboards page render adds zero work for this feature; popup data lazy-loads on click via a new `/api/staffing/leaderboards/person-days` endpoint with TTL caching (1 h for past-only ranges, 60 s when today is included), so repeated opens skip re-aggregation entirely.

### 12:14 PM

- **Refactor pass — DRY, dead-code removal, shared helpers** — seven small commits to clean up duplication and trim the codebase by ~150 lines without changing behavior:
  - **Pyflakes clean** — dropped two unused imports (`psycopg2` in `db.py`; `SKILLS` in `work_centers_store.upsert_work_center`) and two never-read locals (`elapsed_hours`, `people_count` in `value_streams.py`).
  - **Shared `_who_by_wc` helper** — the recycling and new-vs routes each had a 24-line block that built the WC-to-operator label dict from schedule assignments + retro WC-attribution overlay (with dedupe). Extracted to one helper at the top of `value_streams.py`.
  - **Shared `_progress_color` helper** — same routes each defined a near-identical local function for the actual-vs-goal HSL color ramp. Hoisted to a single module-level function.
  - **Shared `resolve_range` helper** — three routes (recycling, new-vs's caller, leaderboards) hand-rolled the same custom-range parsing dance (try `?start`/`?end` as ISO dates, fall back to a named-window preset). Extracted to `deps.resolve_range()`.
  - **Shared `_cumulative_progress_chart.html` partial** — the cumulative-progress Jinja macro was duplicated verbatim between `recycling.html` and `new_vs.html`. Extracted to a partial template imported via `{% from %}` in both pages. Future macro changes happen in one place.
  - **Shift bounds computed once per day** — `_recycling_day_data()` was calling `shift_config.shift_start_for(d)` twice and `shift_config.shift_end_for(d)` twice per invocation under different local-variable names. Resolved once at the top, reused for the man-hours window and grace-interval math.
  - **Dropped local-file-storage-era code** — the bootstrap seed (`_SEED_ACTIVE`, `_SEED_INACTIVE`, `_SEED_SKILL_HINTS`, `_seed_roster`), the unused skill-matrix CSV importer, and the JSON-files schedule iterator (`SCHEDULES_DIR`, `_iter_saved_schedule_files`) all date from before the Postgres migration. None had callers. Net -80 lines.

  Behavior is identical — same outputs from the same inputs. Existing test suite still passes (174 tests). Eyeball the recycling and new-vs dashboards on Railway after deploy to confirm the cumulative chart and bar-chart goal lines render the same as before.

### 10:58 AM

- **Settings panels are always open now** — clicking `Company Schedule` or `Work Centers & Goals` in the settings sidebar used to take you to the section, but you still had to click the panel's chevron to actually see the form. Now both panels render as plain expanded sections — the sidebar is the only thing that decides what you're looking at, no extra click. Per-row pickers inside the Work Centers table (skills, default people, reserves) are still collapsible since expanding them all would make the table unreadable.

### 10:45 AM

- **Custom date-range popup auto-closes after Apply** — the popup used to stay open after submitting because the template re-rendered the `<details>` element with `open` set whenever a custom range was active. The Custom chip already shows the active range (`Custom: 2026-04-19 → 2026-04-25`), so leaving the popup open just got in the way. Now it closes after Apply and on page load with a saved custom-range URL.

### 10:41 AM

- **pallets/hr/person no longer reads 0 on days without a published schedule** — the recycling dashboard's per-day man-hours calculation iterated `sched.assignments`, so any day where Dale hadn't published a schedule contributed zero hours. In a range like 4/19–4/25 that crossed unpublished days, total man-hours got dragged down (often to zero) and `pallets/hr/person` collapsed to 0 even though units were clearly there. Now: if nobody was scheduled on a day but production still happened, each active WC (producing above the 5-unit activity threshold) counts as one person working the full shift window. Days with a published schedule keep using the real assignments — the fallback only kicks in when there's no scheduled labor at all.

### 10:25 AM

- **Recycling range toolbar — Last Week / Last Month + popup fix** —
  (1) Two new preset chips on the recycling dashboard's range bar: **Last Week** (Mon–Sun of the previous calendar week) and **Last Month** (1st through last day of the previous calendar month). Order in the toolbar pairs each "this" with its "last": `Today | Yesterday | This Week | Last Week | This Month | Last Month | Custom`.
  (2) Custom date-picker popup no longer overflows the right edge of the screen. The panel now anchors to the right side of the Custom chip and grows leftward into toolbar space rather than rightward off the viewport.

### 9:42 AM

- **Three dashboard chart fixes** —
  (1) Multi-day range progress charts now anchor 15-minute buckets to the global standard shift hours, so a custom-hours day starting at 7:18 no longer creates a duplicate 7:18 bar adjacent to the standard 7:15 bar — its production lands in whichever standard bucket each sample's actual timestamp falls into. Single-day pages, including single custom-hours days, are unchanged.
  (2) Pallets-by-Work-Center bar charts now show the per-WC vertical goal line in multi-day range mode (previously hidden). The line uses the per-WC expected production summed across the range, prorated by each day's productive intervals — so it correctly reflects the work centers' actual working time over the date range.
  (3) Both the 15-minute progress chart and the daily cumulative progress chart now render the actual unit count inside each bar (top-anchored, white text). The cumulative chart's label moved from above-the-bar to inside; the 15-minute chart got a new in-bar label. Empty buckets render no label.

### 7:29 AM

- **Post to Slack actually works again** — the underlying error the previous deploy surfaced was `int() argument must be a string... not 'Query'`. Cause: `share.py` calls the existing `/staffing` handler as a regular Python function to get the rendered HTML, but that handler uses FastAPI's `Query(default=...)` for `publish_blocked` and `view`. When you bypass the router and call the function directly, those defaults arrive *as Query objects*, not as their inner values — so `int(publish_blocked or 0)` inside the page builder blew up. Fixed by passing explicit `publish_blocked=0, view="draft"` at the call site, plus a regression test that asserts the kwargs are concrete ints/strings (so a future "cleanup" can't reintroduce the bug). Posting today's schedule to Slack should now succeed end-to-end.

### 7:22 AM

- **Post to Slack now shows the real error when it fails** — clicking Post to Slack was sometimes surfacing a cryptic `"Unexpected token 'I', "Internal S"... is not valid JSON"` toast. Cause: the `/staffing/share-to-slack` endpoint wraps three steps (render the schedule page → convert to PDF → upload to Slack), but only the last two had try/except. If the schedule render itself threw — DB hiccup, Zira API blip, StratusTime timeout, anything — FastAPI's default 500 returned a plain-text "Internal Server Error" page, which the client JS then choked on at `r.json()`. Now wrapped: the route always returns JSON, and the toast shows `"Schedule render failed: <actual error>"` so we can see what's actually breaking. (The underlying schedule-render failure is a separate bug; this just makes it visible.)

## 2026-05-01

### 8:45 PM

- **Refactor pass: caches, DRY, shared thread pool** — five small changes that compound:
  - `staffing.load_roster()` now caches in-process for 60s (invalidated on `save_roster()` and on Odoo sync). Was hitting Postgres with a JOIN-heavy query on every page render that touched the roster.
  - `stratustime_client` now uses a single module-level shared `ThreadPoolExecutor` for fan-out fetches inside `time_off_entries_for_day` and `_for_range`, instead of creating a fresh pool per call. Skips the per-call thread-creation overhead.
  - DRY'd the post-mutation cache-bust pattern: every endpoint that writes to Postgres was repeating `stratustime_client.cache_clear() + _bust_late_report_cache() + _http_cache.invalidate_today_cache()`. Now `_bust_after_mutation()` covers all three in one call.
  - Removed leftover debug `console.log` lines from the partial-clear and attribution-clear handlers.
  - Confirmed `staffing.load_schedule()` was already cached per-day with proper invalidation — no change needed there.

### 8:15 PM

- **× on saved retro WC attributions** — Jose Luis's "partial pill" turned out not to be a partial-day off entry at all — it was a saved retro WC attribution (manager said "Jose Luis worked 7:01a-1:01p here"). Different data source, different table, different fix. Every amber attribution pill on the scheduler grid now has a small white-on-red ✕ button. Click it, confirm, and the attribution is removed (calls the existing /api/staffing/attribute/{id} DELETE endpoint, then the leaderboards / dashboards stop crediting that person for that work center).

### 8:00 PM

- **Partial pill is now a real `<button>` with a visible ✕** — the previous span-with-`role="button"` rendered fine but document-delegated clicks on elements inside `<summary>` are unreliable in some browsers (the details toggle wins). Three changes: (1) the partial pill is now a real `<button type="button">`, which gets a built-in browser exemption from triggering the details toggle when clicked inside `<summary>`; (2) the time text now ends with a small `✕` so it visibly reads as a clear-action; (3) handlers are bound directly to each button on page load, not via document delegation, so the click can't be eaten by anything in between. Console logs `[partial] clear for <name> on <date>` so you can verify in DevTools that the click fires.

### 7:50 PM

- **Clear-partial finally works for everyone** — root cause: the click target was conditionally rendered based on whether the underlying StratusTime entry had a `request_id` OR an `emp_id`. Jose Luis's partial came from a path where both happened to be empty, so the pill rendered without the `clearable` class and clicks did nothing. New approach: clear by **name** (the natural key from the user's perspective). Every partial pill is now always clickable, and the backend writes (day, name) to a new `cleared_partials_by_name` table that gets honored on render. Same simplification on the Time Off section rows. The endpoint still accepts the old request_id/emp_id payload shapes for back-compat until the page reloads with new JS.

### 7:30 PM

- **/time-off page rebuilt around a single bulk fetch** — month view was making ~42 sequential calls to `time_off_entries_for_day`, year view ~365. Each call spawned its own thread pool and ran 5+ DB queries. New `time_off_entries_for_range` does the entire calendar in ONE pass: one StratusTime requests fetch, one non-work-shifts fetch, three bulk DB queries (cleared partials, cleared non-work, manual absences) — then bucketizes by day in memory. Should be 5-15× faster for month/year views.
- **/time-off page also gets HTTP response caching** — same 15s today / 5min past pattern as /staffing. Switching tabs back to the calendar now hits cache and serves in <1 ms.

**On StratusTime calls being slow in general** — the limit is HTTPS round-trip latency to `stratustime.centralservers.com`, ~200-500ms per call. There's no way to make a single uncached call faster, only to (1) avoid making it (caching, bulk windows), (2) parallelize so multiple calls share wall-clock time, or (3) keep caches warm so user requests never hit cold. We're now doing all three: 30 min cache on the employee directory, 5 min on time-off requests, bulk-window queries instead of per-day, parallel fan-out everywhere, and a 3-min background warmer.

### 7:00 PM

- **/staffing now caches its full HTTP response** — every previous render rebuilt the page from scratch (DB + StratusTime + Zira chain), even when nothing had changed. Now the rendered HTML is stashed in-process for 15 s today (5 min for past days). Most pageviews — including the reload after a partial-clear click, the redirect after a save, and tab-switching back to the page — serve from cache in <1 ms instead of 1-3 s. Mutations (POST /staffing save, hours edit, attribute, clear-partial, declare-absent, snooze) all invalidate the cache so saves still appear immediately.
- **Periodic StratusTime warmer** — a background task now re-warms the StratusTime caches every 3 minutes (employee directory, name maps, today's full time-off chain). Previously the prewarm fired only once at boot and caches expired after 5 min, meaning the first page request after that was a cold-cache hit. Now every user request lands on warm caches.

### 6:45 PM

- **Whole-site speed pass** — five concentrated changes that should compound into a 3-5× speedup on cold pages and basically eliminate the chain on hot ones:
  - `time_off_entries_for_day` was 5 sequential StratusTime/DB sub-fetches (time-off requests → roster maps → DB cleared sets → non-work shifts → derived absences → manual absences). Now all 7 of those run in parallel via a per-call ThreadPoolExecutor; on a cold path you wait for the slowest sub-call instead of the sum.
  - The `GetUserSchedule` call inside `derived_absences_for_day` was uncached — every call hit StratusTime fresh. Now cached 60s.
  - `list_employees()` was being re-fetched whenever both the `name_to_emp_id_map` and `_employee_id_to_name_map` caches missed (common pattern). Now caches the raw employee list directly for 30 min — derived maps stay at 5 min but they reuse the cached list.
  - `/api/late-report` (polled by every page's footer every 60s) now caches its full JSON response in-process for 30s. Most polls become a dict lookup instead of a StratusTime + DB chain. Cache busts automatically when anyone declares an absence, snoozes, or clears a partial.
  - `/staffing` page's tail work (Zira API call for unattributed WCs, DB query for saved attributions) moved into the same parallel pool that already runs alongside attendance — was sequential after the executor closed.
  - App-startup pre-warm now also warms today's full time-off chain (not just the employee directory) so the first user after a Railway redeploy hits warm caches everywhere.

### 6:15 PM

- **Click the partial pill itself to clear it** — the × button wasn't surfacing well visually, so the whole partial pill is now the click target. Hovering the amber 9-10a badge changes it to a darker amber and grows it slightly; clicking pops a confirm dialog with the person's name. Same on the Time Off section's partial rows. Capture-phase listener so the click doesn't toggle the WC's dropdown.

### 6:00 PM

- **Partial × button now works for non-work-shift partials too** — Jose Luis's partial wasn't a regular StratusTime time-off request, it was a manager-entered "non-work shift" via the V1 punch endpoint (no `request_id`), so the × button was silently never rendering for him. Two changes: (1) every partial pill now gets a × regardless of source — request-id partials post to `cleared_time_off`, non-work-shift partials post to a new `cleared_non_work_shifts (day, emp_id)` table. (2) Added the same fallback in the Time Off section's per-row clear and the "Cleared today" restore list.

### 5:45 PM

- **Absent entries now red** — only true absences (derived no-punch + manager-declared Manual Absent) render in red on the scheduler's Time Off section and on the time-off calendar. Unpaid Time and other non-work-shift entries stay blue with the rest of the planned time-off. Three states: blue (planned PTO / Unpaid), amber (partial), red (absent).

### 5:35 PM

- **Partial × button now actually visible** — the clear button on partial pills was rendering but with transparent-on-amber styling that made it invisible. Now a small white circle with a red × and a subtle shadow, scales up slightly on hover. Same treatment on the Time Off section's clear buttons.

### 5:30 PM

- **Clear a partial-day off when it's wrong** — every amber partial pill on the scheduler (and every partial entry in the Time Off section) now has a small × button. Click it, confirm, and that partial is hidden for the day: no badge, no `partial_hours_by_name` deduction, gone from the Time Off list. The StratusTime request itself isn't touched, so other days render normally. A "Cleared today" footer in the Time Off section lets you restore a mis-clicked clear with one click. Use case: Jose Luis filed PTO 9-10a but actually worked through it — × the partial and he goes back to being a normal scheduled person.
- **Wider scheduler + roomier scheduled column** — page max-width bumped 1600 → 1700. Per-WC notes column shrunk (36% → 22%) and the scheduled-people column expanded (22% → 36%) so long pill rows breathe on big monitors.

### 5:05 PM

- **Two real bug fixes from the diagnostic** — the debug endpoint surfaced what was actually wrong: (1) `derived_absences_for_day` had a `NameError: name 'timezone' is not defined` because the local datetime import didn't include `timezone`. The function was crashing silently every call (caught by a broad `except`), so Porfirio's derived absence never got added to today's time-off list — that's why he stayed in Unscheduled regardless of every other fix. Now imports `timezone` properly. (2) Name disambiguation was picking the wrong Jesus Moreno — when two candidates shared a last-name initial (Martinez + **Moreno** Carreon, both starting "M"), the roster's "Jesus Moreno" matched whichever was first in API order. Now does a full prefix match against the last name first ("Moreno Carreon" starts with "Moreno"), only falling back to single-letter init when the roster name is short-form like "Jesus M".

### 4:50 PM

- **Diagnostic endpoint + looser name-mapping** — Jesus Martinez and Porfirio still showing wrong despite the earlier fixes. Two changes: (1) the active-employee filter on name resolution was too strict (required `Status=='active'` literally), so anyone whose StratusTime Status field is empty/null/whitespace was getting excluded — likely Porfirio's case, which dropped him through to the StratusTime full-name fallback ("Porfirio Cazares Herrera") and broke the time-off-set filter. Now treats empty Status as active and only excludes explicit Inactive/Terminated/Suspended/Deleted. (2) Multi-candidate disambiguation prefers Status=='active' over equally-matching candidates, so the right Jesus M wins when there are several. (3) Added `/api/debug/staffing-diag?names=jesus,porfirio` for one-shot inspection of the full pipeline (employee list, name map, schedule, attendance, derived absences, time-off entries) so we can stop guessing.

### 4:30 PM

- **Late / Absence Report** — replaces the old per-person attendance badges (✓/⚠/✗/⏸) and per-WC rollup pill on the scheduler. New global red **🚨 N Late/Absence** badge sits next to Settings on every page whenever a scheduled person is more than 15 min past shift-start without a clock-in. Click the badge for a modal listing each late person with two actions: **Declare Absent** (writes to `manual_absences`, flows into the Time Off section, drops them from Unscheduled + the picker) or **Snooze 30 min** (silences the alert and re-checks automatically). Snoozed people show in a secondary list with a countdown.
- **Inline late highlight on the scheduler** — anyone who would appear in the report also gets a red ⚠ + pulsing border on their scheduler pill, so a quick glance at the bay shows where the gaps are. The flag clears the moment they punch in.

### 4:00 PM

- **Derived "Absent" now actually fires** — fix to a subtle bug in the attendance calculation: when StratusTime's status board returned a person's last transaction from a previous day (typical case for someone who clocked out yesterday and hasn't punched in today), the app left them as `unknown` instead of `no_punch`. The derived-absence filter only checked `no_punch`, so people like Porfirio (last punch 4/30, scheduled today, no clock-in) were never flagged. `attendance_for_day` now classifies "last transaction not on `day`" as `no_punch`, which is the semantically correct value for the rollup, the per-person ✗ badge, and the derived-absence path.
- **Name-mapping now filters out terminated employees** — Jesus Martinez was showing the ✗ "didn't punch in" badge even though he had clocked in. Root cause: `name_to_emp_id_map` was building its candidate pool from `GetUserBasic` SELECT-ALL, which includes terminated employees. If StratusTime had a terminated "Jesus *M-something*" who appeared before active Jesus Martinez in API order, the roster's "Jesus M" got mapped to the terminated emp_id, the attendance lookup returned empty for that id, and the active Jesus's punch went uncounted. Now skips anyone whose `Status` isn't "active" before adding them to the candidate pool.

### 3:15 PM

- **"Absent" status now derived from scheduled-but-not-punched** — StratusTime's "Absent" flag is computed in their UI in real time and isn't stored in any queryable record (verified via 7 endpoint probes against Porfirio's data). New `derived_absences_for_day` helper does the same derivation locally: scheduled in StratusTime today + no clock-in by shift-start + 30-min buffer + no existing time-off / non-work entry → flagged as Absent in our time-off list. Shows up in the scheduler's Time Off section, the /time-off tab, and gets the same picker-exclusion treatment as PTO and manual non-work entries.

### 3:00 PM

- **"Manual" / non-work-shift absences now show up too** — found that StratusTime's manager-entered manual absences (e.g., Pascual Moreno on 5/1 with status "Manual") don't go through `GetUserTimeOffRequest` at all. They're stored as "non-work shift" punches in the V1 `TimeGetPunchesByEmpIdentifier` endpoint with `InType='Start Non-Work'`. The app now queries that endpoint too and merges those entries into the same time-off list, so manual absences appear in the scheduler's Time Off section, the /time-off tab, and downstream filtering. PayTypeName (e.g., "Unpaid Time") still shows so the type is visible.

### 2:45 PM

- **Time-off cache shortened to 1 minute** (was 5 min) — when you add a new time-off in StratusTime, it now appears in the app within a minute instead of waiting up to five. The "Refresh now" link still works for instant updates.
- **Confirmed all time-off types are read** — verified by live probe that PTO, Unpaid Time, Early Leave – Late Start, and any other StratusTime PayTypeName flow through correctly. The app filter is StatusType == 1 (approved), with no filter on the type itself, so any approved time-off type will show.

### 2:30 PM

- **Dedupe scheduled vs attributed name** — if someone is both regularly scheduled at a WC AND has a retro attribution there (e.g., Jose Luis on 5/1), they no longer show up twice. The attribution version (amber pill with time range) wins; the scheduled pill is hidden. Same dedup applied to the dashboard bar charts (`who_by_wc`) so the bar's primary label doesn't read "Jose Luis + Jose Luis".

### 2:15 PM

- **Undo / Redo moved to top-right** — match the Settings page. The buttons sit next to the Testing-Day pill in the page header instead of inside the title bar.
- **Saved attributions now show on the schedule too** — when you save an assignment via the assign button, the person appears as an amber pill on that WC's row alongside the regularly-scheduled people. The pill carries a small white tag showing their time range (e.g., "Lauro · 9-10a"). Same color family as partial-day off entries.

### 2:00 PM

- **Per-WC attendance rollup now matches roster names correctly** — the rollup pill (✓ 3/4 / ⚠ N late / ✗ N missing) was undercounting because the StratusTime → roster name lookup only matched full "FirstName LastName" strings. The app's roster uses short names ("Lauro", "Jesus M") so most punches never matched and were silently dropped from the count. New matcher tries: (1) exact full match, (2) "First L" → "First L*" by last-name initial, (3) unique-first-name fallback. Same fix applied to partial-day off intervals + time-off entry display so `Lauro` (roster) now matches `Lauro Lopez` (StratusTime) consistently.

### 1:45 PM

- **Past Schedules tab now actually works** — was empty for everyone because the page was reading local JSON files (the pre-Postgres storage), but schedules now live in Postgres on Railway. Switched to a DB query that lists every saved day newest-first; click any row to expand and see what was scheduled.
- **Past Schedules delete fix** — the admin-password delete was unlinking a file that no longer exists; now does a `DELETE FROM schedules` (cascades to assignments + notes via FK).

### 1:30 PM

- **WC name-mapping bug fix** — Junior 2 and Trim Saw were incorrectly flagging as "no one assigned" even when the schedule had people there. Root cause: the Zira station list used short names ("Junior 2", "Trim Saw") while the schedule uses full WC names ("Junior #2", "Trim Saw 1"); the matching now goes through `meter_id` so they line up.
- **Removed Assignments to Do button from the scheduler title bar** — redundant since the global nav badge does the same thing.
- **"to Assign" instead of "to attribute"** on the global nav badge.
- **Badge sits to the right of Settings** instead of the left.

### 1:15 PM

- **Saved attributions update dashboards instantly** — the dashboards' bars and downtime widgets now layer retro-attributions into the `who` slot, so a saved name appears immediately on /recycling and /new-vs in place of `(no assignment)`. Save/delete also invalidates the server-side dashboard cache and forces a page reload, so the change is visible without waiting for the 15s TTL.
- **5-pallet fluke threshold** — WCs that produced 5 or fewer units no longer surface as Assignments to Do (matches the dashboards' existing "active" threshold). A stray sample on a quiet station won't generate noisy attribution prompts.

### 12:48 PM

- **Global Assignments to Do badge** — every page now shows a pulsing amber `⚠ N to attribute` pill next to Settings whenever metered production happened today at unscheduled WCs. Click → modal opens with the same picker + saved-today list as the scheduler. Save/delete works from any page.

### 12:44 PM

- **Downtime Report rows get the assign button too** — every `(no assignment)` line on /recycling and /new-vs (bars + downtime widget) now shows a `↪ assign` button when there's actual unattributed production for that WC.

### 12:39 PM

- **CSS variable cleanup** — `--warn` / `--warn-dim` / `--bad-dim` now defined in `recycling.css` and `new_vs.css` so the amber assign-button colors render correctly without depending on the staffing.css cascade.

### 12:35 PM

- **Retro attributions v1.1** — three follow-ups: (1) edit/delete saved attributions via × buttons in the scheduler modal's "Saved today" list, (2) detection broadened to all metered WCs (not just Recycling cell, so Junior 2 etc. now flag too), (3) the same `↪ assign` inline popover on /new-vs.

### 12:28 PM

- **Inline assign on /recycling** — `(no assignment)` lines on the Pallets-by-WC bars become clickable `↪ assign` buttons (today only). Click → small popover with person picker → Save → page reloads with the attribution applied.

### 12:23 PM

- **Retro WC attributions (v1)** — when a metered WC produced units today but had nobody scheduled (Lauro popped over to Dismantler 3 for an hour), an `Assignments to Do (N)` badge appears in the scheduler toolbar. Click → modal listing each unattributed WC with a person picker (any active employee, even those scheduled elsewhere). Save → leaderboards and dashboards credit the picked person within ~5 min.

### 12:09 PM

- **"Daily Plant Scheduler" title centered** — title sits alone at the top of the main panel; date picker, Today/Next Day, Hours, Reset, Undo/Redo, status pills, Print/Slack/Publish all moved to a centered row below.

### 12:15 PM

- **Patch notes upgrade** — entries now grouped by deployment time within each day. New deployments get briefly highlighted when you open the modal so unread items pop.

### 12:00 PM

- **Browser tab favicon** — every page now shows the GPI logo in the tab.
- **Unread-entry indicator** — a green dot appears on the "What's new" footer link when there's something you haven't read yet.
- **Patch notes added** — this changelog page (you're looking at it). Click "What's new" in the footer of any page.

### 11:30 AM

- **Cross-device responsive sweep** — pages now scale gracefully from 13" laptops to 27" monitors. New intermediate breakpoints around 1300-1400px tighten layouts on smaller screens without forcing the mobile stack.

### 11:00 AM

- **Staffing page ~3× faster** — parallel StratusTime fetches, token-fetch lock, startup pre-warm thread, past-day HTTP cache, and a `Server-Timing` HTTP header so we can profile from devtools.
- **Cleanup pass** — dropped the orphaned `schedule_time_off` Postgres table now that time-off comes from StratusTime live.

### 10:45 AM

- **Per-WC attendance rollup** — each work center row now shows "✓ 3/4" / "⚠ 4/4 (1 late)" / "✗ 1 missing" next to its min/max — scan a whole bay at a glance.
- **Attendance confirmation badges** — viewing today's scheduler after shift-start, each scheduled person shows ✓ on time, ⚠ +Nm late, ✗ no-show, or ⏸ clocked out. Live from StratusTime, refreshed every minute.

### 10:30 AM

- **Partial-day time-off math** — partial-off (e.g., Jesus 9-10a) now subtracts from `total_man_hours`, so `pallets/hr/person` is accurate. Each affected person also shows a small amber badge with their off range on the scheduler.
- **Time-off range display** — partial-day entries show "Early Leave · off 9-10a" instead of just "1h". Color-coded blue (full day) vs amber (partial).

### 10:20 AM

- **StratusTime time-off sync** — scheduler's Time Off section + the /time-off tab are now driven by StratusTime live, cached 5 min, with a "↗ Manage in StratusTime" deep-link and a Refresh button.
- **Time-off month view** — twice-as-tall cells, dropped Sunday column.

### 9:45 AM

- **StratusTime foundation** — client module + auth + Settings → Integrations panel showing connection status. Foundation for everything time-clock-related.

### 7:00 AM

- **Scheduler tighter middle** — 1600px max-width cap with auto margins so widgets sit closer to center on big monitors.
- **Next Day skips weekends** — Friday "Next Day" now jumps to Monday instead of Saturday.

## 2026-04-30

### 4:15 PM

- **Downtime report filter** — only Dismantler + Repair categories show in the recycling downtime widget.

### 3:45 PM

- **Recycling dashboard date ranges** — Today / Yesterday / This Week / This Month / Custom chips. Widgets aggregate across the range; 15-min progress + cumulative charts now sum the same time-of-day bucket across each day.

### 2:15 PM

- **Best Averages leaderboard** — leaderboards page now has two independent panes: Best Days (single-day records) on the left, Best Averages (per-person averages over the range) on the right. Each pane orders, hides, and lays out widgets independently.

### 1:50 PM

- **Today range chip fix** — the Today chip on /staffing/leaderboards now actually shows today's data instead of falling through to week.
- **Custom range popover** — From/To inputs on /staffing/leaderboards moved into a popover from the Custom button.
