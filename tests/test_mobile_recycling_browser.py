"""Real-browser Recycling checks using local templates and intercepted fixture requests."""

from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright
import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src/zira_dashboard"


def fixture(tv=False, empty=False):
    bars = []
    for i, (units, goal) in enumerate([(40, 60), (60, 60), (120, 60), (0, 60), (33, 0)]):
        name = f"Dismantler {i + 1}"
        who = [
            "Alexandria Verylongfamilyname Hernandez",
            "Bo Worker",
            "Casey Worker",
            "Dana Worker",
            "Erin Worker",
        ][i]
        bars.append(
            dict(
                name=name,
                who=who,
                units=units,
                expected=goal,
                pct=units / 1.44,
                target_pct=60 / 1.44,
                pct_of_target=round(units / goal * 100) if goal else None,
                color="#16a34a",
                has_segments=False,
                segments=[],
                current_operators=[dict(person_name=who, physically_present=True)],
                sole_producer_name=None,
                no_one_here_now=False,
                has_worker_history=False,
                show_segment_worker_names=False,
            )
        )
    bars[1]["current_operators"].append(
        dict(person_name="Morgan Secondworker", physically_present=False)
    )
    progress = [
        dict(
            label=f"{7 + i // 4}:{i % 4 * 15:02}", actual=9 + i % 5, target=12, in_progress=i == 31
        )
        for i in range(32)
    ]
    down = [
        dict(
            name=b["name"],
            who=b["who"],
            current_operators=b["current_operators"],
            down=d,
            working=360 - d,
            down_pct=d / 3.6,
            working_pct=(360 - d) / 3.6,
        )
        for b, d in zip(bars, [20, 60, 5, 0, 120])
    ]
    return dict(
        static_v=lambda p: "test",
        can_admin=lambda: True,
        can_operate=lambda: True,
        can_hr=lambda: True,
        can_feedback=lambda: False,
        nav_inbox_summary=lambda: dict(total=0, urgent_total=0, source_errors=[]),
        goat_holders=lambda: {},
        tv_mode=tv,
        tv_theme="dark",
        window="today",
        custom_range_active=False,
        start="2026-09-23",
        end="2026-09-23",
        today="2026-09-23",
        layout={},
        customs={},
        total_units=253,
        pph_per_person=23.1,
        pph_per_person_ex_d4=20.2,
        uptime_pct=94.2,
        elapsed_minutes=0 if empty else 360,
        dismantler_bars=[] if empty else bars,
        repair_bars=[] if empty else bars[:2],
        downtime_rows=[] if empty else down,
        dismantler_people=5,
        repair_people=2,
        is_range=False,
        is_today=True,
        dismantler_progress=[] if empty else progress,
        repair_progress=[] if empty else progress,
        dismantler_group_target=48,
        repair_group_target=24,
        range_includes_today=False,
        refreshed_at="11:30:00 AM",
        all_active_people=[],
        goat_alerts_active=[],
        goat_contenders=[],
        ribbon_announce=None,
        operator_links_by_wc={b["name"]: "/wc/dismantler-1" for b in bars},
        assignments_todo_by_wc={},
        active_dashboard_key="vs_recycling",
        shift_start_label="7:00 AM",
        now_label="11:30 AM",
    )


def render(root, tv=False, empty=False, **extra):
    env = Environment(loader=FileSystemLoader(str(root / "templates")), autoescape=True)
    c = fixture(tv, empty)
    c.update(extra)
    return env.get_template("recycling.html").render(**c)


