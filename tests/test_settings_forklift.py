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


def test_parse_forklift_overrides_utilization_percent_to_fraction():
    from zira_dashboard.routes.settings import _parse_forklift_overrides
    s = _parse_forklift_overrides({"enabled": "1", "utilization_pct": "80"})
    assert s.utilization_override == 0.8
    # blank/auto -> None
    s2 = _parse_forklift_overrides({"enabled": "1", "utilization_pct": "auto"})
    assert s2.utilization_override is None


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


# --- GOAT-score overrides (Task 10) ------------------------------------------
def test_parse_forklift_overrides_score_auto_vs_set():
    s = settings_route._parse_forklift_overrides({
        "score_w_calls": "50", "score_w_ontime": "auto", "score_min_calls": "12",
        "score_target_calls": "", "score_fast_secs": "45"})
    assert s.score_w_calls == 50.0
    assert s.score_w_ontime is None            # "auto" -> follow algorithm
    assert s.score_min_calls == 12
    assert s.score_target_calls is None        # blank -> auto
    assert s.score_fast_secs == 45.0


def test_parse_forklift_overrides_score_clamps():
    s = settings_route._parse_forklift_overrides({
        "score_w_calls": "999", "score_target_calls": "0", "score_ontime_floor": "150",
        "score_fast_secs": "0", "score_slow_secs": "9999", "score_min_calls": "0"})
    assert s.score_w_calls == 100.0            # weights clamp 0-100
    assert s.score_target_calls == 1           # target_calls clamp 1-100
    assert s.score_ontime_floor == 99          # ontime_floor clamp 0-99
    assert s.score_fast_secs == 1              # fast/slow secs clamp 1-600
    assert s.score_slow_secs == 600
    assert s.score_min_calls == 1              # min_calls clamp 1-100


def _stub_score_ctx():
    """The extra GOAT-Score subsection context the panel needs (resolved config,
    the algorithm defaults for the grey ticks, the overrides=None map, and a
    sample scored day for the live worked example)."""
    return {
        "score": {"weights": {"calls": 50.0, "ontime": 30.0, "speed": 20.0,
                              "util": 10.0},
                  "target_calls": 25.0, "ontime_floor": 80.0, "fast_secs": 30.0,
                  "slow_secs": 180.0, "min_calls": 8},
        "score_algo": {"weights": {"calls": 40.0, "ontime": 30.0, "speed": 20.0,
                                  "util": 10.0},
                       "target_calls": 25.0, "ontime_floor": 80.0, "fast_secs": 30.0,
                       "slow_secs": 180.0, "min_calls": 8},
        "score_overrides": {"calls": 50.0, "ontime": None, "speed": None,
                            "util": None, "target_calls": None, "ontime_floor": None,
                            "fast_secs": None, "slow_secs": None, "min_calls": None},
        "score_sample": {"name": "Trent", "day_label": "Apr 14",
                         "calls": 31, "on_time": 30, "late": 1, "avg_ms": 40000,
                         "utilization_pct": 22.0},
    }


def test_forklift_settings_section_renders_goat_score_panel():
    from zira_dashboard.deps import templates
    ctx = _stub_forklift_ctx()
    ctx.update(_stub_score_ctx())
    rendered = templates.env.from_string(_extract_forklift_section()).render(
        forklift=ctx, saved=False, active_section="forklift")
    assert "GOAT Score" in rendered
    # The four weight sliders are present with their named POST fields + ids.
    for field in ("score_w_calls", "score_w_ontime", "score_w_speed", "score_w_util"):
        assert 'name="%s"' % field in rendered
    assert 'id="score-w-calls"' in rendered
    # Advanced targets + gate sliders present.
    for field in ("score_target_calls", "score_ontime_floor", "score_fast_secs",
                  "score_slow_secs", "score_min_calls"):
        assert 'name="%s"' % field in rendered
    # The live worked-example shows the sample day + a live score readout.
    assert "Trent" in rendered
    assert "score-example" in rendered


