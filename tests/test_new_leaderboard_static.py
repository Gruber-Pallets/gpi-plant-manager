from pathlib import Path


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


def test_new_leaderboard_ribbon_grid_is_family_driven():
    assert "--nlb-family-count: {{ data.active_families|length }}" in TEMPLATE
    assert "repeat(var(--nlb-family-count), minmax(0, 1fr))" in CSS
    assert "month.winners[family]" in TEMPLATE


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
