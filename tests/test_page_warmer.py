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
    page_warmer.warm_once()
    assert called == ["lb"]


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