def test_forklift_settings_goat_score_absent_when_no_score_ctx():
    # When the score context is missing (forklift data unavailable), the GOAT
    # Score panel simply doesn't render — the rest of the form still does.
    from zira_dashboard.deps import templates
    rendered = templates.env.from_string(_extract_forklift_section()).render(
        forklift={"enabled": True}, saved=False, active_section="forklift")
    assert "GOAT Score" not in rendered


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
        "overloaded": False,
        "target_seconds": 240.0,
        "target_minutes": 4.0,
        "predicted_claim_seconds": 174.0,
        "backtest": {"n_samples": 40, "mean_actual_seconds": 165.0,
                     "mean_pred_seconds": 170.0, "uncalibrated": False},
        "total_calls": 500,
        "peak_calls": 97.0,
        "peak_label": "9:00–10:00",
        "basis": "history",
        "n_days": 6,
        "algo_values": {"throughput": 16.0, "utilization": 0.65, "percentile": 1.0,
                        "history_samples": 8, "effective_throughput": 10.4},
        "resolved_values": {"throughput": 22.0, "utilization": 0.5, "percentile": 0.9,
                            "history_samples": 4, "effective_throughput": 11.0},
        "overrides": {"throughput": 22.0, "utilization": 0.5, "plan_for": 0.9,
                      "history_samples": 4, "target": None},
        "hour_values": [30.0, 50.0, 97.0],
        "ranges": {"throughput": {"min": 5, "max": 30, "step": 1},
                   "utilization_pct": {"min": 40, "max": 100, "step": 1},
                   "plan_for": {"min": 0.5, "max": 1.0, "step": 0.05},
                   "history_samples": {"min": 2, "max": 20, "step": 1},
                   "target_minutes": {"min": 1, "max": 10, "step": 0.5}},
    }


def test_forklift_settings_section_renders_sliders_and_both_numbers():
    from zira_dashboard.deps import templates
    rendered = templates.env.from_string(_extract_forklift_section()).render(
        forklift=_stub_forklift_ctx(), saved=False, active_section="forklift")
    # The surviving advisor sliders are present.
    for field in ("plan_for", "history_samples"):
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


def test_forklift_panel_has_target_slider_not_capacity_sliders():
    """Task 7: the panel shows a 'Target time-to-claim' slider and has retired the
    Driver-speed (throughput) and Safety-slack (utilization) capacity knobs."""
    from zira_dashboard.deps import templates
    rendered = templates.env.from_string(_extract_forklift_section()).render(
        forklift=_stub_forklift_ctx(), saved=False, active_section="forklift")
    assert 'name="target_claim_seconds"' in rendered
    assert "Target time-to-claim" in rendered
    # capacity knobs retired from the panel
    assert "Driver speed" not in rendered and "Safety slack" not in rendered
    assert 'data-field="throughput"' not in rendered
    assert 'data-field="utilization_pct"' not in rendered


def test_forklift_panel_shows_backtest_and_recommendation_line():
    """The panel surfaces the read-only back-test and the SLA recommendation line
    ('under 4 min · predicted ~X min'), server-rendered."""
    from zira_dashboard.deps import templates
    rendered = templates.env.from_string(_extract_forklift_section()).render(
        forklift=_stub_forklift_ctx(), saved=False, active_section="forklift")
    assert "time-to-claim" in rendered.lower()
    assert "under 4 min" in rendered          # target 240s -> 4 min
    assert "predicted" in rendered.lower()
    # back-test: predicts ~X min vs. actual ~Y min over N days (calibrated).
    assert "predicts" in rendered.lower() and "actual" in rendered.lower()
    assert "uncalibrated" not in rendered.lower()  # backtest.uncalibrated is False


def test_forklift_panel_shows_uncalibrated_note_when_building_history():
    from zira_dashboard.deps import templates
    ctx = _stub_forklift_ctx()
    ctx["backtest"] = {"n_samples": 2, "mean_actual_seconds": 0.0,
                       "mean_pred_seconds": 0.0, "uncalibrated": True}
    rendered = templates.env.from_string(_extract_forklift_section()).render(
        forklift=ctx, saved=False, active_section="forklift")
    assert "uncalibrated" in rendered.lower()


def test_forklift_panel_shows_overloaded_message():
    from zira_dashboard.deps import templates
    ctx = _stub_forklift_ctx(recommended=None)
    ctx["overloaded"] = True
    ctx["predicted_claim_seconds"] = None
    rendered = templates.env.from_string(_extract_forklift_section()).render(
        forklift=ctx, saved=False, active_section="forklift")
    assert "overloaded" in rendered.lower()


def test_forklift_settings_section_unavailable_still_saves():
    from zira_dashboard.deps import templates
    rendered = templates.env.from_string(_extract_forklift_section()).render(
        forklift={"enabled": True}, saved=False, active_section="forklift")
    # No algo_values -> the "not available" notice shows but the form still saves.
    assert "isn't available right now" in rendered
    assert 'action="/settings/forklift"' in rendered
    assert 'name="enabled"' in rendered
