"""Department leaderboard phone layout using fixture HTML, never application data."""

from datetime import date
from pathlib import Path
from io import BytesIO

from PIL import Image, ImageChops

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright
import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src/zira_dashboard"
BASELINE = Path("/tmp/mobile-all-baseline")


def context(department, tv=False, state=None):
    rows = [
        dict(
            rank=i + 1,
            name=name,
            ytd=dict(eligible=True, avg_units=1234.5 - i, days=111),
            l30=dict(eligible=i != 1, avg_units=1300.2, days=19),
        )
        for i, name in enumerate(["Alexandria Verylongfamilyname Hernandez", "Bo Worker"])
    ]
    block = dict(rows=[] if state == "empty" else rows, thresholds=dict(ytd=13, l30=2))
    winner = dict(
        name="Alexandria Verylongfamilyname Hernandez", day=date(2026, 9, 18), amount=1640
    )
    families = ["Viking Champion", "Hand Build"]
    data = dict(
        ytd_start=date(2026, 1, 1),
        ytd_end=date(2026, 9, 23),
        l30_start=date(2026, 8, 25),
        l30_end=date(2026, 9, 23),
        current_goats=[],
        roles=dict(Repair=block, Dismantler=block),
        families={f: block for f in families},
        active_families=[] if state == "empty" else families,
        error_message="Production unavailable." if state == "error" else None,
        ribbons=[
            dict(
                month=9,
                month_label="September",
                repair=winner,
                dismantler=None,
                winners={families[0]: winner, families[1]: None},
            ),
            dict(
                month=8,
                month_label="August",
                repair=None,
                dismantler=winner,
                winners={families[0]: None, families[1]: winner},
            ),
        ],
    )
    return dict(
        data=data,
        tv_mode=tv,
        tv_theme="dark",
        static_v=lambda _: "test",
        can_admin=lambda: True,
        can_operate=lambda: True,
        can_hr=lambda: True,
        can_feedback=lambda: False,
        goat_holders=lambda: {},
        nav_inbox_summary=lambda: dict(total=0, urgent_total=0, source_errors=[]),
        active_dashboard_key=f"vs_{department}_leaderboard",
    )


def render(department, tv=False, root=ASSETS, state=None):
    env = Environment(loader=FileSystemLoader(str(root / "templates")), autoescape=True)
    return env.get_template(f"{department}_leaderboard_tv.html").render(
        **context(department, tv, state)
    )


def setup(page, department, tv=False, root=ASSETS, state=None):
    html = render(department, tv, root, state)
    calls = []

    def route(r):
        path = r.request.url.split("http://fixture.test")[-1].split("?")[0]
        f = root / path.lstrip("/")
        if path.startswith("/static/") and f.is_file():
            r.fulfill(path=str(f))
        elif path == "/fixture":
            r.fulfill(body=html, content_type="text/html")
        else:
            calls.append((r.request.method, path))
            r.fulfill(body="{}", content_type="application/json")

    page.route("**/*", route)
    page.goto("http://fixture.test/fixture", wait_until="networkidle")
    return calls


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.mark.parametrize("department", ["recycling", "new"])
@pytest.mark.parametrize("width", [320, 390, 760])
def test_phone_names_scores_dates_and_awards(browser, department, width):
    page = browser.new_page(viewport=dict(width=width, height=844))
    try:
        calls = setup(page, department)
        assert page.locator(".lm-page").is_visible()
        assert page.locator(".rlb-table").first.is_hidden()
        row = page.locator(".lm-person").first
        name = row.locator(".lm-name").bounding_box()
        ytd = row.locator(".ytd").bounding_box()
        l30 = row.locator(".l30").bounding_box()
        assert name["y"] + name["height"] <= ytd["y"]
        assert ytd["y"] == l30["y"] and ytd["x"] < l30["x"]
        assert row.locator(".lm-name").inner_text() == "Alexandria Verylongfamilyname Hernandez"
        assert row.locator(".ytd small").inner_text() == "111 days"
        assert "Not enough days" in page.locator(".lm-person").nth(1).inner_text()
        page.locator(".lm-range summary").click()
        assert "Jan 1, 2026" in page.locator(".lm-range").inner_text()
        page.locator(".lm-rules summary").first.focus()
        page.keyboard.press("Enter")
        assert "At least 13 days" in page.locator(".lm-rules").first.inner_text()
        assert page.locator(".lm-rules").first.get_attribute("open") is not None
        assert page.locator(".lm-rules summary").first.bounding_box()["height"] >= 44
        assert "Sep 18, 2026" in page.locator(".lm-month").first.inner_text()
        assert "No qualifying day" in page.locator(".lm-month").first.inner_text()
        months = [e.bounding_box() for e in page.locator(".lm-month").all()]
        assert months[0]["y"] + months[0]["height"] < months[1]["y"]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not [c for c in calls if c[0] != "GET"]
    finally:
        page.close()


