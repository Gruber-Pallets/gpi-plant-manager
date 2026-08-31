from zira_dashboard import inbox_keys


def test_breakdown_key_without_person():
    assert inbox_keys.breakdown("Dismantler 2", "2026-07-08T18:02:00+00:00") == \
        "breakdown:Dismantler 2:2026-07-08T18:02:00+00:00"


def test_breakdown_key_with_person():
    assert inbox_keys.breakdown("Dismantler 2", "2026-07-08T18:02:00+00:00", "Juan") == \
        "breakdown:Dismantler 2:2026-07-08T18:02:00+00:00:Juan"


def test_breakdown_key_uses_durable_employee_identity_for_same_display_name():
    one = inbox_keys.breakdown(
        "Dismantler 2", "2026-07-08T18:02:00+00:00", "Alex", 101
    )
    two = inbox_keys.breakdown(
        "Dismantler 2", "2026-07-08T18:02:00+00:00", "Alex", 202
    )

    assert one == "breakdown:Dismantler 2:2026-07-08T18:02:00+00:00:odoo:101"
    assert two == "breakdown:Dismantler 2:2026-07-08T18:02:00+00:00:odoo:202"
