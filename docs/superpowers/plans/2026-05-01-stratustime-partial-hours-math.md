# StratusTime Sub-project #3 (math half) — Partial Hours Flow Through Dashboards

**Goal:** When StratusTime has a partial-day time-off entry for someone (e.g., Jesus 9-10a), have that subtract from the dashboard's man-hour calcs (and therefore from pallets/hr/person) AND show next to their name on the scheduler so it's visible at a glance.

**Architecture:** New `partial_off_intervals_for_day` helper in `stratustime_client.py` that returns `{name: [(start_utc, end_utc), ...]}`. New helper `effective_minutes_worked` (in `staffing.py`) that takes a person + day + window and returns minutes worked = window_minutes − overlap_with_partial_off_intervals. The two value-stream routes (`/recycling`, `/new-vs`) replace `total_man_hours = people * elapsed_hours` with a per-person sum across scheduled WCs. The scheduler template gets a small `(7h)` style badge next to people with partial-off on that day.

**Scope decisions for this first cut:**
- `total_man_hours` reflects partial-off on `/recycling` and `/new-vs`; `pallets/hr/person` therefore adjusts.
- Scheduler shows a per-person partial-hours badge on the day they have partial-off.
- The intra-day progress chart's grace-period target line (`_make_target_fn` first-60-min branch) is NOT adjusted in this pass — it would only matter for partial-off requests within the first hour of shift, which is rare. Defer.
- Multi-day partial-off requests: skip. Only same-day partial entries with explicit times affect math.

---

## File touch map

- **Modify:** `src/zira_dashboard/stratustime_client.py` — add `partial_off_intervals_for_day(day) -> dict[str, list[tuple[datetime, datetime]]]`. Returns UTC datetimes for clean overlap math.
- **Modify:** `src/zira_dashboard/staffing.py` — add `effective_minutes_worked(name, day, window_start_utc, window_end_utc) -> int`. Uses the partial-off cache.
- **Modify:** `src/zira_dashboard/routes/value_streams.py` — replace the man-hours calc in both `_recycling_day_data()` and the `new_vs()` route. New `total_man_hours` = sum of effective_minutes_worked across scheduled people, /60.
- **Modify:** `src/zira_dashboard/routes/staffing.py` — pass `partial_hours_by_name` (dict of {name: hours_off_today}) into the template context.
- **Modify:** `src/zira_dashboard/templates/staffing.html` — small badge on assigned-person pills when they have partial-off.
- **Modify:** `src/zira_dashboard/static/staffing.css` — style for the new badge.
- **Tests:** extend `tests/test_stratustime_client.py` with tests for the interval-overlap math.

No DB changes, no new env vars.

---

## Step 1 — Add interval helper to `stratustime_client.py`

Append after `time_off_names_for_day` (around line 343):

```python
def partial_off_intervals_for_day(day) -> dict[str, list]:
    """Return {name: [(start_utc, end_utc), ...]} of partial-off intervals on `day`.

    Only includes entries where:
    - StatusType == 1 (approved)
    - DurationPerDaySecs < 28800 (under 8h, i.e., partial)
    - StartDateTimeSchema and EndDateTimeSchema fall on the same day as `day`

    Returns datetime objects in UTC for overlap math against shift windows.
    Multi-day requests and full-day off entries are excluded.
    """
    from datetime import datetime as _dt, timedelta, timezone
    from .. import shift_config

    # Reuse the same 7-day window the existing helper queries to share cache.
    start_d = day - timedelta(days=3)
    end_d = day + timedelta(days=3)
    requests_ = get_time_off_requests(start_d, end_d)
    emp_map = _employee_id_to_name_map()
    out: dict[str, list] = {}
    site_tz = shift_config.SITE_TZ
    for r in requests_:
        if r.get("StatusType") != 1:
            continue
        secs = r.get("DurationPerDaySecs") or 0
        if secs >= 28800:  # full-day; not partial
            continue
        s_str = r.get("StartDateTimeSchema") or ""
        e_str = r.get("EndDateTimeSchema") or ""
        if not s_str or not e_str:
            continue
        if s_str[:10] != e_str[:10] or s_str[:10] != day.isoformat():
            continue  # not on `day` or spans multiple days
        try:
            s_local = _dt.fromisoformat(s_str).replace(tzinfo=site_tz)
            e_local = _dt.fromisoformat(e_str).replace(tzinfo=site_tz)
        except (ValueError, TypeError):
            continue
        s_utc = s_local.astimezone(timezone.utc)
        e_utc = e_local.astimezone(timezone.utc)
        if e_utc <= s_utc:
            continue
        emp_id = str(r.get("EmpIdentifier") or "")
        name = emp_map.get(emp_id)
        if not name:
            continue
        out.setdefault(name, []).append((s_utc, e_utc))
    return out
```

