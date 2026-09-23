"""Phone rendering of real templates with local, non-production fixtures."""

from pathlib import Path
from io import BytesIO

import pytest
from PIL import Image, ImageChops
from markupsafe import Markup
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/zira_dashboard"
BASE = """<!doctype html><html data-performance-mobile><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{--bg:#f5f7fa;--panel:#fff;--panel-2:#eef2f6;--border:#ccd3dc;--fg:#182333;--muted:#586576;--accent:#16834a;--accent-dim:#e2f5e9;--bad:#bd3333}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px Arial}main{padding:1rem 1.5rem}h2{font-size:1rem} {% block styles %}{% endblock %}</style>{% block extra_head %}{% endblock %}</head><body><main{% block main_attrs %}{% endblock %}>{% block content %}{% endblock %}</main>{% block scripts %}{% endblock %}</body></html>"""
NAME = "Alexandria Montgomery Rivera Longname"


def render(view, source=SOURCE):
    env = Environment(
        loader=ChoiceLoader(
            [DictLoader({"_staffing_base.html": BASE}), FileSystemLoader(source / "templates")]
        ),
        autoescape=True,
    )
    env.globals.update(
        static_v=lambda _: "fixture",
        static_text=lambda p: Markup((source / "static" / p).read_text()),
        goat_holders=lambda: {},
        month_name=lambda _: "September",
    )
    row = dict(
        rank=1,
        name=NAME,
        display_name=NAME,
        name_count=12,
        day_label="September 18, 2026",
        day="2026-09-18",
        date="2026-09-18",
        wc="Junior #2",
        top_wc="Junior #2",
        units=1120,
        pct=1.12,
        avg_units=842,
        avg_pct=1.04,
        score=94,
        days=12,
        calls=128,
        on_time=121,
        late=7,
        ontime_pct=94.5,
        avg_ms=43000,
        position=1,
        pph=120,
    )
    section = dict(
        loc_name="Recycling production group",
        rows=[dict(row, rank=i + 1) for i in range(7)],
        is_manually_inactive=True,
    )
    context = dict(
        tv_mode=False,
        can_operate=lambda: True,
        can_admin=lambda: True,
        person_certs={},
        window="month",
        metric="units",
        custom_range_active=False,
        start="2026-09-01",
        end="2026-09-23",
        active_groups=[section],
        active_sections=[],
        inactive_groups=[],
        inactive_sections=[],
        active_avg_groups=[section],
        active_avg_sections=[],
        inactive_avg_groups=[],
        inactive_avg_sections=[],
        lb=dict(overall=[row], most_calls=[row], on_time=[row], fastest=[row]),
        min_calls=10,
        goats=[dict(group="Recycling production group", winner=row)],
        annual=[dict(group="Recycling", top_days=[row], best_avg=row, wc_best=[])],
        monthly=[dict(group="Recycling", badges=[row])],
        forklift=dict(most_calls=row, best_ontime=row, fastest=row, badges=[row]),
        year=2026,
        month=9,
        today="2026-09-23",
    )
    if view == "people_performance":
        from scripts.preview_people_performance import _context

        context.update(_context())
        context["poll_disabled"] = True
    html = env.get_template(view + ".html").render(**context)
    return html


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


def open_fixture(browser, view, width, source=SOURCE, tv=False):
    page = browser.new_page(
        viewport={"width": width, "height": 900}, has_touch=width <= 760, is_mobile=width <= 760
    )
    page.route("**/*", lambda route: route.abort())
    page.route(
        "http://fixture/static/**",
        lambda route: route.fulfill(
            body=(
                source / "static" / route.request.url.split("/static/")[1].split("?")[0]
            ).read_bytes(),
            content_type="text/css" if ".css" in route.request.url else "application/javascript",
        ),
    )
    page.route(
        "http://fixture/",
        lambda route: route.fulfill(
            body=render(view, source).replace(
                "<html data-performance-mobile>",
                "<html data-performance-mobile data-tv-theme=light>"
                if tv
                else "<html data-performance-mobile>",
            ),
            content_type="text/html",
        ),
    )
    page.goto("http://fixture/")
    return page


