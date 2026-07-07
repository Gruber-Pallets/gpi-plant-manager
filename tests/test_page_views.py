"""Page-usage tracking: counter, route-pattern resolution, noise-exclusion,
never-hit bucketing. Pure units run everywhere; the upsert/query path is
DB-gated below."""

from __future__ import annotations

from datetime import date

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from zira_dashboard import page_views


def _pattern_capture_app():
    """Tiny app that records the resolved route pattern of each request into a
    list, so we can assert what the middleware would store for a real URL."""
    app = FastAPI()
    seen: list = []

    @app.middleware("http")
    async def _rec(request: Request, call_next):
        resp = await call_next(request)
        seen.append(page_views.route_pattern(request))
        return resp

    @app.get("/things/{thing_id}")
    def _thing(thing_id: str):
        return PlainTextResponse(thing_id)

    @app.get("/plain")
    def _plain():
        return PlainTextResponse("ok")

    return app, seen


def test_counter_records_and_drains():
    c = page_views.PageViewCounter()
    day = date(2026, 7, 1)
    c.record(day, "/staffing", "GET", "dale@gruberpallets.com")
    c.record(day, "/staffing", "GET", "dale@gruberpallets.com")
    c.record(day, "/staffing", "GET", "amy@gruberpallets.com")

    rows = c.drain()

    assert (day, "/staffing", "GET", "dale@gruberpallets.com", 2) in rows
    assert (day, "/staffing", "GET", "amy@gruberpallets.com", 1) in rows
    # drain empties the counter so the next flush doesn't double-count.
    assert c.drain() == []


def test_route_pattern_returns_matched_template_not_concrete_url():
    app, seen = _pattern_capture_app()
    client = TestClient(app)
    client.get("/things/42")
    client.get("/plain")
    assert seen == ["/things/{thing_id}", "/plain"]


def test_route_pattern_is_none_for_unmatched_path():
    app, seen = _pattern_capture_app()
    client = TestClient(app)
    client.get("/nope/does-not-exist")
    assert seen == [None]


def test_should_track_excludes_noise_but_keeps_real_pages():
    assert page_views.should_track("/staffing") is True
    assert page_views.should_track("/timeclock/dashboard/{token}") is True
    assert page_views.should_track("/staffing/people/{name}") is True
    # noise: polling, health, assets, auth handshakes
    assert page_views.should_track("/tv/ping") is False
    assert page_views.should_track("/healthz") is False
    assert page_views.should_track("/favicon.ico") is False
    assert page_views.should_track("/robots.txt") is False
    assert page_views.should_track("/static") is False
    assert page_views.should_track("/auth/login") is False
    assert page_views.should_track("/auth/callback") is False


def test_never_hit_returns_inventory_minus_observed_sorted():
    inventory = ["/staffing", "/trophies", "/operator", "/settings"]
    observed = {"/staffing", "/settings"}
    assert page_views.never_hit(observed, inventory) == ["/operator", "/trophies"]


def test_page_inventory_keeps_get_pages_drops_api_and_noise():
    app = FastAPI()

    @app.get("/staffing")
    def _s():  # a page
        return "ok"

    @app.get("/wc/{slug}")
    def _wc(slug: str):  # a page with a param
        return slug

    @app.post("/staffing")
    def _sp():  # POST action, not a page
        return "ok"

    @app.get("/api/exceptions")
    def _api():  # JSON API, not a page
        return {}

    @app.get("/healthz")
    def _h():  # noise
        return "ok"

    assert page_views.page_inventory(app) == ["/staffing", "/wc/{slug}"]


def test_page_inventory_descends_into_included_routers():
    # Every real route mounts via include_router (see app.py). Modern FastAPI
    # (>=0.139) wraps each include_router() in an _IncludedRouter instead of
    # splicing its routes flat into app.routes — a flat scan sees nothing and
    # the never-hit report goes silently empty (bit prod on 2026-07-07).
    from fastapi import APIRouter

    app = FastAPI()
    router = APIRouter()

    @router.get("/staffing")
    def _s():
        return "ok"

    nested = APIRouter()

    @nested.get("/trophies")
    def _t():
        return "ok"

    router.include_router(nested)
    app.include_router(router)

    assert page_views.page_inventory(app) == ["/staffing", "/trophies"]


