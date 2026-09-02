"""Forklift settings route: the pure override-parsing helper (auto vs set,
clamps) and the settings-page template render (sliders + both numbers). Both run
everywhere (no DB, no network)."""
import pytest

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
        "observed_claim_seconds": 174.0,
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


def test_forklift_settings_renders_focused_identity_forms_without_raw_calls():
    from zira_dashboard.deps import templates

    forklift = _stub_forklift_ctx()
    forklift.update(_stub_score_ctx())
    identities = {
        "day": "2026-09-02",
        "unresolved": ({
            "external_driver_id": "driver-8",
            "source_names": ("Alex", "A."),
            "call_count": 5,
            "first_call": "7:10 AM",
            "last_call": "8:45 AM",
            "name_conflict": True,
            "event_id": "raw-event-must-not-render",
        },),
        "mappings": ({
            "external_driver_id": "driver-old",
            "source_name": "Sam",
            "employee_odoo_id": 708,
            "employee_name": "Sam Rivera",
            "employee_eligible": True,
            "employee_status": "active",
            "employee_status_label": "Active employee",
            "version": 4,
            "updated_at": "Sep 1, 10:30 AM",
            "updated_by_upn": "manager@example.com",
        },),
        "employee_options": (
            {"employee_odoo_id": 707, "employee_name": "Alex Chen"},
            {"employee_odoo_id": 708, "employee_name": "Sam Rivera"},
        ),
    }

    rendered = templates.env.from_string(_extract_forklift_section()).render(
        forklift=forklift,
        forklift_identities=identities,
        today="2026-09-02",
        identity_error="",
        identity_saved=False,
        saved=False,
        active_section="forklift",
    )

    assert "<h2>Forklift</h2>" in rendered
    assert "Forklift identities" in rendered
    assert "Demand Advisor" in rendered
    assert "GOAT Score" in rendered
    assert "Alex Chen" in rendered and "Sam Rivera" in rendered
    assert "Inactive Person" not in rendered
    assert 'action="/settings/forklift-identities"' in rendered
    assert 'name="action" value="save"' in rendered
    assert 'name="action" value="remove"' in rendered
    assert 'name="expected_version" value="4"' in rendered
    assert 'name="identity_day" value="2026-09-02"' in rendered
    assert 'max="2026-09-02"' in rendered
    assert "raw-event-must-not-render" not in rendered
    current_matches = rendered.split("Current matches", 1)[1]
    assert '<option value="">Choose an active employee</option>' in current_matches
    assert '<option value="" selected>' not in current_matches


def test_forklift_identity_styles_keep_controls_accessible_and_responsive():
    from pathlib import Path

    css = Path("src/zira_dashboard/static/settings.css").read_text()

    assert ".forklift-identities" in css
    assert ".forklift-identity-list" in css
    assert ".forklift-identity-card" in css
    assert "overflow-wrap: anywhere" in css
    assert "min-height: 44px" in css
    assert ":focus-visible" in css
    assert "@media (max-width: 760px)" in css


@pytest.mark.parametrize(
    ("employee_name", "status_label"),
    (
        ("Former Person", "Inactive employee"),
        ("Hidden Person", "Excluded from Plant Manager"),
        ("Employee no longer available", "Employee no longer available"),
    ),
)
def test_stale_current_mapping_requires_blank_active_employee_choice(
    employee_name, status_label
):
    import re

    from zira_dashboard.deps import templates

    rendered = templates.get_template("_settings_forklift_identities.html").render(
        forklift_identities={
            "day": "2026-09-02",
            "unresolved": (),
            "mappings": ({
                "external_driver_id": "driver-stale",
                "source_name": "Stored source name",
                "employee_odoo_id": 799,
                "employee_name": employee_name,
                "employee_eligible": False,
                "employee_status_label": status_label,
                "version": 6,
                "updated_at": "Sep 1, 10:30 AM",
                "updated_by_upn": "manager@example.com",
            },),
            "employee_options": (
                {"employee_odoo_id": 707, "employee_name": "Active Choice"},
            ),
        },
        today="2026-09-02",
        identity_error="",
        identity_saved=False,
    )
    current_matches = rendered.split("Current matches", 1)[1]
    select = re.search(
        r'<select name="employee_odoo_id" required>(.*?)</select>',
        current_matches,
        re.DOTALL,
    )

    assert select
    assert re.search(
        r'<option value=""\s+selected>Choose an active employee</option>',
        select.group(1),
    )
    assert not re.search(r'<option value="707"[^>]*selected', select.group(1))
    assert employee_name in current_matches
    assert status_label in current_matches
    assert 'name="expected_version" value="6"' in current_matches
    assert 'name="action" value="remove"' in current_matches


def test_forklift_panel_has_capacity_sliders_not_target():
    """Task 7 (capacity-coverage): the panel shows the driver utilization and
    throughput knobs and has retired the SLA 'Target time-to-claim' slider."""
    from zira_dashboard.deps import templates
    rendered = templates.env.from_string(_extract_forklift_section()).render(
        forklift=_stub_forklift_ctx(), saved=False, active_section="forklift")
    # capacity knobs restored to the panel
    assert 'data-field="utilization_pct"' in rendered
    assert 'name="utilization_pct"' in rendered
    assert 'data-field="throughput"' in rendered
    assert 'name="throughput"' in rendered
    assert "Driver utilization" in rendered and "Driver throughput" in rendered
    # SLA target slider retired
    assert 'name="target_claim_seconds"' not in rendered
    assert "Target time-to-claim" not in rendered


def test_forklift_panel_shows_measured_recommendation_line():
    """The panel surfaces the coverage recommendation line with the measured
    recent average time-to-claim (no SLA target / prediction / back-test copy)."""
    from zira_dashboard.deps import templates
    rendered = templates.env.from_string(_extract_forklift_section()).render(
        forklift=_stub_forklift_ctx(), saved=False, active_section="forklift")
    assert "for coverage of" in rendered
    assert "recent avg time-to-claim" in rendered   # observed 174s -> ~2.9 min
    assert "2.9 min" in rendered
    assert "measured" in rendered
    # retired SLA / back-test copy is gone
    assert "back-test" not in rendered.lower()
    assert "under 4 min" not in rendered


def test_forklift_panel_recommendation_pending_when_no_history():
    """No recommendation yet -> the headline shows the pending copy and neither
    the recommendation line nor the demand-volume line render."""
    from zira_dashboard.deps import templates
    ctx = _stub_forklift_ctx(recommended=None, algo_recommended=None)
    ctx["observed_claim_seconds"] = None
    rendered = templates.env.from_string(_extract_forklift_section()).render(
        forklift=ctx, saved=False, active_section="forklift")
    assert "recommendation builds as history accrues" in rendered
    assert "for coverage of" not in rendered


def test_forklift_settings_section_unavailable_still_saves():
    from zira_dashboard.deps import templates
    rendered = templates.env.from_string(_extract_forklift_section()).render(
        forklift={"enabled": True}, saved=False, active_section="forklift")
    # No algo_values -> the "not available" notice shows but the form still saves.
    assert "isn't available right now" in rendered
    assert 'action="/settings/forklift"' in rendered
    assert 'name="enabled"' in rendered