## Step 2 — Add `effective_minutes_worked` helper to `staffing.py`

Append at the bottom of `src/zira_dashboard/staffing.py`:

```python
def effective_minutes_worked(name: str, day, window_start_utc, window_end_utc) -> int:
    """Minutes the person `name` was actually working in [window_start_utc, window_end_utc]
    on `day`, after subtracting any partial-day StratusTime off-intervals that overlap.

    Falls back to the full window width when StratusTime is unreachable or the
    person has no partial-off on `day`.

    `window_start_utc` and `window_end_utc` must be timezone-aware UTC datetimes.
    """
    from . import stratustime_client
    if window_end_utc <= window_start_utc:
        return 0
    base = int((window_end_utc - window_start_utc).total_seconds() // 60)
    try:
        intervals_by_name = stratustime_client.partial_off_intervals_for_day(day)
    except Exception:
        return base
    intervals = intervals_by_name.get(name) or []
    overlap_min = 0
    for s, e in intervals:
        lo = max(s, window_start_utc)
        hi = min(e, window_end_utc)
        if hi > lo:
            overlap_min += int((hi - lo).total_seconds() // 60)
    return max(0, base - overlap_min)
```

## Step 3 — Wire into `/recycling` man-hours calc

In `src/zira_dashboard/routes/value_streams.py`, find `_recycling_day_data` (around line 20). Locate the existing man-hours block:

```python
total_recycling_people = sum(
    len(sched.assignments.get(loc.name, []))
    for loc in staffing.LOCATIONS
    if work_centers_store.value_stream(loc) == "Recycled"
)
total_man_hours = total_recycling_people * elapsed_hours
```

Replace with a per-person summation that uses the new helper:

```python
# Per-person effective minutes during [shift_start, now-or-shift-end] on this day,
# subtracting any partial-day StratusTime off intervals that overlap.
shift_start_local = datetime.combine(d, shift_config.shift_start_for(d), tzinfo=shift_config.SITE_TZ)
shift_end_local = datetime.combine(d, shift_config.shift_end_for(d), tzinfo=shift_config.SITE_TZ)
window_end_local = min(now.astimezone(shift_config.SITE_TZ), shift_end_local) if is_today_d else shift_end_local
window_start_utc = shift_start_local.astimezone(timezone.utc)
window_end_utc = window_end_local.astimezone(timezone.utc)

total_man_minutes = 0
total_recycling_people = 0
for loc in staffing.LOCATIONS:
    if work_centers_store.value_stream(loc) != "Recycled":
        continue
    for person_name in sched.assignments.get(loc.name, []):
        total_recycling_people += 1
        total_man_minutes += staffing.effective_minutes_worked(
            person_name, d, window_start_utc, window_end_utc,
        )
total_man_hours = total_man_minutes / 60.0
```

(Note: `shift_start_local` already exists earlier in this function; reuse the existing variable. The new `shift_end_local` and `window_*_utc` may need to be added.)

## Step 4 — Wire into `/new-vs` man-hours calc

In the `new_vs()` route (same file, around line 322), find:

```python
total_man_hours = total_new_vs_people * elapsed_hours
```

…or whatever the equivalent line is. Replace with the same per-person summation pattern, but iterating only over WCs where `work_centers_store.value_stream(loc) == "New"`.

## Step 5 — Show partial-hours badge on scheduler

In `src/zira_dashboard/routes/staffing.py`, after the existing `time_off_entries` block, add:

```python
# Per-person hours-off-today (for partial entries) so the scheduler
# can show a badge next to their name.
partial_hours_by_name: dict[str, float] = {
    e["name"]: e["hours"]
    for e in time_off_entries
    if e.get("hours") is not None and e["hours"] < 8 and e["hours"] > 0
}
partial_range_by_name: dict[str, str] = {
    e["name"]: e["time_range"]
    for e in time_off_entries
    if e.get("time_range") and e.get("hours") is not None and e["hours"] < 8
}
```

Pass both into the template context.

In `src/zira_dashboard/templates/staffing.html`, find where assigned people are rendered as pills (look for `lvl-{{ a.level }}` or similar). Add a small badge next to the name when `partial_hours_by_name[a.name]` is set:

```jinja
{% if partial_hours_by_name and a.name in partial_hours_by_name %}
  <span class="partial-hours-badge" title="Off {{ partial_range_by_name.get(a.name, partial_hours_by_name[a.name] ~ 'h') }}">{{ partial_range_by_name.get(a.name) or (partial_hours_by_name[a.name] ~ 'h') }}</span>
{% endif %}
```

In `src/zira_dashboard/static/staffing.css`, add the badge style:

