from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "scripts/_preview_out/people_performance"
VIEWPORTS = ((1440, 900), (1195, 768), (1024, 768), (768, 1024), (390, 844))


def _render_preview() -> subprocess.CompletedProcess[str]:
    env = os.environ | {
        "AUTH_DISABLED": "1",
        "SESSION_SECRET": "preview-secret-32-bytes-of-data",
        "ZIRA_API_KEY": "test",
        "PYTHONPATH": str(ROOT / "src"),
    }
    return subprocess.run(
        [sys.executable, "scripts/preview_people_performance.py"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _install_console_capture(page: Page) -> list[str]:
    errors: list[str] = []
    page.on(
        "console",
        lambda message: errors.append(message.text) if message.type == "error" else None,
    )
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(
        """
        window.__peoplePreviewIntervals = [];
        window.__peoplePreviewPointerTypes = [];
        window.addEventListener('pointerup', function (event) {
          window.__peoplePreviewPointerTypes.push(event.pointerType);
        });
        const nativeSetInterval = window.setInterval.bind(window);
        window.setInterval = function (callback, milliseconds) {
          window.__peoplePreviewIntervals.push(milliseconds);
          return nativeSetInterval(callback, milliseconds);
        };
        window.gpiFetch = async function (url) {
          if (!url.includes('/people-performance/warnings/')) throw new Error('unexpected preview request');
          return new Response(
            '<section id="pp-warning-panel-content" data-warning-state="open" data-warning-key="111111111111111111111111" aria-labelledby="pp-warning-title">' +
            '<header><h2 id="pp-warning-title">Production Meters Unavailable</h2><button type="button" data-pp-warning-close aria-label="Close warning details">×</button></header>' +
            '<p>2 production meters are unavailable.</p><p class="pp-warning-impact"><strong>People page impact:</strong> Production details are hidden for the listed work centers.</p>' +
            '<div class="pp-warning-members"><section class="pp-warning-member" aria-labelledby="pp-warning-member-1"><h3 id="pp-warning-member-1">Hand Build #1</h3><p>Plant Manager could not safely calculate this production result.</p></section>' +
            '<section class="pp-warning-member" aria-labelledby="pp-warning-member-2"><h3 id="pp-warning-member-2">Trim Saw 1</h3><p>Plant Manager could not safely calculate this production result.</p>' +
            '<footer><button type="button" data-pp-warning-action="check_again">Check again</button><a href="/wc/trim-saw-1?day=2026-09-02">Open work center dashboard</a></footer></section></div></section>',
            {status: 200, headers: {'X-People-Performance-Response': 'warning-detail'}}
          );
        };
        """
    )
    return errors


def _install_preview_route(page: Page) -> str:
    def serve(route):
        parsed = urlparse(route.request.url)
        request_path = parsed.path
        if request_path == "/people-performance":
            query = parse_qs(parsed.query)
            state = (
                query.get("status", [""])[0],
                query.get("attention", ["0"])[0],
            )
            filename = {
                ("", "0"): "all.html",
                ("earlier", "0"): "earlier.html",
                ("working", "1"): "working-attention.html",
            }.get(state)
            target = OUT / filename if filename else OUT / "missing-preview-state"
        else:
            target = OUT / (
                "index.html"
                if request_path in ("/", "/index.html")
                else request_path.removeprefix("/")
            )
        if target.is_file():
            route.fulfill(path=str(target))
        else:
            route.abort()

    page.route("http://people-preview.test/**", serve)
    return "http://people-preview.test/index.html"


def test_preview_contains_busy_people_fixture():
    from scripts.preview_people_performance import _context

    assert _context()["source_warnings"] == (
        {
            "key": "111111111111111111111111",
            "kind": "production_metric_unavailable",
            "label": "Production Meters Unavailable",
            "summary": "2 production meters are unavailable.",
            "count": 2,
            "accessible_label": "Production Meters Unavailable: 2",
        },
        {
            "key": "333333333333333333333333",
            "kind": "unmatched_forklift_calls",
            "label": "Unmatched forklift calls: 107",
            "summary": "Forklift calls could not be matched to active employees.",
            "count": None,
            "accessible_label": "Unmatched forklift calls: 107",
        },
    )
    result = _render_preview()

    assert result.stdout.strip() == str(OUT)
    html = (OUT / "index.html").read_text(encoding="utf-8")
    assert html.count('class="pp-row') >= 10
    assert 'class="pp-manager-strip"' in html
    assert 'class="pp-axis"' not in html
    assert html.count('class="pp-section-header"') == 3
    assert html.count('class="pp-schedule-tick ') == 99
    assert "6:00 AM" in html
    assert "11:30" in html
    assert "2:00 PM" in html
    assert html.count('data-warning-kind="production_metric_unavailable"') == 1
    assert "Production Meters Unavailable" in html
    assert '<span class="pp-warning-count" aria-hidden="true">2</span>' in html
    assert "Unmatched forklift calls: 107" in html
    assert html.count('class="pp-source-warnings"') == 1
    assert ">Production<" in html
    assert ">126/168<" in html
    assert ">Centers<" not in html
    assert html.index("Metered production") < html.index("Tablet forklift")
    assert html.index("Tablet forklift") < html.index("Other non-metered people")
    assert html.index("Amy Behind") < html.index("Mia Mixed")
    assert html.index("Mia Mixed") < html.index("Zed Ahead")
    assert html.index("Zed Ahead") < html.index("Chris Complete")
    assert html.index("Dana Needs Attention") < html.index("Ben Busy Driver")
    assert html.index("Ben Busy Driver") < html.index("Eli Completed Driver")
    assert html.index("Olivia Missing") < html.index("Parker Stale")
    assert html.index("Parker Stale") < html.index("Noah Shipping")
    assert "<strong>8</strong> working now" in html
    assert "<strong>2</strong> worked earlier" in html
    assert 'data-status="working"' in html
    assert '<span class="sr-only">Selected filter.</span>' in html
    zero_count_path = OUT / "zero-count.html"
    assert zero_count_path.exists()
    zero_count_html = zero_count_path.read_text(encoding="utf-8")
    earlier_button = zero_count_html.split('data-filter-value="earlier"', 1)[1].split(
        "</button>", 1
    )[0]
    assert "<strong>0</strong> worked earlier" in earlier_button
    assert "disabled" in earlier_button
    assert 'aria-describedby="pp-earlier-empty"' in earlier_button
    assert html.count('style="left:68.75%;width:6.25%"') == 10
    for label in (
        "location missing",
        "source stale",
        "Late call",
        "Planned break",
        "Transferred to",
    ):
        assert label in html
    assert html.count(">Location unavailable<") >= 2
    assert ">No metered goal<" in html
    assert 'data-poll-disabled="1"' in html


def test_preview_metric_marks_do_not_cross_the_planned_break():
    from scripts.preview_people_performance import _context

    for section in _context()["sections"]:
        for row in section["rows"]:
            for interval in row["intervals"]:
                for run in interval["line_runs"]:
                    positions = tuple(
                        interval["left_pct"] + interval["width_pct"] * point["x"] / 100.0
                        for point in run
                    )
                    assert max(positions) <= 68.75 or min(positions) >= 75.0
                for bucket in interval["buckets"]:
                    bucket_start = (
                        interval["left_pct"] + interval["width_pct"] * bucket["left_pct"] / 100.0
                    )
                    bucket_end = bucket_start + (
                        interval["width_pct"] * bucket["width_pct"] / 100.0
                    )
                    assert bucket_end <= 68.75 or bucket_start >= 75.0
                    for marker in bucket["late_markers"]:
                        marker_position = (
                            interval["left_pct"] + interval["width_pct"] * marker / 100.0
                        )
                        assert marker_position <= 68.75 or marker_position >= 75.0


def test_preview_zero_count_disables_the_exact_count_control():
    _render_preview()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844}, has_touch=True)
            page.goto((OUT / "zero-count.html").as_uri(), wait_until="load")
            earlier = page.locator('[data-pp-control-key="earlier"]')

            assert earlier.is_disabled()
            assert earlier.locator("strong").text_content() == "0"
            assert earlier.get_attribute("aria-describedby") == "pp-earlier-empty"
            assert page.locator('[data-pp-control-key="working"]').is_enabled()
            assert page.locator('[data-pp-control-key="attention"]').is_enabled()
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("control_key", "expected_query", "expected_pressed", "expected_rows", "summary"),
    (
        ("working", {"day": ["2026-08-28"]}, [], 10, ""),
        (
            "earlier",
            {"day": ["2026-08-28"], "status": ["earlier"]},
            ["earlier"],
            2,
            "Showing 2 of 2 worked earlier.",
        ),
        (
            "attention",
            {"day": ["2026-08-28"], "status": ["working"], "attention": ["1"]},
            ["working", "attention"],
            4,
            "Showing 4 of 8 working now who need attention.",
        ),
    ),
)
def test_preview_count_filter_activation_preserves_combination_rules(
    control_key, expected_query, expected_pressed, expected_rows, summary
):
    _render_preview()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1024, "height": 768})
            errors = _install_console_capture(page)
            fixture_url = _install_preview_route(page)
            page.goto(fixture_url, wait_until="load")
            assert (
                page.locator("#people-performance-live").get_attribute("data-status") == "working"
            )
            assert page.locator("#people-performance-live").get_attribute("data-attention") == "0"

            page.locator(f'[data-pp-control-key="{control_key}"]').click()

            parsed = urlparse(page.url)
            assert parsed.path == "/people-performance"
            assert parse_qs(parsed.query) == expected_query
            live = page.locator("#people-performance-live")
            assert live.get_attribute("data-status") == expected_query.get("status", [""])[0]
            assert live.get_attribute("data-attention") == expected_query.get("attention", ["0"])[0]
            assert (
                page.locator('.pp-counts > button[aria-pressed="true"]').evaluate_all(
                    "buttons => buttons.map(button => button.dataset.ppControlKey)"
                )
                == expected_pressed
            )
            assert page.locator(".pp-row").count() == expected_rows
            summaries = page.locator(".pp-filter-summary")
            assert summaries.count() == bool(summary)
            if summary:
                assert summaries.text_content() == summary
            assert errors == []
        finally:
            browser.close()


