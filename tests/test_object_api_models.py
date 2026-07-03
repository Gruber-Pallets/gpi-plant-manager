from zira_dashboard import object_models


def test_registry_contains_initial_models():
    reg = object_models.build_registry()
    names = [m["model"] for m in reg.list_models()]
    assert "plant.person" in names
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
        lambda day: object_models.staffing.Schedule(day=day, assignments={}),
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
