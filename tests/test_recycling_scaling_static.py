"""Static guards that the recycling dashboard keeps its widget internals
fully proportional — no fixed pixel floors/caps that fight TV
fit-to-viewport, and no fixed-height chart override in a narrow-width
media query. See docs/superpowers/specs/2026-07-07-recycling-dashboard-scaling-design.md.
"""
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "src/zira_dashboard/static"
_TEMPLATES = Path(__file__).resolve().parent.parent / "src/zira_dashboard/templates"
CSS = (_STATIC / "recycling.css").read_text()
WC_CSS = (_STATIC / "wc_dashboard.css").read_text()
RECYCLING_HTML = (_TEMPLATES / "recycling.html").read_text()


def test_progress_plot_has_no_pixel_min_height_floor():
    # .progress .plot / .cum-progress .plot must shrink with the widget.
    assert "min-height: 60px" not in CSS
    assert "min-height: 80px" not in CSS


def test_bar_track_has_no_fixed_pixel_min_or_max_height():
    # scoped bar-track must be proportional (no 14px floor, no 200px cap).
    assert "min-height: 14px" not in CSS
    assert "max-height: 200px" not in CSS


def test_no_fixed_progress_bars_height_in_media_query():
    # The harmful `@media (max-width:600px){ .progress .bars{height:110px} }`
    # pinned the flex chart to a fixed height on narrow windows.
    assert "height: 110px" not in CSS


def test_widget_content_padding_ceiling_is_modest():
    # cqh on the container resolves against the viewport, so the padding clamp
    # sits at its ceiling. A 1rem ceiling (~21px on a TV) ate short KPI cards.
    assert "clamp(0.4rem, 3cqh, 1rem)" not in CSS


def test_wc_kpi_value_has_low_font_floor():
    # Operator-dashboard KPI number must shrink to fit a short h2 card on a TV
    # instead of clipping — no big 2rem floor.
    assert "clamp(2rem, min(75cqh, 28cqw), 8rem)" not in WC_CSS


def test_recycling_bar_widgets_default_taller():
    # The two "Pallets by Work Center" bar widgets need enough rows that 6
    # operators fit as legible two-line (name over work-center) entries on a
    # TV without colliding; default height was bumped to 8.
    assert "widget_attrs('dismantler-bars', 0, 2, 6, 8)" in RECYCLING_HTML
    assert "widget_attrs('repair-bars', 6, 2, 6, 8)" in RECYCLING_HTML
