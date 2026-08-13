"""Recycling dashboard Uptime KPI — presence, format, and color bands."""

from pathlib import Path

from starlette.requests import Request

from zira_dashboard.deps import templates


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "src" / "zira_dashboard" / "templates" / "recycling.html"
CSS = ROOT / "src" / "zira_dashboard" / "static" / "recycling.css"


def _src() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _css() -> str:
    return CSS.read_text(encoding="utf-8")


def _render(*, uptime_pct: float) -> str:
    """Render recycling.html with the minimum context the KPI block needs."""
    request = Request({"type": "http", "method": "GET", "path": "/recycling", "headers": []})
    return templates.get_template("recycling.html").render(
        request=request,
        static_v=lambda path: "test",
        tv_mode=False,
        tv_theme="dark",
        window="today",
        custom_range_active=False,
        start="2026-08-13",
        end="2026-08-13",
        layout={},
        customs={},
        total_units=1459,
        pph_per_person=63.6,
        pph_per_person_ex_d4=57.1,
        dismantler_bars=[],
        repair_bars=[],
        downtime_rows=[],
        elapsed_minutes=0,
        uptime_pct=uptime_pct,
        dismantler_people=0,
        repair_people=0,
        is_range=False,
        dismantler_progress=[],
        repair_progress=[],
        dismantler_group_target=0,
        repair_group_target=0,
        range_includes_today=False,
        refreshed_at="9:00:00 AM",
        all_active_people=[],
        goat_alerts_active=[],
        goat_contenders=[],
    )


def test_recycling_template_declares_kpi_uptime_widget():
    html = _src()
    assert "kpi-uptime" in html
    assert "'Uptime'" in html or '"Uptime"' in html
    assert "band-good" in html and "band-warn" in html and "band-bad" in html


def test_recycling_css_locks_uptime_band_colors():
    css = _css()
    assert 'gs-id="kpi-uptime"' in css or "[gs-id='kpi-uptime']" in css or '[gs-id="kpi-uptime"]' in css
    assert "band-good" in css and "var(--good)" in css
    assert "band-warn" in css and "var(--warn)" in css
    assert "band-bad" in css and "var(--bad)" in css
    # Threshold colors must win over any customizer inline color.
    assert css.count("!important") >= 3


def test_uptime_kpi_renders_whole_percent_and_warn_band():
    html = _render(uptime_pct=85.4)
    assert 'gs-id="kpi-uptime"' in html
    assert ">85%<" in html or ">85 %</" not in html  # prefer tight "85%"
    assert "85%" in html
    assert 'class="val band-warn"' in html or "val band-warn" in html


def test_uptime_kpi_band_boundaries():
    assert "band-good" in _render(uptime_pct=90.0)
    assert "band-warn" in _render(uptime_pct=80.0)
    assert "band-bad" in _render(uptime_pct=79.9)
    assert "band-good" in _render(uptime_pct=99.9)
    assert "band-bad" in _render(uptime_pct=0.0)
