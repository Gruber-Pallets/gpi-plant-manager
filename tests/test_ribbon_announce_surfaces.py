"""Ribbon winners banner — template wiring + route context."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src/zira_dashboard/templates"


def _read(name: str) -> str:
    return (TEMPLATES / name).read_text()


def test_templates_link_ribbon_css_and_stack_above_goat():
    for name in ("wc_dashboard.html", "recycling.html", "new_dept.html"):
        html = _read(name)
        assert "ribbon_announce.css" in html
        assert 'include "_ribbon_winners_banner.html"' in html
        # Ribbon include must appear before GOAT include in source order
        # so the stack is ribbon-on-top.
        ribbon_i = html.find('include "_ribbon_winners_banner.html"')
        goat_i = html.find('include "_goat_watch_banner.html"')
        assert ribbon_i != -1 and goat_i != -1
        assert ribbon_i < goat_i


def test_wc_dashboard_only_includes_ribbon_in_tv_header_call():
    html = _read("wc_dashboard.html")
    # Non-TV path must NOT include the ribbon partial (spec: TV only).
    assert html.count('include "_ribbon_winners_banner.html"') == 1
    tv_call = html.split("{% if tv_mode %}", 1)[1].split("{% else %}", 1)[0]
    assert 'include "_ribbon_winners_banner.html"' in tv_call


def test_recycling_and_new_include_ribbon_on_desktop_and_tv():
    for name in ("recycling.html", "new_dept.html"):
        html = _read(name)
        assert html.count('include "_ribbon_winners_banner.html"') >= 2
