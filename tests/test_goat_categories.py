from datetime import date
from types import SimpleNamespace

from zira_dashboard import awards, goat_categories


def test_categories_match_the_approved_groups_and_auto_activation(monkeypatch):
    locations = (
        SimpleNamespace(name="Repair 1", skill="Repair", meter_id="r1"),
        SimpleNamespace(name="Dismantler 1", skill="Dismantler", meter_id="d1"),
        SimpleNamespace(name="Junior #2", skill="Junior", meter_id="j2"),
        SimpleNamespace(name="Woodpecker #1", skill="Woodpecker", meter_id=None),
        SimpleNamespace(name="Hand Build #1", skill="Hand Build", meter_id=None),
        SimpleNamespace(name="Big Build #1", skill="Hand Build", meter_id=None),
    )
    monkeypatch.setattr(goat_categories.staffing, "LOCATIONS", locations)
    monkeypatch.setattr(
        goat_categories.work_centers_store,
        "members",
        lambda kind, name: {("group", "Repairs"): [locations[0]], ("group", "Dismantlers"): [locations[1]]}[(kind, name)],
    )

    categories = goat_categories.all_categories()

    assert [category.key for category in categories] == ["repairs", "dismantlers", "juniors", "woodpecker", "hand_build"]
    assert [category.label for category in categories] == ["Repairs", "Dismantlers", "Juniors", "Woodpecker", "Hand Build"]
    assert [goat_categories.has_metered_source(category) for category in categories] == [True, True, True, False, False]
    assert goat_categories.work_center_names(categories[-1]) == {"Hand Build #1", "Big Build #1"}


def test_best_person_day_uses_record_tie_breaks():
    rows = [
        {"name": "Zoe", "day": date(2026, 7, 28), "units": 100, "hours": 7},
        {"name": "Amy", "day": date(2026, 7, 29), "units": 100, "hours": 7},
        {"name": "Bob", "day": date(2026, 7, 28), "units": 100, "hours": 7},
    ]

    assert awards.best_person_day(rows) == {
        "name": "Bob", "day": date(2026, 7, 28), "units": 100, "pph": 14.3,
    }
