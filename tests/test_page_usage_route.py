"""/admin/page-usage report page. usage_report is monkeypatched so no DB is
needed; page_inventory/never_hit run against the real app route table."""
from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from zira_dashboard import page_views
from zira_dashboard.app import app

client = TestClient(app)


def test_page_usage_page_shows_used_and_never_hit(monkeypatch):
    monkeypatch.setattr(page_views, "usage_report", lambda days=7: [
        {"route": "/staffing", "views": 42, "users": 3, "last_day": date(2026, 7, 1)},
    ])
    resp = client.get("/admin/page-usage")
    assert resp.status_code == 200
    body = resp.text
    # the used page and its numbers render
    assert "/staffing" in body
    assert "42" in body
    # a real page absent from the report is listed as never-hit
    assert "/trophies" in body


import os as _os
import re

import pytest


@pytest.mark.skipif(
    not _os.environ.get("DATABASE_URL"), reason="needs Postgres (/settings loads DB)"
)
def test_settings_has_diagnostics_section_linking_to_report():
    resp = client.get("/settings?section=diagnostics")
    assert resp.status_code == 200
    body = resp.text
    # the report is linked from Settings
    assert 'href="/admin/page-usage"' in body
    # and 'diagnostics' is an accepted section (nav item goes active, i.e. the
    # allowlist didn't fall back to work_centers)
    assert re.search(
        r'\?section=diagnostics"\s+class="settings-nav-item active"', body
    )