@pytest.mark.parametrize(
    "view", ["people_performance", "leaderboards", "forklift_leaderboards", "trophy_case"]
)
@pytest.mark.parametrize("width", [320, 390, 760])
def test_phone_content_fits_and_preserves_actions(browser, view, width):
    page = open_fixture(browser, view, width)
    try:
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), view
        if view == "people_performance":
            name = page.locator(".pp-identity").first.bounding_box()
            timeline = page.locator(".pp-timeline-cell").first.bounding_box()
            assert timeline["y"] >= name["y"] + name["height"]
            assert timeline["width"] > width * 0.70
            page.locator(".pp-interval-trigger").first.tap()
            assert page.locator(".pp-detail-popover").is_visible()
        elif view == "trophy_case":
            page.locator(".tc-edit").first.click()
            dialog = page.locator(".tc-modal").bounding_box()
            assert dialog["x"] >= 0 and dialog["x"] + dialog["width"] <= width
            assert page.locator("#tc-save").bounding_box()["height"] >= 44
            page.locator("#tc-cancel").click()
            assert not page.locator(".tc-modal").is_visible()
        else:
            row = page.locator(".lb-table tbody tr").first
            assert row.locator(".op").inner_text().startswith(NAME)
            assert row.locator("td.num").first.evaluate(
                '(e)=>getComputedStyle(e,"::before").content'
            ) not in ("none", "normal", '""')
            page.locator(".lb-custom-popover summary").click()
            assert page.locator("input[name=start]").is_visible()
        Path("/tmp/mobile-people-rankings").mkdir(exist_ok=True)
        page.screenshot(path=f"/tmp/mobile-people-rankings/{view}-{width}.png", full_page=True)
    finally:
        page.close()


@pytest.mark.parametrize(
    "view", ["people_performance", "leaderboards", "forklift_leaderboards", "trophy_case"]
)
@pytest.mark.parametrize("width,tv", [(1440, False), (390, True)])
def test_desktop_and_tv_pixels_match_prechange(browser, view, width, tv):
    baseline = Path("/tmp/mobile-all-baseline")
    if not baseline.exists():
        pytest.skip("Local immutable baseline capture is not available")
    current = open_fixture(browser, view, width, tv=tv)
    original = open_fixture(browser, view, width, baseline, tv=tv)
    try:
        for page in (current, original):
            if tv:
                page.evaluate('document.documentElement.setAttribute("data-tv-theme","light")')
            page.mouse.move(0, 0)
        old = original.screenshot(full_page=True)
        new = current.screenshot(full_page=True)
        Path("/tmp/mobile-people-rankings").mkdir(exist_ok=True)
        Path(f"/tmp/mobile-people-rankings/{view}-{width}-baseline.png").write_bytes(old)
        Path(f"/tmp/mobile-people-rankings/{view}-{width}-current.png").write_bytes(new)
        before = Image.open(BytesIO(old)).convert("RGB")
        after = Image.open(BytesIO(new)).convert("RGB")
        assert before.size == after.size
        # Chromium differed at seven sticky-strip rounded-border pixels by one
        # RGB level in the full suite (backdrop-filter compositor rounding).
        # Keep every pixel in the comparison; permit only that raster rounding.
        difference = ImageChops.difference(before, after)
        assert max(channel[1] for channel in difference.getextrema()) <= 1
    finally:
        current.close()
        original.close()


