# Staffing Page — 3× Performance Pass

**Goal:** Measure and reduce the GET `/staffing` end-to-end response time without changing functionality. Target: ~3× faster on warm cache; significant first-load improvement after deploy.

**Architecture:** 5 layered optimizations — observability first, then progressively more aggressive caching/parallelism — each commit-safe and revertable in isolation.

---

## Optimizations

### Opt 1 — Server-Timing instrumentation (observability)

Add a `Server-Timing` HTTP header to GET `/staffing` exposing phase durations. Phases:
- `db` — concurrent cert + roster + schedule reads
- `stratustime` — time-off + attendance fetches
- `render` — Jinja template + Python render
- `total` — the whole request

Use a small context manager:

```python
import time
class _Phase:
    def __init__(self, store, name):
        self.store, self.name = store, name
    def __enter__(self):
        self.t0 = time.perf_counter()
        return self
    def __exit__(self, *a):
        self.store[self.name] = (time.perf_counter() - self.t0) * 1000.0
```

Used as `phases = {}; with _Phase(phases, "db"): ...`. Header value: `dur;name=db;dur=42.1, dur;name=stratustime;dur=320.4, ...`. Visible in browser devtools → Network → Timing tab.

### Opt 2 — Token-fetch lock (thundering herd fix)

When multiple threads hit `get_token()` simultaneously with a cold cache, they all call `_create_token()`. Add a threading.Lock so only the first thread fetches; the others wait and read the cache:

```python
_token_lock = threading.Lock()

def get_token(force_refresh=False):
    global _token_cache
    now = time.time()
    if not force_refresh and _token_cache is not None:
        token, expires_at = _token_cache
        if expires_at > now:
            return token, ""
    with _token_lock:
        # Double-check inside the lock — another thread may have populated.
        if not force_refresh and _token_cache is not None:
            token, expires_at = _token_cache
            if expires_at > now:
                return token, ""
        token, err = _create_token()
        if token:
            _token_cache = (token, now + TOKEN_TTL_SECONDS)
        return token, err
```

This is a prerequisite for Opt 4 (parallel StratusTime fetches).

### Opt 3 — Startup pre-warm

Add a FastAPI startup hook in `src/zira_dashboard/app.py` (or `lifespan`) that, in a background thread, fires:
- `stratustime_client.get_token()` — pre-creates a token
- `stratustime_client._employee_id_to_name_map()` — pre-caches the employee directory (most expensive call)

So the first user after a Railway redeploy doesn't pay for the cold-cache walk. Wrap in try/except — startup must never crash from a StratusTime outage.

```python
@app.on_event("startup")
def _stratustime_prewarm():
    import threading
    def _warm():
        try:
            from . import stratustime_client
            stratustime_client._employee_id_to_name_map()
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True).start()
```

(If `app.py` already uses `lifespan`, integrate there.)

### Opt 4 — Parallelize StratusTime fetches in the route

Currently the GET `/staffing` route fetches:
1. Three DB reads (already parallel via `ThreadPoolExecutor(max_workers=3)`)
2. `stratustime_client.time_off_entries_for_day(d)` — sequential, AFTER the DB fetches
3. `stratustime_client.attendance_for_day(d, ids)` — sequential, AFTER time_off (and only when today)

Refactor to use ONE `ThreadPoolExecutor(max_workers=5)` that fans out everything that doesn't depend on each other. Specifically:
- DB reads (certs, roster, schedule) are independent of StratusTime
- StratusTime time-off only needs `d`
- StratusTime attendance needs the schedule's assigned names — depends on `schedule`. Fire AFTER schedule resolves but in parallel with the StratusTime time-off + the rest of the page work.

Concretely:

