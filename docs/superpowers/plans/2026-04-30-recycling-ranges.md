# Recycling Dashboard — Date Ranges

**Goal:** Add Today / Yesterday / This Week / This Month / Custom range support to `/recycling` so all widgets aggregate across the selected window.

**Architecture:** Refactor the `recycling()` GET handler so its current single-day computation lives in a private helper `_recycling_day_data(d, now, is_today_d)`. The route iterates each day in the selected range, calls the helper, and aggregates the resulting per-day dicts: per-WC totals sum, top-line stats sum (with weighted-avg uptime/PPH), 15-min buckets aggregate by **time-of-day label** (e.g., "07:00") summing actuals and targets across days. Single-day "Today" view is byte-identical to current behavior (range = `[today, today]`, no aggregation needed but the same code path).

**Tech Stack:** FastAPI, Jinja2, vanilla JS, Python 3.11+.

---

## Files to modify

1. **`src/zira_dashboard/deps.py`** — add `yesterday` to `_window_dates`.
2. **`src/zira_dashboard/routes/value_streams.py`** — refactor the `recycling()` handler. Add `_recycling_day_data()` helper. Aggregate across days.
3. **`src/zira_dashboard/templates/recycling.html`** — replace the day picker with chip strip + Custom popover. Update `bar_chart` macro to support `is_range` flag (drop "who" labels, drop target tick line). Update auto-refresh to gate on range-includes-today.
4. **`src/zira_dashboard/static/recycling.css`** — add chip strip + Custom popover styles (copy pattern from `leaderboards.css`).

The `/new-vs` and `/work-centers` pages are NOT touched in this pass.

---

## Task: Single implementer task — `recycling-ranges`

Dispatch as one subagent, two-stage review (spec compliance + code quality), commit, push.

### Step 1 — Add `yesterday` to `_window_dates`

In `src/zira_dashboard/deps.py`, find:

```python
def _window_dates(window: str, today_d: date) -> tuple[date, date]:
    """Return (start, end) inclusive for one of: today|week|month|quarter|year."""
    if window == "today":
        return today_d, today_d
    if window == "month":
```

Insert a `yesterday` branch BEFORE `month`:

```python
def _window_dates(window: str, today_d: date) -> tuple[date, date]:
    """Return (start, end) inclusive for one of: today|yesterday|week|month|quarter|year."""
    if window == "today":
        return today_d, today_d
    if window == "yesterday":
        from datetime import timedelta
        y = today_d - timedelta(days=1)
        return y, y
    if window == "month":
```

### Step 2 — Refactor `recycling()` to support ranges

In `src/zira_dashboard/routes/value_streams.py`, REPLACE the entire `recycling()` function body (lines 20-318 in current main) with this new version that delegates per-day work to `_recycling_day_data()` and aggregates.

First, ADD this private helper above the route definitions (after the imports). The helper is roughly the existing single-day body, returning a dict:

