# Dashboard Cumulative Buckets, Range Goal Lines, In-Bar Labels — Design

**Date:** 2026-05-05
**Status:** Approved (brainstorming → implementation planning)

## Context

Dale reported three related issues on the Recycling and New VS dashboards:

1. **Cumulative chart "doubles" 7:15 and 7:18 in multi-day ranges.** When
   a custom-hours day starts at 7:18 (instead of the standard 7:00), the
   `progress_buckets()` function anchors that day's bucket grid to 7:18,
   producing buckets labeled `07:18`, `07:33`, `07:48`, ... while standard
   days produce `07:00`, `07:15`, `07:30`. The aggregator
   (`value_streams.py:_aggregate_buckets`) groups by label, so the two
   schemes end up as separate, near-adjacent bars in the same chart.
2. **No vertical goal lines on Pallets-by-Work-Center bars in
   multi-day ranges.** The `_bars()` helper in `routes/value_streams.py`
   deliberately hides the per-bar target tick whenever `is_range` is
   true (gated by `has_target_line = (max_e > 0) and not is_range`).
   Dale wants the goal line to appear in range mode, reflecting the
   prorated expected production for each WC's actual working time
   across the range.
3. **Progress charts have no in-bar unit labels.** The 15-min progress
   chart shows nothing inside its bars; the cumulative daily progress
   chart shows the running total in a label *above* the bar. Dale wants
   the actual count rendered inside each bar in both charts.

All three changes are surgical extensions of existing infrastructure
rather than new components.

## Goals

1. In multi-day ranges, every progress chart bucket is anchored to a
   shared standard 15-min grid (`07:00`, `07:15`, `07:30`, ...).
   Custom-hours days contribute to whichever standard buckets their
   actual sample timestamps fall into.
2. Single-day pages — including single custom-hours days — keep their
   current per-day-shifted bucket boundaries unchanged.
3. The Pallets-by-Work-Center bar charts on the Recycling dashboard
   show the per-WC vertical goal line in multi-day ranges, using the
   already-aggregated `agg_expected[name]` value.
4. Both progress charts (15-min per-bucket and daily cumulative) render
   the actual unit count inside each bar.

## Non-goals

- Changing single-day chart behavior. Single custom-hours days continue
  to use their custom shift start as the bucket anchor — the visual
  "doubling" only happens in mixed-shift ranges.
- New widget customization options. The in-bar label is hardcoded as
  the new default; no per-widget toggle.
- Changes to the New VS dashboard's bar chart goal lines (it has no
  range mode and its bars don't carry a `target_pct` field).
- Changes to bucket WIDTH (still 15 minutes) or to break-skip behavior.
- Changes to the target prorating math. `target_fn` continues to use
  per-WC `active_intervals`, which already correctly handle custom
  hours.

## Design

### 1. Standard-aligned buckets in multi-day ranges

Add an `align_to_standard: bool = False` keyword argument to
`progress_buckets()` in `src/zira_dashboard/progress.py`. When `True`,
the function uses the global shift configuration for bucket boundaries
and break filtering instead of the per-day variants:

```python
def progress_buckets(
    group: Iterable[StationTotal],
    day: date,
    now_utc: datetime,
    bucket_minutes: int = 15,
    target_fn: TargetFn | None = None,
    align_to_standard: bool = False,
) -> list[dict]:
    ...
    if align_to_standard:
        s_start = shift_start()
        s_end = shift_end()
        breaks_iter = breaks()
    else:
        s_start = shift_start_for(day)
        s_end = shift_end_for(day)
        breaks_iter = breaks_for(day)
    start = datetime.combine(day, s_start, tzinfo=SITE_TZ)
    end = datetime.combine(day, s_end, tzinfo=SITE_TZ)
    ...
```

The `_in_any_break` helper currently calls `breaks_for(day)` internally.
Refactor it to accept the breaks iterable as a parameter:
`_in_any_break(breaks_iter, t)`. `progress_buckets()` passes whichever
iterable it picked above. Keeps the function pure and lets the new
align mode work without re-reading per-day config.

