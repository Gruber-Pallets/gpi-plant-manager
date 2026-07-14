from types import SimpleNamespace

from scripts.replay_schedule_solver import serialize_replay


def test_serialize_replay_reports_complete_placement_and_defaults():
    issue = SimpleNamespace(to_dict=lambda: {
        "code": "person_no_enabled_qualified_center",
        "person": "Gerardo Garcia",
    })
    suggestion = SimpleNamespace(
        complete=False,
        available_people=("Gerardo Garcia", "Jose Ochoa"),
        placed_people=("Jose Ochoa",),
        unused_people=("Gerardo Garcia",),
        default_assignments={"Jose Ochoa": "group:Repair"},
        placement_issues=(issue,),
    )

    assert serialize_replay("2026-07-15", suggestion, 12.3456) == {
        "day": "2026-07-15",
        "complete": False,
        "available_people": ["Gerardo Garcia", "Jose Ochoa"],
        "placed_people": ["Jose Ochoa"],
        "unplaced_people": ["Gerardo Garcia"],
        "default_assignments": {"Jose Ochoa": "group:Repair"},
        "issues": [{
            "code": "person_no_enabled_qualified_center",
            "person": "Gerardo Garcia",
        }],
        "elapsed_ms": 12.346,
    }
