"""New/Operator phone checks with intercepted local fixture traffic only."""

from datetime import date
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright
import pytest
from tests.test_mobile_recycling_browser import fixture as recycling_fixture

ASSETS = Path(__file__).resolve().parents[1] / "src/zira_dashboard"
BASELINE = Path("/tmp/mobile-all-baseline")
EVIDENCE = Path("/tmp/mobile-production-evidence")


def fixture(kind, tv=False, **extra):
    c = recycling_fixture(tv)
    c.update(
        new_bars=c["dismantler_bars"],
        new_progress=c["dismantler_progress"],
        new_people=5,
        new_group_target=48,
        configured_new_meter_count=5,
        slug="dismantler-1",
        wc_name="Dismantler 1",
        wc_group="Dismantlers",
        operators_display="Alexandria Verylongfamilyname Hernandez + Morgan Secondworker",
        current_operator_rows=c["dismantler_bars"][1]["current_operators"],
        is_live_operator_display=True,
        no_activity=False,
        operator_day="2026-09-23",
        operator_day_label="Today",
        year=2026,
        month=9,
        month_name=lambda m: "September",
        wc_options=[
            dict(slug="dismantler-1", name="Dismantler 1"),
            dict(slug="dismantler-2", name="Dismantler 2"),
        ],
        pallets=dict(units_today=40, target_today=60, target_full_day=240),
        progress_buckets=c["dismantler_progress"],
        progress_bucket_target=12,
        kpi=dict(pallets_per_hour=23.1, up_time_pct=94.2),
        downtime_row=c["downtime_rows"][0],
        downtime_elapsed_minutes=360,
        goat_race=dict(
            goat_pace_today=50,
            units_today=40,
            goat=dict(units=500, name="Alexandria Verylongfamilyname Hernandez"),
        ),
        ribbons=dict(
            group="Dismantlers",
            entries=[
                dict(
                    position=1,
                    name="Alexandria Verylongfamilyname Hernandez",
                    day=date(2026, 9, 1),
                    units=500,
                )
            ],
        ),
        banner_now_pct=25,
        active_dashboard_key="vs_new" if kind == "new" else "wc:Dismantler 1",
    )
    if kind == "operator":
        for key in ("total_units", "new_bars", "new_progress", "new_people", "new_group_target"):
            c.pop(key)
    c.update(extra)
    return c


def render(kind, root=ASSETS, **extra):
    env = Environment(loader=FileSystemLoader(str(root / "templates")), autoescape=True)
    return env.get_template("new_dept.html" if kind == "new" else "wc_dashboard.html").render(
        **fixture(kind, **extra)
    )


def setup(page, kind, root=ASSETS, **extra):
    html = render(kind, root, **extra)
    calls = []

    def route(r):
        from urllib.parse import urlparse

        path = urlparse(r.request.url).path
        f = root / path.lstrip("/")
        if path.startswith("/static/") and f.is_file():
            r.fulfill(path=str(f))
        elif not path.startswith("/api/") and not path.startswith("/static/"):
            r.fulfill(body=html, content_type="text/html")
        else:
            calls.append((r.request.method, path))
            r.fulfill(body="{}", content_type="application/json")

    page.route("**/*", route)
    page.goto(
        "http://fixture.test/new"
        if kind == "new"
        else "http://fixture.test/wc/dismantler-1?day=2026-09-23",
        wait_until="networkidle",
    )
    return calls


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.mark.parametrize("kind", ["new", "operator"])
@pytest.mark.parametrize("width", [320, 390, 760])
def test_phone(browser, kind, width):
    page = browser.new_page(viewport=dict(width=width, height=844))
    try:
        calls = setup(page, kind)
        assert page.locator(".pm-page").is_visible()
        assert not page.evaluate("!!document.querySelector('.grid-stack').gridstack")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        row = page.locator(".pm-page .rm-production-row").first
        assert row.locator(".rm-now").inner_text() == "Now"
        track = row.locator(".rm-production-track").bounding_box()
        marker = row.locator(".rm-now-marker").bounding_box()
        assert (marker["x"] - track["x"]) / track["width"] == pytest.approx(5 / 6, abs=0.001)
        row.locator("summary").click()
        assert "40 pallets" in row.inner_text()
        page.locator(".rm-intervals summary").click()
        assert page.locator(".rm-intervals tbody tr").count() == 32
        if kind == "operator":
            assert page.locator('[gs-id="goat-race"]').is_visible()
            assert page.locator('[gs-id="monthly-ribbons"]').is_visible()
            assert page.locator('[gs-id="goat-race"]').bounding_box()["height"] > 100
            assert page.locator('[gs-id="monthly-ribbons"]').bounding_box()["height"] > 100
        EVIDENCE.mkdir(exist_ok=True)
        page.screenshot(path=str(EVIDENCE / f"{kind}-{width}.png"), full_page=True)
        assert not [c for c in calls if c[0] != "GET"]
    finally:
        page.close()