In `routes/value_streams.py`, the recycling route passes
`align_to_standard=True` to `progress_buckets()` only when
`is_range` is true:

```python
align = is_range
dism_buckets = progress_buckets(
    dismantlers, d, now,
    target_fn=_make_target_fn(dismantlers),
    align_to_standard=align,
)
repair_buckets = progress_buckets(
    repairs, d, now,
    target_fn=_make_target_fn(repairs),
    align_to_standard=align,
)
```

The `target_fn` already operates on `(b_start_local, b_end_local)`
intervals computed from per-WC `active_intervals` — those active
intervals are derived from sample timestamps + grace period, not from
shift bounds. So a custom-hours day that started production at 7:18
naturally has zero overlap with the standard `07:00–07:15` bucket
(target = 0) and a 12-minute overlap with the standard `07:15–07:30`
bucket. No new prorating logic is required.

**Edge case (accepted):** If a custom-hours day's shift end runs past
the standard global shift end (rare — most custom days only shift the
start), late samples will fall outside the chart's bucket window and
not appear in the range chart. Dale accepted this trade-off in
brainstorming.

### 2. Goal lines on Pallets-by-Work-Center bars in range mode

In `_bars()` inside `routes/value_streams.py`, change:

```python
has_target_line = (max_e > 0) and not is_range
```

to:

```python
has_target_line = (max_e > 0)
```

`agg_expected[name]` is already the per-WC expected production summed
across the range, prorated by each day's productive intervals (which
honor each day's custom hours and breaks via `_productive_minutes`).
The template renders the vertical tick at `b.target_pct` regardless of
range or single-day mode; only the gating in the helper changes.

No changes to `routes/value_streams.py:new_vs` — that route has no
range mode and its `bars` list has no `target_pct` field.

### 3. In-bar unit labels on progress charts

#### 3a. 15-min progress chart (`progress_chart` macro)

In `templates/recycling.html` lines 257–293, add an in-bar `<span>`
inside the bar fill, suppressed when `actual == 0`:

```jinja
<div class="col {% if hit %}hit{% else %}{% if b.in_progress %}hit{% else %}miss{% endif %}{% endif %} {% if b.in_progress %}in-progress{% endif %}"
     title="{{ b.label }} · {{ b.actual }} pallets (goal {{ b.target }})">
  <div class="bar" style="height: {{ h }}%">
    {% if b.actual > 0 %}<span class="bar-label">{{ b.actual }}</span>{% endif %}
  </div>
  {% if not b.in_progress and b.target %}<div class="target-tick" style="bottom: {{ t_h }}%"></div>{% endif %}
</div>
```

#### 3b. Cumulative daily progress chart (`cumulative_progress_chart` macro)

The existing `<span class="bar-label">` currently sits *outside* the
`.bar` div (above the bar). Move it inside:

```jinja
<div class="col {% if hit %}hit{% else %}miss{% endif %} {% if b.in_progress %}in-progress{% endif %}"
     title="{{ b.label }} · {{ '{:,}'.format(cum_a|int) }} cumulative (target {{ '{:,}'.format(cum_t|int) }})">
  <div class="bar" style="height: {{ h }}%">
    {% if cum_a > 0 %}<span class="bar-label">{{ '{:,}'.format(cum_a|int) }}</span>{% endif %}
  </div>
</div>
```

The `cumulative_progress_chart` macro is **duplicated** between
`recycling.html` (line 295) and `new_vs.html` (line 11). Apply the
same change in both files.

#### 3c. CSS

In `static/recycling.css` and `static/new_vs.css`, replace the existing
`.bar-label` positioning (which assumed the label sat above the bar)
with a child-of-`.bar` rule that anchors the label at the inner top
edge of the bar fill:

```css
.cum-progress .bar { position: relative; }
.cum-progress .bar .bar-label,
.progress .bar .bar-label {
  position: absolute;
  top: 2px;
  left: 0;
  right: 0;
  text-align: center;
  font-size: 0.7rem;
  color: var(--bar-label-color, #fff);
  pointer-events: none;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: clip;
}
```

