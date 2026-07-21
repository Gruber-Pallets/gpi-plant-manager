"""Chrome consolidation guards.

Ratchet: every full-page template must extend a base layout
(_base_app.html for desktop, timeclock_base.html for kiosk). Standalone
full-document templates are frozen in ALLOWED_STANDALONE and the list
only shrinks — never add to it. See
docs/superpowers/specs/2026-07-21-ui-consolidation.md.
"""
import os
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard.routes import dashboard as dashboard_route

SRC = Path(__file__).resolve().parents[1] / "src" / "zira_dashboard"
TEMPLATES = SRC / "templates"
STATIC = SRC / "static"

BASES = {"_base_app.html", "timeclock_base.html"}

# auth_denied.html stays standalone permanently: it renders for
# UNAUTHENTICATED users and must not include _topnav.html (which calls
# nav_inbox_summary()). Everything else is queued for conversion.
ALLOWED_STANDALONE = {
    "auth_denied.html",             # permanent
    "recycling_leaderboard_tv.html",  # Wave 2 (TV-shared)
    "staffing.html",                # Wave 3
}


def test_full_page_templates_extend_a_base():
    for path in sorted(TEMPLATES.glob("*.html")):
        if path.name.startswith("_") or path.name in BASES:
            continue
        src = path.read_text(encoding="utf-8")
        if "{% extends" in src:
            assert path.name not in ALLOWED_STANDALONE, (
                f"{path.name} now extends a base — remove it from ALLOWED_STANDALONE"
            )
        else:
            assert path.name in ALLOWED_STANDALONE, (
                f"{path.name} is a standalone document — extend _base_app.html "
                "or timeclock_base.html instead of hand-rolling chrome"
            )


def test_template_static_references_exist():
    """Every /static/<file> referenced by a template must exist on disk."""
    pattern = re.compile(r"/static/([A-Za-z0-9._-]+\.(?:css|js|png|ico|svg))")
    missing = []
    for path in sorted(TEMPLATES.glob("*.html")):
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            if not (STATIC / name).exists():
                missing.append(f"{path.name} -> /static/{name}")
    assert missing == [], f"templates reference missing static assets: {missing}"


def test_work_centers_filter_posts_back_to_work_centers(monkeypatch):
    # The Day/Category form must post to /work-centers itself. It used to
    # post to "/", which 307-redirects to /recycling and drops the query
    # string — silently losing the user's filter.
    monkeypatch.setattr(dashboard_route, "leaderboard", lambda *a, **k: [])
    client = TestClient(app)
    resp = client.get("/work-centers")
    assert resp.status_code == 200
    assert 'action="/work-centers"' in resp.text
    assert 'action="/"' not in resp.text


def _assert_single_chrome(html: str):
    lowered = html.lower()
    assert lowered.count("<!doctype") == 1
    assert html.count('class="brand-row"') == 1
    assert "changelog-modal" in html  # _footer.html present


def test_work_centers_extends_base_app(monkeypatch):
    monkeypatch.setattr(dashboard_route, "leaderboard", lambda *a, **k: [])
    client = TestClient(app)
    resp = client.get("/work-centers")
    assert resp.status_code == 200
    _assert_single_chrome(resp.text)
    assert "<title>Work Centers — GPI Plant Manager</title>" in resp.text


def test_exceptions_extends_base_app(monkeypatch):
    from zira_dashboard.routes import exceptions as exceptions_route

    monkeypatch.setattr(
        exceptions_route.exception_inbox,
        "build_snapshot",
        lambda **k: {
            "today": "2026-07-21", "generated_at": "1:22 PM", "total": 0,
            "urgent_total": 0, "follow_up_total": 0, "source_errors": [],
            "work_centers": [], "people": [], "sections": [], "queue": [],
        },
    )
    client = TestClient(app)
    resp = client.get("/exceptions")
    assert resp.status_code == 200
    _assert_single_chrome(resp.text)
    assert '<main class="inbox-shell">' in resp.text


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres (/settings loads DB)"
)
def test_settings_extends_base_app():
    client = TestClient(app)
    resp = client.get("/settings?section=diagnostics")
    assert resp.status_code == 200
    _assert_single_chrome(resp.text)
    assert 'id="page-undo-btn"' in resp.text  # header_extra survived