```css
  .partial-hours-badge {
    display: inline-block; margin-left: 0.25rem;
    background: var(--warn-dim); color: var(--warn);
    border: 1px solid var(--warn);
    border-radius: 4px;
    padding: 0 0.3rem;
    font-size: 0.65rem; font-weight: 600;
    vertical-align: 1px;
    letter-spacing: 0.3px;
    white-space: nowrap;
  }
```

## Step 6 — Tests

In `tests/test_stratustime_client.py`, add tests for the new helper. Append:

```python
def test_partial_off_intervals_excludes_full_day(env_creds):
    requests_payload = {
        "Report": {},
        "Results": [
            # Full day = 8h; should be excluded
            {"ID": 1, "EmpIdentifier": "100", "StatusType": 1, "DurationPerDaySecs": 28800,
             "StartDateTimeSchema": "2026-04-29T07:00:00", "EndDateTimeSchema": "2026-04-29T15:00:00",
             "PayTypeName": "PTO", "IncludeWeekends": False},
            # Partial 1h
            {"ID": 2, "EmpIdentifier": "200", "StatusType": 1, "DurationPerDaySecs": 3600,
             "StartDateTimeSchema": "2026-04-29T09:00:00", "EndDateTimeSchema": "2026-04-29T10:00:00",
             "PayTypeName": "Early Leave", "IncludeWeekends": False},
        ],
    }
    employees_payload = {"Report": {}, "Results": [
        _fake_emp_data("100", "Alice", "Smith"),
        _fake_emp_data("200", "Bob", "Jones"),
    ]}
    import json as _json

    def fake_post(path, body, **k):
        if path == "CreateToken":
            return 200, '"tok"'
        if path == "GetUserTimeOffRequest":
            return 200, _json.dumps(requests_payload)
        if path == "GetUserBasic":
            return 200, _json.dumps(employees_payload)
        return 404, "not found"

    with patch.object(stc, "_post", side_effect=fake_post):
        intervals = stc.partial_off_intervals_for_day(date(2026, 4, 29))
    assert "Alice Smith" not in intervals  # full-day excluded
    assert "Bob Jones" in intervals
    assert len(intervals["Bob Jones"]) == 1


def test_partial_off_intervals_excludes_unapproved(env_creds):
    requests_payload = {
        "Report": {},
        "Results": [{
            "ID": 1, "EmpIdentifier": "100", "StatusType": 2,  # not 1
            "DurationPerDaySecs": 3600,
            "StartDateTimeSchema": "2026-04-29T09:00:00",
            "EndDateTimeSchema": "2026-04-29T10:00:00",
            "PayTypeName": "Early Leave", "IncludeWeekends": False,
        }],
    }
    employees_payload = {"Report": {}, "Results": [_fake_emp_data("100", "Bob", "Jones")]}
    import json as _json

    def fake_post(path, body, **k):
        if path == "CreateToken":
            return 200, '"tok"'
        if path == "GetUserTimeOffRequest":
            return 200, _json.dumps(requests_payload)
        if path == "GetUserBasic":
            return 200, _json.dumps(employees_payload)
        return 404, "not found"

    with patch.object(stc, "_post", side_effect=fake_post):
        intervals = stc.partial_off_intervals_for_day(date(2026, 4, 29))
    assert intervals == {}
```

## Step 7 — Verify

```bash
.venv/Scripts/python.exe -m pytest tests/test_stratustime_client.py -v
.venv/Scripts/python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates')); env.get_template('staffing.html'); env.get_template('recycling.html'); env.get_template('new_vs.html'); print('OK')"
```

Expected: 23 tests pass (existing 21 + 2 new). All three templates parse.

## Step 8 — Commit + push

```bash
git add src/zira_dashboard/stratustime_client.py \
        src/zira_dashboard/staffing.py \
        src/zira_dashboard/routes/value_streams.py \
        src/zira_dashboard/routes/staffing.py \
        src/zira_dashboard/templates/staffing.html \
        src/zira_dashboard/static/staffing.css \
        tests/test_stratustime_client.py
git commit -m "Subtract partial time-off from man-hours; show per-person hours on scheduler"
git push origin main
```

---

## Acceptance criteria

- `/recycling` and `/new-vs` `pallets/hr/person` KPI reflects partial-off (e.g., a person with 1h off contributes 7h of man-hours, not 8h).
- Scheduler shows a small badge next to people with partial-off, displaying their off range like "9-10a" or fallback "1h".
- Tooltip on the badge shows "Off 9-10a".
- Multi-day full-day PTO requests do NOT show a partial badge (their `hours` is 8, falls outside `< 8`).
- Pages still render fine if StratusTime is unreachable (falls back to the original full man-hours math).
- 23 unit tests pass, no real network calls in tests.
- No regression to the existing scheduler / dashboards otherwise.
