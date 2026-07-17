from zira_dashboard import odoo_sync


def test_roster_names_abbreviate_each_unique_last_name():
    labels = odoo_sync._roster_names([
        {"id": 1, "name": "Porfirio Cazares"},
        {"id": 2, "name": "Lauro Benitez"},
        {"id": 3, "name": "SingleName"},
    ])

    assert labels == {1: "Porfirio C.", 2: "Lauro B.", 3: "SingleName"}


def test_roster_names_expand_surname_only_for_matching_first_and_initial():
    labels = odoo_sync._roster_names([
        {"id": 1, "name": "Jesus Martinez"},
        {"id": 2, "name": "Jesus Morales"},
        {"id": 3, "name": "Carlos Jimenez"},
    ])

    assert labels == {1: "Jesus Ma.", 2: "Jesus Mo.", 3: "Carlos J."}


def test_roster_names_use_later_tokens_then_id_for_unresolved_collisions():
    labels = odoo_sync._roster_names([
        {"id": 7, "name": "Juan Garcia Lopez"},
        {"id": 8, "name": "Juan Garcia Martinez"},
        {"id": 9, "name": "Juan Garcia Lopez"},
    ])

    assert labels == {
        7: "Juan Garcia L. #7",
        8: "Juan Garcia M.",
        9: "Juan Garcia L. #9",
    }


def test_merge_legacy_skill_into_stable_moves_dependencies_before_delete():
    calls = []

    class FakeCursor:
        def execute(self, sql, params=None):
            calls.append((" ".join(sql.split()), params))

    odoo_sync._merge_legacy_skill_into_stable(
        FakeCursor(),
        stable_skill_id=10,
        legacy_skill_id=20,
    )

    assert "DELETE FROM person_skills" in calls[0][0]
    assert "INSERT INTO person_skills" in calls[0][0]
    assert calls[0][1] == (20, 10)
    assert "INSERT INTO work_center_required_skills" in calls[1][0]
    assert calls[1][1] == (10, 20)
    assert calls[2] == (
        "DELETE FROM work_center_required_skills WHERE skill_id = %s",
        (20,),
    )
    assert calls[3] == ("DELETE FROM skills WHERE id = %s", (20,))
