"""Static guards that the recycling dashboard keeps its widget internals
fully proportional — no fixed pixel floors/caps that fight TV
fit-to-viewport, and no fixed-height chart override in a narrow-width
media query. See docs/superpowers/specs/2026-07-07-recycling-dashboard-scaling-design.md.
"""
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent
       / "src/zira_dashboard/static/recycling.css").read_text()


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
