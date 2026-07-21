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
    # Capacity model: 120 calls/hr planned-hour volume -> 10 drivers.
    adv = forklift_advisor.build_advisor(date(2026, 6, 26), scheduled=1, backups=0)
    assert adv["recommended"] == 10
    assert adv["coverage"].status == "short" and adv["coverage"].gap == 9


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


def _bay_model(recommended=6, observed_claim_seconds=250.0, coverage=None,
               peak_label="12:00–13:00", basis="history", n_days=8):
    """Capacity-coverage advisor dict of the shape the route now feeds the bay."""
    return {
        "available": True,
        "day_label": "Sat Jun 27",
        "total_calls": 420,
        "peak_label": peak_label,
        "hours": [(11, 0.5), (12, 1.0)],
        "recommended": recommended,
        "observed_claim_seconds": observed_claim_seconds,
        "coverage": coverage,
        "basis": basis,
        "n_days": n_days,
        "backup_names": [],
    }


def test_forklift_bay_cell_renders_coverage_badge():
    """The compact bay summary shows the suggested count and the measured recent
    average claim time, and never the retired SLA strings."""
    from zira_dashboard import forklift_demand
    from zira_dashboard.deps import templates

    coverage = forklift_demand.assess_coverage(
        recommended=6, scheduled=4, backups=0)  # gap == 2 -> short
    model = _bay_model(recommended=6, observed_claim_seconds=250.0, coverage=coverage)
    rendered = templates.env.from_string(_extract_bay_cell()).render(
        bay={"name": "Forklift", "rows": [1, 2], "subtitle": None},
        forklift_advisor=model,
    )
    assert "Forklift" in rendered
    assert "6 suggested" in rendered
    assert "4.2 min" in rendered              # 250 / 60 -> 4.2
    assert "Overloaded" not in rendered
    assert "Predicted Time-to-Claim" not in rendered


def test_forklift_bay_cell_status_reflects_coverage_gap():
    from zira_dashboard import forklift_demand
    from zira_dashboard.deps import templates

    def _render(coverage, recommended):
        return templates.env.from_string(_extract_bay_cell()).render(
            bay={"name": "Forklift", "rows": [1, 2], "subtitle": None},
            forklift_advisor=_bay_model(recommended=recommended, coverage=coverage),
        )

    ok = forklift_demand.assess_coverage(recommended=6, scheduled=6, backups=0)  # gap 0
    assert "forklift-bay-summary ok" in _render(ok, 6)

    warn = forklift_demand.assess_coverage(recommended=4, scheduled=3, backups=0)  # gap 1
    rendered_warn = _render(warn, 4)
    assert "forklift-bay-summary warn" in rendered_warn
    assert "Overloaded" not in rendered_warn

    danger = forklift_demand.assess_coverage(recommended=4, scheduled=1, backups=0)  # gap 3
    assert "forklift-bay-summary danger" in _render(danger, 4)


def test_forklift_bay_cell_claim_time_building_when_no_observation():
    """No measured claim time yet -> the bay shows the fallback, not a number."""
    from zira_dashboard import forklift_demand
    from zira_dashboard.deps import templates

    coverage = forklift_demand.assess_coverage(recommended=6, scheduled=6, backups=0)
    model = _bay_model(recommended=6, observed_claim_seconds=None, coverage=coverage)
    rendered = templates.env.from_string(_extract_bay_cell()).render(
        bay={"name": "Forklift", "rows": [1, 2], "subtitle": None},
        forklift_advisor=model,
    )
    assert "6 suggested" in rendered
    assert "claim time building" in rendered
    assert "min" not in rendered.split("claim time building")[1]


def test_forklift_bay_cell_coverage_building_without_recommendation():
    """No recommendation yet -> the bay degrades to the history-accruing copy."""
    from zira_dashboard.deps import templates

    model = _bay_model(recommended=None, observed_claim_seconds=None, coverage=None)
    rendered = templates.env.from_string(_extract_bay_cell()).render(
        bay={"name": "Forklift", "rows": [1, 2], "subtitle": None},
        forklift_advisor=model,
    )
    assert "coverage building" in rendered
    assert "history accruing" in rendered
    assert "suggested" not in rendered
    # gap defaults to 2 when coverage is absent -> danger severity.
    assert "forklift-bay-summary danger" in rendered


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
