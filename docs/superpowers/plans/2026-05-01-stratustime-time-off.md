# StratusTime Sub-project #2 — Time-Off Sync

**Goal:** Replace the scheduler's manual time-off pills with read-only display of approved time-off requests pulled from StratusTime. Same on the `/time-off` tab. Add deep-links to StratusTime so users can add/edit there. Cache 5 minutes with manual "Refresh now" button.

**Architecture:** Extend `stratustime_client.py` with `get_time_off_requests(start_d, end_d)` and `time_off_names_for_day(d)`. Add a small in-process TTL cache. Switch the scheduler and time-off-tab routes to read from this cache. Remove the manual time-off picker from the scheduler template; add deep-links.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2.

**Sub-project context:** Sub-project #1 (foundation) already shipped — `stratustime_client.health_check()` and `list_employees()` work. This builds on top.

---

## Live data confirmed by probe

- 60-day max window per `GetUserTimeOffRequest` call.
- Sample response items have these fields we'll use: `EmpIdentifier`, `StartDateTimeSchema` (e.g., `"2026-05-08T13:00:00"`), `EndDateTimeSchema`, `IncludeWeekends`, `PayTypeName` (e.g., `"PTO"`, `"Early Leave - Late Start"`), `DurationPerDaySecs`, `StatusType`.
- `StatusType: 1` appears in real-world data (likely Approved). The implementer will probe other values during dev and treat StatusType==1 as "approved/active" while logging unknowns.
- Partial-day = `DurationPerDaySecs < 28800` (under 8h). For this sub-project we render ALL time-off entries the same way regardless of partial/full; sub-project #3 will turn partial into per-person schedule overrides.

## File touch map

- **Modify:** `src/zira_dashboard/stratustime_client.py` — add `get_time_off_requests(start_d, end_d)`, `time_off_names_for_day(d)`, `time_off_entries_for_day(d)`, `_employee_id_to_name_map()`, plus a tiny TTL cache.
- **Modify:** `src/zira_dashboard/routes/staffing.py` — read time-off from StratusTime on render; ignore the DB-backed `assignments[TIME_OFF_KEY]` for read; skip writing time-off rows on save.
- **Modify:** `src/zira_dashboard/templates/staffing.html` — replace the editable time-off picker with read-only pills + deep-link.
- **Modify:** `src/zira_dashboard/routes/time_off.py` — switch `_time_off_by_day()` to StratusTime cache.
- **Modify:** `src/zira_dashboard/templates/time_off.html` — header deep-link + Refresh button + a small note that the data is live from StratusTime.
- **Add:** new endpoint `POST /api/stratustime/refresh` → busts cache, redirects back to referrer (or `/staffing`).
- **Tests:** `tests/test_stratustime_client.py` — add tests for the new helpers (mocked).

