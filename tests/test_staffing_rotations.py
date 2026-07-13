"""Task 4 — rotation JSON APIs + Staffing recommendation wiring.

DB-free route/orchestration tests. The FastAPI endpoints are exercised through
a TestClient with only ``rotations.router`` mounted; all DB and store calls are
monkeypatched. The Staffing orchestration helpers are called directly with the
recommendation inputs stubbed, so nothing here touches Postgres or the clock.
"""

from __future__ import annotations

from datetime import date, time, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zira_dashboard import staffing
from zira_dashboard.auto_schedule_capacity import AutoCapacity


TARGET_DAY = date(2026, 7, 14)

ROOT = Path(__file__).resolve().parents[1]


def _person(name: str, level: int, group: str = "Repair", *, active: bool = True, reserve: bool = False):
    return staffing.Person(name=name, active=active, reserve=reserve, skills={group: level})


def _rotations_client(monkeypatch, *, raise_server_exceptions: bool = True):
    from zira_dashboard.routes import rotations

    app = FastAPI()
    app.include_router(rotations.router)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions), rotations


# --------------------------------------------------------------------------- #
# POST /api/rotations/preferences
# --------------------------------------------------------------------------- #


def test_preference_endpoint_saves_valid(monkeypatch):
    from zira_dashboard import rotation_store

    client, rotations = _rotations_client(monkeypatch)
    saved: dict = {}

    monkeypatch.setattr(
        rotations.db, "query",
        lambda sql, params=None: [{"id": 7}] if "FROM people" in sql else [],
    )
    monkeypatch.setattr(
        rotations.staffing, "load_roster",
        lambda: [staffing.Person("Alex", skills={"Repair": 1})],
    )

    def fake_save(person_id, group, preference):
        saved["args"] = (person_id, group, preference)
        return rotation_store.RotationPreference(
            person_id=person_id, rotation_group=group, preference=preference
        )

    monkeypatch.setattr(rotations.rotation_store, "save_preference", fake_save)
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: None)

    resp = client.post(
        "/api/rotations/preferences",
        json={"person": "Alex", "group": "Repair", "preference": "primary"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["preference"] == "primary"
    assert saved["args"] == (7, "Repair", "primary")


def test_preference_endpoint_rejects_unqualified_target(monkeypatch):
    from zira_dashboard import rotation_store

    client, rotations = _rotations_client(monkeypatch)
    monkeypatch.setattr(rotations.db, "query", lambda sql, params=None: [{"id": 7}])
    monkeypatch.setattr(
        rotations.staffing, "load_roster",
        lambda: [staffing.Person("Alex", skills={"Repair": 0})],
    )
    monkeypatch.setattr(
        rotations.rotation_store,
        "save_preference",
        lambda person_id, group, preference: rotation_store.RotationPreference(
            person_id, group, preference
        ),
    )

    resp = client.post(
        "/api/rotations/preferences",
        json={"person": "Alex", "group": "Repair", "preference": "primary"},
    )

    assert resp.status_code == 422
    assert "qualified" in resp.json()["error"]


def test_preference_endpoint_preserves_invalid_target_validation(monkeypatch):
    from zira_dashboard import rotation_store

    client, rotations = _rotations_client(monkeypatch)
    monkeypatch.setattr(
        rotations.db, "query",
        lambda sql, params=None: [{"id": 7}] if "FROM people" in sql else [],
    )
    monkeypatch.setattr(
        rotations.staffing, "load_roster",
        lambda: [staffing.Person("Alex", skills={"Repair": 1})],
    )

    def boom(person_id, group, preference):
        raise rotation_store.InvalidRotationPreference(
            f"Unknown rotation group: {group!r}"
        )

    monkeypatch.setattr(rotations.rotation_store, "save_preference", boom)

    resp = client.post(
        "/api/rotations/preferences",
        json={"person": "Alex", "group": "Not a target", "preference": "primary"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"] == "Unknown rotation group: 'Not a target'"


def test_preference_endpoint_unknown_person_422(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    monkeypatch.setattr(rotations.db, "query", lambda sql, params=None: [])

    resp = client.post(
        "/api/rotations/preferences",
        json={"person": "Ghost", "group": "Repair", "preference": "primary"},
    )

    assert resp.status_code == 422
    assert resp.json()["ok"] is False
    assert "Ghost" in resp.json()["error"]


def test_preference_endpoint_invalid_preference_422(monkeypatch):
    from zira_dashboard import rotation_store

    client, rotations = _rotations_client(monkeypatch)
    monkeypatch.setattr(
        rotations.db, "query",
        lambda sql, params=None: [{"id": 7}] if "FROM people" in sql else [],
    )
    monkeypatch.setattr(
        rotations.staffing, "load_roster",
        lambda: [staffing.Person("Alex", skills={"Repair": 1})],
    )

    def boom(person_id, group, preference):
        raise rotation_store.InvalidRotationPreference("Unknown rotation preference: 'sometimes'")

    monkeypatch.setattr(rotations.rotation_store, "save_preference", boom)

    resp = client.post(
        "/api/rotations/preferences",
        json={"person": "Alex", "group": "Repair", "preference": "sometimes"},
    )

    assert resp.status_code == 422
    assert resp.json()["ok"] is False
    assert "preference" in resp.json()["error"]


# --------------------------------------------------------------------------- #
# POST /api/rotations/training-blocks
# --------------------------------------------------------------------------- #


def _training_block_query(monkeypatch, rotations, *, trainee_level: int, trainer_level: int):
    """Wire db.query so the endpoint resolves ids and rotation_store.create_block
    reads the given skill levels."""

    def fake_query(sql, params=None):
        if "FROM people" in sql:
            name = params[0]
            return [{"id": 100 + len(name)}]  # deterministic, positive
        if sql.strip().startswith("SELECT id FROM skills"):
            return [{"id": 9}]
        if "FROM skills WHERE id" in sql:
            return [{"name": "Repair"}]
        if "trainee_level" in sql:
            return [{"trainee_level": trainee_level, "trainer_level": trainer_level}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(rotations.db, "query", fake_query)
    monkeypatch.setattr(rotations.rotation_store.db, "query", fake_query)
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: None)


def test_training_block_endpoint_rejects_invalid_trainer(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    _training_block_query(monkeypatch, rotations, trainee_level=0, trainer_level=2)

    # The real rotation_store.create_block validation runs; a non-green trainer
    # raises InvalidTrainingBlock, which the endpoint maps to 422.
    def fake_insert(*a, **k):
        raise AssertionError("insert must not run for a rejected block")

    resp = client.post(
        "/api/rotations/training-blocks",
        json={
            "trainee": "Alex",
            "trainer": "Not Green",
            "group": "Repair",
            "start_day": "2026-07-14",
            "workdays": 5,
        },
    )

    assert resp.status_code == 422
    assert resp.json()["ok"] is False
    assert resp.json()["error"] == "Day-one trainer must be level 3 for the target skill."


def test_training_block_endpoint_success(monkeypatch):
    from zira_dashboard import rotation_store

    client, rotations = _rotations_client(monkeypatch)
    monkeypatch.setattr(
        rotations.db, "query",
        lambda sql, params=None: (
            [{"id": 1}] if "FROM people" in sql
            else [{"id": 9}] if sql.strip().startswith("SELECT id FROM skills")
            else []
        ),
    )
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: None)

    block = rotation_store.TrainingBlock(
        id=42, trainee_name="Alex", trainer_name="Green", skill="Repair",
        start_day=TARGET_DAY, planned_attended_days=5, status="active",
        trainee_id=1, skill_id=9,
    )
    monkeypatch.setattr(rotations.rotation_store, "create_block", lambda **kw: block)

    resp = client.post(
        "/api/rotations/training-blocks",
        json={
            "trainee": "Alex", "trainer": "Green", "group": "Repair",
            "start_day": "2026-07-14", "workdays": 5,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["block"]["id"] == 42
    assert body["block"]["group"] == "Repair"
    assert body["block"]["trainer"] == "Green"


def test_training_block_endpoint_unknown_person_422(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    monkeypatch.setattr(rotations.db, "query", lambda sql, params=None: [])

    resp = client.post(
        "/api/rotations/training-blocks",
        json={
            "trainee": "Ghost", "trainer": "Green", "group": "Repair",
            "start_day": "2026-07-14", "workdays": 5,
        },
    )
    assert resp.status_code == 422
    assert "Ghost" in resp.json()["error"]


def test_training_block_endpoint_unknown_group_422(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)

    def fake_query(sql, params=None):
        if "FROM people" in sql:
            return [{"id": 1}]
        if sql.strip().startswith("SELECT id FROM skills"):
            return []  # unknown skill/group
        return []

    monkeypatch.setattr(rotations.db, "query", fake_query)

    resp = client.post(
        "/api/rotations/training-blocks",
        json={
            "trainee": "Alex", "trainer": "Green", "group": "Nope",
            "start_day": "2026-07-14", "workdays": 5,
        },
    )
    assert resp.status_code == 422
    assert "Nope" in resp.json()["error"]


def test_training_block_endpoint_bad_date_422(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    resp = client.post(
        "/api/rotations/training-blocks",
        json={
            "trainee": "Alex", "trainer": "Green", "group": "Repair",
            "start_day": "not-a-date", "workdays": 5,
        },
    )
    assert resp.status_code == 422
    assert "start day" in resp.json()["error"].lower()


@pytest.mark.parametrize("workdays", [0, -1, True, "5", 2.5])
def test_training_block_endpoint_bad_workdays_422(monkeypatch, workdays):
    client, rotations = _rotations_client(monkeypatch)
    resp = client.post(
        "/api/rotations/training-blocks",
        json={
            "trainee": "Alex", "trainer": "Green", "group": "Repair",
            "start_day": "2026-07-14", "workdays": workdays,
        },
    )
    assert resp.status_code == 422
    assert "workday" in resp.json()["error"].lower()


# --------------------------------------------------------------------------- #
# POST /api/rotations/training-blocks/{id}/{pause,resume,end}
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "action,store_fn,expected_status",
    [
        ("pause", "pause_block", "paused"),
        ("resume", "resume_block", "active"),
        ("end", "end_block", "ended"),
    ],
)
def test_block_lifecycle_endpoint_success(monkeypatch, action, store_fn, expected_status):
    client, rotations = _rotations_client(monkeypatch)
    calls: dict = {}
    monkeypatch.setattr(
        rotations.rotation_store, store_fn,
        lambda block_id: calls.__setitem__("block_id", block_id),
    )
    invalidated: list[str] = []
    monkeypatch.setattr(
        rotations._http_cache, "invalidate_today_cache",
        lambda: invalidated.append("today"),
    )
    monkeypatch.setattr(
        rotations._http_cache, "invalidate_stable_cache",
        lambda: invalidated.append("stable"),
    )

    resp = client.post(f"/api/rotations/training-blocks/42/{action}")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "id": 42, "status": expected_status}
    assert calls["block_id"] == 42
    assert invalidated == ["today", "stable"]


@pytest.mark.parametrize("action", ["pause", "resume", "end"])
def test_block_lifecycle_endpoint_bad_id_422(monkeypatch, action):
    client, rotations = _rotations_client(monkeypatch)

    def boom(block_id):
        raise AssertionError("store must not run for a non-positive id")

    monkeypatch.setattr(rotations.rotation_store, "pause_block", boom)
    monkeypatch.setattr(rotations.rotation_store, "resume_block", boom)
    monkeypatch.setattr(rotations.rotation_store, "end_block", boom)

    resp = client.post(f"/api/rotations/training-blocks/0/{action}")

    assert resp.status_code == 422
    assert resp.json()["ok"] is False
    assert "block_id" in resp.json()["error"]


# --------------------------------------------------------------------------- #
# POST /api/rotations/rebuild
# --------------------------------------------------------------------------- #


def _stub_recommendation_inputs(monkeypatch):
    """Make the shared recommendation gather DB-free and empty so the pure
    engine runs deterministically on the roster/base/locks the test provides."""
    from zira_dashboard.routes import staffing as staffing_route
    from zira_dashboard import rotation_suggestions

    monkeypatch.setattr(staffing_route.rotation_store, "load_preferences_by_name", lambda: {})
    monkeypatch.setattr(
        rotation_suggestions, "_load_recycled_history",
        lambda d, group_locations=None: rotation_suggestions.RecycledHistory(),
    )
    monkeypatch.setattr(staffing_route.rotation_training, "reconcile_blocks", lambda as_of: [])
    monkeypatch.setattr(staffing_route.app_settings, "get_setting", lambda key: ["Repair 1"])
    monkeypatch.setattr(staffing_route.rotation_store, "active_blocks_for_day", lambda d: [])
    monkeypatch.setattr(staffing_route, "_safe_time_off_entries", lambda d: [])
    monkeypatch.setattr(
        staffing_route,
        "_enabled_auto_work_centers",
        lambda d: {"Repair 1", "Repair 2", "Repair 3", "Dismantler 1", "Dismantler 2", "Trim Saw 1"},
    )
    monkeypatch.setattr(staffing_route.work_centers_store, "min_ops", lambda loc: loc.min_ops)
    monkeypatch.setattr(staffing_route.work_centers_store, "default_people", lambda loc: [])
    return staffing_route


def test_rebuild_preserves_manual_assignment(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    _stub_recommendation_inputs(monkeypatch)

    saved: list = []
    sched = staffing.Schedule(
        day=TARGET_DAY,
        assignments={"Repair 1": ["Manual Person"]},
        assignment_sources={"Repair 1": {"Manual Person": "manual"}},
    )
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Manual Person", 3)])
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda d: sched)
    monkeypatch.setattr(rotations.staffing, "save_schedule", lambda s: saved.append(s))
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)

    resp = client.post(
        "/api/rotations/rebuild",
        json={"day": "2026-07-14", "mode": "optimized"},
    )

    assert resp.status_code == 200
    assert resp.json()["assignments"]["Repair 1"] == ["Manual Person"]
    assert resp.json()["sources"]["Repair 1"]["Manual Person"] == "manual"
    assert saved and saved[-1].rotation_mode == "optimized"
    assert saved[-1].assignment_sources["Repair 1"]["Manual Person"] == "manual"


def test_manual_repair_assignment_survives_optimized_rebuild(monkeypatch):
    """End-to-end (Task 7): a manual Repair 1 pick keeps its 'manual' source
    through an optimized rebuild, in both the response and the saved schedule."""
    client, rotations = _rotations_client(monkeypatch)
    _stub_recommendation_inputs(monkeypatch)

    saved: list = []
    sched = staffing.Schedule(
        day=TARGET_DAY,
        assignments={"Repair 1": ["Manual Person"]},
        assignment_sources={"Repair 1": {"Manual Person": "manual"}},
    )
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Manual Person", 3)])
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda d: sched)
    monkeypatch.setattr(rotations.staffing, "save_schedule", lambda s: saved.append(s))
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)

    response = client.post(
        "/api/rotations/rebuild",
        json={"day": "2026-07-14", "mode": "optimized"},
    )

    assert response.status_code == 200
    assert response.json()["sources"]["Repair 1"]["Manual Person"] == "manual"
    assert response.json()["assignments"]["Repair 1"] == ["Manual Person"]
    assert saved and saved[-1].rotation_mode == "optimized"
    assert saved[-1].assignment_sources["Repair 1"]["Manual Person"] == "manual"


def test_rebuild_generates_and_reports_reasons(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    _stub_recommendation_inputs(monkeypatch)

    saved: list = []
    sched = staffing.Schedule(day=TARGET_DAY, assignments={})
    monkeypatch.setattr(
        rotations.staffing, "load_roster",
        lambda: [_person("Green One", 3), _person("Green Two", 3)],
    )
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda d: sched)
    monkeypatch.setattr(rotations.staffing, "save_schedule", lambda s: saved.append(s))
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)

    resp = client.post(
        "/api/rotations/rebuild",
        json={"day": "2026-07-14", "mode": "normal"},
    )

    assert resp.status_code == 200
    body = resp.json()
    generated = {n for names in body["assignments"].values() for n in names}
    assert {"Green One", "Green Two"} <= generated
    # Every generated placement carries a source. Green/proficient placements
    # intentionally do not render a redundant visible reason badge.
    for wc, sources in body["sources"].items():
        for name, src in sources.items():
            assert src in ("generated", "manual")
    assert "green coverage" not in str(body["reasons"])


def test_rebuild_uses_enabled_new_work_center_and_leaves_disabled_recycled(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    staffing_route = _stub_recommendation_inputs(monkeypatch)

    saved: list = []
    sched = staffing.Schedule(
        day=TARGET_DAY,
        assignments={"Repair 1": ["Keep Repair"]},
    )
    roster = [
        staffing.Person(name="Keep Repair", skills={"Repair": 3}),
        staffing.Person(name="Junior Pro", skills={"Junior": 3}),
    ]
    monkeypatch.setattr(staffing_route, "_enabled_auto_work_centers", lambda d: {"Junior #1"})
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: roster)
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda d: sched)
    monkeypatch.setattr(rotations.staffing, "save_schedule", lambda s: saved.append(s))
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)

    resp = client.post(
        "/api/rotations/rebuild",
        json={"day": "2026-07-14", "mode": "normal"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["assignments"]["Junior #1"] == ["Junior Pro"]
    assert body["assignments"]["Repair 1"] == ["Keep Repair"]
    assert saved[-1].assignments["Junior #1"] == ["Junior Pro"]
    assert saved[-1].assignments["Repair 1"] == ["Keep Repair"]


def test_rebuild_treats_default_people_as_locks(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    staffing_route = _stub_recommendation_inputs(monkeypatch)

    saved: list = []
    sched = staffing.Schedule(day=TARGET_DAY, assignments={})
    roster = [
        staffing.Person(name="Default Green", skills={"Repair": 3}),
        staffing.Person(name="Other Green", skills={"Repair": 3}),
    ]
    monkeypatch.setattr(staffing_route, "_enabled_auto_work_centers", lambda d: {"Repair 1", "Repair 2"})
    monkeypatch.setattr(
        staffing_route.work_centers_store,
        "default_people",
        lambda loc: ["Default Green"] if loc.name == "Repair 1" else [],
    )
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: roster)
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda d: sched)
    monkeypatch.setattr(rotations.staffing, "save_schedule", lambda s: saved.append(s))
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)

    resp = client.post(
        "/api/rotations/rebuild",
        json={"day": "2026-07-14", "mode": "optimized"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["assignments"]["Repair 1"] == ["Default Green"]
    assert body["sources"]["Repair 1"]["Default Green"] == "manual"
    assert saved[-1].assignments["Repair 1"] == ["Default Green"]
    assert saved[-1].assignment_sources["Repair 1"]["Default Green"] == "manual"


def test_auto_work_centers_endpoint_saves_global_setting(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    saved: dict[str, list[str]] = {}
    invalidated: list[str] = []

    monkeypatch.setattr(
        rotations.staffing_route.app_settings,
        "set_setting",
        lambda key, value: saved.setdefault(key, value),
    )
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [])
    monkeypatch.setattr(
        rotations.staffing,
        "load_schedule",
        lambda d: staffing.Schedule(day=d),
    )
    monkeypatch.setattr(rotations.staffing_route, "_safe_time_off_entries", lambda d: [])
    monkeypatch.setattr(
        rotations.staffing_route,
        "_auto_capacity_for_day",
        lambda **kwargs: AutoCapacity(0, 0, 0, 0, (), ()),
    )
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: invalidated.append("today"))
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: invalidated.append("stable"))

    resp = client.post(
        "/api/rotations/auto-work-centers",
        json={
            "day": "2026-07-14",
            "work_centers": ["Junior #1", "Unknown", "Repair 1"],
            "turn_off": [],
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "enabled_work_centers": ["Repair 1", "Junior #1"],
        "capacity": {
            "required_people": 0,
            "available_people": 0,
            "shortage": 0,
            "centers_to_disable": 0,
            "runnable_centers": [],
            "blocked_centers": [],
        },
    }
    assert saved[rotations.staffing_route.AUTO_SCHEDULE_WC_SETTING] == ["Repair 1", "Junior #1"]
    assert invalidated == ["today", "stable"]


def test_auto_center_endpoint_rejects_unsafe_enable_without_replacement(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    monkeypatch.setattr(
        rotations.staffing_route,
        "_auto_capacity_for_day",
        lambda **kwargs: AutoCapacity(4, 2, 2, 1, ("Repair 1",), ("Hand Build #2",)),
    )
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [])
    monkeypatch.setattr(
        rotations.staffing,
        "load_schedule",
        lambda d: staffing.Schedule(day=d),
    )
    monkeypatch.setattr(rotations.staffing_route, "_safe_time_off_entries", lambda d: [])

    resp = client.post("/api/rotations/auto-work-centers", json={
        "day": "2026-07-14",
        "work_centers": ["Repair 1", "Hand Build #2"],
        "turn_off": [],
    })

    assert resp.status_code == 409
    assert resp.json()["required_disable_count"] == 1
    assert "need 2 more people" in resp.json()["error"]


def test_auto_center_endpoint_accepts_sufficient_replacement(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    saved = []
    monkeypatch.setattr(
        rotations.staffing_route,
        "_auto_capacity_for_day",
        lambda **kwargs: AutoCapacity(2, 2, 0, 0, ("Hand Build #2",), ()),
    )
    monkeypatch.setattr(
        rotations.staffing_route,
        "_save_enabled_auto_work_centers",
        lambda names: saved.append(names) or names,
    )
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [])
    monkeypatch.setattr(
        rotations.staffing,
        "load_schedule",
        lambda d: staffing.Schedule(day=d),
    )
    monkeypatch.setattr(rotations.staffing_route, "_safe_time_off_entries", lambda d: [])

    resp = client.post("/api/rotations/auto-work-centers", json={
        "day": "2026-07-14",
        "work_centers": ["Hand Build #2"],
        "turn_off": ["Repair 1"],
    })

    assert resp.status_code == 200
    assert saved == [["Hand Build #2"]]


def test_auto_center_endpoint_fails_closed_when_minimum_lookup_fails(monkeypatch):
    client, rotations = _rotations_client(monkeypatch, raise_server_exceptions=False)
    saved = []
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Green", 3, "Hand Build")])
    monkeypatch.setattr(
        rotations.staffing,
        "load_schedule",
        lambda d: staffing.Schedule(day=d),
    )
    monkeypatch.setattr(rotations.staffing_route, "_safe_time_off_entries", lambda d: [])
    monkeypatch.setattr(
        rotations.staffing_route.work_centers_store,
        "min_ops",
        lambda loc: (_ for _ in ()).throw(RuntimeError("settings unavailable")),
    )
    monkeypatch.setattr(
        rotations.staffing_route,
        "_save_enabled_auto_work_centers",
        lambda names: saved.append(names),
    )

    resp = client.post("/api/rotations/auto-work-centers", json={
        "day": "2026-07-14",
        "work_centers": ["Hand Build #2"],
        "turn_off": [],
    })

    assert resp.status_code == 503
    assert saved == []


def test_auto_center_endpoint_fails_closed_when_training_effect_read_fails(monkeypatch):
    client, rotations = _rotations_client(monkeypatch, raise_server_exceptions=False)
    saved = []
    invalidated = []
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Green", 3)])
    monkeypatch.setattr(
        rotations.staffing,
        "load_schedule",
        lambda d: staffing.Schedule(day=d),
    )
    monkeypatch.setattr(rotations.staffing_route, "_safe_time_off_entries", lambda d: [])
    monkeypatch.setattr(
        rotations.staffing_route,
        "_block_effects_for_day",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("blocks unavailable")),
    )
    monkeypatch.setattr(rotations.staffing_route.work_centers_store, "min_ops", lambda loc: loc.min_ops)
    monkeypatch.setattr(
        rotations.staffing_route,
        "_save_enabled_auto_work_centers",
        lambda names: saved.append(names),
    )
    monkeypatch.setattr(
        rotations._http_cache,
        "invalidate_today_cache",
        lambda: invalidated.append("today"),
    )
    monkeypatch.setattr(
        rotations._http_cache,
        "invalidate_stable_cache",
        lambda: invalidated.append("stable"),
    )

    resp = client.post("/api/rotations/auto-work-centers", json={
        "day": "2026-07-14",
        "work_centers": ["Repair 1"],
        "turn_off": [],
    })

    assert resp.status_code == 503
    assert saved == []
    assert invalidated == []


def test_rebuild_rejects_unknown_mode(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    resp = client.post("/api/rotations/rebuild", json={"day": "2026-07-14", "mode": "chaos"})
    assert resp.status_code == 422
    assert "chaos" in resp.json()["error"]


def test_rebuild_rejects_bad_day(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    resp = client.post("/api/rotations/rebuild", json={"day": "nope", "mode": "normal"})
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Staffing controls + reason data (static template / JS contract)
# --------------------------------------------------------------------------- #


def test_staffing_has_rotation_mode_controls_and_reason_data():
    html = (ROOT / "src/zira_dashboard/templates/staffing.html").read_text()
    js = (ROOT / "src/zira_dashboard/static/staffing.js").read_text()
    assert 'data-rotation-mode="optimized"' in html
    assert 'data-rotation-mode="normal"' in html
    assert 'data-rotation-mode="training"' in html
    assert 'aria-label="Optimized schedule goal"' in html
    assert 'aria-label="Normal schedule goal"' in html
    assert 'aria-label="Training schedule goal"' in html
    assert 'title="Optimized: strongest coverage"' in html
    assert 'title="Normal: balanced coverage and fair rotation"' in html
    assert 'title="Training: develop operator skills"' in html
    assert '⚡⚡⚡' in html
    assert '⚖' in html
    assert '🎓' in html
    assert 'class="wc-auto-cb"' in html
    assert "rotation_reasons" in html
    assert "/api/rotations/rebuild" in js
    assert "/api/rotations/auto-work-centers" in js
    assert "function postAutoCenters(workCenters, turnOff)" in js
    assert "JSON.stringify({ day, work_centers: workCenters, turn_off: turnOff })" in js


def test_skills_matrix_exposes_scheduling_preferences_and_recycled_training():
    html = (ROOT / "src/zira_dashboard/templates/skills.html").read_text()
    js = (ROOT / "src/zira_dashboard/static/skills-page.js").read_text()
    assert "Scheduling Preferences" in html
    assert 'class="rotation-open-btn"' in html
    assert 'aria-label="Scheduling preferences for {{ p.name }}"' in html
    assert '<circle cx="12" cy="12" r="9" stroke-dasharray="50 6" transform="rotate(-14 12 12)"/>' in html
    assert '<polygon points="20 8.7 16 7 18.3 4.7" fill="currentColor" stroke="none"/>' in html
    assert '<circle cx="9" cy="5.6" r="1"' in html
    assert '<circle cx="18.9" cy="12.9" r="1"' in html
    assert '<circle cx="8.7" cy="18.3" r="1"' in html
    assert "Recycled training" in html
    assert 'id="rotation-pref-grid"' in html
    assert "dataset.rotationPreference" in js
    assert "/api/rotations/preferences" in js
    assert "/api/rotations/training-blocks" in js


# --------------------------------------------------------------------------- #
# Additive store / history helpers
# --------------------------------------------------------------------------- #


def test_load_preferences_by_name_keys_by_person_name(monkeypatch):
    from zira_dashboard import rotation_store

    rows = [
        {"name": "Alex", "rotation_group": "Repair", "preference": "primary"},
        {"name": "Alex", "rotation_group": "Dismantler", "preference": "never"},
        {"name": "Bo", "rotation_group": "Trim Saw", "preference": "occasional"},
    ]
    monkeypatch.setattr(rotation_store.db, "query", lambda sql, params=None: rows)

    out = rotation_store.load_preferences_by_name()
    assert out == {
        "Alex": {"Repair": "primary", "Dismantler": "never"},
        "Bo": {"Trim Saw": "occasional"},
    }


def test_recycled_history_from_rows_aggregates_all_centers():
    from zira_dashboard import rotation_suggestions as rs

    group_locations = {"Repair": ("Repair 1", "Repair 2", "Repair 3")}
    rows = [
        # idx 0 == most recent
        {"assignments": {"Repair 2": ["Jordan"], "Repair 1": ["Sam"]}},
        {"assignments": {"Repair 1": ["Jordan"]}},
        {"assignments": {"Repair 1": ["Jordan"]}},
    ]

    hist = rs._recycled_history_from_rows(rows, group_locations)

    assert hist.center_counts[("Jordan", "Repair 1")] == 2
    assert hist.center_counts[("Jordan", "Repair 2")] == 1
    assert hist.group_counts[("Jordan", "Repair")] == 3
    # Most recent center for Jordan in Repair is Repair 2 (row idx 0).
    assert hist.last_center_by_person_group[("Jordan", "Repair")] == "Repair 2"
    assert hist.most_recent_group_names["Repair"] == {"Jordan", "Sam"}


def test_recycled_history_prefers_published_snapshot():
    from zira_dashboard import rotation_suggestions as rs

    group_locations = {"Repair": ("Repair 1", "Repair 2")}
    rows = [
        {
            "assignments": {"Repair 1": ["Draft"]},
            "published_snapshot": {"assignments": {"Repair 2": ["Posted"]}},
        }
    ]
    hist = rs._recycled_history_from_rows(rows, group_locations)
    assert hist.center_counts.get(("Posted", "Repair 2")) == 1
    assert ("Draft", "Repair 1") not in hist.center_counts


# --------------------------------------------------------------------------- #
# Staffing orchestration helpers
# --------------------------------------------------------------------------- #


def test_capacity_for_day_uses_effective_minimum_and_excludes_absent_and_manual(monkeypatch):
    staffing_route = _stub_recommendation_inputs(monkeypatch)
    monkeypatch.setattr(
        staffing_route.work_centers_store,
        "min_ops",
        lambda loc: 2 if loc.name == "Hand Build #2" else 1,
    )
    roster = [
        staffing.Person(name="Manual", skills={"Hand Build": 3}),
        staffing.Person(name="Off", skills={"Hand Build": 3}),
        staffing.Person(name="Available", skills={"Hand Build": 3}),
    ]

    result = staffing_route._auto_capacity_for_day(
        d=TARGET_DAY,
        enabled_work_centers={"Hand Build #2"},
        roster=roster,
        assignments={"Repair 1": ["Manual"]},
        assignment_sources={"Repair 1": {"Manual": "manual"}},
        time_off_entries=[{"name": "Off", "hours": None}],
    )

    assert result.available_people == 1
    assert result.required_people == 2
    assert result.shortage == 1


def test_capacity_for_day_excludes_generated_assignment_outside_auto_centers(monkeypatch):
    staffing_route = _stub_recommendation_inputs(monkeypatch)
    monkeypatch.setattr(staffing_route.work_centers_store, "min_ops", lambda loc: loc.min_ops)

    result = staffing_route._auto_capacity_for_day(
        d=TARGET_DAY,
        enabled_work_centers={"Hand Build #2"},
        roster=[
            staffing.Person(name="Auto Candidate", skills={"Hand Build": 3}),
            staffing.Person(name="Generated Elsewhere", skills={"Hand Build": 3}),
        ],
        assignments={"Junior #1": ["Generated Elsewhere"]},
        assignment_sources={"Junior #1": {"Generated Elsewhere": "generated"}},
        time_off_entries=[],
        block_effects=(),
    )

    assert result.available_people == 1
    assert result.required_people == 2
    assert result.shortage == 1


@pytest.mark.parametrize(
    "lock_kind,locked_person,time_off",
    [
        ("manual", _person("Inactive", 3, "Hand Build", active=False), []),
        ("manual", _person("Reserve", 3, "Hand Build", reserve=True), []),
        ("manual", _person("Absent", 3, "Hand Build"), [{"name": "Absent", "hours": None}]),
        ("default", _person("Default Absent", 3, "Hand Build"), [{"name": "Default Absent", "hours": None}]),
        (
            "training",
            _person("Trainee Absent", 0, "Hand Build"),
            [{"name": "Trainee Absent", "hours": None}],
        ),
    ],
)
def test_capacity_for_day_nonworking_locks_do_not_reduce_minimum(
    monkeypatch, lock_kind, locked_person, time_off,
):
    from zira_dashboard.routes import staffing as staffing_route

    _stub_recommendation_inputs(monkeypatch)
    monkeypatch.setattr(staffing_route.work_centers_store, "min_ops", lambda loc: loc.min_ops)
    if lock_kind == "default":
        monkeypatch.setattr(
            staffing_route.work_centers_store,
            "default_people",
            lambda loc: [locked_person.name] if loc.name == "Hand Build #2" else [],
        )
        assignments, sources = {}, {}
        block_effects = ()
    elif lock_kind == "training":
        assignments, sources = {}, {}
        block_effects = (
            staffing_route.rotation_training.BlockEffect(
                locked_people={"Hand Build": [locked_person.name]},
            ),
        )
    else:
        assignments = {"Hand Build #2": [locked_person.name]}
        sources = {"Hand Build #2": {locked_person.name: "manual"}}
        block_effects = ()

    result = staffing_route._auto_capacity_for_day(
        d=TARGET_DAY,
        enabled_work_centers={"Hand Build #2"},
        roster=[locked_person, _person("Available", 3, "Hand Build")],
        assignments=assignments,
        assignment_sources=sources,
        time_off_entries=time_off,
        block_effects=block_effects,
    )

    assert result.available_people == 1
    assert result.required_people == 2
    assert result.shortage == 1


def test_auto_group_maps_keep_hand_build_centers_under_one_target():
    from zira_dashboard.routes import staffing as staffing_route

    locations, skills = staffing_route._auto_group_maps({"Hand Build #1", "Hand Build #2"})

    assert locations == {"Hand Build": ("Hand Build #2", "Hand Build #1")}
    assert skills == {"Hand Build": ("Hand Build",)}


def test_auto_group_maps_keep_trim_saw_pairing_protection():
    from zira_dashboard import rotation_suggestions
    from zira_dashboard.routes import staffing as staffing_route

    locations, skills = staffing_route._auto_group_maps({"Trim Saw 1"})
    out = rotation_suggestions.suggest_recycled_assignments(
        day=TARGET_DAY,
        mode="normal",
        roster=[_person("First Learner", 1, "Trim Saw"), _person("Second Learner", 1, "Trim Saw")],
        preferences={},
        base_assignments={},
        group_locations=locations,
        group_required_skills=skills,
        history=rotation_suggestions.RecycledHistory(),
        locked_assignments={},
        block_effects=(),
    )

    assert locations == {"Trim Saw": ("Trim Saw 1",)}
    assert out.assignments.get("Trim Saw 1", []) == []
    assert "No safe operator pairing available for Trim Saw 1." in out.warnings


def test_staffing_history_counts_hand_build_and_rotates_to_other_center(monkeypatch):
    from zira_dashboard import db, rotation_suggestions
    from zira_dashboard.routes import staffing as staffing_route

    monkeypatch.setattr(staffing_route.rotation_store, "load_preferences_by_name", lambda: {})
    monkeypatch.setattr(staffing_route.rotation_training, "reconcile_blocks", lambda _as_of: [])
    monkeypatch.setattr(staffing_route.rotation_store, "active_blocks_for_day", lambda _d: [])
    monkeypatch.setattr(
        db,
        "query",
        lambda _sql, _params=None: [
            {"assignments": {"Hand Build #1": ["Builder"]}, "published_snapshot": None}
        ],
    )

    _preferences, history, _effects, _blocks = staffing_route._gather_recycled_inputs(
        TARGET_DAY, []
    )
    locations, skills = staffing_route._auto_group_maps({"Hand Build #1", "Hand Build #2"})
    out = rotation_suggestions.suggest_recycled_assignments(
        day=TARGET_DAY,
        mode="normal",
        roster=[
            staffing.Person("Builder", skills={"Hand Build": 3}),
            staffing.Person("Partner", skills={"Hand Build": 3}),
        ],
        preferences={},
        base_assignments={},
        group_locations=locations,
        group_required_skills=skills,
        history=history,
        locked_assignments={},
        block_effects=(),
        center_minimums={"Hand Build #1": 2, "Hand Build #2": 2},
        runnable_centers={"Hand Build #2"},
    )

    assert history.group_counts[("Builder", "Hand Build")] == 1
    assert history.last_center_by_person_group[("Builder", "Hand Build")] == "Hand Build #1"
    assert set(out.assignments["Hand Build #2"]) == {"Builder", "Partner"}


def test_smart_defaults_merges_recycled_and_keeps_non_recycled(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_route
    from zira_dashboard import rotation_suggestions

    _stub_recommendation_inputs(monkeypatch)

    def fake_engine(**kwargs):
        return rotation_suggestions.RecycledSuggestion(
            assignments={"Junior #1": ["Keep Me"], "Repair 1": ["Rotated"]},
            sources={"Repair 1": {"Rotated": "generated"}},
            reasons={},
            warnings=(),
            group_locations={"Repair": ("Repair 1", "Repair 2", "Repair 3")},
        )

    monkeypatch.setattr(rotation_suggestions, "suggest_recycled_assignments", fake_engine)

    out = staffing_route._smart_defaults_for_day(
        TARGET_DAY,
        roster=[_person("Rotated", 3)],
        defaults={"Junior #1": ["Keep Me"], "Repair 1": ["Old"], "Repair 2": ["Stale"]},
        time_off_entries=[],
    )

    # Recycled centers replaced by the engine; the old Repair 2 pick is gone.
    assert out["Repair 1"] == ["Rotated"]
    assert "Repair 2" not in out or out["Repair 2"] == []
    # Non-Recycled center left exactly as it was.
    assert out["Junior #1"] == ["Keep Me"]


def test_smart_defaults_falls_back_when_engine_fails(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_route
    from zira_dashboard import rotation_suggestions

    _stub_recommendation_inputs(monkeypatch)

    def boom(**kwargs):
        raise RuntimeError("engine down")

    monkeypatch.setattr(rotation_suggestions, "suggest_recycled_assignments", boom)

    out = staffing_route._smart_defaults_for_day(
        TARGET_DAY,
        roster=[],
        defaults={"Repair 1": ["Stored"], "Junior #1": ["Also"]},
        time_off_entries=[],
    )
    assert out == {"Repair 1": ["Stored"], "Junior #1": ["Also"]}


def test_smart_defaults_runs_real_engine_and_keeps_trim_saw_pair(monkeypatch):
    """End-to-end through the REAL engine (only DB reads stubbed): a blank day
    seeds a safe Trim Saw pair and preserves each non-Recycled default."""
    from zira_dashboard.routes import staffing as staffing_route

    _stub_recommendation_inputs(monkeypatch)

    roster = [
        _person("Green", 3, "Trim Saw"),
        _person("Rosa", 1, "Trim Saw"),
    ]
    defaults = {"Trim Saw 1": [], "Junior #1": ["Static Junior"]}

    out = staffing_route._smart_defaults_for_day(
        TARGET_DAY,
        roster=roster,
        defaults=defaults,
        time_off_entries=[],
        enabled_work_centers={"Trim Saw 1"},
    )

    # Trim Saw 1 gets a valid, capacity-respecting pair from the engine.
    assert set(out["Trim Saw 1"]) == {"Green", "Rosa"}
    # Non-Recycled default is untouched.
    assert out["Junior #1"] == ["Static Junior"]


def test_recycled_context_surfaces_reasons_warnings_blocks(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_route
    from zira_dashboard import rotation_suggestions, rotation_store

    monkeypatch.setattr(staffing_route.rotation_store, "load_preferences_by_name", lambda: {})
    monkeypatch.setattr(
        rotation_suggestions, "_load_recycled_history",
        lambda d, group_locations=None: rotation_suggestions.RecycledHistory(),
    )
    monkeypatch.setattr(staffing_route.rotation_training, "reconcile_blocks", lambda as_of: [])
    monkeypatch.setattr(staffing_route.app_settings, "get_setting", lambda key: ["Repair 1"])
    monkeypatch.setattr(staffing_route.work_centers_store, "min_ops", lambda loc: loc.min_ops)

    block = rotation_store.TrainingBlock(
        id=1, trainee_name="Learner", trainer_name="Green", skill="Repair",
        start_day=TARGET_DAY, planned_attended_days=5, status="active",
    )
    monkeypatch.setattr(staffing_route.rotation_store, "active_blocks_for_day", lambda d: [block])
    monkeypatch.setattr(staffing_route.rotation_training, "effect_for_day", lambda *a, **k: staffing_route.rotation_training.BlockEffect())
    monkeypatch.setattr(
        staffing_route.rotation_store, "resolved_days",
        lambda block_id: [
            rotation_store.TrainingBlockDay(day=TARGET_DAY, status="attended"),
            rotation_store.TrainingBlockDay(day=date(2026, 7, 15), status="attended"),
        ],
    )
    monkeypatch.setattr(staffing_route, "_absence_by_day_for_block", lambda block, d: {})

    def fake_engine(**kwargs):
        return rotation_suggestions.RecycledSuggestion(
            assignments={"Repair 1": ["Green"]},
            sources={"Repair 1": {"Green": "generated"}},
            reasons={},
            warnings=("Trim Saw 1 short",),
            group_locations={"Repair": ("Repair 1",)},
        )

    monkeypatch.setattr(rotation_suggestions, "suggest_recycled_assignments", fake_engine)

    ctx = staffing_route._recycled_context_for_day(
        TARGET_DAY, roster=[_person("Green", 3)], mode="training",
        base_assignments={}, locked_assignments={}, time_off_entries=[],
    )

    assert ctx["recycled_rotation_mode"] == "training"
    assert ctx["rotation_reasons"] == {}
    assert "Trim Saw 1 short" in ctx["rotation_warnings"]
    assert len(ctx["active_training_blocks"]) == 1
    tb = ctx["active_training_blocks"][0]
    assert tb["trainee"] == "Learner"
    assert tb["trainer"] == "Green"
    assert tb["group"] == "Repair"
    assert tb["remaining_attended_days"] == 3  # 5 planned - 2 attended


def test_recycled_suggestion_uses_regular_preferences_when_preference_read_fails(monkeypatch):
    from zira_dashboard import rotation_suggestions
    from zira_dashboard.routes import staffing as staffing_route

    def boom():
        raise RuntimeError("preferences unavailable")

    monkeypatch.setattr(staffing_route.rotation_store, "load_preferences_by_name", boom)
    monkeypatch.setattr(
        rotation_suggestions,
        "_load_recycled_history",
        lambda _d, group_locations=None: rotation_suggestions.RecycledHistory(),
    )
    monkeypatch.setattr(staffing_route.rotation_training, "reconcile_blocks", lambda _as_of: [])
    monkeypatch.setattr(staffing_route.rotation_store, "active_blocks_for_day", lambda _d: [])
    monkeypatch.setattr(staffing_route.work_centers_store, "min_ops", lambda loc: loc.min_ops)

    captured = {}
    sentinel = object()

    def fake_engine(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(rotation_suggestions, "suggest_recycled_assignments", fake_engine)

    assert staffing_route._recycled_suggestion_for_day(
        TARGET_DAY,
        roster=[_person("Green", 3)],
        mode="normal",
        base_assignments={},
        locked_assignments={},
        time_off_entries=[],
        enabled_work_centers={"Repair 1"},
    ) is sentinel
    assert captured["preferences"] == {}


def test_recycled_context_uses_regular_preferences_when_preference_read_fails(monkeypatch):
    from zira_dashboard import rotation_suggestions
    from zira_dashboard.routes import staffing as staffing_route

    def boom():
        raise RuntimeError("preferences unavailable")

    monkeypatch.setattr(staffing_route.rotation_store, "load_preferences_by_name", boom)
    monkeypatch.setattr(
        rotation_suggestions,
        "_load_recycled_history",
        lambda _d, group_locations=None: rotation_suggestions.RecycledHistory(),
    )
    monkeypatch.setattr(staffing_route.rotation_training, "reconcile_blocks", lambda _as_of: [])
    monkeypatch.setattr(staffing_route.rotation_store, "active_blocks_for_day", lambda _d: [])
    monkeypatch.setattr(staffing_route.work_centers_store, "min_ops", lambda loc: loc.min_ops)

    captured = {}

    def fake_engine(**kwargs):
        captured.update(kwargs)
        return rotation_suggestions.RecycledSuggestion(
            assignments={},
            sources={},
            reasons={},
            warnings=(),
            group_locations={},
        )

    monkeypatch.setattr(rotation_suggestions, "suggest_recycled_assignments", fake_engine)

    ctx = staffing_route._recycled_context_for_day(
        TARGET_DAY,
        roster=[_person("Green", 3)],
        mode="normal",
        base_assignments={},
        locked_assignments={},
        time_off_entries=[],
        enabled_work_centers={"Repair 1"},
    )

    assert ctx["recycled_rotation_mode"] == "normal"
    assert captured["preferences"] == {}


def test_manual_locks_from_sources_extracts_manual_only():
    from zira_dashboard.routes import staffing as staffing_route

    sources = {
        "Repair 1": {"Manual Person": "manual", "Bot": "generated"},
        "Repair 2": {"Bot Two": "generated"},
    }
    assignments = {"Repair 1": ["Manual Person", "Bot"], "Repair 2": ["Bot Two"]}
    locks = staffing_route._manual_locks_from_sources(sources, assignments)
    assert locks == {"Repair 1": ["Manual Person"]}


def test_default_people_locks_merge_with_manual_locks(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_route

    monkeypatch.setattr(
        staffing_route.work_centers_store,
        "default_people",
        lambda loc: ["Default Green"] if loc.name == "Repair 1" else [],
    )

    locks = staffing_route._protected_locks(
        {"Repair 2": {"Manual Person": "manual"}},
        {"Repair 2": ["Manual Person"]},
    )

    assert locks["Repair 1"] == ["Default Green"]
    assert locks["Repair 2"] == ["Manual Person"]


def test_enabled_auto_work_centers_initialize_from_recent_schedule_history(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_route

    settings: dict[str, list[str]] = {}
    monkeypatch.setattr(staffing_route.app_settings, "get_setting", lambda key: None)
    monkeypatch.setattr(staffing_route.app_settings, "set_setting", lambda key, value: settings.setdefault(key, value))
    monkeypatch.setattr(
        staffing_route.db,
        "query",
        lambda sql, params=None: [
            {"name": "Repair 1"},
            {"name": "Junior #1"},
            {"name": "Not A Real WC"},
        ],
    )

    enabled = staffing_route._enabled_auto_work_centers(TARGET_DAY)

    assert enabled == {"Repair 1", "Junior #1"}
    assert settings[staffing_route.AUTO_SCHEDULE_WC_SETTING] == ["Repair 1", "Junior #1"]


def test_enabled_auto_work_centers_use_saved_setting_without_history_query(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_route

    monkeypatch.setattr(
        staffing_route.app_settings,
        "get_setting",
        lambda key: ["Junior #1", "Unknown", "Repair 2"],
    )
    monkeypatch.setattr(
        staffing_route.db,
        "query",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("history query should not run")),
    )

    assert staffing_route._enabled_auto_work_centers(TARGET_DAY) == {"Junior #1", "Repair 2"}


# --------------------------------------------------------------------------- #
# GET /staffing context wiring (rendered via a fake TemplateResponse)
# --------------------------------------------------------------------------- #


def _render_staffing_page(monkeypatch, *, saved_schedule=None, day=None, smart_defaults=None):
    """Render the staffing page with all I/O stubbed, returning the captured
    template context. Mirrors the harness in test_staffing_trim_saw_defaults.

    ``smart_defaults`` optionally replaces the ``_smart_defaults_for_day`` stub
    (e.g. a spy that records the ``mode`` it was called with)."""
    from zira_dashboard import cert_lookup
    from zira_dashboard import staffing as staffing_mod, staffing_view
    from zira_dashboard.routes import staffing as staffing_routes

    the_day = day or TARGET_DAY
    captured: dict = {}

    monkeypatch.setattr(staffing_routes, "plant_today", lambda: date(2026, 7, 13))
    monkeypatch.setattr(staffing_routes, "_next_working_day", lambda today: the_day)
    monkeypatch.setattr(staffing_routes._http_cache, "get_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(staffing_routes._http_cache, "set_cache_headers", lambda *a, **k: None)
    monkeypatch.setattr(staffing_routes._http_cache, "store_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(staffing_routes.app_settings, "get_setting", lambda key: [])
    monkeypatch.setattr(cert_lookup, "load_person_certs", lambda: {})
    monkeypatch.setattr(staffing_mod, "load_roster", lambda: [])
    monkeypatch.setattr(
        staffing_mod, "load_schedule",
        lambda d: saved_schedule or staffing_mod.Schedule(day=d, published=False, assignments={}),
    )
    monkeypatch.setattr(staffing_routes, "_safe_time_off_entries", lambda d: [])
    monkeypatch.setattr(
        staffing_routes, "_safe_attendance",
        lambda d, sched, today: {"by_name": {}, "name_to_id": {}},
    )
    monkeypatch.setattr(staffing_routes, "_late_emp_ids", lambda d, today, pkg: set())
    monkeypatch.setattr(staffing_routes.attendance, "person_id_to_name", lambda name_to_id: {})
    monkeypatch.setattr(staffing_routes.shift_config, "configured_shift_start_for", lambda d: time(7, 0))
    monkeypatch.setattr(staffing_routes.shift_config, "configured_shift_end_for", lambda d: time(15, 30))
    monkeypatch.setattr(staffing_routes.shift_config, "configured_breaks_for", lambda d: [])
    monkeypatch.setattr(staffing_routes.shift_config, "scheduler_hours_source", lambda d, custom: "weekday_default")
    monkeypatch.setattr(staffing_routes.work_centers_store, "default_people", lambda loc: [])
    monkeypatch.setattr(
        staffing_routes, "_smart_defaults_for_day",
        smart_defaults
        or (lambda d, roster, defaults, time_off, mode="normal", **kwargs: {k: list(v) for k, v in defaults.items()}),
    )

    def fake_build_staffing_bays(roster, sched, time_off_entries, publish_blocked):
        return {
            "bays": [], "publish_block_reasons": [], "defaults_by_loc": {},
            "unassigned": [], "reserves": [], "time_off_names": [], "time_off_entries": [],
            "partial_hours_by_name": {}, "partial_range_by_name": {},
            "partial_clear_by_name": {}, "people_meta": {}, "all_active_people": [],
        }

    monkeypatch.setattr(staffing_view, "build_staffing_bays", fake_build_staffing_bays)

    class FakeResponse:
        def __init__(self, context):
            self.context = context
            self.headers = {}

    class FakeTemplates:
        def TemplateResponse(self, request, template, context):
            captured["context"] = context
            return FakeResponse(context)

    monkeypatch.setattr(staffing_routes, "templates", FakeTemplates())

    staffing_routes.staffing_page(request=object(), day=the_day.isoformat(), publish_blocked=0, view="draft")
    return captured["context"]


def test_blank_staffing_day_context_defaults_to_normal(monkeypatch):
    ctx = _render_staffing_page(monkeypatch)
    assert ctx["recycled_rotation_mode"] == "normal"
    assert ctx["rotation_reasons"] == {}
    assert ctx["rotation_warnings"] == []
    assert ctx["active_training_blocks"] == []


def test_blank_staffing_day_stays_empty_without_default_or_smart_seed(monkeypatch):
    calls = []
    ctx = _render_staffing_page(
        monkeypatch,
        smart_defaults=lambda *args, **kwargs: calls.append(args) or {"Repair 1": ["Unexpected"]},
    )

    assert calls == []
    assert ctx["sched"].assignments == {}
    assert ctx["auto_scheduler_available"] is True


def test_saturday_staffing_context_is_manual_only(monkeypatch):
    ctx = _render_staffing_page(monkeypatch, day=date(2026, 7, 18))

    assert ctx["auto_scheduler_available"] is False


def test_staffing_template_gates_auto_controls_for_saturday():
    html = (ROOT / "src/zira_dashboard/templates/staffing.html").read_text()

    assert "{% if auto_scheduler_available %}" in html
    assert 'class="rotation-controls"' in html
    assert 'class="wc-auto-cb"' in html


def test_saved_staffing_day_context_hydrates_stored_mode(monkeypatch):
    sched = staffing.Schedule(
        day=TARGET_DAY, published=False,
        assignments={"Repair 1": ["Someone"]},
        rotation_mode="optimized",
        assignment_sources={"Repair 1": {"Someone": "manual"}},
    )
    ctx = _render_staffing_page(monkeypatch, saved_schedule=sched)
    assert ctx["recycled_rotation_mode"] == "optimized"


def test_saved_day_hints_thread_stored_mode(monkeypatch):
    """The saved-day empty-slot hints compute with the schedule's stored mode,
    not a hard-coded 'normal', so hints agree with the reason badges."""
    calls: list[str] = []

    def spy(d, roster, defaults, time_off, mode="normal", **kwargs):
        calls.append(mode)
        return {k: list(v) for k, v in defaults.items()}

    sched = staffing.Schedule(
        day=TARGET_DAY, published=False,
        assignments={"Repair 1": ["Someone"]},  # saved → the else/hints branch runs
        rotation_mode="optimized",
    )
    _render_staffing_page(monkeypatch, saved_schedule=sched, smart_defaults=spy)
    assert calls == ["optimized"]


# --------------------------------------------------------------------------- #
# Bad JSON, rebuild source hygiene, and the bounded absence window
# --------------------------------------------------------------------------- #


def test_bad_json_body_returns_400(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    resp = client.post(
        "/api/rotations/preferences",
        content=b"{not valid json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    assert "JSON" in resp.json()["error"]


def test_rebuild_drops_stale_generated_source(monkeypatch):
    """A person left as a 'generated' source but no longer placed by the engine
    disappears from BOTH assignments and sources after a rebuild."""
    client, rotations = _rotations_client(monkeypatch)
    _stub_recommendation_inputs(monkeypatch)

    saved: list = []
    sched = staffing.Schedule(
        day=TARGET_DAY,
        assignments={"Repair 1": ["Gone"]},
        assignment_sources={"Repair 1": {"Gone": "generated"}},
    )
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Green One", 3)])
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda d: sched)
    monkeypatch.setattr(rotations.staffing, "save_schedule", lambda s: saved.append(s))
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)

    resp = client.post("/api/rotations/rebuild", json={"day": "2026-07-14", "mode": "normal"})

    assert resp.status_code == 200
    body = resp.json()
    all_assigned = {n for names in body["assignments"].values() for n in names}
    all_sourced = {n for src in body["sources"].values() for n in src}
    assert "Gone" not in all_assigned
    assert "Gone" not in all_sourced
    assert "Green One" in all_assigned  # the real green person took the slot
    # And the persisted schedule is equally clean.
    assert "Gone" not in {n for names in saved[-1].assignments.values() for n in names}


def test_rebuild_leaves_non_recycled_center_untouched(monkeypatch):
    """A non-Recycled center and its source pass through a rebuild unchanged
    (verified end-to-end through the endpoint, not just the merge helper)."""
    client, rotations = _rotations_client(monkeypatch)
    _stub_recommendation_inputs(monkeypatch)

    saved: list = []
    sched = staffing.Schedule(
        day=TARGET_DAY,
        assignments={"Junior #1": ["Static"], "Repair 1": ["Old"]},
        assignment_sources={
            "Junior #1": {"Static": "generated"},  # not manual → not a lock
            "Repair 1": {"Old": "generated"},
        },
    )
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Green One", 3)])
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda d: sched)
    monkeypatch.setattr(rotations.staffing, "save_schedule", lambda s: saved.append(s))
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)

    resp = client.post("/api/rotations/rebuild", json={"day": "2026-07-14", "mode": "normal"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["assignments"]["Junior #1"] == ["Static"]
    assert body["sources"]["Junior #1"] == {"Static": "generated"}
    assert saved[-1].assignments["Junior #1"] == ["Static"]
    assert saved[-1].assignment_sources["Junior #1"] == {"Static": "generated"}


def test_absence_by_day_window_is_bounded(monkeypatch):
    """A stale block with an old start_day must not fan out O(days) DB lookups:
    the window is capped at planned_block_days' own scan horizon."""
    from zira_dashboard.routes import staffing as staffing_route
    from zira_dashboard import rotation_store, rotation_training, scheduler_time_off

    queried: list[date] = []
    monkeypatch.setattr(
        scheduler_time_off, "full_day_off_names",
        lambda day: (queried.append(day) or set()),
    )

    start = date(2025, 1, 1)
    d = start + timedelta(days=400)  # far past the block's horizon
    block = rotation_store.TrainingBlock(
        id=1, trainee_name="T", trainer_name="G", skill="Repair",
        start_day=start, planned_attended_days=5, status="active",
    )

    staffing_route._absence_by_day_for_block(block, d)

    horizon = start + timedelta(days=5 + rotation_training._MAX_SCAN_DAYS)
    assert max(queried) == horizon
    assert d not in queried
    assert len(queried) == (5 + rotation_training._MAX_SCAN_DAYS + 1)


# --------------------------------------------------------------------------- #
# GET /staffing/skills — People Matrix rotation editor context
# --------------------------------------------------------------------------- #


def test_staffing_skills_context_includes_rotation_editor_data(monkeypatch):
    """The People Matrix GET hands the template the per-person rotation
    preferences and active training blocks the editor renders."""
    from types import SimpleNamespace
    from zira_dashboard import (
        odoo_sync, cert_lookup, rotation_store,
        skill_matrix_views_store as views_store,
    )
    from zira_dashboard.routes import skills as skills_routes

    monkeypatch.setattr(skills_routes._http_cache, "get_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(skills_routes._http_cache, "set_cache_headers", lambda *a, **k: None)
    monkeypatch.setattr(skills_routes._http_cache, "store_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(cert_lookup, "load_person_certs", lambda: {})
    monkeypatch.setattr(
        odoo_sync, "sync",
        lambda force=False: SimpleNamespace(ok=True, last_sync_at=None, error=None),
    )
    monkeypatch.setattr(
        skills_routes.staffing, "load_roster",
        lambda: [_person("Alex", 0, "Repair"), _person("Green", 3, "Repair")],
    )
    monkeypatch.setattr(skills_routes.db, "query", lambda *a, **k: [])
    monkeypatch.setattr(views_store, "list_views", lambda: [])
    monkeypatch.setattr(views_store, "get_default_view", lambda: None)
    monkeypatch.setattr(
        skills_routes.rotation_store, "load_preferences_by_name",
        lambda: {"Alex": {"Repair": "primary"}},
    )
    block = rotation_store.TrainingBlock(
        id=7, trainee_name="Alex", trainer_name="Green", skill="Repair",
        start_day=TARGET_DAY, planned_attended_days=5, status="active",
    )
    monkeypatch.setattr(skills_routes.rotation_store, "active_blocks", lambda: [block])

    captured: dict = {}

    class FakeResponse:
        def __init__(self, ctx):
            self.context = ctx
            self.headers = {}

    class FakeTemplates:
        def TemplateResponse(self, request, template, context):
            captured["context"] = context
            return FakeResponse(context)

    monkeypatch.setattr(skills_routes, "templates", FakeTemplates())

    skills_routes.staffing_skills(request=object())
    ctx = captured["context"]

    assert ctx["rotation_groups"] == ["Dismantler", "Repair", "Trim Saw"]
    assert ctx["rotation_preference_options"] == ["primary", "regular", "occasional", "never"]
    assert ctx["rotation_preferences"] == {"Alex": {"Repair": "primary"}}
    assert ctx["rotation_levels"]["Green"]["Repair"] == 3
    assert ctx["rotation_levels"]["Alex"]["Repair"] == 0
    assert "Alex" in ctx["rotation_active_people"]
    assert len(ctx["active_training_blocks"]) == 1
    tb = ctx["active_training_blocks"][0]
    assert tb["trainee"] == "Alex"
    assert tb["group"] == "Repair"
    assert tb["status"] == "active"


def test_skills_context_only_exposes_qualified_preference_targets(monkeypatch):
    """The matrix only offers scheduling preferences a person can use."""
    from types import SimpleNamespace
    from zira_dashboard import (
        odoo_sync, cert_lookup, skill_matrix_views_store as views_store,
    )
    from zira_dashboard.routes import skills as skills_routes

    monkeypatch.setattr(skills_routes._http_cache, "get_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(skills_routes._http_cache, "set_cache_headers", lambda *a, **k: None)
    monkeypatch.setattr(skills_routes._http_cache, "store_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(cert_lookup, "load_person_certs", lambda: {})
    monkeypatch.setattr(
        odoo_sync, "sync",
        lambda force=False: SimpleNamespace(ok=True, last_sync_at=None, error=None),
    )
    monkeypatch.setattr(
        skills_routes.staffing, "load_roster",
        lambda: [staffing.Person("Alex", skills={"Repair": 1, "Woodpecker": 0})],
    )
    monkeypatch.setattr(skills_routes.db, "query", lambda *a, **k: [])
    monkeypatch.setattr(views_store, "list_views", lambda: [])
    monkeypatch.setattr(views_store, "get_default_view", lambda: None)
    monkeypatch.setattr(skills_routes.rotation_store, "load_preferences_by_name", lambda: {})
    monkeypatch.setattr(skills_routes.rotation_store, "active_blocks", lambda: [])

    captured: dict = {}

    class FakeTemplates:
        def TemplateResponse(self, request, template, context):
            captured["context"] = context
            return SimpleNamespace(context=context, headers={})

    monkeypatch.setattr(skills_routes, "templates", FakeTemplates())

    skills_routes.staffing_skills(request=object())

    assert captured["context"]["rotation_preference_targets_by_person"]["Alex"] == [
        {"key": "Repair", "label": "Repair"}
    ]


def test_people_matrix_uses_dynamic_scheduling_preferences_picker():
    html = (ROOT / "src/zira_dashboard/templates/skills.html").read_text()
    js = (ROOT / "src/zira_dashboard/static/skills-page.js").read_text()

    assert 'aria-label="Scheduling preferences for {{ p.name }}"' in html
    assert "<svg" in html
    assert "ROTATION_PREFERENCE_TARGETS_BY_PERSON" in html
    assert "renderPreferences(person)" in js
    assert "dataset.rotationPreference" in js


def test_staffing_skills_context_degrades_when_rotation_load_fails(monkeypatch):
    """A DB hiccup loading rotation data leaves the matrix renderable with an
    empty editor rather than 500ing."""
    from types import SimpleNamespace
    from zira_dashboard import (
        odoo_sync, cert_lookup, skill_matrix_views_store as views_store,
    )
    from zira_dashboard.routes import skills as skills_routes

    monkeypatch.setattr(skills_routes._http_cache, "get_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(skills_routes._http_cache, "set_cache_headers", lambda *a, **k: None)
    monkeypatch.setattr(skills_routes._http_cache, "store_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(cert_lookup, "load_person_certs", lambda: {})
    monkeypatch.setattr(
        odoo_sync, "sync",
        lambda force=False: SimpleNamespace(ok=True, last_sync_at=None, error=None),
    )
    monkeypatch.setattr(skills_routes.staffing, "load_roster", lambda: [_person("Alex", 0)])
    monkeypatch.setattr(skills_routes.db, "query", lambda *a, **k: [])
    monkeypatch.setattr(views_store, "list_views", lambda: [])
    monkeypatch.setattr(views_store, "get_default_view", lambda: None)

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(skills_routes.rotation_store, "load_preferences_by_name", boom)
    monkeypatch.setattr(skills_routes.rotation_store, "active_blocks", boom)

    captured: dict = {}

    class FakeTemplates:
        def TemplateResponse(self, request, template, context):
            captured["context"] = context
            return SimpleNamespace(context=context, headers={})

    monkeypatch.setattr(skills_routes, "templates", FakeTemplates())

    skills_routes.staffing_skills(request=object())
    ctx = captured["context"]

    assert ctx["rotation_preferences"] == {}
    assert ctx["active_training_blocks"] == []
