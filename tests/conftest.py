"""Pytest bootstrap.

Set ``AUTH_DISABLED=1`` before any test module imports ``zira_dashboard.app``
so the new ``RequireAuthMiddleware`` short-circuits and existing TestClient
tests (which don't carry session cookies) keep working unchanged.

Also seeds a deterministic ``SESSION_SECRET`` so the session signer doesn't
randomly invalidate fixtures between runs.

Both use ``setdefault`` — a test that wants to exercise the real auth gate
can still set the env vars before importing the app.
"""

from __future__ import annotations

import os

os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault(
    "SESSION_SECRET", "test-secret-32-bytes-of-random-data!!"
)

import pytest


@pytest.fixture(autouse=True)
def _reset_response_cache():
    """Clear the process-global HTTP response cache before every test.

    Cache-backed routes (the player card, /staffing) store rendered
    responses in module-level TTLCaches (see ``_http_cache``). Without a
    reset, the first test to render a given person/day populates the cache,
    and later tests requesting the same key get a cache hit — skipping the
    render path they assert on (e.g. a monkeypatched ``TemplateResponse``
    never fires, leaving ``KeyError: 'ctx'``). Clearing before each test
    restores isolation. Wrapped in try/except so it's a no-op when the app
    package can't import (env-gated tests).
    """
    try:
        from zira_dashboard import _http_cache
        _http_cache.invalidate_all_cache()
    except Exception:
        pass
    yield
