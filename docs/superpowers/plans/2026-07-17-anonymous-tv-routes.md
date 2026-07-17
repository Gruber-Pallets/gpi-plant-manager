# Anonymous TV Routes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Existing TV dashboard URLs load without Microsoft authentication while all non-TV routes remain protected.

**Architecture:** Add the `/tv/` prefix to the authentication middleware's explicit bypass list. The bypass happens before session, IP allowlist, and device-token checks, so it preserves every existing TV URL—including legacy redirects and the refresh probe—without changing the routes themselves. Regression tests exercise anonymous representative TV URLs and an authenticated boundary check.

**Tech Stack:** Python 3.12, FastAPI, Starlette middleware, pytest.

## Global Constraints

- Only paths starting with `/tv/` become anonymous.
- No TV URL, display record, device token, or deployment variable changes.
- Every non-TV route must retain Microsoft-authentication behavior.

---

### Task 1: Permit bare TV URLs in the authentication middleware

**Files:**
- Modify: `tests/test_auth_middleware.py:33-41`
- Modify: `tests/test_tv_ping.py`
- Modify: `src/zira_dashboard/auth.py:171-188`

**Interfaces:**
- Consumes: `RequireAuthMiddleware.dispatch(request, call_next)` and `_is_bypass_path(path)`.
- Produces: Anonymous access for all requests whose `request.url.path` starts with `/tv/`; existing redirect behavior for non-TV paths.

- [x] **Step 1: Write the failing tests**

Add two representative TV routes to `mini_app`, then add the assertions below. The first must prove a saved-slug URL works without a cookie; the second must prove a legacy URL works without a cookie; the existing `/recycling` test remains the non-TV boundary.

```python
    @app.get("/tv/dismantler-1")
    def _tv_slug(): return PlainTextResponse("tv-slug-ok")

    @app.get("/tv/d/dismantler-1")
    def _tv_legacy(): return PlainTextResponse("tv-legacy-ok")


def test_tv_slug_path_bypasses_auth_without_device_setup(mini_app):
    c = TestClient(mini_app)
    r = c.get("/tv/dismantler-1", follow_redirects=False)
    assert r.status_code == 200
    assert r.text == "tv-slug-ok"


def test_legacy_tv_path_bypasses_auth_without_device_setup(mini_app):
    c = TestClient(mini_app)
    r = c.get("/tv/d/dismantler-1", follow_redirects=False)
    assert r.status_code == 200
    assert r.text == "tv-legacy-ok"
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_auth_middleware.py::test_tv_slug_path_bypasses_auth_without_device_setup tests/test_auth_middleware.py::test_legacy_tv_path_bypasses_auth_without_device_setup -q`

Expected: both tests fail with an HTTP 302 redirect to `/auth/login` because `/tv/` is not currently an auth-bypass prefix.

- [x] **Step 3: Add the minimal middleware change**

Add `"/tv/"` to `_BYPASS_PREFIXES` in `src/zira_dashboard/auth.py`:

```python
_BYPASS_PREFIXES = (
    "/auth/",
    "/static/",
    "/tv/",
    "/api/v1/object/",
)
```

Keep the existing device-token and IP-allowlist code in place for compatibility; it becomes unnecessary for `/tv/*` but remains harmless.

- [x] **Step 4: Run focused verification**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_auth_middleware.py tests/test_tv_ping.py -q`

Expected: all tests pass, including the existing assertion that `/recycling` redirects to `/auth/login`, anonymous `/tv/ping` checks, and the TV refresh-script probe tests.

- [x] **Step 5: Run the complete suite and commit**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`

Expected: exit code 0, with only environment-gated/skipped tests skipped.

Then commit only the middleware, its tests, and this plan:

```bash
git add src/zira_dashboard/auth.py tests/test_auth_middleware.py tests/test_tv_ping.py docs/superpowers/plans/2026-07-17-anonymous-tv-routes.md
git commit -m "fix: allow bare TV dashboard URLs"
```