def test_preview_warning_supports_native_keyboard_and_panel_action():
    _render_preview()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1024, "height": 768})
            errors = _install_console_capture(page)
            page.goto((OUT / "index.html").as_uri(), wait_until="load")
            warning_trigger = page.locator(".pp-warning-trigger").first
            panel = page.locator("#pp-warning-popover")

            warning_trigger.focus()
            page.keyboard.press("Enter")
            page.locator('#pp-warning-panel-content[data-warning-state="open"]').wait_for(
                state="visible"
            )
            assert warning_trigger.get_attribute("aria-expanded") == "true"
            page.keyboard.press("Escape")
            assert not panel.is_visible()
            assert warning_trigger.evaluate("trigger => document.activeElement === trigger")

            warning_trigger.evaluate("trigger => trigger.blur()")
            warning_trigger.focus()
            page.keyboard.press("Space")
            page.locator('#pp-warning-panel-content[data-warning-state="open"]').wait_for(
                state="visible"
            )
            panel.locator('[data-pp-warning-action="check_again"]').click()
            page.wait_for_function(
                "document.getElementById('pp-action-status').textContent === "
                "'The check could not finish.'"
            )
            assert "The check could not finish." in panel.text_content()
            page.keyboard.press("Escape")
            assert not panel.is_visible()
            assert warning_trigger.evaluate("trigger => document.activeElement === trigger")
            assert errors == []
        finally:
            browser.close()