def test_leaderboard_expand_drilldown_and_metric_links(browser):
    page = open_fixture(browser, "leaderboards", 320)
    try:
        page.route(
            "**/api/staffing/leaderboards/person-days?*",
            lambda route: route.fulfill(
                json={
                    "rows": [
                        {"date": "2026-09-18", "wcs": ["Junior #2"], "units": 1120, "downtime": 12}
                    ]
                }
            ),
        )
        page.locator(".lb-expand-btn").click()
        assert page.locator(".lb-pane-avg tbody tr:visible").count() == 7
        page.locator(".lb-name-btn").first.click()
        page.locator("#lb-popup-table tbody").get_by_text("1,120").wait_for()
        popup = page.locator(".popover").bounding_box()
        assert popup["x"] >= 0 and popup["x"] + popup["width"] <= 320
        assert "start=2026-09-01" in page.locator("#lb-popup-card-link").get_attribute("href")
        page.get_by_role("button", name="Close leaderboard detail popup").click()
        assert not page.locator(".popover").is_visible()
        assert "metric=pct" in page.get_by_role("link", name="% of goal", exact=True).get_attribute(
            "href"
        )
    finally:
        page.close()


def test_people_filter_replacement_and_date_navigation(browser):
    from scripts.preview_people_performance import _filter_context

    page = open_fixture(browser, "people_performance", 390)
    try:
        env = Environment(loader=FileSystemLoader(SOURCE / "templates"), autoescape=True)
        filtered = env.get_template("_people_performance_rows.html").render(
            **{**_filter_context("earlier"), "is_today": True}
        )
        page.route(
            "**/people-performance/rows?*",
            lambda route: route.fulfill(
                body=filtered,
                content_type="text/html",
                headers={"X-People-Performance-Response": "rows"},
            ),
        )
        page.evaluate(
            "document.querySelector('.pp-page').dataset.today='1'; document.querySelector('.pp-page').dataset.pollDisabled='0'"
        )
        assert page.evaluate("peoplePerformanceController.refreshRows()")
        page.wait_for_function(
            'document.querySelector("#people-performance-live").dataset.status === "earlier"'
        )
        assert page.locator("[data-pp-control-key=earlier]").get_attribute("aria-pressed") == "true"
        timeline = page.locator(".pp-timeline-cell").first.bounding_box()
        identity = page.locator(".pp-identity").first.bounding_box()
        assert timeline["y"] >= identity["y"] + identity["height"]
        page.route(
            "**/people-performance?*",
            lambda route: route.fulfill(
                body=render("people_performance"), content_type="text/html"
            ),
        )
        page.locator("[data-pp-control-key=working]").click()
        page.wait_for_url("**/people-performance?**status=working**")
        page.locator("input[name=day]").fill("2026-08-27")
        page.wait_for_url("**/people-performance?**day=2026-08-27**")
    finally:
        page.close()


def test_trophy_override_request_and_pickers(browser):
    page = open_fixture(browser, "trophy_case", 320)
    try:
        assert page.locator("#year-picker").bounding_box()["height"] >= 44
        assert page.locator("#month-picker").bounding_box()["height"] >= 44
        sent = []
        page.route(
            "**/api/awards/override",
            lambda route: (
                sent.append(route.request.post_data_json),
                route.fulfill(status=200, json={}),
            ),
        )
        page.locator(".tc-edit").first.click()
        page.locator("#tc-name").fill("Local fixture winner")
        page.locator("#tc-note").fill("Phone test only")
        page.locator("#tc-save").click()
        page.wait_for_load_state()
        assert sent[0]["name"] == "Local fixture winner"
        assert sent[0]["scope"] == "award_goat"
    finally:
        page.close()


def test_people_warning_panel_stays_in_phone_bounds(browser):
    page = open_fixture(browser, "people_performance", 320)
    try:
        page.route(
            "**/people-performance/warnings/**",
            lambda route: route.fulfill(
                content_type="text/html",
                headers={"X-People-Performance-Response": "warning-detail"},
                body='<div id="pp-warning-panel-content" data-warning-state="active"><header><h2>Assignment warning</h2></header><p>Check the current location for this person.</p><footer><a href="/staffing/assignments">Review assignments</a></footer></div>',
            ),
        )
        page.locator(".pp-warning-trigger").first.tap()
        page.get_by_role("heading", name="Assignment warning").wait_for()
        box = page.locator("#pp-warning-popover").bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 320
        assert page.get_by_role("link", name="Review assignments").bounding_box()["height"] >= 44
    finally:
        page.close()