```python
with ThreadPoolExecutor(max_workers=5) as pool:
    f_certs  = pool.submit(cert_lookup.load_person_certs)
    f_roster = pool.submit(staffing.load_roster)
    f_sched  = pool.submit(staffing.load_schedule, d)
    f_time_off_entries = pool.submit(_safe_time_off_entries, d)  # local wrapper that swallows exceptions

    person_certs = f_certs.result()
    roster = f_roster.result()
    sched = f_sched.result()
    time_off_entries = f_time_off_entries.result()

    # Attendance depends on schedule — fire it now in another thread while
    # we proceed with render prep.
    f_attendance = pool.submit(_safe_attendance, d, sched, today)

    # ... existing prep that doesn't need attendance ...

    attendance_by_name = f_attendance.result()
```

The two `_safe_*` wrappers wrap the existing try/except logic. This buys ~200-400ms on warm StratusTime cache (calls run concurrent), more on cold.

### Opt 5 — Combine load_schedule's 3 queries

Currently `staffing.load_schedule(day)` does 3 sequential SQL queries:
1. `SELECT FROM schedules WHERE day = %s` (1 row)
2. `SELECT FROM schedule_assignments JOIN ...` (N rows)
3. `SELECT FROM schedule_wc_notes JOIN ...` (M rows)

Replace queries 2 + 3 with a single thread-pool dispatch so they run in parallel:

```python
from concurrent.futures import ThreadPoolExecutor

def load_schedule(day):
    from . import db
    rows = db.query("SELECT ... FROM schedules WHERE day = %s", (day,))
    if not rows:
        return Schedule(day=day, published=False, assignments={})
    r = rows[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_assignments = pool.submit(db.query,
            "SELECT wc.name AS wc_name, pe.name AS person_name "
            "FROM schedule_assignments sa "
            "JOIN work_centers wc ON wc.id = sa.wc_id "
            "JOIN people pe ON pe.id = sa.person_id "
            "WHERE sa.day = %s ORDER BY sa.wc_id, sa.sort_order",
            (day,))
        f_notes = pool.submit(db.query,
            "SELECT wc.name AS wc_name, sn.note "
            "FROM schedule_wc_notes sn JOIN work_centers wc ON wc.id = sn.wc_id "
            "WHERE sn.day = %s",
            (day,))
        asg_rows = f_assignments.result()
        notes_rows = f_notes.result()
    # ... existing logic ...
```

Saves roughly one DB roundtrip per page load.

### Opt 6 — HTTP caching for past days only

Past-day staffing views are immutable (the day is over; no edits coming). Add a long Cache-Control header for `d < today`:

```python
from .._http_cache import set_cache_headers
set_cache_headers(response, includes_today=(d >= today))
```

For today/future days, default behavior (no cache) preserves edit immediacy. For past days, browser caches the response.

---

## File touch map

- **Modify:** `src/zira_dashboard/stratustime_client.py` — add `_token_lock`; pre-warm helper if needed.
- **Modify:** `src/zira_dashboard/staffing.py` — parallelize the two non-schedule queries in `load_schedule`.
- **Modify:** `src/zira_dashboard/routes/staffing.py` — `_Phase` instrumentation, single ThreadPoolExecutor dispatch, Server-Timing header, past-day cache.
- **Modify:** `src/zira_dashboard/app.py` — startup pre-warm thread.

No DB schema changes. No env-var changes. No new dependencies.

---

## Verification

Manual:
1. Hit `/staffing` cold (after deploy). Check Server-Timing in devtools.
2. Hit `/staffing` warm (within 1 min). Compare. Should be ~50-70% of cold.
3. Hit `/staffing?day=2026-04-15` (past day) twice. Second hit should be browser-cached (200 OK from disk, no Server-Timing because it's not regenerated).
4. Edit + save a schedule. Reload — see fresh data immediately (caching is past-day-only).
5. Stop StratusTime by setting a fake env var, restart Railway. Hit `/staffing` — page renders without time-off / attendance, no crash.

Automated: existing test suite still passes (`pytest tests/` should report the same pass count as before).

## Acceptance criteria

- Server-Timing header visible in devtools.
- Warm-cache `/staffing` is 2-3× faster than current.
- Cold-cache `/staffing` (after deploy) is materially faster (pre-warm helps).
- Past-day pages cached by browser.
- Today/future pages NOT cached — edits show immediately.
- StratusTime outage doesn't break the page.
- All existing tests pass.
