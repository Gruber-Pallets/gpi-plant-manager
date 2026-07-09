from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "src/zira_dashboard/templates/recycling_leaderboard_tv.html").read_text()
CSS = (ROOT / "src/zira_dashboard/static/recycling_leaderboard.css").read_text()
PLAYER_CARD = (ROOT / "src/zira_dashboard/templates/player_card.html").read_text()
TV_DISPLAYS_STORE = (ROOT / "src/zira_dashboard/tv_displays_store.py").read_text()
SETTINGS_ROUTE = (ROOT / "src/zira_dashboard/routes/settings.py").read_text()


def test_tv_leaderboard_copy_uses_days_not_q_days_or_actual_times():
    assert "q-days" not in TEMPLATE
    assert "actual times" not in TEMPLATE
    assert "not enough days" in TEMPLATE


def test_tv_leaderboard_names_have_dark_mode_foreground_color():
    assert ".rlb-table .name" in CSS
    name_block = CSS[CSS.index(".rlb-table .name") : CSS.index(".rlb-table .num")]
    assert "color: var(--fg)" in name_block


def test_player_card_no_longer_labels_production_average_as_pph():
    assert "Avg (pph)" not in PLAYER_CARD
    assert ">pph<" not in PLAYER_CARD
    assert "Full-day avg" in PLAYER_CARD


def test_recycling_leaderboard_display_name_stays_hyphenated():
    assert "Recycling-leaderboard" in TEMPLATE
    assert "Recycling-leaderboard" in TV_DISPLAYS_STORE
    assert "Recycling-leaderboard" in SETTINGS_ROUTE
    assert "Recycling Leaderboard" not in TEMPLATE
    assert "Recycling Leaderboard" not in TV_DISPLAYS_STORE
    assert "Recycling Leaderboard" not in SETTINGS_ROUTE
