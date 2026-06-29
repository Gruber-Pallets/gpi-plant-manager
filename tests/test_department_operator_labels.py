from datetime import date


def test_who_by_wc_excludes_absent_people_from_schedule_and_attributions(monkeypatch):
    from zira_dashboard.routes import departments
    from zira_dashboard import wc_attributions

    day = date(2026, 6, 29)
    monkeypatch.setattr(
        wc_attributions,
        "people_by_wc",
        lambda d: {"Repair 1": ["Bob", "Cara"], "Repair 3": ["Bob"]},
    )

    out = departments._who_by_wc(
        {
            "Repair 1": ["Ana", "Bob"],
            "Repair 2": ["Bob"],
        },
        day,
        absent_names={"Bob"},
    )

    assert out == {"Repair 1": "Ana + Cara"}
