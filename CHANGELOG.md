# What's New

Latest updates to GPI Plant Manager. Newest first. Each day is split by deployment time so you can tell what shipped together.

## 2026-05-18

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
