from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "scripts/_preview_out/new_leaderboard"


def test_preview_renderer_creates_all_new_leaderboard_fixtures():
    env = os.environ | {
        "ZIRA_API_KEY": "test",
        "AUTH_DISABLED": "1",
        "PYTHONPATH": str(ROOT / "src"),
    }
    result = subprocess.run(
        [sys.executable, "scripts/preview_new_leaderboard.py"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == str(OUT)
    assert {
        "dashboard-junior-only.html",
        "tv-dark-junior-only.html",
        "tv-dark-three-families.html",
        "tv-light-three-families.html",
        "static",
    } <= {path.name for path in OUT.iterdir()}
    dashboard = (OUT / "dashboard-junior-only.html").read_text(encoding="utf-8")
    assert 'id="gpi-inbox-summary-bootstrap" type="application/json">{"source_errors": [], "total": 0, "urgent_total": 0}</script>' in dashboard


def test_preview_three_family_fixture_contains_calendar_ribbon_headers():
    env = os.environ | {
        "ZIRA_API_KEY": "test",
        "AUTH_DISABLED": "1",
        "PYTHONPATH": str(ROOT / "src"),
    }
    subprocess.run(
        [sys.executable, "scripts/preview_new_leaderboard.py"],
        cwd=ROOT,
        env=env,
        check=True,
    )
    html = (OUT / "tv-dark-three-families.html").read_text(encoding="utf-8")
    assert html.index(">Jan<") < html.index(">Dec<")
    for family in ("Juniors", "Woodpecker", "Hand Build"):
        assert f'class="nlb-work-center">{family}</strong>' in html


def test_preview_three_family_tv_ribbon_geometry_fits_target_viewports():
    env = os.environ | {
        "ZIRA_API_KEY": "test",
        "AUTH_DISABLED": "1",
        "PYTHONPATH": str(ROOT / "src"),
    }
    subprocess.run(
        [sys.executable, "scripts/preview_new_leaderboard.py"],
        cwd=ROOT,
        env=env,
        check=True,
    )

    fixture_url = (OUT / "tv-dark-three-families.html").as_uri()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for width, height in ((1920, 1080), (1280, 720)):
                page = browser.new_page(viewport={"width": width, "height": height})
                try:
                    page.goto(fixture_url, wait_until="load")
                    geometry = page.locator(".nlb-ribbon-grid").evaluate(
                        """grid => ({
                            viewportHeight: window.innerHeight,
                            gridBottom: grid.getBoundingClientRect().bottom,
                            rowHeights: [...grid.querySelectorAll('.nlb-ribbon-cell')]
                                .map(cell => cell.getBoundingClientRect().height),
                            rowBottoms: [...grid.querySelectorAll('.nlb-ribbon-cell')]
                                .map(cell => cell.getBoundingClientRect().bottom),
                        })"""
                    )
                    assert geometry["gridBottom"] <= geometry["viewportHeight"]
                    assert max(geometry["rowBottoms"]) <= geometry["viewportHeight"]
                    assert min(geometry["rowHeights"]) >= 32
                finally:
                    page.close()
        finally:
            browser.close()
