from fastapi import FastAPI
from fastapi.testclient import TestClient

from zira_dashboard.routes.skills import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_automation_endpoint_saves_and_recalculates(monkeypatch):
    from zira_dashboard.routes import skills

    saved = []
    monkeypatch.setattr(
        skills.automated_skill_settings,
        "save",
        lambda group, settings: saved.append((group, settings)),
    )
    monkeypatch.setattr(
        skills.automated_skills,
        "run_group",
        lambda group, trigger, day: skills.automated_skill_settings.RunSummary(
            group, trigger, 4, 1, 2, 1, (), "2026-07-13T18:00:00+00:00"
        ),
    )

    response = _client().post(
        "/staffing/skills/automation/Repair",
        json={"level_3_min": 91, "level_2_min": 81, "level_1_min": 71},
    )

    assert response.status_code == 200
    assert saved[0][0] == "Repair"
    assert response.json()["summary"]["changed"] == 1


def test_automation_endpoint_rejects_bad_bucket_order():
    response = _client().post(
        "/staffing/skills/automation/Repair",
        json={"level_3_min": 80, "level_2_min": 90, "level_1_min": 70},
    )

    assert response.status_code == 400
    assert response.json()["ok"] is False