def test_preview_fits_all_manager_viewports_with_one_compact_manager_row():
    _render_preview()
    fixture_url = (OUT / "index.html").as_uri()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for width, height in VIEWPORTS:
                page = browser.new_page(
                    viewport={"width": width, "height": height},
                    has_touch=width <= 760,
                )
                errors = _install_console_capture(page)
                page.goto(fixture_url, wait_until="load")
                count_buttons = page.locator(".pp-counts > button")
                warning_buttons = page.locator(".pp-source-warnings > .pp-warning-trigger")
                assert count_buttons.count() == 3
                assert warning_buttons.count() == 2
                assert count_buttons.evaluate_all(
                    "buttons => buttons.every(button => button.tagName === 'BUTTON' && button.type === 'button')"
                )
                assert warning_buttons.evaluate_all(
                    "buttons => buttons.every(button => button.tagName === 'BUTTON' && button.type === 'button')"
                )
                count_states = count_buttons.evaluate_all(
                    """
                    buttons => buttons.map(button => ({
                      pressed: button.getAttribute('aria-pressed'),
                      height: button.getBoundingClientRect().height,
                    }))
                    """
                )
                assert [state["pressed"] for state in count_states] == [
                    "true",
                    "false",
                    "false",
                ]
                assert all(state["height"] >= 44 for state in count_states)
                selected_count = page.locator('.pp-counts > button[aria-pressed="true"]')
                assert selected_count.count() == 1
                assert selected_count.locator(".pp-filter-selected").is_visible()
                assert selected_count.locator(".pp-filter-selected").text_content() == "✓"
                assert selected_count.locator(".sr-only").text_content() == "Selected filter."

                warning_trigger = warning_buttons.first
                if width > 760:
                    warning_trigger.hover()
                    assert page.locator("#pp-warning-popover").is_visible()
                    assert (
                        "2 production meters are unavailable."
                        in page.locator("#pp-warning-popover").text_content()
                    )
                else:
                    assert page.evaluate("matchMedia('(pointer: coarse)').matches") is True
                    warning_trigger.hover()
                    assert not page.locator("#pp-warning-popover").is_visible()
                    assert warning_trigger.get_attribute("aria-expanded") == "false"
                    warning_trigger.tap()
                    touch_panel_content = page.locator(
                        '#pp-warning-panel-content[data-warning-state="open"]'
                    )
                    touch_panel_content.wait_for(state="visible")
                    assert "touch" in page.evaluate("window.__peoplePreviewPointerTypes")
                    page.keyboard.press("Escape")
                    assert not page.locator("#pp-warning-popover").is_visible()
                    assert warning_trigger.evaluate("trigger => document.activeElement === trigger")
                    warning_trigger.evaluate("trigger => trigger.blur()")
                warning_trigger.focus()
                assert warning_trigger.get_attribute("aria-expanded") == "true"
                warning_trigger.click()
                panel_content = page.locator('#pp-warning-panel-content[data-warning-state="open"]')
                panel_content.wait_for(state="visible")
                panel_content.evaluate(
                    """
                    content => {
                      const footer = content.querySelector('footer');
                      footer.insertAdjacentHTML(
                        'beforebegin',
                        '<dl data-preview-warning-facts>' +
                          '<div><dt>Work center</dt><dd>Trim Saw 1</dd></div>' +
                          '<div><dt>Last checked</dt><dd>2:00 PM</dd></div>' +
                        '</dl>'
                      );
                      window.dispatchEvent(new Event('resize'));
                    }
                    """
                )
                panel = page.locator("#pp-warning-popover")
                trigger_box = warning_trigger.bounding_box()
                panel_box = panel.bounding_box()
                assert trigger_box and panel_box
                assert panel_box["x"] >= 0
                assert panel_box["y"] >= 0
                assert panel_box["x"] + panel_box["width"] <= width
                assert panel_box["y"] + panel_box["height"] <= height
                assert (
                    panel_box["x"] + panel_box["width"] <= trigger_box["x"]
                    or trigger_box["x"] + trigger_box["width"] <= panel_box["x"]
                    or panel_box["y"] + panel_box["height"] <= trigger_box["y"]
                    or trigger_box["y"] + trigger_box["height"] <= panel_box["y"]
                )
                interactive_heights = panel.locator("button, a").evaluate_all(
                    "targets => targets.map(target => target.getBoundingClientRect().height)"
                )
                assert len(interactive_heights) == 3
                assert min(interactive_heights) >= 44
                if width <= 760:
                    mobile_panel_geometry = panel.evaluate(
                        """
                        panel => ({
                          factsStack: [...panel.querySelectorAll('dl div')].every(row => {
                            const children = [...row.children].map(child => child.getBoundingClientRect());
                            return children.length === 2
                              && children[1].top >= children[0].bottom - 0.5
                              && getComputedStyle(row).gridTemplateColumns.trim().split(/\\s+/).length === 1;
                          }),
                          actionsStack: (() => {
                            const footer = panel.querySelector('footer');
                            const boxes = [...footer.children].map(child => child.getBoundingClientRect());
                            return boxes.every((box, index) => (
                              box.width >= footer.clientWidth - 1
                              && (index === 0 || box.top >= boxes[index - 1].bottom - 0.5)
                            ));
                          })(),
                        })
                        """
                    )
                    assert mobile_panel_geometry["factsStack"] is True
                    assert mobile_panel_geometry["actionsStack"] is True
                before = page.locator(".pp-manager-strip").bounding_box()
                geometry = page.evaluate(
                    """
                    () => ({
                      documentWidth: document.documentElement.scrollWidth,
                      bodyWidth: document.body.scrollWidth,
                      managerHeight: document.querySelector('.pp-manager-strip')
                        .getBoundingClientRect().height,
                      rowRights: [...document.querySelectorAll('.pp-row')]
                        .map(row => row.getBoundingClientRect().right),
                      scheduleLabelsDoNotOverlap: [...document.querySelectorAll('.pp-section-header')]
                        .every(header => [...header.querySelectorAll('.pp-schedule-time-group')]
                          .map(label => label.getBoundingClientRect())
                          .every((box, index, boxes) => index === 0 || box.left >= boxes[index - 1].right)),
                      identityNamesFit: [...document.querySelectorAll('.pp-identity h3')]
                        .every(name => name.scrollWidth <= name.clientWidth),
                      localOverflowContained: [...document.querySelectorAll('.pp-timeline-viewport')]
                        .every(view => (
                          view.scrollWidth >= view.clientWidth
                          && view.getBoundingClientRect().right <= window.innerWidth
                          && ['auto', 'scroll'].includes(getComputedStyle(view).overflowX)
                        )),
                      managerGroupsDoNotScroll: [...document.querySelectorAll(
                        '.pp-counts, .pp-source-warnings, .pp-controls'
                      )].every(group => (
                        group.scrollWidth <= group.clientWidth + 0.5
                        && !['auto', 'scroll'].includes(getComputedStyle(group).overflowX)
                      )),
                      warningsStayWithinPrimary: (() => {
                        const primary = document.querySelector('.pp-manager-primary');
                        const warnings = document.querySelector('.pp-source-warnings');
                        if (!primary || !warnings) return false;
                        const primaryBox = primary.getBoundingClientRect();
                        const warningBox = warnings.getBoundingClientRect();
                        return warningBox.left >= primaryBox.left - 0.5
                          && warningBox.right <= primaryBox.right + 0.5
                          && warningBox.top >= primaryBox.top - 0.5
                          && warningBox.bottom <= primaryBox.bottom + 0.5;
                      })(),
                      primaryGroupsDoNotOverlap: (() => {
                        const counts = document.querySelector('.pp-counts');
                        const warnings = document.querySelector('.pp-source-warnings');
                        const actions = document.querySelector('.pp-manager-actions');
                        const boxes = [counts, warnings, actions]
                          .map(node => node.getBoundingClientRect());
                        return boxes.every((box, index) => boxes.slice(index + 1).every(other => {
                          const separatedHorizontally = box.right <= other.left + 0.5
                            || other.right <= box.left + 0.5;
                          const separatedVertically = box.bottom <= other.top + 0.5
                            || other.bottom <= box.top + 0.5;
                          return separatedHorizontally || separatedVertically;
                        }));
                      })(),
                      managerDescendantsContained: (() => {
                        const strip = document.querySelector('.pp-manager-strip');
                        const stripBox = strip.getBoundingClientRect();
                        return [...strip.querySelectorAll('*:not(.sr-only)')]
                          .filter(node => {
                            const box = node.getBoundingClientRect();
                            return box.width > 0 && box.height > 0;
                          })
                          .every(node => {
                            const box = node.getBoundingClientRect();
                            return box.left >= stripBox.left - 0.5
                              && box.right <= stripBox.right + 0.5
                              && box.top >= stripBox.top - 0.5
                              && box.bottom <= stripBox.bottom + 0.5;
                          });
                      })(),
                      managerClearsFirstSection: (() => {
                        const strip = document.querySelector('.pp-manager-strip');
                        const section = document.querySelector('.pp-section');
                        return section.getBoundingClientRect().top
                          >= strip.getBoundingClientRect().bottom - 0.5;
                      })(),
                      warningTextFits: [...document.querySelectorAll('.pp-source-warnings span')]
                        .every(warning => warning.scrollWidth <= warning.clientWidth + 0.5),
                      controlsReachable: [...document.querySelectorAll('.pp-controls > *')]
                        .every(control => {
                          const box = control.getBoundingClientRect();
                          return box.left >= 0 && box.right <= window.innerWidth;
                        }),
                      scheduleAndTimelineWidthsMatch: [...document.querySelectorAll('.pp-section')]
                        .every(section => {
                          const schedule = section.querySelector('.pp-schedule-track');
                          const timeline = section.querySelector('.pp-timeline');
                          return !timeline || Math.abs(schedule.getBoundingClientRect().width
                            - timeline.getBoundingClientRect().width) < 0.5;
                        }),
                      scheduleOverflowContained: [...document.querySelectorAll('.pp-schedule-viewport')]
                        .every(view => (
                          view.scrollWidth > view.clientWidth
                          && view.getBoundingClientRect().right <= window.innerWidth
                          && ['auto', 'scroll'].includes(getComputedStyle(view).overflowX)
                        )),
                      intervals: window.__peoplePreviewIntervals,
                    })
                    """
                )
                page.evaluate("window.scrollTo(0, 900)")
                after = page.locator(".pp-manager-strip").bounding_box()
                assert before and after
                assert 0 <= after["y"] < height
                assert after["x"] >= 0
                assert after["x"] + after["width"] <= width
                assert geometry["documentWidth"] <= width
                assert geometry["bodyWidth"] <= width
                if width >= 1195:
                    assert geometry["managerHeight"] <= 60
                assert max(geometry["rowRights"]) <= width
                assert geometry["scheduleLabelsDoNotOverlap"] is True
                assert geometry["identityNamesFit"] is True
                assert page.locator(".pp-timeline").first.bounding_box()["width"] >= 480
                assert geometry["localOverflowContained"] is True
                assert geometry["managerGroupsDoNotScroll"] is True
                assert geometry["warningsStayWithinPrimary"] is True
                assert geometry["primaryGroupsDoNotOverlap"] is True
                assert geometry["managerDescendantsContained"] is True
                assert geometry["managerClearsFirstSection"] is True
                assert geometry["warningTextFits"] is True
                assert geometry["controlsReachable"] is True
                assert geometry["scheduleAndTimelineWidthsMatch"] is True
                assert geometry["scheduleOverflowContained"] is True
                assert 30000 not in geometry["intervals"]
                assert errors == []
                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(
                    path=OUT / f"people-performance-{width}x{height}.png",
                    full_page=True,
                )
                page.keyboard.press("Escape")
                assert not panel.is_visible()
                assert warning_trigger.get_attribute("aria-expanded") == "false"
                assert warning_trigger.evaluate("trigger => document.activeElement === trigger")
                page.close()
        finally:
            browser.close()