```python
def _recycling_day_data(d, now, is_today_d):
    """Compute the per-day numbers for the recycling dashboard.

    Returns a dict with the keys the route handler needs to aggregate:
      total_units, total_downtime, elapsed, available, uptime_minutes,
      total_man_hours, total_recycling_people,
      per_wc_units {name: int}, per_wc_downtime_min {name: int},
      per_wc_expected {name: float}, per_wc_who {name: str|None},
      per_wc_state {name: str},  # only meaningful when is_today_d
      dism_buckets, repair_buckets,  # list[dict] from progress_buckets
      stations_dism, stations_repair,  # list[StationTotal] for fallback
      shift_start_label, schedule_assignments,
    Days outside the working schedule (weekends) return zero-shaped values.
    """
    from .. import settings_store, shift_config, staffing, work_centers_store
    from ..leaderboard import cached_leaderboard as leaderboard
    from ..progress import progress_buckets
    from ..shift_config import shift_elapsed_minutes
    from ..stations import recycling_stations
    from ..deps import client, _state

    stations = recycling_stations()
    results = leaderboard(client, stations, d, now_utc=now if is_today_d else None)

    sched = staffing.load_schedule(d)
    who_by_wc = {}
    for wc_name, ops in sched.assignments.items():
        if wc_name == staffing.TIME_OFF_KEY or not ops:
            continue
        who_by_wc[wc_name] = " + ".join(ops)

    ACTIVE_UNITS_THRESHOLD = 5
    active_wc_names = set(who_by_wc.keys())
    for r in results:
        if r.units > ACTIVE_UNITS_THRESHOLD:
            active_wc_names.add(r.station.name)

    active_results = [r for r in results if r.station.name in active_wc_names]
    active_stations = [s for s in stations if s.name in active_wc_names]

    total_units = sum(r.units for r in active_results)
    total_downtime = sum(r.downtime_minutes for r in active_results)
    elapsed = shift_elapsed_minutes(d, now)
    available = elapsed * len(active_stations)
    uptime_minutes = max(0, available - total_downtime)
    elapsed_hours = elapsed / 60.0 if elapsed else 0.0
    total_recycling_people = sum(
        len(sched.assignments.get(loc.name, []))
        for loc in staffing.LOCATIONS
        if work_centers_store.value_stream(loc) == "Recycled"
    )
    total_man_hours = total_recycling_people * elapsed_hours

    dismantlers = [r for r in active_results if r.station.category == "Dismantler"]
    dismantlers.sort(key=lambda r: r.station.name)
    repairs = [r for r in active_results if r.station.category == "Repair"]
    repairs.sort(key=lambda r: r.station.name)

    # Productive intervals (existing logic — copied verbatim from old route).
    from datetime import datetime, timedelta, timezone
    shift_start_local = datetime.combine(d, shift_config.shift_start_for(d), tzinfo=shift_config.SITE_TZ)
    grace_end_local = shift_start_local + timedelta(minutes=60)
    grace_end_capped_local = min(grace_end_local, now.astimezone(shift_config.SITE_TZ)) if is_today_d else grace_end_local
    grace_interval_utc = (
        shift_start_local.astimezone(timezone.utc),
        grace_end_capped_local.astimezone(timezone.utc),
    )
    people_by_wc = {
        wc: len(ops) for wc, ops in sched.assignments.items()
        if wc != staffing.TIME_OFF_KEY and ops
    }

    def _merge(intervals):
        if not intervals:
            return []
        intervals = sorted(intervals, key=lambda x: x[0])
        out = [intervals[0]]
        for s, e in intervals[1:]:
            if s <= out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], e))
            else:
                out.append((s, e))
        return out

    breaks_utc = []
    for b in shift_config.breaks_for(d):
        bs = datetime.combine(d, b.start, tzinfo=shift_config.SITE_TZ).astimezone(timezone.utc)
        be = datetime.combine(d, b.end, tzinfo=shift_config.SITE_TZ).astimezone(timezone.utc)
        if be > bs:
            breaks_utc.append((bs, be))

    def _subtract_breaks(intervals):
        if not breaks_utc:
            return intervals
        chunks = list(intervals)
        for b_s, b_e in breaks_utc:
            new_chunks = []
            for c_s, c_e in chunks:
                if b_e <= c_s or b_s >= c_e:
                    new_chunks.append((c_s, c_e))
                    continue
                if c_s < b_s:
                    new_chunks.append((c_s, b_s))
                if c_e > b_e:
                    new_chunks.append((b_e, c_e))
            chunks = new_chunks
        return chunks

    grace_has_duration = grace_interval_utc[1] > grace_interval_utc[0]
    productive_by_wc = {}
    for r in active_results:
        ints = list(r.active_intervals)
        if r.station.name in people_by_wc and grace_has_duration:
            ints.append(grace_interval_utc)
        productive_by_wc[r.station.name] = _subtract_breaks(_merge(ints))

    def _productive_minutes(name):
        return sum((b - a).total_seconds() / 60.0 for a, b in productive_by_wc.get(name, []))

    def _make_target_fn(group):
        def fn(b_start_local, b_end_local):
            bucket_min = (b_end_local - b_start_local).total_seconds() / 60.0
            if b_end_local <= grace_end_local:
                tot = 0.0
                for r in group:
                    name = r.station.name
                    if name not in people_by_wc:
                        continue
                    tot += settings_store.station_target(r.station) * people_by_wc[name] * bucket_min / 60.0
                return tot
            tot = 0.0
            for r in group:
                hr = settings_store.station_target(r.station)
                if hr <= 0:
                    continue
                for ai_s_utc, ai_e_utc in productive_by_wc.get(r.station.name, []):
                    ai_s = ai_s_utc.astimezone(shift_config.SITE_TZ)
                    ai_e = ai_e_utc.astimezone(shift_config.SITE_TZ)
                    o_s = max(ai_s, b_start_local)
                    o_e = min(ai_e, b_end_local)
                    if o_e > o_s:
                        tot += hr * (o_e - o_s).total_seconds() / 60.0 / 60.0
            return tot
        return fn

    dism_buckets = progress_buckets(dismantlers, d, now, target_fn=_make_target_fn(dismantlers))
    repair_buckets = progress_buckets(repairs, d, now, target_fn=_make_target_fn(repairs))

    # Per-WC dicts the aggregator can sum.
    per_wc_units = {r.station.name: r.units for r in active_results}
    per_wc_downtime = {r.station.name: r.downtime_minutes for r in active_results}
    per_wc_expected = {
        r.station.name: settings_store.station_target(r.station) * (_productive_minutes(r.station.name) / 60.0)
        for r in active_results
    }
    per_wc_state = {r.station.name: _state(r, now, is_today_d) for r in active_results}
    per_wc_who = {r.station.name: who_by_wc.get(r.station.name) for r in active_results}
    per_wc_category = {r.station.name: r.station.category for r in active_results}
    per_wc_station_obj = {r.station.name: r.station for r in active_results}

    return {
        "total_units": total_units,
        "total_downtime": total_downtime,
        "elapsed": elapsed,
        "available": available,
        "uptime_minutes": uptime_minutes,
        "total_man_hours": total_man_hours,
        "total_recycling_people": total_recycling_people,
        "per_wc_units": per_wc_units,
        "per_wc_downtime": per_wc_downtime,
        "per_wc_expected": per_wc_expected,
        "per_wc_state": per_wc_state,
        "per_wc_who": per_wc_who,
        "per_wc_category": per_wc_category,
        "per_wc_station_obj": per_wc_station_obj,
        "active_wc_names": active_wc_names,
        "schedule_assignments": dict(sched.assignments),
        "dism_buckets": dism_buckets,
        "repair_buckets": repair_buckets,
        "shift_start_label": shift_start_local.strftime("%H:%M"),
    }
```

