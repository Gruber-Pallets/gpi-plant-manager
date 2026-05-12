# StratusTime Sub-project #4 — Attendance Confirmation

**Goal:** When viewing today's scheduler, show a small status indicator next to each scheduled person reflecting their StratusTime punch state — ✓ on time, ⚠ late, ✗ no-show, or no indicator if shift hasn't started yet / range view / past day.

**Architecture:** New `attendance_for_day(day, emp_ids)` helper in `stratustime_client.py` calls `GetUserTimeOnStatusBoard` with the scheduled EmpIdentifiers, parses `LastTransactionType` and `LastTransactionDate`, and returns a per-name status dict. Route only fetches when viewing today and current time is past shift-start. Cache 1 min (punches change fast). Template renders icons + tooltips.

**Tech Stack:** Python 3.11+, Jinja2.

**Scope decisions:**
- Active **only** when viewing today's date (`is_today` is true).
- Active **only** after `shift_start + GRACE_MINUTES` (no point flagging anyone before then).
- Grace period: 7 minutes (constant for now; later could move to settings).
- Indicator is render-only — clicking it does nothing for now (could later jump to StratusTime).
- Cache 1 minute (much shorter than time-off's 5 min, since punches matter for live monitoring).

---

## Live data confirmed by probe

`GetUserTimeOnStatusBoard` returns one record per EmpIdentifier passed (omits employees with no status board record). Sample:

```json
{
  "EmpIdentifier": "013",
  "LastTransactionDate": "05/01/2026 06:41 AM",
  "LastTransactionDateResult": "/Date(1777617660000+0000)/",
  "LastTransactionType": "Clock In",
  "LastTransctionTypeID": 2,  // sic — typo in API
  "LL01ID": 1, ...
}
```

DataAction is `SELECT-EMPID` with the IDs in `Values`. NOT `SELECT-ALL`.

---

## File touch map

- **Modify:** `src/zira_dashboard/stratustime_client.py` — add `attendance_for_day(day, emp_ids)` plus a punch-specific 1-min cache key.
- **Modify:** `src/zira_dashboard/staffing.py` — add a name → EmpIdentifier reverse lookup helper.
- **Modify:** `src/zira_dashboard/routes/staffing.py` — when viewing today, populate `attendance_by_name` context.
- **Modify:** `src/zira_dashboard/templates/staffing.html` — render status icon next to each assigned person.
- **Modify:** `src/zira_dashboard/static/staffing.css` — status icon styles.
- **Tests:** `tests/test_stratustime_client.py` — new tests for status parsing.

No DB changes, no new env vars.

---

## Step 1 — Add `attendance_for_day` to `stratustime_client.py`

Append after `partial_off_intervals_for_day`:

```python
ATTENDANCE_CACHE_TTL_SECONDS = 60  # punches move fast


def _parse_status_board_datetime(date_str: str):
    """Parse '05/01/2026 06:41 AM' into a naive datetime (site-local).
    Returns None on failure. Returned datetime has SITE_TZ attached.
    """
    from datetime import datetime as _dt
    from . import shift_config
    if not date_str:
        return None
    try:
        dt = _dt.strptime(date_str, "%m/%d/%Y %I:%M %p")
    except ValueError:
        return None
    return dt.replace(tzinfo=shift_config.SITE_TZ)


def attendance_for_day(day, emp_ids: list[str], grace_minutes: int = 7) -> dict:
    """Return per-EmpIdentifier attendance status against `day`'s shift-start.

    Result shape:
      {
        emp_id: {
          "status": "on_time" | "late" | "clocked_out" | "no_punch" | "unknown",
          "clocked_in_at": "06:41 AM" | None,    # display string (site-local)
          "minutes_late": int,                    # 0 if on_time, positive if late
          "transaction_type": str,                # raw LastTransactionType
        },
        ...
      }

    `status` values:
      - on_time: Clock In transaction on `day`, at or before shift_start + grace.
      - late: Clock In transaction on `day`, after shift_start + grace; minutes_late > 0.
      - clocked_out: most-recent transaction is a Clock Out today. Person worked today
        but is not currently on the clock (lunch, left for the day, etc.).
      - no_punch: no transaction record found for this emp on `day`.
      - unknown: data shape unexpected.

    Empty `emp_ids` returns {}. Cached 60s per (day, sorted_emp_ids tuple).
    """
    if not emp_ids:
        return {}
    from datetime import datetime as _dt, timedelta
    from . import shift_config

    cache_key = ("attendance", day.isoformat(), tuple(sorted(set(emp_ids))))
    cached = _cache_get_with_ttl(cache_key, ATTENDANCE_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached

    status, parsed = authenticated_post("GetUserTimeOnStatusBoard", {
        "DataAction": {"Name": "SELECT-EMPID", "Values": list(emp_ids)},
    })
    out: dict = {}
    if status < 200 or status >= 300 or not isinstance(parsed, dict):
        _cache_set_with_ttl(cache_key, out, ATTENDANCE_CACHE_TTL_SECONDS)
        return out

    results = parsed.get("Results")
    seen_ids: set[str] = set()
    if isinstance(results, list):
        shift_start_dt = _dt.combine(day, shift_config.shift_start_for(day),
                                     tzinfo=shift_config.SITE_TZ)
        on_time_cutoff = shift_start_dt + timedelta(minutes=grace_minutes)
        for r in results:
            emp_id = str(r.get("EmpIdentifier") or "")
            if not emp_id:
                continue
            seen_ids.add(emp_id)
            tx_type = r.get("LastTransactionType") or ""
            tx_dt = _parse_status_board_datetime(r.get("LastTransactionDate") or "")
            entry = {
                "status": "unknown",
                "clocked_in_at": None,
                "minutes_late": 0,
                "transaction_type": tx_type,
            }
            if tx_dt is not None and tx_dt.date() == day:
                hr_min = tx_dt.strftime("%I:%M %p").lstrip("0")
                if tx_type.lower().startswith("clock in"):
                    entry["clocked_in_at"] = hr_min
                    if tx_dt <= on_time_cutoff:
                        entry["status"] = "on_time"
                    else:
                        late = int((tx_dt - shift_start_dt).total_seconds() // 60)
                        entry["status"] = "late"
                        entry["minutes_late"] = max(0, late)
                elif tx_type.lower().startswith("clock out"):
                    entry["status"] = "clocked_out"
                    entry["clocked_in_at"] = hr_min
                else:
                    # Lunch, transfer, etc. — treat as on the clock today.
                    entry["status"] = "on_time"
                    entry["clocked_in_at"] = hr_min
            out[emp_id] = entry

    # Anyone we asked about who didn't appear in Results -> no_punch.
    for emp_id in emp_ids:
        if emp_id not in seen_ids:
            out[str(emp_id)] = {
                "status": "no_punch",
                "clocked_in_at": None,
                "minutes_late": 0,
                "transaction_type": "",
            }

    _cache_set_with_ttl(cache_key, out, ATTENDANCE_CACHE_TTL_SECONDS)
    return out
```

The 1-min cache needs ttl-aware variants of `_cache_get` / `_cache_set`. Add these too (right next to the existing pair):

```python
def _cache_get_with_ttl(key, ttl_seconds):
    """Like _cache_get but treats entries written via _cache_set_with_ttl correctly."""
    return _cache_get(key)


def _cache_set_with_ttl(key, value, ttl_seconds):
    """Set with custom TTL (overrides DATA_CACHE_TTL_SECONDS)."""
    _data_cache[key] = (value, time.time() + ttl_seconds)
```

Both are thin wrappers — the existing `_cache_get` already checks expiry. The TTL wrapper just lets callers customize the expiration time.

## Step 2 — Add name → EmpIdentifier reverse lookup

Append to `src/zira_dashboard/stratustime_client.py` after `_employee_id_to_name_map`:

```python
def name_to_emp_id_map() -> dict[str, str]:
    """Reverse of _employee_id_to_name_map: 'FirstName LastName' → 'EmpIdentifier'.
    Cached via the same emp_map cache (just inverted on first build)."""
    cached = _cache_get(("name_to_id_map",))
    if cached is not None:
        return cached
    forward = _employee_id_to_name_map()
    inverted = {name: emp_id for emp_id, name in forward.items()}
    _cache_set(("name_to_id_map",), inverted)
    return inverted
```

## Step 3 — Route: pass `attendance_by_name` into scheduler context

In `src/zira_dashboard/routes/staffing.py`, after the existing `partial_hours_by_name` block, add:

```python
# Attendance: only meaningful when viewing today and current time is past shift-start.
attendance_by_name: dict[str, dict] = {}
today_for_attendance = datetime.now(timezone.utc).date()
if d == today_for_attendance:
    from datetime import datetime as _dt
    now_local = datetime.now(timezone.utc).astimezone(shift_config.SITE_TZ)
    shift_start_local = _dt.combine(d, shift_config.shift_start_for(d),
                                    tzinfo=shift_config.SITE_TZ)
    if now_local >= shift_start_local:
        try:
            name_to_id = stratustime_client.name_to_emp_id_map()
            scheduled_names = set()
            for ops in sched.assignments.values():
                for n in (ops or []):
                    scheduled_names.add(n)
            scheduled_ids = [name_to_id[n] for n in scheduled_names if n in name_to_id]
            id_to_name = {v: k for k, v in name_to_id.items()}
            attendance_by_id = stratustime_client.attendance_for_day(d, scheduled_ids)
            for emp_id, info in attendance_by_id.items():
                name = id_to_name.get(emp_id)
                if name:
                    attendance_by_name[name] = info
        except Exception:
            attendance_by_name = {}
```

Add `"attendance_by_name": attendance_by_name,` to the template context dict.

## Step 4 — Template: render status icon

In `src/zira_dashboard/templates/staffing.html`, find the assigned-person pill rendering (where `partial_hours_by_name` was added in the math sub-project). Right after the partial-hours badge, add:

```jinja
{% if attendance_by_name and a.name in attendance_by_name %}
  {% set att = attendance_by_name[a.name] %}
  {% if att.status == 'on_time' %}
    <span class="att-badge att-on-time" title="Clocked in {{ att.clocked_in_at }} (on time)">✓</span>
  {% elif att.status == 'late' %}
    <span class="att-badge att-late" title="Clocked in {{ att.clocked_in_at }} ({{ att.minutes_late }}m late)">⚠ +{{ att.minutes_late }}m</span>
  {% elif att.status == 'no_punch' %}
    <span class="att-badge att-no-punch" title="No clock-in yet today">✗</span>
  {% elif att.status == 'clocked_out' %}
    <span class="att-badge att-clocked-out" title="Clocked out at {{ att.clocked_in_at }}">⏸</span>
  {% endif %}
{% endif %}
```

## Step 5 — CSS

Add to `src/zira_dashboard/static/staffing.css`:

```css
  .att-badge {
    display: inline-block; margin-left: 0.2rem;
    padding: 0 0.3rem;
    border-radius: 4px;
    font-size: 0.7rem; font-weight: 700;
    vertical-align: 1px;
    line-height: 1.4;
  }
  .att-badge.att-on-time { background: var(--accent-dim); color: var(--accent); }
  .att-badge.att-late    { background: var(--warn-dim);   color: var(--warn);   }
  .att-badge.att-no-punch{ background: var(--bad-dim);    color: var(--bad);    }
  .att-badge.att-clocked-out { background: var(--panel-3); color: var(--muted); }
```

## Step 6 — Tests

Append to `tests/test_stratustime_client.py`:

```python
def _fake_status_record(empid, last_tx, tx_type):
    return {
        "EmpIdentifier": empid,
        "LastTransactionDate": last_tx,
        "LastTransactionType": tx_type,
        "LastTransctionTypeID": 2,
    }


def test_attendance_for_day_marks_on_time_and_late(env_creds, monkeypatch):
    # Mock SITE_TZ-aware shift_start to 7:00 AM on 2026-05-01.
    from datetime import time as _time
    monkeypatch.setattr(stc, "_employee_id_to_name_map", lambda: {})

    # Need to also stub shift_config.shift_start_for to a known value.
    from zira_dashboard import shift_config as sc
    monkeypatch.setattr(sc, "shift_start_for", lambda d: _time(7, 0))

    payload = {
        "Report": {},
        "Results": [
            _fake_status_record("AAA", "05/01/2026 06:55 AM", "Clock In"),  # on time
            _fake_status_record("BBB", "05/01/2026 07:15 AM", "Clock In"),  # 15m late
            _fake_status_record("CCC", "05/01/2026 09:30 AM", "Clock Out"), # clocked out
        ],
    }
    import json as _json

    def fake_post(path, body, **k):
        if path == "CreateToken":
            return 200, '"tok"'
        if path == "GetUserTimeOnStatusBoard":
            return 200, _json.dumps(payload)
        return 404, "not found"

    with patch.object(stc, "_post", side_effect=fake_post):
        result = stc.attendance_for_day(date(2026, 5, 1), ["AAA", "BBB", "CCC", "DDD"])

    assert result["AAA"]["status"] == "on_time"
    assert result["BBB"]["status"] == "late"
    assert result["BBB"]["minutes_late"] == 15
    assert result["CCC"]["status"] == "clocked_out"
    assert result["DDD"]["status"] == "no_punch"  # not in Results


def test_attendance_for_day_empty_emp_ids(env_creds):
    assert stc.attendance_for_day(date(2026, 5, 1), []) == {}


def test_parse_status_board_datetime_handles_garbage():
    assert stc._parse_status_board_datetime("garbage") is None
    assert stc._parse_status_board_datetime("") is None
    dt = stc._parse_status_board_datetime("05/01/2026 06:41 AM")
    assert dt is not None
    assert dt.hour == 6
    assert dt.minute == 41
```

## Step 7 — Verify

```bash
.venv/Scripts/python.exe -m pytest tests/test_stratustime_client.py -v
.venv/Scripts/python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates')); env.get_template('staffing.html'); print('OK')"
```

Expected: 26 tests pass (23 existing + 3 new). Template parses.

## Step 8 — Commit + push

```bash
git add src/zira_dashboard/stratustime_client.py \
        src/zira_dashboard/routes/staffing.py \
        src/zira_dashboard/templates/staffing.html \
        src/zira_dashboard/static/staffing.css \
        tests/test_stratustime_client.py
git commit -m "Show StratusTime attendance status on scheduler (sub-project #4)"
git push origin main
```

---

## Acceptance criteria

- Viewing today's scheduler after shift-start: each assigned person shows ✓ / ⚠ +Nm / ✗ / ⏸ next to their name.
- Tooltip on the badge shows the actual punch time and detail.
- No badges shown when viewing yesterday, tomorrow, or any non-today date.
- No badges shown if current time is before shift-start.
- 1-min cache means the indicator updates roughly each minute on auto-refresh; "Refresh now" button forces immediate update.
- StratusTime unreachable → no badges (page still renders).
- 26 unit tests pass; no real network calls.
- No regression to existing scheduler functionality.
