# What's New

Latest updates to GPI Plant Manager. Newest first. Each day is split by deployment time so you can tell what shipped together.

## 2026-05-01

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