@pytest.mark.parametrize("kind", ["new", "operator"])
@pytest.mark.parametrize("width,tv", [(1440, False), (1920, True), (390, True)])
def test_baseline(browser, kind, width, tv):
    if not (BASELINE / "templates").exists():
        pytest.skip("Immutable local pre-change snapshot is not available")
    current = browser.new_page(viewport=dict(width=width, height=1080))
    old = browser.new_page(viewport=dict(width=width, height=1080))
    try:
        setup(current, kind, tv=tv)
        assert current.locator(".pm-page").count() == 0 or current.locator(".pm-page").is_hidden()
        assert current.evaluate("!!document.querySelector('.grid-stack').gridstack")
        setup(old, kind, root=BASELINE, tv=tv)
        current.evaluate("document.fonts.ready")
        old.evaluate("document.fonts.ready")
        EVIDENCE.mkdir(exist_ok=True)
        a = current.screenshot(path=str(EVIDENCE / f"{kind}-{width}-{tv}-current.png"))
        b = old.screenshot(path=str(EVIDENCE / f"{kind}-{width}-{tv}-baseline.png"))
        assert a == b
    finally:
        current.close()
        old.close()


def test_operator_picker_preserves_day(browser):
    page = browser.new_page(viewport=dict(width=320, height=844))
    try:
        setup(page, "operator")
        page.locator("#wc-picker").select_option("dismantler-2")
        page.wait_for_url("**/wc/dismantler-2?day=2026-09-23")
    finally:
        page.close()


def test_goal_edges_and_history(browser):
    page = browser.new_page(viewport=dict(width=320, height=844))
    try:
        setup(page, "new", is_today=False, is_range=True)
        rows = page.locator(".pm-page .rm-production-row")
        assert ["below", "met", "met", "below", "no-goal"] == [
            r.get_attribute("class").split()[-1] for r in rows.all()
        ]
        assert all(g.inner_text() == "Goal" for g in page.locator(".rm-now").all())
        assert "Alexandria" not in rows.first.locator(".rm-identity").inner_text()
        assert rows.nth(2).locator(".rm-production-fill").evaluate("e=>e.style.width") == "100%"
        assert rows.nth(4).locator(".rm-now").count() == 0
    finally:
        page.close()


@pytest.mark.parametrize("kind", ["new", "operator"])
def test_breakpoint_change_never_saves(browser, kind):
    page = browser.new_page(viewport=dict(width=390, height=844))
    try:
        calls = setup(page, kind)
        page.set_viewport_size(dict(width=1440, height=1000))
        page.wait_for_timeout(300)
        assert page.locator(".pm-page").is_hidden()
        page.set_viewport_size(dict(width=390, height=844))
        page.wait_for_timeout(300)
        assert page.locator(".pm-page").is_visible()
        assert not [c for c in calls if c[0] != "GET"]
    finally:
        page.close()
