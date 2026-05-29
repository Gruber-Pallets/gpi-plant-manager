# Staffing Section 5× — Proactive Cache Warming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every page under the staffing section feel ~5× faster by ensuring a human never eats a cold render — a background loop keeps today's hot pages pre-rendered in the existing HTTP response cache, the two uncached pages (skills matrix, player cards) gain response caches, and the shared roster cache is kept warm for the long tail.

**Architecture:** The day-view and leaderboards handlers already render *and* `store_cached_response()` themselves. A new `page_warmer` module builds a synthetic `Request` and calls those handlers on a background tick, so the cache is populated proactively instead of by the first unlucky human. The response-cache TTL is raised to sit above the warmer cadence so it never goes cold between ticks. Skills/player-card handlers get the same get-cached/render/store pattern. Each tier is independently shippable and revertable.

**Tech Stack:** Python (FastAPI + Starlette, Jinja2 templates, psycopg2/Postgres), `asyncio` background loops in the app lifespan, the existing in-process `TTLCache` (`_http_cache.py`), pytest.

**Spec:** `docs/superpowers/specs/2026-05-29-staffing-section-5x-design.md`

---

## Verification environment

Per the project's setup, **the local machine runs Python 3.9 and cannot run the test suite**; production is Railway (auto-deploy on push to `main`). Therefore:

- **Local sanity check for every changed file:** `python -m py_compile <file>` (catches syntax errors). The repo memory's standard local check.
- **Canonical test run:** the `pytest` commands below run in the project's real test environment (a 3.11+ env / Railway shell / CI), where `tests/conftest.py` sets `AUTH_DISABLED=1` and test env vars. Postgres-gated tests `skip` without `DATABASE_URL` — that's expected and matches the existing suite (~138 skipped).
- **Behavioral verification:** after each tier deploys to Railway, read the `Server-Timing` header on a cold load (devtools → Network → request → Timing).

When a step says "Run … Expected: FAIL/PASS," run it in that test environment. Locally, substitute the `py_compile` check.

## Measurement protocol (run at tier boundaries)

1. **Baseline (before Task 1):** On Railway, force a cold load of `/staffing` (e.g., right after a deploy, or wait out the cache), record `Server-Timing` `total`. Do the same for `/staffing/leaderboards`, `/staffing/skills`, and a `/staffing/people/<name>` card (use the browser Network tab's total time for the three that don't emit `Server-Timing`).
2. **After Tier 1 (Task 3), Tier 2 (Task 4), Tier 3 (Tasks 5–7):** re-measure the same four pages — a cold load (first hit after deploy) and a warm reload.
3. **Stop when the cold-first-load is ≥5× faster** than baseline. Tier 1 alone very likely clears it for the day-view/leaderboards; Tiers 2–3 extend the win to skills/player-cards and the long tail. Only consider template surgery (spec "Approach B") if a measured gap remains.

---

## File Structure

**New files:**
- `src/zira_dashboard/page_warmer.py` — one responsibility: pre-render the hot staffing pages into the response cache. Holds the synthetic-`Request` builder and the warm-tick functions. No app/router import (avoids an import cycle; app imports it, not vice-versa).
- `tests/test_page_warmer.py` — unit tests for the warmer (no DB needed).

**Modified files:**
- `src/zira_dashboard/_http_cache.py` — raise `_RESPONSE_CACHE_TODAY` TTL 15 → 60s.
- `src/zira_dashboard/app.py` — add two background loops to the lifespan (`_warm_staffing_pages_loop`, `_warm_staffing_stable_loop`).
- `src/zira_dashboard/staffing.py` — raise `_ROSTER_CACHE_TTL_SECONDS` 60 → 3600s.
- `src/zira_dashboard/routes/skills.py` — response cache on GET `/staffing/skills` + invalidation on roster writes.
- `src/zira_dashboard/routes/people.py` — response cache on GET `/staffing/people/{name}` + invalidation on the attendance-reason write.
- `tests/test_http_cache.py`, `tests/test_staffing.py` (or nearest existing) — constant assertions.
- `CHANGELOG.md` — one entry per deploy (tier).

---

# TIER 1 — Warm the pages that already cache

## Task 1: Raise the today response-cache TTL to 60s