Then replace the `recycling()` route with this aggregating version:

```python
@router.get("/recycling", response_class=HTMLResponse)
def recycling(
    request: Request,
    window: str = Query(default="today"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    from datetime import date as _date, datetime, timedelta, timezone
    from .. import settings_store, shift_config, staffing, widget_customizer
    from ..deps import _window_dates

    today = datetime.now(timezone.utc).date()
    custom_range_active = False
    if start and end:
        try:
            start_d = _date.fromisoformat(start)
            end_d = _date.fromisoformat(end)
            if end_d >= start_d:
                custom_range_active = True
        except ValueError:
            start_d, end_d = _window_dates(window, today)
    if not custom_range_active:
        start_d, end_d = _window_dates(window, today)

    is_today = (start_d == end_d == today)
    is_range = (start_d != end_d)
    range_includes_today = (start_d <= today <= end_d)

    # Cache key includes both bounds.
    from .._http_cache import get_cached_response, set_cache_headers, store_cached_response
    cache_key = ("recycling", start_d.isoformat(), end_d.isoformat())
    cached = get_cached_response(cache_key, includes_today=range_includes_today)
    if cached is not None:
        return cached

    now = datetime.now(timezone.utc)

    # Walk every day in the range, computing per-day data.
    days = []
    cursor = start_d
    while cursor <= end_d:
        days.append(cursor)
        cursor += timedelta(days=1)

    per_day = [_recycling_day_data(d, now, d == today) for d in days]

    # Aggregate.
    total_units = sum(p["total_units"] for p in per_day)
    total_downtime = sum(p["total_downtime"] for p in per_day)
    total_elapsed = sum(p["elapsed"] for p in per_day)
    total_available = sum(p["available"] for p in per_day)
    total_uptime_minutes = sum(p["uptime_minutes"] for p in per_day)
    total_man_hours = sum(p["total_man_hours"] for p in per_day)

    uptime_pct = (total_uptime_minutes / total_available * 100.0) if total_available > 0 else 0.0
    pallets_per_hour = (total_units / (total_elapsed / 60.0)) if total_elapsed > 0 else 0.0
    pph_per_person = (total_units / total_man_hours) if total_man_hours > 0 else 0.0

    # Per-WC aggregation.
    agg_units: dict[str, int] = {}
    agg_downtime: dict[str, int] = {}
    agg_expected: dict[str, float] = {}
    agg_who_today: dict[str, str | None] = {}
    agg_category: dict[str, str] = {}
    agg_station_obj: dict[str, object] = {}
    agg_active_names: set[str] = set()
    schedule_today_assignments: dict[str, list[str]] = {}

    for p, d in zip(per_day, days):
        agg_active_names |= p["active_wc_names"]
        for name, units in p["per_wc_units"].items():
            agg_units[name] = agg_units.get(name, 0) + units
        for name, dt in p["per_wc_downtime"].items():
            agg_downtime[name] = agg_downtime.get(name, 0) + dt
        for name, exp in p["per_wc_expected"].items():
            agg_expected[name] = agg_expected.get(name, 0.0) + exp
        for name, cat in p["per_wc_category"].items():
            agg_category[name] = cat
        for name, obj in p["per_wc_station_obj"].items():
            agg_station_obj[name] = obj
        if d == today:
            agg_who_today = p["per_wc_who"]
            schedule_today_assignments = p["schedule_assignments"]

    # Buckets aggregated by time-of-day label.
    def _aggregate_buckets(per_day_buckets: list[list[dict]]) -> list[dict]:
        agg: dict[str, dict] = {}
        order: list[str] = []
        for day_buckets in per_day_buckets:
            for b in day_buckets:
                lbl = b["label"]
                if lbl not in agg:
                    agg[lbl] = {"label": lbl, "actual": 0, "target": 0, "in_progress": False}
                    order.append(lbl)
                agg[lbl]["actual"] += b["actual"]
                agg[lbl]["target"] += b["target"]
                if b["in_progress"]:
                    agg[lbl]["in_progress"] = True
        order.sort()
        return [agg[lbl] for lbl in order]

    dism_progress = _aggregate_buckets([p["dism_buckets"] for p in per_day])
    repair_progress = _aggregate_buckets([p["repair_buckets"] for p in per_day])

    # Group hourly target — average over total elapsed hours, summing per-WC expected.
    elapsed_hours_total = total_elapsed / 60.0 if total_elapsed else 0.0
    def _group_goal(category: str) -> float:
        if elapsed_hours_total <= 0:
            return 0.0
        total_expected = sum(
            agg_expected[name]
            for name in agg_expected
            if agg_category.get(name) == category
        )
        return total_expected / elapsed_hours_total
    dism_group_target = _group_goal("Dismantler")
    repair_group_target = _group_goal("Repair")

    # Build bar rows. For ranges, drop the "who" label; for single Today, keep it.
    customs_all = widget_customizer.load_all("recycling")

    def _progress_color(pct_of_target):
        if pct_of_target is None:
            return None
        p = max(0.0, min(200.0, pct_of_target))
        delta = p - 100.0
        if abs(delta) < 1.0:
            return "#ffffff"
        step = min(12, max(1, round(abs(delta) / 100.0 * 12)))
        sat = 55.0 + step * 2.0
        light = 65.0 - step * 3.5
        hue = 130 if delta > 0 else 0
        return f"hsl({hue:.0f}, {sat:.0f}%, {light:.0f}%)"

    def _bars(category: str) -> list[dict]:
        names = sorted(n for n in agg_active_names if agg_category.get(n) == category)
        out = []
        for name in names:
            units = agg_units.get(name, 0)
            expected = agg_expected.get(name, 0.0)
            pct_of_target = (units / expected * 100.0) if expected > 0 else None
            out.append({
                "name": name,
                "who": agg_who_today.get(name) if not is_range else None,
                "units": units,
                "pct_of_target": round(pct_of_target, 1) if pct_of_target is not None else None,
                "expected": int(round(expected)),
                "color": _progress_color(pct_of_target),
                "downtime_minutes": agg_downtime.get(name, 0),
            })
        max_u = max((r["units"] for r in out), default=0)
        max_e = max((r["expected"] for r in out), default=0)
        base = max(max_u, max_e)
        scale = (base * 1.1) if base > 0 else 1.0
        # Hide target tick line for multi-day ranges: it represents "where you should be by now"
        # which only makes sense for an in-progress single day.
        has_target_line = (max_e > 0) and not is_range
        for r in out:
            r["pct"] = (r["units"] / scale * 100.0) if scale else 0.0
            r["target_pct"] = (r["expected"] / scale * 100.0) if (scale and has_target_line) else None
        return out

    def _sorted_bars(items: list, widget_id: str) -> list:
        s = customs_all.get(widget_id, {}).get("sort", "preset")
        if s == "desc":  return sorted(items, key=lambda x: -x["units"])
        if s == "asc":   return sorted(items, key=lambda x: x["units"])
        if s == "alpha": return sorted(items, key=lambda x: x["name"].lower())
        return items

    def _downtime_rows(category_filter):
        names = sorted(
            n for n in agg_active_names
            if (category_filter is None or agg_category.get(n) == category_filter)
        )
        out = []
        for name in names:
            down = agg_downtime.get(name, 0)
            working = max(0, total_elapsed - down)
            total = total_elapsed if total_elapsed else 1
            out.append({
                "name": name,
                "who": agg_who_today.get(name) if not is_range else None,
                "working": working,
                "down": down,
                "working_pct": working / total * 100.0,
                "down_pct": down / total * 100.0,
            })
        return out

    now_local = now.astimezone(shift_config.SITE_TZ)
    now_label = now_local.strftime("%H:%M")
    shift_start_label = per_day[-1]["shift_start_label"] if per_day else ""

    # People count: total person-days across the range for ranges; today's count for Today.
    if is_range:
        dism_people = sum(p["per_wc_who"] and 0 or 0 for p in per_day)  # placeholder, replaced below
        dism_people = 0
        repair_people = 0
        for p in per_day:
            for name, ops in p["schedule_assignments"].items():
                if name == staffing.TIME_OFF_KEY or not ops:
                    continue
                cat = p["per_wc_category"].get(name)
                if cat == "Dismantler":
                    dism_people += len(ops)
                elif cat == "Repair":
                    repair_people += len(ops)
    else:
        dism_people = sum(
            len(schedule_today_assignments.get(name, []))
            for name in agg_active_names
            if agg_category.get(name) == "Dismantler"
        )
        repair_people = sum(
            len(schedule_today_assignments.get(name, []))
            for name in agg_active_names
            if agg_category.get(name) == "Repair"
        )

    response = templates.TemplateResponse(
        request,
        "recycling.html",
        {
            "active_vs": "recycling",
            "window": window,
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "today": today.isoformat(),
            "is_today": is_today,
            "is_range": is_range,
            "range_includes_today": range_includes_today,
            "custom_range_active": custom_range_active,
            "total_units": total_units,
            "total_downtime_minutes": total_downtime,
            "total_downtime_display": f"{total_downtime / 60:.1f} h",
            "uptime_pct": round(uptime_pct, 1),
            "pallets_per_hour": round(pallets_per_hour, 1),
            "pph_per_person": round(pph_per_person, 1),
            "elapsed_minutes": total_elapsed,
            "dismantler_bars": _sorted_bars(_bars("Dismantler"), "dismantler-bars"),
            "repair_bars": _sorted_bars(_bars("Repair"), "repair-bars"),
            "downtime_rows": _downtime_rows(None),
            "dismantler_progress": dism_progress,
            "repair_progress": repair_progress,
            "dismantler_group_target": dism_group_target,
            "repair_group_target": repair_group_target,
            "dismantler_people": dism_people,
            "repair_people": repair_people,
            "layout": layout_store.layout_map("recycling"),
            "customs": customs_all,
            "now_label": now_label,
            "shift_start_label": shift_start_label,
            "refreshed_at": now.strftime("%H:%M:%S UTC"),
        },
    )
    set_cache_headers(response, includes_today=range_includes_today)
    store_cached_response(cache_key, includes_today=range_includes_today, response=response)
    return response
```