No DB migrations. The existing `schedule_time_off` table is now orphaned — leave it for now (sub-project #4 cleanup pass can drop it if Dale wants).

## Step 1 — Extend `stratustime_client.py`

Add to `src/zira_dashboard/stratustime_client.py` (after `list_employees`):

```python
# --- Time-off + employee directory caching ---

# (cache_key) -> (value, expires_at_epoch_seconds)
_data_cache: dict[tuple, tuple[object, float]] = {}
DATA_CACHE_TTL_SECONDS = 5 * 60


def _cache_get(key):
    entry = _data_cache.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if expires_at < time.time():
        return None
    return value


def _cache_set(key, value):
    _data_cache[key] = (value, time.time() + DATA_CACHE_TTL_SECONDS)


def cache_clear() -> None:
    """Drop all cached data (token cache untouched)."""
    _data_cache.clear()


def _wcf_date(epoch_ms: int) -> str:
    return f"/Date({epoch_ms}+0000)/"


def _epoch_ms(d) -> int:
    """Convert a `datetime.date` to UTC epoch ms (midnight)."""
    from datetime import datetime, timezone
    dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _employee_id_to_name_map() -> dict[str, str]:
    """{ EmpIdentifier: 'FirstName LastName' } — cached 5 min."""
    cached = _cache_get(("emp_map",))
    if cached is not None:
        return cached
    out: dict[str, str] = {}
    for emp in list_employees():
        emp_id = emp.get("EmpIdentifier")
        first = (emp.get("FirstName") or "").strip()
        last = (emp.get("LastName") or "").strip()
        if emp_id and (first or last):
            out[str(emp_id)] = f"{first} {last}".strip()
    _cache_set(("emp_map",), out)
    return out


def get_time_off_requests(start_d, end_d) -> list[dict]:
    """Return raw time-off request dicts for [start_d, end_d] (inclusive).

    StratusTime caps each call at a 60-day window — caller passes ranges
    within that. Cached 5 minutes per (start, end).
    """
    key = ("time_off", start_d.isoformat(), end_d.isoformat())
    cached = _cache_get(key)
    if cached is not None:
        return cached
    status, parsed = authenticated_post("GetUserTimeOffRequest", {
        "StartDate": _wcf_date(_epoch_ms(start_d)),
        "EndDate": _wcf_date(_epoch_ms(end_d)),
        "DateTimeSchema": 0,
        "IgnoreDeletedRequests": True,
        "IgnoreDetails": False,
        "DataAction": {"Name": "SELECT-ALL", "Values": []},
    })
    if status < 200 or status >= 300 or not isinstance(parsed, dict):
        return []
    results = parsed.get("Results")
    if not isinstance(results, list):
        return []
    _cache_set(key, results)
    return results


def _request_covers_day(req: dict, day) -> bool:
    """True if the time-off request `req` includes `day`.

    Uses StartDateTimeSchema/EndDateTimeSchema (ISO local strings).
    Honors `IncludeWeekends` — if False, skips Sat/Sun within the range.
    """
    from datetime import date as _date
    s_str = (req.get("StartDateTimeSchema") or "")[:10]  # YYYY-MM-DD
    e_str = (req.get("EndDateTimeSchema") or "")[:10]
    if not s_str or not e_str:
        return False
    try:
        s = _date.fromisoformat(s_str)
        e = _date.fromisoformat(e_str)
    except ValueError:
        return False
    if not (s <= day <= e):
        return False
    if not req.get("IncludeWeekends", False) and day.weekday() >= 5:
        return False
    return True


def time_off_entries_for_day(day) -> list[dict]:
    """Return list of {name, pay_type, hours, status_type, request_id} for `day`.

    Treats StatusType==1 as approved/active. Other StatusType values are
    skipped (likely pending/rejected). Names come from the employee map;
    unmapped EmpIdentifiers are surfaced as 'Unknown ({id})' so it's visible.
    """
    # Use a 7-day window centered on `day` — small request, hits cache often.
    from datetime import timedelta
    start_d = day - timedelta(days=3)
    end_d = day + timedelta(days=3)
    requests_ = get_time_off_requests(start_d, end_d)
    emp_map = _employee_id_to_name_map()
    out = []
    for r in requests_:
        if r.get("StatusType") != 1:
            continue
        if not _request_covers_day(r, day):
            continue
        emp_id = str(r.get("EmpIdentifier") or "")
        name = emp_map.get(emp_id) or f"Unknown ({emp_id})"
        secs = r.get("DurationPerDaySecs") or 0
        out.append({
            "name": name,
            "pay_type": r.get("PayTypeName") or "",
            "hours": round(secs / 3600.0, 1),
            "status_type": r.get("StatusType"),
            "request_id": r.get("ID"),
        })
    return out


def time_off_names_for_day(day) -> list[str]:
    """Just the names — convenience for callers that only need a list of strings."""
    return [e["name"] for e in time_off_entries_for_day(day)]


# Public deep-link to StratusTime's time-off page (for "Manage in StratusTime ↗" links).
STRATUSTIME_TIME_OFF_URL = "https://stratustime.centralservers.com/"
```

## Step 2 — Tests for new helpers

Append to `tests/test_stratustime_client.py`:

```python
from datetime import date


def _fake_emp_data(empid, first, last):
    return {"EmpIdentifier": empid, "FirstName": first, "LastName": last}


def _fake_request(empid, start_iso, end_iso, status=1, secs=28800, paytype="PTO", include_weekends=False):
    return {
        "ID": 1,
        "EmpIdentifier": empid,
        "StartDateTimeSchema": start_iso + "T07:00:00",
        "EndDateTimeSchema": end_iso + "T15:00:00",
        "StatusType": status,
        "DurationPerDaySecs": secs,
        "PayTypeName": paytype,
        "IncludeWeekends": include_weekends,
    }


def test_request_covers_day_simple_range():
    req = _fake_request("1", "2026-05-04", "2026-05-06", include_weekends=True)
    assert stc._request_covers_day(req, date(2026, 5, 4)) is True
    assert stc._request_covers_day(req, date(2026, 5, 5)) is True
    assert stc._request_covers_day(req, date(2026, 5, 6)) is True
    assert stc._request_covers_day(req, date(2026, 5, 3)) is False
    assert stc._request_covers_day(req, date(2026, 5, 7)) is False


def test_request_covers_day_skips_weekends_when_flag_false():
    # Range covers Mon-Sun; flag false should hide Sat (5/9) and Sun (5/10).
    req = _fake_request("1", "2026-05-04", "2026-05-10", include_weekends=False)
    assert stc._request_covers_day(req, date(2026, 5, 8)) is True   # Friday
    assert stc._request_covers_day(req, date(2026, 5, 9)) is False  # Saturday
    assert stc._request_covers_day(req, date(2026, 5, 10)) is False # Sunday


def test_time_off_entries_for_day_filters_by_status(env_creds):
    requests_payload = {
        "Report": {},
        "Results": [
            _fake_request("100", "2026-05-04", "2026-05-04", status=1),
            _fake_request("200", "2026-05-04", "2026-05-04", status=2),  # pending? skipped
        ],
    }
    employees_payload = {
        "Report": {},
        "Results": [
            _fake_emp_data("100", "Alice", "Smith"),
            _fake_emp_data("200", "Bob", "Jones"),
        ],
    }
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
        entries = stc.time_off_entries_for_day(date(2026, 5, 4))
    assert len(entries) == 1
    assert entries[0]["name"] == "Alice Smith"


def test_time_off_entries_for_day_unmapped_emp_id(env_creds):
    requests_payload = {
        "Report": {},
        "Results": [_fake_request("999", "2026-05-04", "2026-05-04")],
    }
    employees_payload = {"Report": {}, "Results": []}
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
        entries = stc.time_off_entries_for_day(date(2026, 5, 4))
    assert len(entries) == 1
    assert "999" in entries[0]["name"]


def test_cache_clear_drops_data_cache(env_creds):
    stc._cache_set(("time_off", "x", "y"), [{"foo": "bar"}])
    assert stc._cache_get(("time_off", "x", "y")) is not None
    stc.cache_clear()
    assert stc._cache_get(("time_off", "x", "y")) is None


def test_cache_clear_resets_per_test(env_creds):
    """Sanity check that the autouse cache reset works."""
    stc._cache_set(("test_key",), "value")
    assert stc._cache_get(("test_key",)) == "value"
```

Also update the autouse `reset_token_cache` fixture to also clear `_data_cache`:

```python
@pytest.fixture(autouse=True)
def reset_token_cache():
    stc._token_cache = None
    stc._data_cache.clear()
    yield
    stc._token_cache = None
    stc._data_cache.clear()
```

## Step 3 — Wire into `routes/staffing.py`

Find where the route reads `sched.assignments.get(staffing.TIME_OFF_KEY, [])` to render the time-off pills. Replace with a fetch from StratusTime.

The exact location: search for `time_off_names` or similar context variable name passed to the template. The handler currently builds a `time_off_names` (or similar) list. Replace its source.

Concretely, add at the top of the handler (after `d` is computed):

```python
from .. import stratustime_client
try:
    time_off_entries = stratustime_client.time_off_entries_for_day(d)
except Exception:
    time_off_entries = []
time_off_names = [e["name"] for e in time_off_entries]
```

And pass `time_off_entries` into the context (in addition to or replacing the existing `time_off_names`).

The save handler (`POST /staffing`) currently writes `schedule_time_off` rows when `wc_name == TIME_OFF_KEY`. Skip that branch — the UI no longer collects time-off entries:

```python
# In save handler, find the loop that builds `assignments` from form fields:
for wc_name, names in form_data.items():
    if wc_name == staffing.TIME_OFF_KEY:
        continue  # time-off is now StratusTime-driven, not user-edited here
    assignments[wc_name] = names
```

(Exact phrasing depends on existing code; the implementer will adjust.)

## Step 4 — Update `templates/staffing.html` time-off section

Find the `<div class="section timeoff">` block (around line 123 in current file). Replace its contents with a read-only display:

```jinja
    <div class="section timeoff">
      <h3>Time Off ☀️ <span class="count">{{ time_off_entries|length }}</span>
        <a href="https://stratustime.centralservers.com/" target="_blank" rel="noopener" class="ts-deeplink" title="Manage time-off in StratusTime">↗</a>
      </h3>
      {% if time_off_entries %}
        <ul class="time-off-list">
          {% for e in time_off_entries %}
            <li class="time-off-row" title="{{ e.pay_type }} · {{ e.hours }}h">
              <span class="name">{{ e.name }}</span>
              <span class="ts-meta">{{ e.pay_type }}{% if e.hours and e.hours < 8 %} · {{ e.hours }}h{% endif %}</span>
            </li>
          {% endfor %}
        </ul>
      {% else %}
        <p class="empty">— none —</p>
      {% endif %}
      <p class="ts-note">From StratusTime · cached 5 min · <a href="/api/stratustime/refresh?back=/staffing?day={{ day }}">Refresh now</a></p>
    </div>
```

Drop the `<div class="picker">…<select class="add-select">…` block — it's no longer interactive.

Add CSS to `src/zira_dashboard/static/staffing.css` (find the existing `.timeoff` rules and append):

```css
  .timeoff .ts-deeplink { color: var(--accent); margin-left: 0.4rem; text-decoration: none; font-size: 0.9em; }
  .timeoff .time-off-list { list-style: none; margin: 0; padding: 0; }
  .timeoff .time-off-row { display: flex; flex-direction: column; padding: 0.25rem 0; border-bottom: 1px solid var(--panel-2); }
  .timeoff .time-off-row:last-child { border-bottom: none; }
  .timeoff .time-off-row .name { font-weight: 500; }
  .timeoff .time-off-row .ts-meta { font-size: 0.78rem; color: var(--muted); }
  .timeoff .ts-note { font-size: 0.72rem; color: var(--muted); margin-top: 0.6rem; font-style: italic; }
  .timeoff .ts-note a { color: var(--accent); text-decoration: none; }
  .timeoff .ts-note a:hover { text-decoration: underline; }
```

## Step 5 — Wire `routes/time_off.py` to StratusTime

Replace `_time_off_by_day()` with a StratusTime-backed version. The function currently iterates schedules from the DB. New version:

```python
def _time_off_by_day(start_d, end_d) -> dict[date, list[dict]]:
    """Return {day: [time_off_entry, ...]} for [start_d, end_d] inclusive,
    sourced from StratusTime."""
    from datetime import timedelta
    from .. import stratustime_client
    out: dict[date, list[dict]] = {}
    cursor = start_d
    while cursor <= end_d:
        try:
            out[cursor] = stratustime_client.time_off_entries_for_day(cursor)
        except Exception:
            out[cursor] = []
        cursor += timedelta(days=1)
    return out
```

Adjust the route handler call site to pass the visible date range (it already has start/end variables for the page).

## Step 6 — Update `templates/time_off.html`

Add a header link + Refresh button at the top of the page:

```jinja
<div class="time-off-page-header">
  <a href="https://stratustime.centralservers.com/" target="_blank" rel="noopener" class="ts-deeplink-btn">↗ Open StratusTime</a>
  <a href="/api/stratustime/refresh?back={{ request.url.path }}{% if request.url.query %}?{{ request.url.query }}{% endif %}" class="ts-refresh-btn">↻ Refresh</a>
  <span class="ts-note">All time-off entries are live from StratusTime · cached 5 min</span>
</div>
```

(The implementer will place this in the right spot in the existing template structure.)

In each row that renders a time-off entry, show `pay_type` and `hours` from the entry dict (instead of just the name).

## Step 7 — Add refresh endpoint

In `src/zira_dashboard/routes/staffing.py` (or a new tiny module — implementer's call), add:

```python
@router.get("/api/stratustime/refresh")
def stratustime_refresh(back: str | None = Query(default=None)):
    from .. import stratustime_client
    stratustime_client.cache_clear()
    target = back or "/staffing"
    # Basic safety: only allow same-origin paths.
    if not target.startswith("/"):
        target = "/staffing"
    return RedirectResponse(target, status_code=303)
```

This is GET because it's triggered by a plain `<a>` link. (POST would require a form.)

## Step 8 — Verify

```bash
.venv/Scripts/python.exe -m pytest tests/test_stratustime_client.py -v
.venv/Scripts/python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates')); env.get_template('staffing.html'); env.get_template('time_off.html'); print('OK')"
```

Expected: ~18 tests pass (existing 12 + 6 new). Both templates parse.

## Step 9 — Commit + push

```bash
git add src/zira_dashboard/stratustime_client.py \
        src/zira_dashboard/routes/staffing.py \
        src/zira_dashboard/routes/time_off.py \
        src/zira_dashboard/templates/staffing.html \
        src/zira_dashboard/templates/time_off.html \
        src/zira_dashboard/static/staffing.css \
        tests/test_stratustime_client.py
git commit -m "Drive scheduler & time-off tab from StratusTime (sub-project #2)"
git push origin main
```

---

## Acceptance criteria

- Scheduler page's Time Off section renders names + pay type + duration from StratusTime, no add/remove buttons.
- "↗" link in Time Off header opens StratusTime in new tab.
- "Refresh now" link clears cache and reloads.
- Manual time-off pills are gone from the scheduler.
- /time-off tab renders StratusTime data with header deep-link + Refresh.
- 5-min cache means repeat page loads don't slow down to 2-3 API roundtrips each.
- Page still renders if StratusTime is unreachable (empty time-off list, no crash).
- Existing 12 unit tests still pass; 6 new tests cover new helpers + cache clear.
- StratusTime credentials are STILL not committed to repo or written to memory (only env vars).
