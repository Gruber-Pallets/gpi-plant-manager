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


def test_warm_once_calls_day_view_only(monkeypatch):
    # Leaderboards were dropped from the 45s warm loop 2026-07-07 (2 human
    # views/week vs ~13k warm renders; see page_warmer.warm_once docstring).
    calls = []

    def fake_day(request, *, day, publish_blocked, view):
        calls.append(("day", day, publish_blocked, view))
        return object()

    def fake_lb(request, *, window, metric, start, end):
        calls.append("lb")
        return object()

    monkeypatch.setattr("zira_dashboard.routes.staffing.staffing_page", fake_day)
    monkeypatch.setattr(
        "zira_dashboard.routes.leaderboards.staffing_leaderboards", fake_lb
    )

    from zira_dashboard import page_warmer
    page_warmer.warm_once()

    assert calls == [("day", None, 0, "draft")]


def test_warm_once_swallows_a_failing_handler(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("stratustime down")

    monkeypatch.setattr("zira_dashboard.routes.staffing.staffing_page", boom)

    from zira_dashboard import page_warmer
    page_warmer.warm_once()  # must not raise


import asyncio


def test_app_defines_staffing_pages_loop():
    # Structural check: the staffing-pages warmer tick exists, is a coroutine,
    # and is registered in the warmer registry. conftest sets the test env so
    # importing app is safe.
    from zira_dashboard import app as app_module
    assert asyncio.iscoroutinefunction(app_module._tick_staffing_pages)
    assert any(t is app_module._tick_staffing_pages for _, t, _ in app_module._WARMERS)


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
    from zira_dashboard import app as app_module
    assert asyncio.iscoroutinefunction(app_module._tick_staffing_stable)
    assert any(t is app_module._tick_staffing_stable for _, t, _ in app_module._WARMERS)


# --- inbox top-nav cache warmer -----------------------------------------
# build_summary() renders on every page via _topnav.html. Its two expensive
# sub-sources (assignments-todo + late-report) self-cache for 30s but the TTL
# doesn't slide on hits, so without a dedicated warmer humans repeatedly pay the
# cold Zira/Odoo cascade just to draw the nav badge. These tests pin the fix.

def test_warm_inbox_once_force_refreshes_both_payloads(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "zira_dashboard.routes.staffing.assignments_todo_payload",
        lambda force=False: calls.append(("assign", force)),
    )
    monkeypatch.setattr(
        "zira_dashboard.routes.staffing.late_report_payload",
        lambda force=False: calls.append(("late", force)),
    )
    from zira_dashboard import page_warmer
    page_warmer.warm_inbox_once()
    # Both must be force-refreshed (force=True) so the TTL is reset every tick.
    assert ("assign", True) in calls
    assert ("late", True) in calls


def test_warm_inbox_once_swallows_a_failing_source(monkeypatch):
    called = []

    def boom(force=False):
        raise RuntimeError("odoo down")

    monkeypatch.setattr(
        "zira_dashboard.routes.staffing.assignments_todo_payload", boom
    )
    monkeypatch.setattr(
        "zira_dashboard.routes.staffing.late_report_payload",
        lambda force=False: called.append("late"),
    )
    from zira_dashboard import page_warmer
    page_warmer.warm_inbox_once()  # must not raise
    assert called == ["late"]


def test_app_inbox_warmer_registered_below_subcache_ttl():
    # Structural: the inbox warmer exists, is a coroutine, and runs more often
    # than the 30s sub-cache TTL — otherwise a cold gap reopens every cycle.
    from zira_dashboard import app as app_module
    assert asyncio.iscoroutinefunction(app_module._tick_inbox)
    entry = next(
        (e for e in app_module._WARMERS if e[1] is app_module._tick_inbox), None
    )
    assert entry is not None, "inbox warmer not registered in _WARMERS"
    _name, _tick, interval = entry
    assert interval < 30, "inbox warmer must refresh before the 30s sub-cache TTL"


def test_assignments_payload_force_bypasses_a_fresh_cache(monkeypatch):
    import time as _time
    from zira_dashboard.routes import staffing
    monkeypatch.setitem(staffing._ASSIGNMENTS_TODO_CACHE, "value", {"sentinel": "x"})
    monkeypatch.setitem(
        staffing._ASSIGNMENTS_TODO_CACHE, "expires_at", _time.time() + 10_000
    )
    # Normal read returns the fresh cached value.
    assert staffing.assignments_todo_payload() == {"sentinel": "x"}
    # force=True recomputes (no DB in tests -> degraded) instead of returning it.
    out = staffing.assignments_todo_payload(force=True)
    assert out.get("sentinel") is None
    assert "today" in out


def test_late_report_payload_force_bypasses_a_fresh_cache(monkeypatch):
    import time as _time
    from zira_dashboard.routes import staffing
    monkeypatch.setitem(staffing._LATE_REPORT_CACHE, "value", {"sentinel": "y"})
    monkeypatch.setitem(
        staffing._LATE_REPORT_CACHE, "expires_at", _time.time() + 10_000
    )
    assert staffing.late_report_payload() == {"sentinel": "y"}
    out = staffing.late_report_payload(force=True)
    assert out.get("sentinel") is None
    assert "today" in out
