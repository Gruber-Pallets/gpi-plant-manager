from datetime import date
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "src/zira_dashboard/templates/new_leaderboard_tv.html").read_text()
CSS = (ROOT / "src/zira_dashboard/static/new_leaderboard.css").read_text()
RECYCLING_CSS = (ROOT / "src/zira_dashboard/static/recycling_leaderboard.css").read_text()


def test_new_leaderboard_uses_recycling_visual_base_and_own_layout_css():
    assert "/static/recycling_leaderboard.css" in TEMPLATE
    assert "/static/new_leaderboard.css" in TEMPLATE
    assert "recycling-leaderboard-tv new-leaderboard-tv" in TEMPLATE
    assert "recycling-leaderboard-page new-leaderboard-page" in TEMPLATE


def test_new_leaderboard_layout_responds_to_active_family_count():
    assert "nlb-family-count-{{ data.active_families|length }}" in TEMPLATE
    assert ".nlb-family-count-1" in CSS
    assert ".nlb-family-count-2" in CSS
    assert ".nlb-family-count-3" in CSS
    assert "repeat(2, minmax(0, 1fr))" in CSS
    assert "repeat(3, minmax(0, 1fr))" in CSS


def test_new_leaderboard_ribbon_matrix_uses_calendar_columns_and_family_rows():
    assert "data.ribbons|sort(attribute='month')" in TEMPLATE
    assert "{% for month in calendar_ribbons %}" in TEMPLATE
    assert "{% for family in data.active_families %}" in TEMPLATE
    assert "{% set winner = month.winners[family] %}" in TEMPLATE
    assert "nlb-work-center" in TEMPLATE

    calendar_ribbons = [
        SimpleNamespace(month=month, month_label=label, winners={"Alpha": None, "Beta": None})
        for month, label in reversed(list(enumerate((
            "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        ), start=1)))
    ]
    data = SimpleNamespace(
        active_families=["Alpha", "Beta"],
        current_goats=[],
        error_message=None,
        families={
            family: SimpleNamespace(
                thresholds=SimpleNamespace(ytd=1, l30=1),
                rows=[],
            )
            for family in ("Alpha", "Beta")
        },
        ribbons=calendar_ribbons,
        ytd_start=date(2026, 1, 1),
        ytd_end=date(2026, 12, 31),
        l30_start=date(2026, 12, 2),
        l30_end=date(2026, 12, 31),
    )
    environment = Environment(loader=FileSystemLoader(ROOT / "src/zira_dashboard/templates"))
    environment.globals["static_v"] = lambda _: "test"
    rendered = environment.get_template("new_leaderboard_tv.html").render(data=data)
    ribbon_grid = rendered.split('<div class="nlb-ribbon-grid"', 1)[1].split("</div>", 1)[0]
    month_positions = [
        ribbon_grid.index(f'class="nlb-month">{label}</strong>')
        for label in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    ]
    work_center_positions = [
        ribbon_grid.index(f'class="nlb-work-center">{family}</strong>')
        for family in ("Alpha", "Beta")
    ]
    assert month_positions == sorted(month_positions)
    assert max(month_positions) < min(work_center_positions)


def test_new_leaderboard_ribbon_matrix_fits_all_months_without_scrolling():
    assert "grid-template-columns: minmax(4.5rem, 0.7fr) repeat(12, minmax(0, 1fr));" in CSS
    assert "grid-template-rows: auto repeat(var(--nlb-family-count), minmax(0, 1fr));" in CSS
    assert "overflow: hidden;" in CSS
    assert ".nlb-work-center" in CSS


def test_new_leaderboard_copy_and_empty_states_are_exact():
    assert "New-Leaderboard" in TEMPLATE
    assert "Waiting for qualifying Zira production." in TEMPLATE
    assert "Production data is temporarily unavailable." not in TEMPLATE
    assert "not enough days" in TEMPLATE


def test_new_leaderboard_has_mobile_stack_and_name_safety():
    assert "@media (max-width: 1100px)" in CSS
    name_start = RECYCLING_CSS.index(".rlb-table .name")
    name_end = RECYCLING_CSS.index(".rlb-table .num", name_start)
    assert "text-overflow: ellipsis" in RECYCLING_CSS[name_start:name_end]
    assert 'aria-label="{{ row.name }}"' in TEMPLATE


def test_new_leaderboard_tv_keeps_three_goat_chips_in_one_row_at_all_tv_widths():
    goat_list_selector = "body.new-leaderboard-tv .rlb-goat-banner .tv-header-right-list"
    assert goat_list_selector in CSS
    goat_list_start = CSS.index(goat_list_selector)
    goat_list_end = CSS.index("}", goat_list_start)
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in CSS[goat_list_start:goat_list_end]
    fallback_start = CSS.index("@media (max-width: 1400px)")
    fallback_end = CSS.index("@media (max-width: 1100px)", fallback_start)
    fallback = CSS[fallback_start:fallback_end]
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in fallback
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" not in fallback
