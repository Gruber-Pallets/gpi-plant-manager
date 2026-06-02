"""Pure-logic tests for the retro WC attribution extension to
``attribute_for_day``. No DB or network needed."""

from zira_dashboard.production_history import attribute_for_day


def test_attribute_for_day_includes_extras_for_unscheduled_wc():
    assignments = {"Forklift": ["Lauro"]}
    extra = {"Dismantler 3": ["Lauro"]}
    wc_totals = {"Forklift": (10, 0), "Dismantler 3": (7, 0)}
    out = attribute_for_day(assignments, wc_totals, 480, extra_assignments=extra)
    assert out["Lauro"]["Forklift"]["units"] == 10
    assert out["Lauro"]["Dismantler 3"]["units"] == 7


def test_attribute_for_day_extras_skipped_when_wc_already_scheduled():
    assignments = {"Repair 1": ["Iban"]}
    extra = {"Repair 1": ["Lauro"]}  # should be ignored — Repair 1 is scheduled
    wc_totals = {"Repair 1": (12, 0)}
    out = attribute_for_day(assignments, wc_totals, 480, extra_assignments=extra)
    assert out["Iban"]["Repair 1"]["units"] == 12
    assert "Lauro" not in out


def test_attribute_for_day_extras_split_among_multiple_attributions():
    assignments = {}
    extra = {"Dismantler 3": ["Lauro", "Iban"]}
    wc_totals = {"Dismantler 3": (10, 0)}
    out = attribute_for_day(assignments, wc_totals, 480, extra_assignments=extra)
    assert out["Lauro"]["Dismantler 3"]["units"] == 5
    assert out["Iban"]["Dismantler 3"]["units"] == 5


def test_attribute_for_day_no_extras_argument_unchanged():
    """Backward-compat: not passing extra_assignments should behave like before."""
    assignments = {"Forklift": ["Lauro"]}
    wc_totals = {"Forklift": (8, 0)}
    out = attribute_for_day(assignments, wc_totals, 480)
    assert out["Lauro"]["Forklift"]["units"] == 8


def test_creditable_for_day_excludes_testing_rows(monkeypatch):
    """creditable_for_day drops source='testing' rows so a no-credit testing
    window never feeds a credited operator OR a dashboard goal."""
    from zira_dashboard import wc_attributions
    rows = [
        {"id": 1, "wc_name": "Dismantler 4", "person_name": "Eulogio",
         "start_utc": None, "end_utc": None, "source": "manual"},
        {"id": 2, "wc_name": "Dismantler 4", "person_name": wc_attributions.TESTING_PERSON,
         "start_utc": None, "end_utc": None, "source": wc_attributions.TESTING_SOURCE},
    ]
    monkeypatch.setattr(wc_attributions, "for_day", lambda day: rows)
    out = wc_attributions.creditable_for_day("2026-06-02")
    assert [r["person_name"] for r in out] == ["Eulogio"]
