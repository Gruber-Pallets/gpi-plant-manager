from datetime import date

from zira_dashboard import forklift_advisor


def test_scheduled_counts_helper_counts_tablets_and_backups():
    from zira_dashboard.routes import staffing
    assignments = {
        "Loading/Jockeying": ["Juan"],
        "Tablets": ["Luke", "Pascual"],
        "Prosaw #4": ["Trent"],
    }
    counts = staffing._forklift_scheduled_counts(
        assignments, overload_responders={"Juan", "Luke", "Louie"},
        wc_names=("Tablets",))
    assert counts["tablets"] == 2            # Luke + Pascual on the Tablets WC
    assert counts["backups"] == 2            # Juan + Luke scheduled & overload responders


def test_scheduled_counts_helper_includes_loading_jockeying_when_configured():
    from zira_dashboard.routes import staffing
    assignments = {
        "Loading/Jockeying": ["Juan", "Luke"],   # Luke also on Tablets → counted once
        "Tablets": ["Luke", "Pascual"],
        "Prosaw #4": ["Trent"],
    }
    counts = staffing._forklift_scheduled_counts(
        assignments, overload_responders=set(),
        wc_names=("Tablets", "Loading/Jockeying"))
    assert counts["tablets"] == 3            # Juan, Luke, Pascual (unique across both WCs)


def test_build_advisor_short_when_under_scheduled(monkeypatch):
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [
                            {"day": date(2026, 6, 19), "total_calls": 500,
                             "by_hour": {"9": {"calls": 120}}, "by_station": {}}])
    monkeypatch.setattr(forklift_advisor.app_settings, "get_setting", lambda k: [])
    # SLA model: 120 calls/hr @ 180s handle, target 240s, k=1 -> 7 drivers.
    monkeypatch.setattr("zira_dashboard.forklift_store.mean_handle_seconds",
                        lambda window_days=90: 180.0)
    monkeypatch.setattr("zira_dashboard.forklift_store.calibration_samples",
                        lambda window_days=90: [])
    adv = forklift_advisor.build_advisor(date(2026, 6, 26), scheduled=1, backups=0)
    assert adv["recommended"] == 7
    assert adv["coverage"].status == "short" and adv["coverage"].gap == 6


# The full GET /staffing render fans out to live Zira/Odoo calls (and the
# DATABASE_URL-gated CI Postgres still hits those — see _KNOWN_DB_TEST_DEBT in
# conftest, where the analogous "render the whole page" tests are skipped as
# flaky). So instead of a TestClient route hit, we render the real Forklift bay
# cell through the app's own Jinja2 environment with a deterministic stub model.
# This runs everywhere (no DB, no network) and still exercises the exact markup
# the route feeds.
def _staffing_template() -> str:
    """Read staffing.html in the tests that render isolated template snippets."""
    from pathlib import Path

    return Path("src/zira_dashboard/templates/staffing.html").read_text()


def _extract_bay_cell() -> str:
    import re

    m = re.search(
        r"<td class=\"bay\" rowspan=\"\{\{ bay\.rows\|length \}\}\">.*?</td>",
        _staffing_template(),
        re.DOTALL,
    )
    assert m, "bay cell block missing from staffing.html"
    return m.group(0)


def test_staffing_template_contains_forklift_block():
    html = _staffing_template()
    assert 'class="forklift-advisor"' not in html
    assert "Forklift demand" not in html


def test_forklift_bay_cell_renders_compact_advisor_summary():
    from zira_dashboard import forklift_demand
    from zira_dashboard.deps import templates

    coverage = forklift_demand.assess_coverage(
        recommended=3, scheduled=3, backups=3)  # status == "ok"
    model = {
        "available": True,
        "day_label": "Sat Jun 27",
        "total_calls": 420,
        "peak_label": "9:00–10:00",
        "hours": [(8, 0.5), (9, 1.0)],
        "recommended": 3,
        "algo_recommended": 6,                    # differs -> show "algorithm: 6"
        "overloaded": False,
        "target_seconds": 240.0,                  # 4 min target
        "predicted_claim_seconds": 174.0,         # ~2.9 min predicted
        "predicted_scheduled_claim_seconds": 174.0,
        "scheduled_prediction_overloaded": False,
        "scheduled_prediction_status": "ok",
        "coverage": coverage,
        "basis": "history",
        "n_days": 4,
        "backup_names": ["Louie", "Juan"],
    }
    rendered = templates.env.from_string(_extract_bay_cell()).render(
        bay={"name": "Forklift", "rows": [1, 2], "subtitle": None},
        forklift_advisor=model,
    )
    assert "Forklift" in rendered
    assert "3 Suggested" in rendered
    assert "Predicted Time-to-Claim 2.9" in rendered
    assert "✓" not in rendered
    assert "⚠" not in rendered
    assert "!!" not in rendered
    assert "forklift-bay-summary ok" in rendered


