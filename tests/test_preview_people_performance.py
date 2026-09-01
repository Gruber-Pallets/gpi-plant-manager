from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest
from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "scripts/_preview_out/people_performance"
VIEWPORTS = ((1440, 900), (1024, 768), (768, 1024))


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
        const nativeSetInterval = window.setInterval.bind(window);
        window.setInterval = function (callback, milliseconds) {
          window.__peoplePreviewIntervals.push(milliseconds);
          return nativeSetInterval(callback, milliseconds);
        };
        """
    )
    return errors


def test_preview_contains_busy_people_fixture():
    result = _render_preview()

    assert result.stdout.strip() == str(OUT)
    html = (OUT / "index.html").read_text(encoding="utf-8")
    assert html.count('class="pp-row') >= 10
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


def test_preview_fits_all_manager_viewports_and_keeps_sticky_time_context():
    _render_preview()
    fixture_url = (OUT / "index.html").as_uri()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for width, height in VIEWPORTS:
                page = browser.new_page(viewport={"width": width, "height": height})
                errors = _install_console_capture(page)
                page.goto(fixture_url, wait_until="load")
                before = page.locator(".pp-axis").bounding_box()
                page.evaluate("window.scrollTo(0, 900)")
                after = page.locator(".pp-axis").bounding_box()
                geometry = page.evaluate(
                    """
                    () => ({
                      documentWidth: document.documentElement.scrollWidth,
                      bodyWidth: document.body.scrollWidth,
                      rowRights: [...document.querySelectorAll('.pp-row')]
                        .map(row => row.getBoundingClientRect().right),
                      axisLabelsDoNotOverlap: [...document.querySelectorAll('.pp-axis-track span')]
                        .map(label => label.getBoundingClientRect())
                        .every((box, index, boxes) => index === 0 || box.left >= boxes[index - 1].right),
                      identityNamesFit: [...document.querySelectorAll('.pp-identity h3')]
                        .every(name => name.scrollWidth <= name.clientWidth),
                      localOverflowContained: [...document.querySelectorAll('.pp-timeline-viewport')]
                        .every(view => (
                          view.scrollWidth >= view.clientWidth
                          && view.getBoundingClientRect().right <= window.innerWidth
                          && ['auto', 'scroll'].includes(getComputedStyle(view).overflowX)
                        )),
                      intervals: window.__peoplePreviewIntervals,
                    })
                    """
                )
                assert before and after
                assert 0 <= after["y"] < height
                assert geometry["documentWidth"] <= width
                assert geometry["bodyWidth"] <= width
                assert max(geometry["rowRights"]) <= width
                assert geometry["axisLabelsDoNotOverlap"] is True
                assert geometry["identityNamesFit"] is True
                assert page.locator(".pp-timeline").first.bounding_box()["width"] >= 480
                assert geometry["localOverflowContained"] is True
                assert 30000 not in geometry["intervals"]
                assert errors == []
                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(
                    path=OUT / f"people-performance-{width}x{height}.png",
                    full_page=True,
                )
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