def setup(page, root, html):
    calls = []

    def route(r):
        path = r.request.url.split("http://fixture.test")[-1].split("?")[0]
        if path.startswith("/static/"):
            f = root / path.lstrip("/")
            if f.exists():
                r.fulfill(path=str(f))
                return
        if path == "/recycling" or path == "/tv/recycling":
            r.fulfill(body=html, content_type="text/html")
            return
        calls.append((r.request.method, path))
        r.fulfill(body="{}", content_type="application/json")

    page.route("**/*", route)
    page.goto("http://fixture.test/recycling", wait_until="networkidle")
    return calls


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.mark.parametrize("width", [320, 390, 760])
def test_phone_has_no_overflow_or_desktop_layout(browser, width):
    page = browser.new_page(viewport={"width": width, "height": 844})
    try:
        calls = setup(page, ASSETS, render(ASSETS))
        assert page.locator(".rm-page").is_visible()
        assert page.locator(".grid-stack").is_hidden()
        assert not page.evaluate("!!document.querySelector('.grid-stack').gridstack")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not [c for c in calls if c[0] != "GET"]
    finally:
        page.close()


@pytest.mark.parametrize("width,tv", [(1440, False), (1920, True), (390, True)])
def test_desktop_and_tv_keep_grid(browser, width, tv):
    page = browser.new_page(viewport={"width": width, "height": 1080})
    try:
        setup(page, ASSETS, render(ASSETS, tv))
        assert page.locator(".grid-stack").is_visible()
        assert page.locator(".rm-page").count() == 0 or page.locator(".rm-page").is_hidden()
        assert page.evaluate("!!document.querySelector('.grid-stack').gridstack")
    finally:
        page.close()


def test_resizing_does_not_save_desktop_layout(browser):
    page = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        calls = setup(page, ASSETS, render(ASSETS))
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.wait_for_timeout(500)
        assert page.locator(".grid-stack").is_visible()
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(500)
        assert page.locator(".rm-page").is_visible()
        assert not [c for c in calls if c[0] != "GET"]
    finally:
        page.close()


def test_phone_production_and_downtime_details(browser):
    page = browser.new_page(viewport={"width": 320, "height": 844})
    try:
        setup(page, ASSETS, render(ASSETS))
        rows = page.locator(".rm-production-row")
        assert "Alexandria Verylongfamilyname Hernandez" in rows.nth(0).inner_text()
        assert "Bo Worker + Morgan Secondworker" in " ".join(rows.nth(1).inner_text().split())
        for row in rows.all():
            assert row.bounding_box()["width"] <= 320
        goal = rows.nth(0).locator(".rm-now")
        assert goal.inner_text() == "Now"
        marker = rows.nth(0).locator(".rm-now-marker").bounding_box()
        track = rows.nth(0).locator(".rm-production-track").bounding_box()
        assert (marker["x"] - track["x"]) / track["width"] == pytest.approx(5 / 6, abs=0.001)
        assert rows.nth(0).locator(".rm-production-fill").evaluate(
            "e => getComputedStyle(e).backgroundColor"
        ) != rows.nth(1).locator(".rm-production-fill").evaluate(
            "e => getComputedStyle(e).backgroundColor"
        )
        fill = rows.nth(2).locator(".rm-production-fill")
        assert fill.evaluate("e => e.style.width") == "100%"
        assert "met" in rows.nth(2).get_attribute("class")
        assert "below" in rows.nth(0).get_attribute("class")
        assert "no-goal" in rows.nth(4).get_attribute("class")
        assert rows.nth(4).locator(".rm-now").count() == 0
        details = rows.nth(2).locator("details")
        details.locator("summary").click()
        assert "120" in details.inner_text()
        assert "60" in details.inner_text()
        operator_link = details.locator(".rm-operator-link")
        assert operator_link.get_attribute("href") == "/wc/dismantler-1"
        assert operator_link.bounding_box()["height"] >= 44
        assert rows.nth(2).locator(".rm-identity a").count() == 0
        assert details.bounding_box()["width"] <= 320
        downtime = page.locator(".rm-downtime-row")
        assert [r.locator(".rm-downtime-minutes").inner_text() for r in downtime.all()] == [
            "120 min",
            "60 min",
            "20 min",
            "5 min",
            "0 min",
        ]
        boxes = [r.bounding_box() for r in downtime.all()]
        assert all(box["height"] >= 64 for box in boxes)
        assert all(abs(a["y"] + a["height"] - b["y"]) <= 1 for a, b in zip(boxes, boxes[1:]))
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        page.close()


