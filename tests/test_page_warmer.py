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
    monkeypatch.setattr("zira_dashboard.routes.leaderboards.staffing_leaderboards", fake_lb)

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
import logging

import pytest


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


def test_app_company_holiday_warmer_registered_every_ten_minutes():
    from zira_dashboard import app as app_module

    assert asyncio.iscoroutinefunction(app_module._tick_company_holidays)
    entry = next(
        (
            warmer
            for warmer in app_module._WARMERS
            if warmer[1] is app_module._tick_company_holidays
        ),
        None,
    )
    assert entry == (
        "company holidays",
        app_module._tick_company_holidays,
        600,
    )


def test_lifespan_hydrates_persisted_holidays_before_seed_warmers_or_yield(
    monkeypatch,
):
    from zira_dashboard import app as app_module, company_holidays, tv_displays_store

    events = []
    monkeypatch.setattr(app_module.db, "init_pool", lambda: events.append("pool"))
    monkeypatch.setattr(app_module.db, "bootstrap_schema", lambda: events.append("schema"))
    monkeypatch.setattr(
        company_holidays,
        "reload",
        lambda: events.append("holidays") or {},
    )
    monkeypatch.setattr(
        tv_displays_store,
        "seed_defaults_if_empty",
        lambda: events.append("seed"),
    )
    monkeypatch.setattr(
        app_module,
        "_WARMERS",
        [("dependent", lambda: events.append("warmer"), 60)],
    )

    async def exercise():
        async with app_module.lifespan(app_module.app):
            events.append("yield")
            assert events[:5] == ["pool", "schema", "holidays", "seed", "yield"]

    asyncio.run(exercise())

    assert events[-1] == "yield"
    assert "warmer" not in events


def test_lifespan_fails_before_serving_when_persisted_holiday_reload_fails(
    monkeypatch,
    caplog,
):
    from zira_dashboard import app as app_module, company_holidays, tv_displays_store

    events = []
    monkeypatch.setattr(app_module.db, "init_pool", lambda: events.append("pool"))
    monkeypatch.setattr(app_module.db, "bootstrap_schema", lambda: events.append("schema"))
    monkeypatch.setattr(app_module.db, "shutdown_pool", lambda: events.append("shutdown"))
    monkeypatch.setattr(
        company_holidays,
        "reload",
        lambda: (_ for _ in ()).throw(RuntimeError("mirror database unavailable")),
    )
    monkeypatch.setattr(
        tv_displays_store,
        "seed_defaults_if_empty",
        lambda: events.append("seed"),
    )
    monkeypatch.setattr(app_module, "_WARMERS", [])

    async def exercise():
        async with app_module.lifespan(app_module.app):
            events.append("yield")

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(
            RuntimeError,
            match="mirror database unavailable",
        ),
    ):
        asyncio.run(exercise())

    assert events == ["pool", "schema", "shutdown"]
    assert "company holiday" in caplog.text.lower()


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


def test_warm_inbox_once_refreshes_auto_lunch_guard_once(monkeypatch):
    from zira_dashboard import auto_lunch_guard, page_warmer

    calls = []
    monkeypatch.setattr(
        "zira_dashboard.routes.staffing.assignments_todo_payload",
        lambda force=False: None,
    )
    monkeypatch.setattr(
        "zira_dashboard.routes.staffing.late_report_payload",
        lambda force=False: None,
    )
    monkeypatch.setattr(
        auto_lunch_guard,
        "refresh",
        lambda: calls.append("auto_lunch"),
        raising=False,
    )

    page_warmer.warm_inbox_once()

    assert calls == ["auto_lunch"]


def test_next_inbox_warm_tick_publishes_external_auto_lunch_change(monkeypatch):
    from zira_dashboard import auto_lunch_guard, page_warmer
    from zira_dashboard.auto_lunch_settings import Settings

    observed = iter([
        Settings(False, True, 5.0, 30),
        Settings(True, False, 5.0, 30),
    ])
    calls = []
    monkeypatch.setattr(
        auto_lunch_guard,
        "_published_alert",
        getattr(auto_lunch_guard, "_UNSET", object()),
        raising=False,
    )
    monkeypatch.setattr(
        auto_lunch_guard,
        "_published_failure",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        auto_lunch_guard,
        "observe",
        lambda: calls.append("observe") or next(observed),
    )
    monkeypatch.setattr(
        "zira_dashboard.routes.staffing.assignments_todo_payload",
        lambda force=False: None,
    )
    monkeypatch.setattr(
        "zira_dashboard.routes.staffing.late_report_payload",
        lambda force=False: None,
    )

    page_warmer.warm_inbox_once()
    assert auto_lunch_guard.current_alert()["label"] == "Off"

    page_warmer.warm_inbox_once()
    assert auto_lunch_guard.current_alert() is None
    assert calls == ["observe", "observe"]


def test_warm_inbox_once_swallows_a_failing_source(monkeypatch):
    called = []

    def boom(force=False):
        raise RuntimeError("odoo down")

    monkeypatch.setattr("zira_dashboard.routes.staffing.assignments_todo_payload", boom)
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
    entry = next((e for e in app_module._WARMERS if e[1] is app_module._tick_inbox), None)
    assert entry is not None, "inbox warmer not registered in _WARMERS"
    _name, _tick, interval = entry
    assert interval < 30, "inbox warmer must refresh before the 30s sub-cache TTL"


def test_assignments_payload_force_bypasses_a_fresh_cache(monkeypatch):
    import time as _time
    from zira_dashboard.routes import staffing

    monkeypatch.setitem(staffing._ASSIGNMENTS_TODO_CACHE, "value", {"sentinel": "x"})
    monkeypatch.setitem(staffing._ASSIGNMENTS_TODO_CACHE, "expires_at", _time.time() + 10_000)
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
    monkeypatch.setitem(staffing._LATE_REPORT_CACHE, "expires_at", _time.time() + 10_000)
    assert staffing.late_report_payload() == {"sentinel": "y"}
    out = staffing.late_report_payload(force=True)
    assert out.get("sentinel") is None
    assert "today" in out


def test_payroll_work_entry_guard_registered_every_five_minutes():
    from zira_dashboard import app as app_module

    assert asyncio.iscoroutinefunction(app_module._tick_payroll_work_entry_guard)
    entry = next(
        (
            item
            for item in app_module._WARMERS
            if item[1] is app_module._tick_payroll_work_entry_guard
        ),
        None,
    )
    assert entry == (
        "payroll work-entry guard",
        app_module._tick_payroll_work_entry_guard,
        300,
    )


def test_payroll_guard_tick_runs_blocking_work_off_event_loop(monkeypatch):
    from zira_dashboard import app as app_module, payroll_work_entry_guard

    calls = []

    async def fake_to_thread(fn, *args):
        calls.append((fn, args))
        return fn(*args)

    monkeypatch.setattr(app_module.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        payroll_work_entry_guard, "run_once", lambda: {"corrected": 0}
    )
    asyncio.run(app_module._tick_payroll_work_entry_guard())
    assert calls == [(payroll_work_entry_guard.run_once, ())]