@pytest.mark.parametrize(
    "department,state", [("recycling", "empty"), ("new", "empty"), ("new", "error")]
)
def test_empty_and_error(browser, department, state):
    page = browser.new_page(viewport=dict(width=320, height=844))
    try:
        setup(page, department, state=state)
        expected = (
            "No days yet."
            if department == "recycling"
            else (
                "Production unavailable."
                if state == "error"
                else "Waiting for qualifying Zira production."
            )
        )
        assert expected in page.locator(".lm-page").inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        page.close()


@pytest.mark.parametrize("department", ["recycling", "new"])
@pytest.mark.parametrize("width,tv", [(1440, False), (1920, True), (390, True)])
def test_desktop_tv_match_immutable_baseline(browser, department, width, tv):
    if not BASELINE.is_dir():
        pytest.skip("Local immutable pre-change assets required for pixel comparison")
    shots = []
    for root in (BASELINE, ASSETS):
        page = browser.new_page(viewport=dict(width=width, height=1080), device_scale_factor=1)
        try:
            setup(page, department, tv=tv, root=root)
            page.evaluate("document.fonts.ready")
            assert page.locator(".rlb-table").first.is_visible()
            assert page.locator(".lm-page").count() == 0 or page.locator(".lm-page").is_hidden()
            shots.append(page.screenshot(full_page=True, animations="disabled"))
        finally:
            page.close()
    assert (
        ImageChops.difference(
            Image.open(BytesIO(shots[0])).convert("RGB"),
            Image.open(BytesIO(shots[1])).convert("RGB"),
        ).getbbox()
        is None
    )


@pytest.mark.parametrize("department", ["recycling", "new"])
def test_phone_navigation_preserves_links_and_toggles(browser, department):
    page = browser.new_page(viewport=dict(width=320, height=844))
    try:
        setup(page, department)
        button = page.locator(".pm-menu-button")
        nav = page.locator("#pm-app-navigation")
        links = nav.locator("a").evaluate_all(
            "els => els.map(e => [e.textContent.trim(), e.getAttribute('href')])"
        )
        assert nav.is_hidden()
        button.click()
        assert nav.is_visible() and button.get_attribute("aria-expanded") == "true"
        assert (
            nav.locator("a").evaluate_all(
                "els => els.map(e => [e.textContent.trim(), e.getAttribute('href')])"
            )
            == links
        )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        button.click()
        assert nav.is_hidden() and button.get_attribute("aria-expanded") == "false"
        expected = page.locator(".dash-subnav a").evaluate_all(
            "els => els.map(e => [e.textContent.trim(), e.getAttribute('href')])"
        )
        actual = page.locator("#pm-dashboard option").evaluate_all(
            "els => els.map(e => [e.textContent.trim(), e.value])"
        )
        assert actual == expected
        assert page.locator("#pm-dashboard").input_value() == f"/{department}-leaderboard"
        assert button.bounding_box()["height"] >= 44
        with page.expect_navigation():
            page.locator("#pm-dashboard").select_option("/new")
        assert page.url.endswith("/new")
    finally:
        page.close()