def test_flush_drains_counter_into_persist_and_skips_when_empty(monkeypatch):
    captured = []
    monkeypatch.setattr(page_views, "_persist", lambda rows: captured.append(rows))
    monkeypatch.setattr(page_views, "_counter", page_views.PageViewCounter())

    day = date(2026, 7, 1)
    page_views._counter.record(day, "/staffing", "GET", "dale@gruberpallets.com")

    page_views.flush()
    assert captured == [[(day, "/staffing", "GET", "dale@gruberpallets.com", 1)]]

    # Nothing accumulated since last flush -> no DB round-trip.
    page_views.flush()
    assert len(captured) == 1


def test_tracking_enabled_defaults_on_and_respects_killswitch(monkeypatch):
    monkeypatch.delenv("PAGE_VIEW_TRACKING_ENABLED", raising=False)
    assert page_views.tracking_enabled() is True
    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv("PAGE_VIEW_TRACKING_ENABLED", off)
        assert page_views.tracking_enabled() is False
    monkeypatch.setenv("PAGE_VIEW_TRACKING_ENABLED", "1")
    assert page_views.tracking_enabled() is True


def _record_view_app():
    app = FastAPI()

    @app.middleware("http")
    async def _rec(request: Request, call_next):
        resp = await call_next(request)
        page_views.record_view(request)
        return resp

    @app.get("/staffing/people/{name}")
    def _p(name: str):
        return PlainTextResponse(name)

    @app.get("/healthz")
    def _h():
        return PlainTextResponse("ok")

    return app


def test_record_view_counts_pages_and_skips_noise_and_unmatched(monkeypatch):
    monkeypatch.setenv("PAGE_VIEW_TRACKING_ENABLED", "1")
    monkeypatch.setattr(page_views, "_counter", page_views.PageViewCounter())
    client = TestClient(_record_view_app())

    client.get("/staffing/people/Juan")   # a page -> counted
    client.get("/staffing/people/Amy")    # same pattern -> merges
    client.get("/healthz")                # noise -> skipped
    client.get("/nope")                   # unmatched -> skipped, no raise

    rows = page_views._counter.drain()
    assert len(rows) == 1
    day, route, method, user, views = rows[0]
    assert (route, method, views) == ("/staffing/people/{name}", "GET", 2)


def test_record_view_records_nothing_when_disabled(monkeypatch):
    monkeypatch.setenv("PAGE_VIEW_TRACKING_ENABLED", "0")
    monkeypatch.setattr(page_views, "_counter", page_views.PageViewCounter())
    client = TestClient(_record_view_app())

    client.get("/staffing/people/Juan")
    assert page_views._counter.drain() == []


# --- DB-gated: real upsert + aggregation. Skipped without DATABASE_URL. -------
import os as _os

import pytest

pg = pytest.mark.skipif(
    not _os.environ.get("DATABASE_URL"), reason="needs Postgres"
)


@pg
def test_persist_upserts_and_usage_report_aggregates():
    from zira_dashboard import db
    db.init_pool()
    db.bootstrap_schema()
    day = date(2026, 7, 1)
    r = "/zz-test-usage/{id}"  # distinctive so it can't collide with real data
    try:
        page_views._persist([
            (day, r, "GET", "a@x.com", 2),
            (day, r, "GET", "b@x.com", 1),
        ])
        # second flush for the same key adds to the existing total
        page_views._persist([(day, r, "GET", "a@x.com", 3)])

        rows = db.query(
            "SELECT route, SUM(views) v, COUNT(DISTINCT user_email) u "
            "FROM page_views WHERE route=%s GROUP BY route", (r,),
        )
        assert rows[0]["v"] == 6      # 2 + 1 + 3
        assert rows[0]["u"] == 2      # a@x.com, b@x.com

        report = page_views.usage_report(days=3650)
        mine = [x for x in report if x["route"] == r]
        assert mine and mine[0]["views"] == 6 and mine[0]["users"] == 2
    finally:
        db.execute("DELETE FROM page_views WHERE route=%s", (r,))
