"""Forklift settings route: the pure override-parsing helper (auto vs set,
clamps) and the settings-page template render (sliders + both numbers). Both run
everywhere (no DB, no network)."""
from zira_dashboard.routes import settings as settings_route


def test_parse_forklift_overrides_auto_vs_set():
    # "auto" / blank -> None (follow algorithm); a value -> override
    s = settings_route._parse_forklift_overrides({
        "enabled": "on", "throughput": "auto", "utilization_pct": "70",
        "plan_for": "0.8", "history_samples": "", "include_loading_jockeying": "on",
        "coldstart_calls_per_day": "0",
    })
    assert s.enabled is True
    assert s.throughput_override is None          # "auto"
    assert s.utilization_override == 0.70         # 70% -> 0.70
    assert s.plan_for_percentile_override == 0.8
    assert s.history_samples_override is None     # blank -> auto
    assert s.include_loading_jockeying is True


def test_parse_forklift_overrides_clamps():
    s = settings_route._parse_forklift_overrides({"utilization_pct": "999", "throughput": "0"})
    assert s.utilization_override == 1.0          # clamp <=100%
    assert s.throughput_override == 0.1 or s.throughput_override >= 1  # clamp >0 (floor 5)


def test_parse_forklift_overrides_disabled_and_unchecked():
    s = settings_route._parse_forklift_overrides({})  # nothing checked
    assert s.enabled is False
    assert s.include_loading_jockeying is False
    assert s.throughput_override is None


def test_parse_forklift_overrides_history_and_plan_clamp():
    s = settings_route._parse_forklift_overrides({
        "history_samples": "100", "plan_for": "0.1"})
    assert s.history_samples_override == 20       # clamp 2-20
    assert s.plan_for_percentile_override == 0.5  # clamp 0.5-1.0


# --- Settings page render (Jinja env, no DB / network) -----------------------
# Like tests/test_staffing_forklift_card.py, render just the forklift <section>
# from settings.html through the app's Jinja2 environment with a stub ctx, so we
# exercise the exact markup that ships without standing up the whole page.
def _extract_forklift_section() -> str:
    import re
    from pathlib import Path
    html = Path("src/zira_dashboard/templates/settings.html").read_text()
    m = re.search(
        r"<section class=\"panel\" id=\"forklift-panel\".*?</section>",
        html, re.DOTALL)
    assert m, "forklift-panel section missing from settings.html"
    return m.group(0)


def _stub_forklift_ctx(recommended=4, algo_recommended=6):
    return {
        "enabled": True,
        "target_day_label": "Sat Jun 28",
        "weekday_label": "Saturday",
        "include_loading_jockeying": False,
        "coldstart_calls_per_day": 0.0,
        "recommended": recommended,
        "algo_recommended": algo_recommended,
        "total_calls": 500,
        "peak_calls": 97.0,
        "peak_label": "9:00–10:00",
        "basis": "history",
        "n_days": 6,
        "effective_throughput": 10.4,
        "algo_values": {"throughput": 16.0, "utilization": 0.65, "percentile": 1.0,
                        "history_samples": 8, "effective_throughput": 10.4},
        "resolved_values": {"throughput": 22.0, "utilization": 0.5, "percentile": 0.9,
                            "history_samples": 4, "effective_throughput": 11.0},
        "overrides": {"throughput": 22.0, "utilization": 0.5, "plan_for": 0.9,
                      "history_samples": 4},
        "hour_values": [30.0, 50.0, 97.0],
        "ranges": {"throughput": {"min": 5, "max": 30, "step": 1},
                   "utilization_pct": {"min": 40, "max": 100, "step": 1},
                   "plan_for": {"min": 0.5, "max": 1.0, "step": 0.05},
                   "history_samples": {"min": 2, "max": 20, "step": 1}},
    }


def test_forklift_settings_section_renders_sliders_and_both_numbers():
    from zira_dashboard.deps import templates
    rendered = templates.env.from_string(_extract_forklift_section()).render(
        forklift=_stub_forklift_ctx(), saved=False, active_section="forklift")
    # The four named slider fields are present.
    for field in ("throughput", "utilization_pct", "plan_for", "history_samples"):
        assert 'data-field="%s"' % field in rendered
        assert 'name="%s"' % field in rendered
    assert 'type="range"' in rendered
    # Headline (your recommendation) and the algorithm baseline both show.
    assert "fl_headline_num" in rendered and ">4<" in rendered
    assert "the algorithm would recommend" in rendered and "<strong id=\"fl_algo_num\">6</strong>" in rendered
    assert "match it" in rendered
    # Live-preview data + algorithm tick data are embedded.
    assert "data-hour-values" in rendered
    assert "Reset all to algorithm" in rendered


def test_forklift_settings_section_unavailable_still_saves():
    from zira_dashboard.deps import templates
    rendered = templates.env.from_string(_extract_forklift_section()).render(
        forklift={"enabled": True}, saved=False, active_section="forklift")
    # No algo_values -> the "not available" notice shows but the form still saves.
    assert "isn't available right now" in rendered
    assert 'action="/settings/forklift"' in rendered
    assert 'name="enabled"' in rendered
