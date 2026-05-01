# What's New

Latest updates to GPI Plant Manager. Newest first. Each day is split by deployment time so you can tell what shipped together.

## 2026-05-01

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