Note: `Query` is already imported. The route still imports `_state`, `client`, `_parse_day` from `..deps` — keep `client` and drop the unused ones if they become orphans. Verify with grep.

### Step 3 — Update `templates/recycling.html` toolbar

REPLACE the existing `<form method="get" action="/recycling">...</form>` block in the header (currently lines 23-27) with the chip strip + Custom popover:

```jinja
<form class="rc-toolbar" method="get" action="/recycling">
  <span class="rc-tool-label">Range:</span>
  {% set windows = [('today', 'Today'), ('yesterday', 'Yesterday'), ('week', 'This Week'), ('month', 'This Month')] %}
  {% for wval, wlabel in windows %}
    <a href="/recycling?window={{ wval }}"
       class="rc-chip{% if window == wval and not custom_range_active %} rc-chip-on{% endif %}">{{ wlabel }}</a>
  {% endfor %}
  <details class="rc-custom-popover"{% if custom_range_active %} open{% endif %}>
    <summary class="rc-chip{% if custom_range_active %} rc-chip-on{% endif %}">{% if custom_range_active %}Custom: {{ start }} → {{ end }}{% else %}Custom{% endif %}</summary>
    <div class="rc-custom-panel">
      <label>Start <input type="date" name="start" value="{{ start if custom_range_active else '' }}"></label>
      <label>End <input type="date" name="end" value="{{ end if custom_range_active else '' }}"></label>
      <button type="submit" class="rc-chip rc-custom-apply">Apply</button>
    </div>
  </details>
</form>
```