@pytest.mark.parametrize(("width", "height"), VIEWPORTS)
def test_preview_completed_rows_keep_computed_text_contrast(width, height):
    _render_preview()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto((OUT / "index.html").as_uri(), wait_until="load")
            ratios = page.locator(".pp-row.is-complete").evaluate_all(
                """
                rows => {
                  function rgb(value) {
                    const values = value.match(/[\\d.]+/g).slice(0, 3).map(Number);
                    return value.startsWith('color(srgb')
                      ? values.map(component => component * 255)
                      : values;
                  }
                  function luminance(color) {
                    return rgb(color).map(value => value / 255).map(value => (
                      value <= 0.04045 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4)
                    )).reduce((total, value, index) => total + value * [0.2126, 0.7152, 0.0722][index], 0);
                  }
                  function ratio(foreground, background) {
                    const light = Math.max(luminance(foreground), luminance(background));
                    const dark = Math.min(luminance(foreground), luminance(background));
                    return (light + 0.05) / (dark + 0.05);
                  }
                  return rows.flatMap(row => {
                    const background = getComputedStyle(row).backgroundColor;
                    return [...row.querySelectorAll('.pp-identity h3, .pp-summary dd')]
                      .map(node => ratio(getComputedStyle(node).color, background));
                  });
                }
                """
            )
            assert ratios
            assert min(ratios) >= 4.5
        finally:
            browser.close()


@pytest.mark.parametrize(("width", "height"), VIEWPORTS)
def test_preview_short_details_work_with_keyboard_pointer_and_escape(width, height):
    _render_preview()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = _install_console_capture(page)
            page.goto((OUT / "index.html").as_uri(), wait_until="load")
            trigger = page.locator(".pp-interval-shortcut").first
            trigger.focus()
            popover = page.locator("#pp-detail-popover")
            assert popover.is_visible()
            trigger.click()
            assert trigger.get_attribute("aria-expanded") == "true"
            page.keyboard.press("Escape")
            assert not popover.is_visible()
            assert errors == []
        finally:
            browser.close()
