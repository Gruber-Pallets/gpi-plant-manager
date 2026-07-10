import pytest

from zira_dashboard import object_api, object_models


def test_registry_contains_initial_models():
    reg = object_models.build_registry()
    names = [m["model"] for m in reg.list_models()]
    assert "plant.person" in names
    assert "plant.skill" in names
    assert "plant.person_skill" in names
    assert "plant.work_center" in names
    assert "plant.schedule" in names
    assert "plant.time_off_request" in names


def test_person_model_reads_people_with_skills(monkeypatch):
    queries = []

    def fake_query(sql, params=None):
        queries.append(sql)
        return [
            {
                "id": 1,
                "odoo_id": 10,
                "name": "Dale",
                "active": True,
                "reserve": False,
                "excluded": False,
                "wage_type": "hourly",
                "spanish_speaker": False,
                "skills": {"Repair": 3},
                "departments": ["Recycled"],
            },
        ]

    monkeypatch.setattr(object_models.db, "query", fake_query)
    model = object_models.PersonModel()
    assert model.all_records({})[0]["name"] == "Dale"
    assert model.all_records({})[0]["skills"] == {"Repair": 3}


def test_work_center_model_uses_effective_settings(monkeypatch):
    loc = object_models.staffing.Location("Repair 1", "Repair", "Bay 1", "Recycled", "40721")
    monkeypatch.setattr(object_models.staffing, "LOCATIONS", (loc,))
    monkeypatch.setattr(
        object_models.work_centers_store,
        "effective",
        lambda l: {
            "goal_per_day": 50,
            "min_ops": 1,
            "max_ops": 2,
            "required_skills": ["Repair"],
            "note": "",
            "groups": ["A"],
            "department": "Recycled",
            "default_people": ["Dale"],
        },
    )
    row = object_models.WorkCenterModel().all_records({})[0]
    assert row["id"] == "Repair 1"
    assert row["required_skills"] == ["Repair"]


def test_schedule_model_create_saves_schedule(monkeypatch):
    saved = {}
    monkeypatch.setattr(
        object_models.staffing,
        "load_schedule",
        lambda day: object_models.staffing.Schedule(
            day=day,
            assignments={},
            rotation_mode="training",
            assignment_sources={"Repair 1": {"Dale": "manual"}},
        ),
    )
    monkeypatch.setattr(
        object_models.staffing,
        "save_schedule",
        lambda sched: saved.setdefault("schedule", sched),
    )
    new_id = object_models.ScheduleModel().create_record(
        {
            "day": "2026-07-06",
            "assignments": {"Repair 1": ["Dale"]},
            "notes": "note",
            "testing_day": True,
        },
        {},
    )
    assert new_id == "2026-07-06"
    assert saved["schedule"].assignments == {"Repair 1": ["Dale"]}
    assert saved["schedule"].testing_day is True
    assert saved["schedule"].rotation_mode == "training"
    assert saved["schedule"].assignment_sources == {"Repair 1": {"Dale": "manual"}}


def test_skill_model_reads_skill_definitions(monkeypatch):
    monkeypatch.setattr(
        object_models.db,
        "query",
        lambda sql, params=None: [
            {
                "id": 2,
                "odoo_id": 55,
                "name": "Repair",
                "skill_type": "Production Skills",
                "sort_order": 10,
            }
        ],
    )
    row = object_models.SkillModel().all_records({})[0]
    assert row["name"] == "Repair"
    assert row["skill_type"] == "Production Skills"


def test_person_skill_model_create_upserts_by_names(monkeypatch):
    queries = []
    executed = []

    def fake_query(sql, params=None):
        queries.append((sql, params))
        if "FROM people" in sql:
            return [{"id": 1, "name": "Dale"}]
        if "FROM skills" in sql:
            return [{"id": 2, "name": "Repair"}]
        return []

    class FakeCursor:
        def execute(self, sql, params=None):
            executed.append((sql, params))

    class FakeCursorContext:
        def __enter__(self):
            return FakeCursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(object_models.db, "query", fake_query)
    monkeypatch.setattr(object_models.db, "cursor", lambda: FakeCursorContext())
    monkeypatch.setattr(object_models.staffing, "_invalidate_roster_cache", lambda: None)

    new_id = object_models.PersonSkillModel().create_record(
        {"person_name": "Dale", "skill_name": "Repair", "level": 3},
        {},
    )

    assert new_id == "1:2"
    assert any("INSERT INTO person_skills" in sql for sql, _params in executed)


def test_person_skill_model_write_zero_deletes(monkeypatch):
    executed = []

    class FakeCursor:
        def execute(self, sql, params=None):
            executed.append((sql, params))

    class FakeCursorContext:
        def __enter__(self):
            return FakeCursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(object_models.db, "cursor", lambda: FakeCursorContext())
    monkeypatch.setattr(object_models.staffing, "_invalidate_roster_cache", lambda: None)

    assert object_models.PersonSkillModel().write_records(["1:2"], {"level": 0}, {}) is True
    assert any("DELETE FROM person_skills" in sql for sql, _params in executed)


def test_person_skill_model_write_rejects_relation_move():
    with pytest.raises(object_api.ObjectAPIError):
        object_models.PersonSkillModel().write_records(["1:2"], {"person_id": 9}, {})