The existing `<header>` wrapper and the `_value_streams_subnav.html` include stay unchanged.

### Step 4 — Update the `bar_chart` macro for ranges

In `templates/recycling.html`, find the `bar_chart` macro (`{%- macro bar_chart(widget_id, items) -%}`). The macro currently shows `b.who` as the primary line and the WC name as secondary, with "(no assignment)" subtext when `who` is missing. For ranges, the route already sets `b.who = None`. We need the macro to show ONLY the WC name (no "(no assignment)" subtext) when `is_range` is True.

The macro receives `is_range` from the template's outer context (Jinja closure). REPLACE the four "name primary/secondary" blocks in the macro with:

For the vertical orientation (`{% if orient == 'vertical' %}` branch, the inner `<div class="vbar-name">` block):

```jinja
          <div class="vbar-name">
            {% if is_range %}
              <span class="name-primary">{{ b.name }}</span>
            {% elif b.who and b.who != b.name %}
              <span class="name-primary">{{ b.who }}</span>
              <span class="name-secondary">{{ b.name }}</span>
            {% else %}
              <span class="name-primary">{{ b.name }}</span>
              <span class="name-secondary"><em>(no assignment)</em></span>
            {% endif %}
          </div>
```

For the horizontal orientation, same replacement inside the `<div class="name">` block:

```jinja
        <div class="name">
          {% if is_range %}
            <span class="name-primary">{{ b.name }}</span>
          {% elif b.who and b.who != b.name %}
            <span class="name-primary">{{ b.who }}</span>
            <span class="name-secondary">{{ b.name }}</span>
          {% else %}
            <span class="name-primary">{{ b.name }}</span>
            <span class="name-secondary"><em>(no assignment)</em></span>
          {% endif %}
        </div>
```

Also the bottom "axis-row" with the now-tick is for in-progress shifts only. Wrap it in `{% if not is_range %}`:

```jinja
    {% if widget_target_pct is not none and not is_range %}
      <div class="bar-row axis-row numpos-{{ numpos }}">
        ...
```

### Step 5 — Update the downtime widget for ranges

In `templates/recycling.html`, the `downtime-report` widget body has the same "name primary/secondary" pattern. Find it (around the `{% for d in downtime_rows %}` loop) and apply the same conditional:

```jinja
                <div class="name">
                  {% if is_range %}
                    <span class="name-primary">{{ d.name }}</span>
                  {% elif d.who and d.who != d.name %}
                    <span class="name-primary">{{ d.who }}</span>
                    <span class="name-secondary">{{ d.name }}</span>
                  {% else %}
                    <span class="name-primary">{{ d.name }}</span>
                    <span class="name-secondary"><em>(no assignment)</em></span>
                  {% endif %}
                </div>
```

### Step 6 — Update auto-refresh

In `templates/recycling.html`, find the bottom auto-refresh `<script>` block:

```jinja
{% if is_today %}
<script>setTimeout(() => location.reload(), 60000);</script>
{% endif %}
```

