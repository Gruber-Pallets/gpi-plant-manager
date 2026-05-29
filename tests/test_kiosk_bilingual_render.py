"""End-to-end-ish: the dashboard template renders Spanish under English
only for bilingual users."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from zira_dashboard import kiosk_i18n

TEMPLATES = Path("src/zira_dashboard/templates")


def _env():
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    env.globals["static_v"] = lambda _f: "test"
    env.globals["t"] = kiosk_i18n.t
    return env


def _ctx(bilingual):
    return {
        "person": {"name": "Maria Garcia"},
        "token": "t",
        "is_clocked_in": False,
        "scheduled_wc": None,
        "sync_warning": None,
        "time_off_enabled": True,
        "pending_time_off_count": 0,
        "bilingual": bilingual,
    }


def test_dashboard_english_only_when_not_bilingual():
    html = _env().get_template("kiosk_dashboard.html").render(**_ctx(False))
    assert "Pick Work Center" in html
    assert "Elegir estación" not in html


def test_dashboard_bilingual_shows_spanish():
    html = _env().get_template("kiosk_dashboard.html").render(**_ctx(True))
    assert "Pick Work Center" in html       # English still present
    assert "Elegir estación" in html        # Spanish added
    assert 'class="k-es"' in html