**Files:**
- Modify: `src/zira_dashboard/_http_cache.py:56`
- Test: `tests/test_http_cache.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_http_cache.py` (create the file if it doesn't exist, with `from zira_dashboard import _http_cache` at top):

```python
def test_today_response_cache_ttl_is_60s():
    # The staffing-page warmer re-renders today's hot pages every 45s; the
    # today response-cache TTL must sit above that cadence so the cache
    # never goes cold between ticks.
    from zira_dashboard import _http_cache
    assert _http_cache._RESPONSE_CACHE_TODAY._ttl == 60.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_http_cache.py::test_today_response_cache_ttl_is_60s -v`
Expected: FAIL — `_ttl` is still `15.0`.

- [ ] **Step 3: Make the change**

In `src/zira_dashboard/_http_cache.py`, change line 56 from:

```python
_RESPONSE_CACHE_TODAY = TTLCache(ttl_seconds=15.0, max_entries=64)
```

to:

```python
# 60s, not 15s: the staffing page-warmer re-renders today's hot pages
# every 45s, so a 60s TTL keeps a comfortable margin and the cache never
# goes cold between ticks. Mutations still call invalidate_today_cache()
# so saves appear immediately regardless of TTL. (Browser-side
# Cache-Control stays at _TODAY_MAX_AGE=15s — the browser revalidates
# every 15s and hits this warm server cache, so revalidation is ~free.)
_RESPONSE_CACHE_TODAY = TTLCache(ttl_seconds=60.0, max_entries=64)
```

Leave `_TODAY_MAX_AGE = 15` unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_http_cache.py::test_today_response_cache_ttl_is_60s -v`
Expected: PASS. Local: `python -m py_compile src/zira_dashboard/_http_cache.py`.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/_http_cache.py tests/test_http_cache.py
git commit -m "perf(staffing): raise today response-cache TTL to 60s for the page warmer"
```

---

## Task 2: `page_warmer` module — synthetic request + warm tick

**Files:**
- Create: `src/zira_dashboard/page_warmer.py`
- Test: `tests/test_page_warmer.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_page_warmer.py`:

```python
"""Unit tests for the staffing page warmer. No DB required — the handlers
are monkeypatched so we test the warmer's wiring, not the pages."""
from starlette.requests import Request


def test_synthetic_get_request_shape():
    from zira_dashboard.page_warmer import _synthetic_get_request
    req = _synthetic_get_request("/staffing", b"day=2026-05-29")
    assert isinstance(req, Request)
    assert req.method == "GET"
    assert req.url.path == "/staffing"
    assert req.query_params["day"] == "2026-05-29"


def test_warm_once_calls_day_view_and_leaderboards(monkeypatch):
    calls = []

    def fake_day(request, *, day, publish_blocked, view):
        calls.append(("day", day, publish_blocked, view))
        return object()

    def fake_lb(request, *, window, metric, start, end):
        calls.append(("lb", window, metric, start, end))
        return object()

    monkeypatch.setattr("zira_dashboard.routes.staffing.staffing_page", fake_day)
    monkeypatch.setattr(
        "zira_dashboard.routes.leaderboards.staffing_leaderboards", fake_lb
    )

    from zira_dashboard import page_warmer
    page_warmer.warm_once()

    # Reproduces exactly what a bare /staffing and /staffing/leaderboards
    # navigation renders (day=None -> next working day; window="week").
    assert ("day", None, 0, "draft") in calls
    assert ("lb", "week", "pct", None, None) in calls


def test_warm_once_swallows_a_failing_handler(monkeypatch):
    called = []

    def boom(*a, **k):
        raise RuntimeError("stratustime down")

    def ok_lb(request, *, window, metric, start, end):
        called.append("lb")
        return object()

    monkeypatch.setattr("zira_dashboard.routes.staffing.staffing_page", boom)
    monkeypatch.setattr(
        "zira_dashboard.routes.leaderboards.staffing_leaderboards", ok_lb
    )

    from zira_dashboard import page_warmer
    # Must not raise even though the day-view handler blew up, and must
    # still warm the leaderboards after the day-view failure.
    page_warmer.warm_once()
    assert called == ["lb"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_page_warmer.py -v`
Expected: FAIL — `zira_dashboard.page_warmer` doesn't exist.

- [ ] **Step 3: Create `src/zira_dashboard/page_warmer.py`**

```python
"""Pre-render the hot staffing pages into the HTTP response cache.

The day-view and leaderboards GET handlers already render AND call
``_http_cache.store_cached_response()`` themselves. This module simply
invokes them on a background tick (from ``app.py``'s lifespan loops) so
the response cache is populated proactively — a human never pays the
~1.9s cold render; they hit the warm <1ms cached bytes instead.

Calling the handlers as plain functions (the ``share.py`` pattern)
bypasses the ASGI middleware stack entirely, so no auth is involved. The
handlers only touch ``request`` to pass it to
``templates.TemplateResponse``; the staffing-section templates never
dereference ``request.session`` / ``url_for`` / ``request.url`` (verified),
so a minimal synthetic Request renders byte-identical HTML.
"""
from __future__ import annotations

import logging

from starlette.requests import Request

_log = logging.getLogger(__name__)


def _synthetic_get_request(path: str, query_string: bytes = b"") -> Request:
    """Build a minimal ASGI GET ``Request`` for calling a page handler
    outside the request cycle. Enough scope for Starlette's
    ``TemplateResponse``; no ``app``/``session`` needed because the
    staffing templates don't use ``url_for`` or ``request.session``."""
    async def _receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("127.0.0.1", 80),
        "client": ("127.0.0.1", 0),
        "root_path": "",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query_string,
        "headers": [],
    }
    return Request(scope, receive=_receive)


def warm_once() -> None:
    """Render today's hot, frequently-changing staffing pages so their
    handlers repopulate the response cache. Each page is warmed
    independently; a failure in one must never block the others or crash
    the caller (the warmer loop must never die)."""
    # Day-view: a bare /staffing nav resolves day=None -> next working day,
    # view="draft", publish_blocked=0. Pass them explicitly (not via Query
    # defaults) so the handler sees real values, reproducing the exact
    # cache key a human's bare navigation produces.
    try:
        from .routes.staffing import staffing_page
        staffing_page(
            _synthetic_get_request("/staffing"),
            day=None,
            publish_blocked=0,
            view="draft",
        )
    except Exception as e:  # noqa: BLE001 — warmer must never bubble
        _log.warning("page_warmer: day-view warm failed: %s", e)

    # Leaderboards: bare nav -> window="week", metric="pct".
    try:
        from .routes.leaderboards import staffing_leaderboards
        staffing_leaderboards(
            _synthetic_get_request("/staffing/leaderboards"),
            window="week",
            metric="pct",
            start=None,
            end=None,
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("page_warmer: leaderboards warm failed: %s", e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_page_warmer.py -v`
Expected: 3 PASS. Local: `python -m py_compile src/zira_dashboard/page_warmer.py`.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/page_warmer.py tests/test_page_warmer.py
git commit -m "feat(page_warmer): synthetic-request warm tick for day-view + leaderboards"
```

---

## Task 3: Wire the day-view/leaderboards warmer loop into the lifespan

**Files:**
- Modify: `src/zira_dashboard/app.py` (loop definition near the other `_warm_*` loops ~line 181; task creation ~line 241; cancellation tuple ~line 247)
- Test: `tests/test_page_warmer.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_page_warmer.py`:

```python
import asyncio


def test_app_defines_staffing_pages_loop():
    # Structural check: the lifespan loop exists and is a coroutine fn.
    # conftest sets the test env so importing app is safe.
    from zira_dashboard import app as app_module
    assert hasattr(app_module, "_warm_staffing_pages_loop")
    assert asyncio.iscoroutinefunction(app_module._warm_staffing_pages_loop)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_page_warmer.py::test_app_defines_staffing_pages_loop -v`
Expected: FAIL — `_warm_staffing_pages_loop` not defined.

- [ ] **Step 3: Add the loop definition**

In `src/zira_dashboard/app.py`, add this alongside the other `_warm_*` loop definitions — anywhere at module level above the `lifespan` function (e.g., just after `_prewarm_stratustime`, ~line 215). It uses the module-level `_log` the other loops already use:

```python
async def _warm_staffing_pages_loop():
    """Keep today's hot staffing pages pre-rendered in the response cache
    so the first human load — including the first after a Railway deploy —
    is a warm <1ms hit instead of a ~1.9s cold render. Ticks every 45s
    (matching the live_cache data-refresh cadence); the response cache TTL
    is 60s so it never goes cold between ticks. The first iteration runs
    immediately on boot, before the first sleep."""
    from . import page_warmer
    while True:
        try:
            await asyncio.to_thread(page_warmer.warm_once)
        except Exception as e:  # noqa: BLE001 — warmer must never die
            _log.warning("staffing page warmer tick failed: %s", e)
        await asyncio.sleep(45)
```

- [ ] **Step 4: Schedule + cancel the task in the lifespan**

In `lifespan` (~line 241), after the `time_off_balance_task = ...` line, add:

```python
    staffing_pages_task = asyncio.create_task(_warm_staffing_pages_loop())
```

Then add `staffing_pages_task,` to the cancellation tuple in the `finally` block (the `for t in (...)` list, ~line 247):

```python
        for t in (
            warmer_task,
            st_warmer_task,
            live_cache_task,
            kiosk_sync_task,
            time_off_sync_task,
            time_off_poll_task,
            time_off_balance_task,
            staffing_pages_task,
        ):
```

- [ ] **Step 5: Run test + compile check**

Run: `pytest tests/test_page_warmer.py::test_app_defines_staffing_pages_loop -v`
Expected: PASS. Local: `python -m py_compile src/zira_dashboard/app.py`.

- [ ] **Step 6: Add CHANGELOG entry (Tier 1 deploy)**

Insert a new `### <HH:MM AM/PM>` block under today's date in `CHANGELOG.md`:

```markdown
### <HH:MM AM/PM>

- **Staffing first-load is now warm, not cold** — the day-view and leaderboards already cache their rendered HTML, but the cache was populated lazily by the first visitor, who paid the full ~1.9s render (and again after every deploy / 15s TTL expiry). A new background loop (`page_warmer.warm_once`, ticking every 45s and on boot) now pre-renders today's `/staffing` and default `/staffing/leaderboards` straight into the response cache, and the today cache TTL was raised 15s → 60s so it stays warm between ticks. First load — including the first after a Railway redeploy — now serves the cached bytes in <1ms instead of re-rendering. Mutations still call `invalidate_today_cache()`, so saves appear immediately. The warmer calls the handlers directly (bypassing auth/middleware) via a minimal synthetic request and can never crash the app.
```

- [ ] **Step 7: Commit + deploy + measure**

```bash
git add src/zira_dashboard/app.py tests/test_page_warmer.py CHANGELOG.md
git commit -m "feat(staffing): background-warm today's day-view + leaderboards response cache"
```

After Railway deploys, follow the **Measurement protocol** step 2 for `/staffing` and `/staffing/leaderboards`. Record the cold-load `Server-Timing`/total before vs. after. **If the section's first-load is already ≥5× faster, Tiers 2–3 are optional polish — decide based on the numbers.**

---

# TIER 2 — Keep shared data warm for the long tail

## Task 4: Extend the roster in-process cache TTL to 1 hour

**Files:**
- Modify: `src/zira_dashboard/staffing.py:147`
- Test: `tests/test_staffing_roster_cache.py` (new) or nearest existing roster test module

- [ ] **Step 1: Write the failing tests**

Create `tests/test_staffing_roster_cache.py`:

```python
def test_roster_cache_ttl_is_one_hour():
    # Roster changes only on save_roster() / Odoo sync, both of which
    # invalidate the cache directly — so a short TTL just causes cold
    # misses on long-tail pages (player cards, odd date ranges). 1 hour.
    from zira_dashboard import staffing
    assert staffing._ROSTER_CACHE_TTL_SECONDS == 3600.0


def test_invalidate_roster_cache_clears_entry():
    # Invalidation must still work, so a TTL bump can't serve stale data
    # after an edit.
    from zira_dashboard import staffing
    staffing._ROSTER_CACHE = (["sentinel"], float("inf"))
    staffing._invalidate_roster_cache()
    assert staffing._ROSTER_CACHE is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_staffing_roster_cache.py -v`
Expected: `test_roster_cache_ttl_is_one_hour` FAILS (TTL is `60.0`); the invalidate test PASSES (already works).

- [ ] **Step 3: Make the change**

In `src/zira_dashboard/staffing.py`, change line 147 from:

```python
_ROSTER_CACHE_TTL_SECONDS = 60.0
```

to:

```python
# 1 hour. The roster only changes on save_roster() and Odoo sync, both of
# which call _invalidate_roster_cache() — so a short TTL buys no freshness,
# it just forces cold JOIN-heavy reloads on long-tail pages (player cards,
# unusual leaderboard ranges) that aren't covered by the page warmer.
_ROSTER_CACHE_TTL_SECONDS = 3600.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_staffing_roster_cache.py -v`
Expected: both PASS. Local: `python -m py_compile src/zira_dashboard/staffing.py`.

- [ ] **Step 5: CHANGELOG + commit (Tier 2 deploy)**

Add a `### <HH:MM AM/PM>` entry:

```markdown
### <HH:MM AM/PM>

- **Roster cache TTL raised 60s → 1 hour** — `staffing.load_roster()` runs a JOIN-heavy people+skills query; its in-process cache already invalidates on every roster save and Odoo sync, so the 60s TTL only forced needless cold reloads on long-tail staffing pages (player cards, unusual leaderboard ranges) that the page warmer doesn't pre-render. Bumped to 1 hour. Freshness is unchanged — writes still invalidate immediately.
```

```bash
git add src/zira_dashboard/staffing.py tests/test_staffing_roster_cache.py CHANGELOG.md
git commit -m "perf(staffing): roster in-process cache TTL 60s -> 1h (writes still invalidate)"
```

After deploy, re-measure a cold player-card load per the Measurement protocol.

---

# TIER 3 — Close the two uncached pages

## Task 5: Response cache for the skills matrix

**Files:**
- Modify: `src/zira_dashboard/routes/skills.py` (GET handler ~line 23; save/add/delete handlers ~lines 69, 150, 167; refresh ~line 86)
- Test: `tests/test_skills_cache.py` (new)

- [ ] **Step 1: Write the failing test (Postgres-gated)**

Create `tests/test_skills_cache.py`:

```python
import os

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="skills-matrix cache test needs a live DATABASE_URL",
)


@pytest.fixture
def client():
    from zira_dashboard import db
    db.init_pool()
    db.bootstrap_schema()
    from zira_dashboard.app import app
    return TestClient(app)


def test_skills_matrix_serves_from_cache(client, monkeypatch):
    # First GET renders + caches.
    r1 = client.get("/staffing/skills")
    assert r1.status_code == 200

    # Poison the roster load: a genuine second render would call it and
    # blow up. A cache hit skips it entirely.
    from zira_dashboard import staffing

    def _poison():
        raise AssertionError("load_roster called — skills matrix was not cached")

    monkeypatch.setattr(staffing, "load_roster", _poison)
    r2 = client.get("/staffing/skills")
    assert r2.status_code == 200
    assert r2.content == r1.content


def test_skills_save_invalidates_cache(client):
    from zira_dashboard import _http_cache

    client.get("/staffing/skills")  # populate cache
    assert _http_cache._RESPONSE_CACHE_TODAY.peek(("staffing_skills",)) is not None
    # A roster save must clear it so edits show immediately. Don't follow
    # the 303 redirect — the redirected GET would just repopulate the cache.
    client.post("/staffing/skills", data={}, follow_redirects=False)
    assert _http_cache._RESPONSE_CACHE_TODAY.peek(("staffing_skills",)) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_skills_cache.py -v`
Expected (with DATABASE_URL): FAIL — no caching yet (`test_skills_save_invalidates_cache` finds `None` because nothing was stored; `test_skills_matrix_serves_from_cache` calls the poisoned `load_roster` on the second GET). Without DATABASE_URL: SKIP.

- [ ] **Step 3: Add the response cache to the GET handler**

In `src/zira_dashboard/routes/skills.py`, replace the GET handler body (lines 23–66) so it checks/stores the cache. Change the imports line and wrap render + store:

```python
@router.get("/staffing/skills", response_class=HTMLResponse)
def staffing_skills(request: Request):
    from .. import odoo_sync, skill_matrix_views_store as views_store, db
    from .. import cert_lookup, _http_cache

    # Response cache. The matrix is roster + skill-level data, which changes
    # only on roster/skill writes (each invalidates the today bucket) and on
    # Odoo sync. On a cache hit we also skip the per-request
    # odoo_sync.sync(force=False) freshness check — fine within the 60s TTL,
    # and the page warmer / a real miss will re-trigger it.
    response_cache_key = ("staffing_skills",)
    cached_resp = _http_cache.get_cached_response(
        response_cache_key, includes_today=True
    )
    if cached_resp is not None:
        return cached_resp

    person_certs = cert_lookup.load_person_certs()
    sync_result = odoo_sync.sync(force=False)
    roster = staffing.load_roster()
    roster.sort(key=lambda p: (not p.active, p.name.lower()))
    active_count = sum(1 for p in roster if p.active)

    skill_rows = db.query(
        "SELECT name, skill_type FROM skills "
        "WHERE skill_type IN ('Production Skills', 'Supervisor Skills') "
        "ORDER BY skill_type, lower(name)"
    )
    columns = [r["name"] for r in skill_rows]
    type_by_skill = {r["name"]: r["skill_type"] for r in skill_rows}

    all_views = views_store.list_views()
    default_view = views_store.get_default_view()

    response = templates.TemplateResponse(
        request,
        "skills.html",
        {
            "active": "skills",
            "people": roster,
            "person_certs": person_certs,
            "skills": columns,
            "type_by_skill": type_by_skill,
            "views": all_views,
            "default_view_name": default_view["name"] if default_view else None,
            "default_view_state": default_view,
            "active_count": active_count,
            "inactive_count": len(roster) - active_count,
            "sync_ok": sync_result.ok,
            "sync_last_at": sync_result.last_sync_at.isoformat() if sync_result.last_sync_at else None,
            "sync_error": sync_result.error,
            "odoo_url": os.environ.get("ODOO_URL", "").rstrip("/"),
        },
    )
    _http_cache.set_cache_headers(response, includes_today=True)
    _http_cache.store_cached_response(
        response_cache_key, includes_today=True, response=response
    )
    return response
```

- [ ] **Step 4: Add invalidation to the roster-write handlers**

In the same file, after each `staffing.save_roster(roster)` call (in `staffing_skills_save` ~line 80, `staffing_person_add` ~line 161, `staffing_person_delete` ~line 178) and after the force-sync in `staffing_skills_refresh` (~line 91, after `result = odoo_sync.sync(force=True)`), add:

```python
    from .. import _http_cache
    _http_cache.invalidate_today_cache()
```

(Placement: immediately after the save/sync line, before the response is built. `invalidate_today_cache()` clears the whole today bucket — day-view, leaderboards, skills, today player-cards — which is correct: a roster change can affect all of them.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_skills_cache.py -v`
Expected (with DATABASE_URL): PASS. Local: `python -m py_compile src/zira_dashboard/routes/skills.py`.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/skills.py tests/test_skills_cache.py
git commit -m "perf(skills): response-cache the skills matrix + invalidate on roster writes"
```

---

## Task 6: Response cache for player cards

**Files:**
- Modify: `src/zira_dashboard/routes/people.py` (GET handler ~line 39; attendance-reason write ~line 150)
- Test: `tests/test_player_card_cache.py` (new)

- [ ] **Step 1: Write the failing test (Postgres-gated)**

Create `tests/test_player_card_cache.py`:

```python
import os

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="player-card cache test needs a live DATABASE_URL",
)


@pytest.fixture
def client():
    from zira_dashboard import db
    db.init_pool()
    db.bootstrap_schema()
    from zira_dashboard.app import app
    return TestClient(app)


def test_player_card_serves_from_cache(client, monkeypatch):
    # Any name renders (empty data is fine — it 200s with zeros). Use a
    # genuinely past range (end < today) so it lands in the 5-min past
    # bucket and is immutable for the test.
    r1 = client.get("/staffing/people/Nobody?start=2025-01-01&end=2025-01-31")
    assert r1.status_code == 200

    # A second GET must hit the cache and not recompute attribution.
    from zira_dashboard import production_history

    def _poison(*a, **k):
        raise AssertionError("attribution_range called — player card not cached")

    monkeypatch.setattr(production_history, "attribution_range", _poison)
    r2 = client.get("/staffing/people/Nobody?start=2025-01-01&end=2025-01-31")
    assert r2.status_code == 200
    assert r2.content == r1.content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_player_card_cache.py -v`
Expected (with DATABASE_URL): FAIL — second GET calls the poisoned `attribution_range`. Without DATABASE_URL: SKIP.

- [ ] **Step 3: Add the response cache to the GET handler**

In `src/zira_dashboard/routes/people.py`, restructure the top of `staffing_player_card` (lines 46–50) so date resolution happens before the cache check, then wrap render + store. Replace lines 46–50:

```python
    from .. import production_history, _http_cache
    today = datetime.now(timezone.utc).date()
    end_d = date.fromisoformat(end) if end else today
    start_d = date.fromisoformat(start) if start else (end_d - timedelta(days=29))

    # Response cache, keyed per person + range. A today-inclusive range goes
    # in the 60s today bucket (busted by attribution/attendance writes via
    # invalidate_today_cache); a past-only range is immutable for the 5min
    # past bucket (only the nightly precompute changes past attribution).
    includes_today = end_d >= today
    response_cache_key = ("player_card", name, start_d.isoformat(), end_d.isoformat())
    cached_resp = _http_cache.get_cached_response(
        response_cache_key, includes_today=includes_today
    )
    if cached_resp is not None:
        return cached_resp

    range_out = production_history.attribution_range(start_d, end_d)
```

Then at the end of the handler, change the final `return templates.TemplateResponse(...)` (lines 125–147) to capture, cache-tag, store, and return:

```python
    response = templates.TemplateResponse(
        request,
        "player_card.html",
        {
            "active": "people",
            "name": name,
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "today": today.isoformat(),
            "rows": rows,
            "group_avgs": group_avgs,
            "total_units": round(total_units, 1),
            "total_downtime": round(total_downtime, 1),
            "total_days": total_days,
            "skills": skills,
            "day_rows": day_rows,
            "attendance_rows": attendance_rows,
            "total_absent_days": total_absent_days,
            "total_late_days": total_late_days,
            "roster_names": roster_names,
            "awards_earned": awards_earned,
        },
    )
    _http_cache.set_cache_headers(response, includes_today=includes_today)
    _http_cache.store_cached_response(
        response_cache_key, includes_today=includes_today, response=response
    )
    return response
```

- [ ] **Step 4: Invalidate on the attendance-reason write**

In the same file, in `update_attendance_reason` (~line 150), after the `db.execute(...)` UPDATE (line 169–172) and before `return JSONResponse({"ok": True})`, add:

```python
    from .. import _http_cache
    _http_cache.invalidate_today_cache()
```

(Attribution edits already call `invalidate_today_cache()` from `routes/staffing.py` — confirmed at 7 sites — which clears today-inclusive player cards too. This adds the one write path that lives in `people.py`.)

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_player_card_cache.py -v`
Expected (with DATABASE_URL): PASS. Local: `python -m py_compile src/zira_dashboard/routes/people.py`.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/people.py tests/test_player_card_cache.py
git commit -m "perf(player-card): response-cache per person+range + invalidate on attendance edit"
```

---

## Task 7: Warm the skills matrix on a relaxed cadence

**Files:**
- Modify: `src/zira_dashboard/page_warmer.py` (add `warm_skills_once`)
- Modify: `src/zira_dashboard/app.py` (add `_warm_staffing_stable_loop` + schedule/cancel)
- Test: `tests/test_page_warmer.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_page_warmer.py`:

```python
def test_warm_skills_once_calls_handler(monkeypatch):
    calls = []

    def fake_skills(request):
        calls.append("skills")
        return object()

    monkeypatch.setattr("zira_dashboard.routes.skills.staffing_skills", fake_skills)
    from zira_dashboard import page_warmer
    page_warmer.warm_skills_once()
    assert calls == ["skills"]


def test_warm_skills_once_swallows_exception(monkeypatch):
    def boom(request):
        raise RuntimeError("db down")

    monkeypatch.setattr("zira_dashboard.routes.skills.staffing_skills", boom)
    from zira_dashboard import page_warmer
    page_warmer.warm_skills_once()  # must not raise


def test_app_defines_staffing_stable_loop():
    import asyncio
    from zira_dashboard import app as app_module
    assert asyncio.iscoroutinefunction(app_module._warm_staffing_stable_loop)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_page_warmer.py -k "skills or stable_loop" -v`
Expected: FAIL — `warm_skills_once` / `_warm_staffing_stable_loop` not defined.

- [ ] **Step 3: Add `warm_skills_once` to `page_warmer.py`**

Append to `src/zira_dashboard/page_warmer.py`:

```python
def warm_skills_once() -> None:
    """Warm the skills matrix. Separate from warm_once() because roster /
    skill data changes rarely (and writes invalidate the cache directly),
    so this runs on a relaxed cadence — and warming it triggers
    odoo_sync.sync(force=False), which we don't want to fire every 45s."""
    try:
        from .routes.skills import staffing_skills
        staffing_skills(_synthetic_get_request("/staffing/skills"))
    except Exception as e:  # noqa: BLE001
        _log.warning("page_warmer: skills warm failed: %s", e)
```

- [ ] **Step 4: Add the relaxed loop to `app.py`**

After `_warm_staffing_pages_loop` in `src/zira_dashboard/app.py`, add:

```python
async def _warm_staffing_stable_loop():
    """Warm the slow-changing staffing pages (the skills matrix) every
    5 min. Roster/skill data rarely changes and writes invalidate the
    cache directly, so 5 min is plenty — and it avoids triggering
    odoo_sync.sync(force=False) every 45s. First iteration runs on boot."""
    from . import page_warmer
    while True:
        try:
            await asyncio.to_thread(page_warmer.warm_skills_once)
        except Exception as e:  # noqa: BLE001
            _log.warning("staffing stable warmer tick failed: %s", e)
        await asyncio.sleep(300)
```

In `lifespan`, after the `staffing_pages_task = ...` line add:

```python
    staffing_stable_task = asyncio.create_task(_warm_staffing_stable_loop())
```

and add `staffing_stable_task,` to the cancellation tuple in `finally`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_page_warmer.py -v`
Expected: all PASS. Local: `python -m py_compile src/zira_dashboard/page_warmer.py src/zira_dashboard/app.py`.

- [ ] **Step 6: CHANGELOG + commit (Tier 3 deploy)**

Add a `### <HH:MM AM/PM>` entry:

```markdown
### <HH:MM AM/PM>

- **Skills matrix + player cards now cached (and the matrix pre-warmed)** — these were the two staffing pages with no response cache, so they re-rendered from scratch on every load (the skills matrix is a 40 KB person×skill grid). The skills matrix now caches its render (invalidated on every roster/skill write and Odoo force-sync) and is pre-warmed every 5 min by a background loop. Player cards cache per person+range — today-inclusive ranges in the 60s bucket (busted by attribution/attendance edits), past-only ranges for 5 min. Second views are now instant; cold first views are cheaper thanks to the 1-hour roster cache.
```

```bash
git add src/zira_dashboard/page_warmer.py src/zira_dashboard/app.py tests/test_page_warmer.py CHANGELOG.md
git commit -m "perf(staffing): warm the skills matrix on a 5-min loop"
```

After deploy, re-measure cold loads of `/staffing/skills` and a player card.

---

## Task 8: Final measurement & acceptance

**Files:**
- Modify: `CHANGELOG.md` (only if a measurement summary is worth recording)

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q`
Expected: same pass count as baseline plus the new tests; no regressions. Investigate any failure before declaring done.

- [ ] **Step 2: Measure all four pages cold vs. warm**

Per the Measurement protocol, capture cold-first-load and warm-reload numbers for `/staffing`, `/staffing/leaderboards`, `/staffing/skills`, and a `/staffing/people/<name>` card. Compare to baseline.

- [ ] **Step 3: Acceptance check**

- [ ] Cold first-load of each staffing page is ≥5× faster than baseline (or the warm-hit path is what users now reliably get).
- [ ] Editing + saving a schedule / roster / attribution still shows fresh data immediately (invalidation works).
- [ ] A StratusTime/Odoo outage doesn't crash the app — the warmer logs and keeps going.
- [ ] All existing tests pass.

If a page still misses 5× on cold load, that's the signal (and only then) to open the spec's deferred **Approach B** (template-render surgery) as a follow-up.

---

## Notes for the implementer

- **Why calling handlers directly is safe:** the staffing-section templates never touch `request.session`, `url_for`, or `request.url` (verified by grep), and `static_v` is a request-independent Jinja global. The minimal synthetic `Request` renders byte-identical HTML, and the direct call bypasses the auth middleware entirely.
- **The handlers store to the cache themselves** (`staffing_page` at `routes/staffing.py:685`, `staffing_leaderboards` at `routes/leaderboards.py:376`), so the warmer only needs to *call* them — no cache plumbing in the warmer.
- **All `_warm_*` loops run their first iteration before the first `sleep`**, so adding the loop to the lifespan is all that's needed for boot-time (post-deploy) warming. No separate startup hook.
- **TTL vs. cadence invariant:** the day-view/leaderboards warmer ticks at 45s and the today cache TTL is 60s — keep TTL > tick so the cache never goes cold between ticks. If you change one, change the other.
- **Rollback:** each tier is its own commit(s). Tier 1 reverts by deleting `_warm_staffing_pages_loop` (+ its task line) and reverting the one TTL constant; Tier 2 reverts the roster TTL constant; Tier 3 reverts the per-route cache blocks. No data migrations, so any tier can be reverted independently without cleanup.