Replace with:

```jinja
{% if range_includes_today %}
<script>setTimeout(() => location.reload(), 60000);</script>
{% endif %}
```

### Step 7 — Update people count badges for ranges

In `templates/recycling.html`, find the two `<span class="people-count">` lines (one inside `dismantler-cumulative`, one inside `repair-cumulative`):

```jinja
        <span class="people-count" title="People scheduled in this group">{{ dismantler_people }}</span>
```

Replace with conditional title + label:

```jinja
        <span class="people-count" title="{% if is_range %}Total person-days in this group across the range{% else %}People scheduled in this group{% endif %}">{{ dismantler_people }}{% if is_range %} pd{% endif %}</span>
```

(Same for `repair_people` / `repair-cumulative`.)

### Step 8 — Add toolbar CSS

In `src/zira_dashboard/static/recycling.css`, append (or insert near the top — wherever it fits stylistically):

```css
  .rc-toolbar {
    display: inline-flex; flex-wrap: wrap; gap: 0.4rem; align-items: center;
    margin-left: 1rem;
    font-size: 0.85rem;
  }
  .rc-tool-label { color: var(--muted); margin-right: 0.2rem; }
  .rc-chip {
    display: inline-block; padding: 0.25rem 0.6rem; border-radius: 999px;
    background: var(--panel-2); color: var(--fg);
    border: 1px solid var(--border); text-decoration: none;
    font: inherit; font-size: 0.82rem; cursor: pointer;
  }
  .rc-chip:hover { border-color: var(--accent); color: var(--accent); }
  .rc-chip-on { background: var(--accent-dim); border-color: var(--accent); color: var(--accent); font-weight: 600; }
  .rc-custom-popover { position: relative; }
  .rc-custom-popover > summary {
    list-style: none;
    cursor: pointer;
  }
  .rc-custom-popover > summary::-webkit-details-marker { display: none; }
  .rc-custom-panel {
    position: absolute;
    top: calc(100% + 0.3rem);
    left: 0;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.6rem 0.7rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    box-shadow: 0 4px 12px rgba(0,0,0,0.12);
    z-index: 10;
    white-space: nowrap;
  }
  .rc-custom-panel label {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.82rem;
    color: var(--muted);
  }
  .rc-custom-panel input[type=date] {
    background: var(--panel-2); color: var(--fg); border: 1px solid var(--border);
    border-radius: 4px; padding: 0.25rem 0.4rem; font: inherit; font-size: 0.85rem;
    color-scheme: light;
  }
  .rc-custom-panel input[type=date]::-webkit-calendar-picker-indicator {
    cursor: pointer; filter: brightness(0.4);
  }
  .rc-custom-apply { align-self: flex-end; }
```

### Step 9 — Smoke check

```bash
.venv\Scripts\python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates')); env.get_template('recycling.html'); print('OK')"
.venv\Scripts\python.exe -m pytest tests/ -v
```

Expected: Jinja prints `OK`. Existing tests still pass (no test for /recycling, but pytest shouldn't regress).

Optional: try a TestClient hit on `/recycling?window=today`. If it requires DATABASE_URL, skip — we'll smoke on Railway.

### Step 10 — Commit and push

```bash
git add src/zira_dashboard/deps.py src/zira_dashboard/routes/value_streams.py src/zira_dashboard/templates/recycling.html src/zira_dashboard/static/recycling.css
git commit -m "Add date range support to recycling dashboard"
git push origin main
```

---

## Acceptance criteria

- `/recycling?window=today` renders identically to current single-day view.
- `/recycling?window=yesterday` shows yesterday's data.
- `/recycling?window=week` shows Mon→today's aggregated data: KPI sums, per-WC sums in bar charts, per-time-of-day-bucket sums in progress + cumulative charts.
- `/recycling?window=month` same shape, 1st-of-month → today.
- Custom popover opens from the Custom chip, accepts Start/End, applies via GET.
- Bar charts for ranges drop the "who" operator label and the now-tick axis row.
- Downtime widget for ranges drops the "who" label.
- People-count badge for ranges shows total person-days with " pd" suffix.
- Auto-refresh fires only when the range includes today.
- Cache key keys off `(start, end)` so different ranges don't collide.
- No regression to single-day "Today" view.
