"""Chrome consolidation guards.

Ratchet: every full-page template must extend a base layout
(_base_app.html for desktop, timeclock_base.html for kiosk). Standalone
full-document templates are frozen in ALLOWED_STANDALONE and the list
only shrinks — never add to it. See
docs/superpowers/specs/2026-07-21-ui-consolidation.md.
"""
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "zira_dashboard"
TEMPLATES = SRC / "templates"
STATIC = SRC / "static"

BASES = {"_base_app.html", "timeclock_base.html"}

# auth_denied.html stays standalone permanently: it renders for
# UNAUTHENTICATED users and must not include _topnav.html (which calls
# nav_inbox_summary()). Everything else is queued for conversion.
ALLOWED_STANDALONE = {
    "auth_denied.html",             # permanent
    "exceptions.html",              # Wave 1
    "index.html",                   # Wave 1
    "settings.html",                # Wave 1
    "new_dept.html",                # Wave 2 (TV-shared)
    "new_leaderboard_tv.html",      # Wave 2 (TV-shared)
    "recycling.html",               # Wave 2 (TV-shared)
    "recycling_leaderboard_tv.html",  # Wave 2 (TV-shared)
    "wc_dashboard.html",            # Wave 2 (TV-shared)
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


@pytest.mark.xfail(
    reason="auth_denied.html links nonexistent dashboard.css — fixed in Wave 1 Task 2",
    strict=True,
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