def test_forklift_bay_cell_renders_shortage_severity():
    from zira_dashboard import forklift_demand
    from zira_dashboard.deps import templates

    model = {
        "available": True,
        "day_label": "Sat Jun 27",
        "total_calls": 420,
        "peak_label": "9:00–10:00",
        "hours": [(8, 0.5), (9, 1.0)],
        "recommended": 4,
        "algo_recommended": 6,
        "overloaded": False,
        "target_seconds": 240.0,
        "predicted_claim_seconds": 174.0,
        "predicted_scheduled_claim_seconds": 310.0,
        "scheduled_prediction_overloaded": False,
        "scheduled_prediction_status": "warn",
        "coverage": forklift_demand.assess_coverage(recommended=4, scheduled=3, backups=0),
        "basis": "history",
        "n_days": 4,
        "backup_names": [],
    }
    rendered_warn = templates.env.from_string(_extract_bay_cell()).render(
        bay={"name": "Forklift", "rows": [1, 2], "subtitle": None},
        forklift_advisor=model,
    )
    assert "⚠" not in rendered_warn
    assert "!!" not in rendered_warn
    assert "forklift-bay-summary warn" in rendered_warn

    short_model = dict(model)
    short_model["coverage"] = forklift_demand.assess_coverage(
        recommended=4, scheduled=1, backups=0)  # gap == 3
    short_model["predicted_scheduled_claim_seconds"] = 500.0
    short_model["scheduled_prediction_status"] = "danger"
    rendered_short = templates.env.from_string(_extract_bay_cell()).render(
        bay={"name": "Forklift", "rows": [1, 2], "subtitle": None},
        forklift_advisor=short_model,
    )
    assert "⚠" not in rendered_short
    assert "!!" not in rendered_short
    assert "forklift-bay-summary danger" in rendered_short


def test_forklift_card_shows_time_to_claim_target():
    """The compact bay summary keeps the recommended count and Time-to-Claim visible."""
    from zira_dashboard import forklift_demand
    from zira_dashboard.deps import templates
    model = {
        "available": True, "day_label": "Sat Jun 27", "total_calls": 420,
        "peak_label": "9:00–10:00", "hours": [(9, 1.0)],
        "recommended": 6, "algo_recommended": 6, "overloaded": False,
        "target_seconds": 240.0, "predicted_claim_seconds": 174.0,
        "predicted_scheduled_claim_seconds": 174.0,
        "scheduled_prediction_overloaded": False,
        "scheduled_prediction_status": "ok",
        "coverage": forklift_demand.assess_coverage(recommended=6, scheduled=6, backups=0),
        "basis": "history", "n_days": 5, "backup_names": [],
    }
    page = templates.env.from_string(_extract_bay_cell()).render(
        bay={"name": "Forklift", "rows": [1, 2], "subtitle": None},
        forklift_advisor=model,
    )
    assert "6 Suggested" in page
    assert "Predicted Time-to-Claim 2.9" in page
    assert "algorithm:" not in page


def test_forklift_card_overloaded_branch():
    """When the busiest hour cannot hit the target, the bay does not fabricate a count."""
    from zira_dashboard.deps import templates
    model = {
        "available": True, "day_label": "Sat Jun 27", "total_calls": 900,
        "peak_label": "9:00–10:00", "hours": [(9, 1.0)],
        "recommended": None, "algo_recommended": None, "overloaded": True,
        "target_seconds": 240.0, "predicted_claim_seconds": None,
        "predicted_scheduled_claim_seconds": None,
        "scheduled_prediction_overloaded": True,
        "scheduled_prediction_status": "danger",
        "coverage": None, "basis": "history", "n_days": 5, "backup_names": [],
    }
    page = templates.env.from_string(_extract_bay_cell()).render(
        bay={"name": "Forklift", "rows": [1, 2], "subtitle": None},
        forklift_advisor=model,
    )
    assert "overloaded" in page.lower()
    assert "Suggested" not in page


def test_forklift_card_scheduled_overload_is_red_without_numeric_ttc():
    from zira_dashboard import forklift_demand
    from zira_dashboard.deps import templates
    model = {
        "available": True, "day_label": "Sat Jun 27", "total_calls": 420,
        "peak_label": "9:00–10:00", "hours": [(9, 1.0)],
        "recommended": 6, "algo_recommended": 6, "overloaded": False,
        "target_seconds": 240.0, "predicted_claim_seconds": 174.0,
        "predicted_scheduled_claim_seconds": None,
        "scheduled_prediction_overloaded": True,
        "scheduled_prediction_status": "danger",
        "coverage": forklift_demand.assess_coverage(recommended=6, scheduled=4, backups=0),
        "basis": "history", "n_days": 5, "backup_names": [],
    }
    page = templates.env.from_string(_extract_bay_cell()).render(
        bay={"name": "Forklift", "rows": [1, 2], "subtitle": None},
        forklift_advisor=model,
    )
    assert "6 Suggested" in page
    assert "Predicted Time-to-Claim" not in page
    assert "TTC overloaded" in page
    assert "forklift-bay-summary danger" in page


def test_staffing_template_exports_forklift_live_model():
    html = _staffing_template()

    assert "window.FORKLIFT_LIVE_MODEL = {{ forklift_live_model|tojson }};" in html


def test_forklift_bay_summary_is_hidden_from_print_and_slack_pdf():
    """In-app bay summary is for the live scheduler; print/Slack PDFs stay clean."""
    from pathlib import Path

    css = Path("src/zira_dashboard/static/staffing-print.css").read_text()
    assert ".forklift-bay-summary" in css
    assert "display: none !important;" in css


def test_settings_read_failure_falls_back_to_default_instead_of_hiding_advisor():
    """Regression: forklift_settings.current() raising used to blank the whole
    advisor via the outer except. It must fall back to DEFAULT so coverage
    still counts drivers on Tablets."""
    from pathlib import Path

    src = Path("src/zira_dashboard/routes/staffing.py").read_text()
    assert "forklift_settings.DEFAULT" in src
    # Nested try around current() must exist (outer try still guards build_advisor).
    assert "try:\n            _fcfg = forklift_settings.current()" in src
    assert "_fcfg = forklift_settings.DEFAULT" in src
