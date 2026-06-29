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
    adv = forklift_advisor.build_advisor(date(2026, 6, 26), scheduled=1, backups=0)
    assert adv["recommended"] == 12 and adv["coverage"].status == "short" and adv["coverage"].gap == 11


# The full GET /staffing render fans out to live Zira/Odoo calls (and the
# DATABASE_URL-gated CI Postgres still hits those — see _KNOWN_DB_TEST_DEBT in
# conftest, where the analogous "render the whole page" tests are skipped as
# flaky). So instead of a TestClient route hit, we render the real
# forklift-advisor template block through the app's own Jinja2 environment with
# a deterministic stub model. This runs everywhere (no DB, no network) and still
# exercises the exact markup the route feeds. (Substitution per Task 10 Step 7.)
def _extract_forklift_block() -> str:
    """Pull the `forklift-advisor` {% if %}...{% endif %} block out of
    staffing.html so we render exactly the markup that ships, in isolation
    from the page's hundreds of other context variables."""
    import re
    from pathlib import Path
    html = Path("src/zira_dashboard/templates/staffing.html").read_text()
    # The block nests several inner {% if %}...{% endif %} tags, so match
    # greedily from the outer guard to the LAST {% endif %} that immediately
    # precedes the aside's closing tag (the outer block's endif).
    m = re.search(
        r"\{%\s*if forklift_advisor and forklift_advisor\.available\s*%\}"
        r".*\{%\s*endif\s*%\}(?=\s*</aside>)",
        html,
        re.DOTALL,
    )
    assert m, "forklift-advisor block missing from staffing.html"
    return m.group(0)


def test_staffing_template_contains_forklift_block():
    block = _extract_forklift_block()
    assert 'class="forklift-advisor"' in block
    assert "Forklift demand" in block


def test_forklift_block_renders_card_from_advisor_model():
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
        "coverage": coverage,
        "basis": "history",
        "n_days": 4,
        "backup_names": ["Louie", "Juan"],
    }
    rendered = templates.env.from_string(_extract_forklift_block()).render(
        forklift_advisor=model)
    assert "Forklift demand" in rendered
    assert "Recommend 3 dedicated" in rendered
    assert "algorithm: 6" in rendered         # discreet baseline beside the rec
    assert "Coverage OK" in rendered          # status == "ok" branch
    assert "based on 4 recent Sats" in rendered   # history-basis footer
    assert "backups: Louie, Juan" in rendered

    # And the short-coverage branch renders the gap message.
    short_model = dict(model)
    short_model["coverage"] = forklift_demand.assess_coverage(
        recommended=4, scheduled=1, backups=0)  # gap == 3
    short_model["recommended"] = 4
    rendered_short = templates.env.from_string(_extract_forklift_block()).render(
        forklift_advisor=short_model)
    assert "Recommend 4 dedicated" in rendered_short
    assert "Short 3 — 1 on Tablets of 4" in rendered_short