@pytest.mark.parametrize("empty,historical", [(True, False), (False, True)])
def test_empty_and_historical_phone_views(browser, empty, historical):
    page = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        setup(
            page, ASSETS, render(ASSETS, empty=empty, is_today=not historical, is_range=historical)
        )
        if empty:
            assert "No production data" in page.locator(".rm-page").inner_text()
        else:
            assert all(g.inner_text() == "Goal" for g in page.locator(".rm-now").all())
            assert "Alexandria" not in page.locator(".rm-identity").first.inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        page.close()


def test_mobile_navigation_dates_and_interval_controls(browser):
    page = browser.new_page(viewport={"width": 320, "height": 844})
    try:
        setup(page, ASSETS, render(ASSETS))
        menu = page.locator(".rm-menu-button")
        menu.click()
        assert menu.get_attribute("aria-expanded") == "true"
        assert page.locator("#rm-app-navigation").is_visible()
        menu.click()
        assert page.locator("#rm-app-navigation").is_hidden()
        page.locator(".rm-controls details > summary").click()
        for field in page.locator(".rm-controls input[type=date]").all():
            field.fill("2026-09-21")
            box = field.bounding_box()
            assert box["height"] >= 44
            assert box["x"] >= 0 and box["x"] + box["width"] <= 320
        for control in page.locator(
            ".rm-controls select,.rm-controls button,.rm-menu-button,.rm-production-detail > summary"
        ).all():
            assert control.bounding_box()["height"] >= 44
        page.locator(".rm-intervals > summary").first.click()
        assert page.locator(".rm-intervals table").first.is_visible()
        assert page.locator(".rm-intervals tbody").first.locator("tr").count() == 32
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        with page.expect_navigation():
            page.locator("#rm-range").select_option("yesterday")
        assert page.url.endswith("/recycling?window=yesterday")
        with page.expect_navigation():
            page.locator("#rm-dashboard").select_option("/new")
        assert page.url.endswith("/new")
    finally:
        page.close()


def test_unchanged_media_query_does_not_reload_desktop(browser):
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    try:
        page.add_init_script("""
            const original = window.matchMedia;
            window.queries = [];
            window.matchMedia = function(query) {
                const value = original.call(window, query);
                window.queries.push(value);
                return value;
            };
        """)
        setup(page, ASSETS, render(ASSETS))
        page.evaluate("""() => {
            window.stablePageMarker = true;
            const query = window.queries.find(q => q.media === '(max-width: 760px)');
            query.dispatchEvent(new MediaQueryListEvent('change', {matches: false, media: query.media}));
        }""")
        page.wait_for_timeout(200)
        assert page.evaluate("window.stablePageMarker === true")
    finally:
        page.close()


def test_phone_assignment_picker_fits_long_roster_without_writes(browser):
    page = browser.new_page(viewport={"width": 320, "height": 568})
    try:
        html = render(
            ASSETS,
            assignments_todo_by_wc={
                "Dismantler 1": dict(
                    first_iso="2026-09-23T07:00:00Z", last_iso="2026-09-23T08:00:00Z"
                )
            },
            all_active_people=[
                "Alexandria Extremelylongfamilyname Hernandez " + str(i) for i in range(40)
            ],
        )
        calls = setup(page, ASSETS, html)
        row = page.locator(".rm-production-row").first
        row.locator("summary").click()
        row.locator(".no-assign-btn").click()
        picker = page.locator(".assign-popover")
        assert picker.is_visible()
        box = picker.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 320
        assert box["y"] >= 0 and box["y"] + box["height"] <= 568
        for control in picker.locator("select,button").all():
            assert control.bounding_box()["height"] >= 44
        assert picker.locator("option").count() == 41
        page.keyboard.press("Escape")
        assert picker.count() == 0
        assert not [c for c in calls if c[0] != "GET"]
    finally:
        page.close()
