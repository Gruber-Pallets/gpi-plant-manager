# What's New

Latest updates to GPI Plant Manager. Newest first. Each day is split by deployment time so you can tell what shipped together.

## 2026-05-07

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