When the bar is too short to fit the text, CSS clips it (no overflow
into adjacent bars). Dale will eyeball acceptability on Railway.

## Components and data flow

```
[StratusTime / Zira samples]
        |
        v
  leaderboard()  ── per-station StationTotal (samples + active_intervals)
        |
        v
  progress_buckets(..., align_to_standard=is_range)
        |   per-day list[dict] of {label, actual, target, in_progress}
        v
  _aggregate_buckets()  ── sums by label across days
        |   range-aggregated list[dict]
        v
  cumulative_progress_chart / progress_chart macros
        |   adds in-bar <span class="bar-label">
        v
  rendered HTML
```

The `_bars()` helper for Pallets-by-Work-Center charts is on the same
data path but a separate component:

```
  results × per-day  ── per-WC units, downtime, expected
        |
        v
  agg_units / agg_expected  ── range-summed per-WC totals
        |
        v
  _bars()  ── computes pct, target_pct (now always when max_e > 0)
        |
        v
  bar_chart macro  ── renders bar + vertical goal tick at target_pct
```

## Testing

**Unit tests** (`tests/test_progress.py`):

1. `test_progress_buckets_align_to_standard_uses_global_shift_start` —
   given a day with custom_hours start at 07:18 and `align_to_standard=True`,
   the first bucket label is `"07:00"` (or whatever the global shift
   start is in the test fixture).
2. `test_progress_buckets_sample_at_0718_lands_in_0715_bucket` — a
   sample at 07:20 with `align_to_standard=True` shows up in the
   `"07:15"` bucket's `actual` count.
3. `test_progress_buckets_target_prorates_partial_overlap` — given an
   `active_intervals` start at 07:18 and `align_to_standard=True`, the
   `"07:15"` bucket's target equals `per_hour * 12 / 60`.
4. `test_progress_buckets_default_align_unchanged` — without the new
   kwarg, behavior matches existing tests (regression guard).

**Route test** (`tests/test_value_streams.py`, new or extended):

5. `test_recycling_range_includes_target_pct_on_bars` — a 2-day range
   produces `dismantler_bars` entries each having a non-null
   `target_pct` when `expected > 0`.
6. `test_recycling_range_uses_standard_buckets` — when one of the
   range days has custom_hours `{start: "07:18"}`, the rendered
   `dismantler_progress` list contains `"07:00"`, `"07:15"`, `"07:30"`
   labels (no `"07:18"`).

**Visual / manual:**

- In-bar label CSS — eyeball on Railway after deploy. No automated
  visual regression testing in this project.

## Files touched

- `src/zira_dashboard/progress.py` — add `align_to_standard` kwarg and
  thread it through bucket-start, bucket-end, and break-filter logic.
- `src/zira_dashboard/routes/value_streams.py` — pass `align_to_standard=is_range`
  in the recycling range path; drop `not is_range` from
  `has_target_line`.
- `src/zira_dashboard/templates/recycling.html` — add in-bar label to
  `progress_chart` macro; move existing label inside bar in
  `cumulative_progress_chart` macro.
- `src/zira_dashboard/templates/new_vs.html` — same change in the
  duplicated `cumulative_progress_chart` macro (lines 11+).
- `src/zira_dashboard/static/recycling.css` — adjust `.bar-label`
  positioning for child-of-bar.
- `src/zira_dashboard/static/new_vs.css` — same.
- `tests/test_progress.py` — new tests (1–4) for `align_to_standard`.
- `tests/test_value_streams.py` — new or extended tests (5–6) for
  range-mode goal lines and standard buckets.

## Implementation notes

- `progress.py` currently imports only `breaks_for, shift_end_for,
  shift_start_for` from `.shift_config`. Add the global versions:
  `from .shift_config import breaks, shift_end, shift_start, ...`.
- The `target_fn` callbacks in `value_streams.py` (`_make_target_fn`)
  use `productive_by_wc` which is built from per-day
  `active_intervals` and `breaks_for(d)`. Those don't change. Only the
  bucket *boundaries* shift to standard hours.
- Cache invalidation is unaffected — the response cache is keyed on
  range bounds, not on shift configuration. Existing keys cover this.
