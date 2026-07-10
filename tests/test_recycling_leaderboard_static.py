from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "src/zira_dashboard/templates/recycling_leaderboard_tv.html").read_text()
CSS = (ROOT / "src/zira_dashboard/static/recycling_leaderboard.css").read_text()
PLAYER_CARD = (ROOT / "src/zira_dashboard/templates/player_card.html").read_text()
TV_DISPLAYS_STORE = (ROOT / "src/zira_dashboard/tv_displays_store.py").read_text()
SETTINGS_ROUTE = (ROOT / "src/zira_dashboard/routes/settings.py").read_text()


def test_tv_leaderboard_copy_uses_days_not_q_days_or_actual_times():
    assert "q-days" not in TEMPLATE
    assert "qualified days" not in TEMPLATE
    assert "actual times" not in TEMPLATE
    assert "not enough days" in TEMPLATE


def test_tv_leaderboard_names_have_dark_mode_foreground_color():
    assert ".rlb-table .name" in CSS
    name_block = CSS[CSS.index(".rlb-table .name") : CSS.index(".rlb-table .num")]
    assert "color: var(--fg)" in name_block


def test_tv_leaderboard_table_pins_rank_and_name_columns():
    assert 'class="rlb-rank-col"' in TEMPLATE
    assert 'class="rlb-name-col"' in TEMPLATE
    assert 'class="rlb-score-col"' in TEMPLATE
    assert ".rlb-table .rlb-rank-col" in CSS
    assert ".rlb-table .rlb-name-col" in CSS
    rank_block = CSS[CSS.index(".rlb-table .rlb-rank-col") : CSS.index(".rlb-table .rlb-name-col")]
    name_block = CSS[CSS.index(".rlb-table .rlb-name-col") : CSS.index(".rlb-table .rlb-score-col")]
    assert "width: clamp(" in rank_block
    assert "width: 46%" in name_block


def test_tv_leaderboard_horizontal_spacing_stays_compact():
    assert "padding: 0 clamp(8px, 1vw, 20px) clamp(10px, 1.2vh, 22px)" in CSS
    assert "gap: clamp(6px, 0.6vw, 12px)" in CSS
    assert "padding: clamp(8px, 0.8vw, 14px)" in CSS
    assert "padding: 0.3rem 0.2rem" in CSS
    assert "padding: clamp(0.35rem, 1vh, 0.85rem) 0.2rem" in CSS
    assert "gap: 0.2rem" in CSS
    assert "padding: 0.25rem 0.25rem" in CSS


def test_tv_range_and_goat_group_have_scoped_responsive_styles():
    title_start = CSS.index("html[data-tv-theme] .tv-header-title-line")
    title_end = CSS.index(
        "html[data-tv-theme] .tv-header-title-meta", title_start
    )
    title_block = CSS[title_start:title_end]
    assert "display: flex" in title_block
    assert "align-items: baseline" in title_block

    meta_start = title_end
    meta_end = CSS.index(
        "html[data-tv-theme] .tv-header .right.rlb-goat-banner", meta_start
    )
    meta_block = CSS[meta_start:meta_end]
    assert "opacity: 0.7" in meta_block
    assert "color: var(--fg)" in meta_block
    assert "white-space: nowrap" in meta_block
    assert "font-size: clamp(" in meta_block

    icon_selector = (
        "html[data-tv-theme] .tv-header .right.rlb-goat-banner"
        ".tv-header-right-has-icon"
    )
    icon_layout_start = CSS.index(icon_selector)
    icon_layout_end = CSS.index("}", icon_layout_start)
    icon_layout = CSS[icon_layout_start:icon_layout_end]
    assert "grid-template-columns: auto minmax(0, 1fr)" in icon_layout
    assert "align-items: center" in icon_layout

    icon_start = CSS.index("html[data-tv-theme] .rlb-goat-banner .tv-header-right-icon")
    icon_end = CSS.index("}", icon_start)
    assert "font-size: clamp(" in CSS[icon_start:icon_end]

    wide_grid = re.search(
        r"@media \(min-width: 1101px\)\s*\{.*?"
        r"body\.recycling-leaderboard-tv:not\(\.new-leaderboard-tv\) "
        r"\.rlb-grid\s*\{\s*height:\s*100%;\s*\}",
        CSS,
        re.DOTALL,
    )
    assert wide_grid is not None


def test_tv_leaderboard_role_titles_are_not_header_elements():
    assert '<header class="rlb-panel-head">' not in TEMPLATE
    assert '<div class="rlb-panel-head">' in TEMPLATE
    assert "Repairs" in TEMPLATE
    assert "Dismantlers" in TEMPLATE


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


def test_recycling_leaderboard_document_title_is_exact_name():
    assert "<title>Recycling-leaderboard</title>" in TEMPLATE


def test_gold_ribbons_use_column_headers_not_repeated_card_labels():
    assert 'class="rlb-ribbon-cols"' in TEMPLATE
    assert "<span>Repair</span>" in TEMPLATE
    assert "<span>Dismantler</span>" in TEMPLATE
    assert "<b>Repair</b>" not in TEMPLATE
    assert "<b>Dism" not in TEMPLATE
