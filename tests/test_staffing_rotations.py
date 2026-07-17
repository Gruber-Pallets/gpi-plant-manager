"""Task 4 — rotation JSON APIs + Staffing recommendation wiring.

DB-free route/orchestration tests. The FastAPI endpoints are exercised through
a TestClient with only ``rotations.router`` mounted; all DB and store calls are
monkeypatched. The Staffing orchestration helpers are called directly with the
recommendation inputs stubbed, so nothing here touches Postgres or the clock.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import date, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import FormData

from zira_dashboard import rotation_suggestions, schedule_solver, staffing


TARGET_DAY = date(2026, 7, 14)

ROOT = Path(__file__).resolve().parents[1]


def test_readme_describes_exact_work_center_training_protocol():
    readme = (ROOT / "README.md").read_text()
    assert "exact work center" in readme
    assert "day one" in readme
    assert "level 3" in readme


def _person(name: str, level: int, group: str = "Repair", *, active: bool = True, reserve: bool = False):
    return staffing.Person(name=name, active=active, reserve=reserve, skills={group: level})


def _rotations_client(monkeypatch, *, raise_server_exceptions: bool = True):
    from zira_dashboard.routes import rotations

    app = FastAPI()
    app.include_router(rotations.router)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions), rotations


class _RouteTransaction:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


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
        if "SELECT id, name FROM skills" in sql:
            return [{"id": 9, "name": "Repair"}]
        if "FROM skills WHERE id" in sql:
            return [{"name": "Repair"}]
        if "trainee_level" in sql:
            return [{"trainee_level": trainee_level, "trainer_level": trainer_level}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(rotations.db, "query", fake_query)
    monkeypatch.setattr(rotations.rotation_store.db, "query", fake_query)
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: None)


def protocol_block(work_center: str):
    from zira_dashboard import rotation_store

    return rotation_store.TrainingBlock(
        id=42,
        trainee_name="Alex",
        trainer_name="Green",
        skill="Repair",
        start_day=TARGET_DAY,
        planned_attended_days=5,
        status="active",
        trainee_id=1,
        skill_id=9,
        work_center=work_center,
        skill_ids=(9,),
    )


def test_training_protocol_endpoint_creates_exact_center_block(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    monkeypatch.setattr(
        rotations.db,
        "query",
        lambda sql, params=None: [{"id": 1}] if "FROM people" in sql else [],
    )
    monkeypatch.setattr(
        rotations.rotation_store,
        "create_block",
        lambda **kw: protocol_block("Repair 2"),
    )
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: None)

    resp = client.post("/api/rotations/training-blocks", json={
        "trainee": "Alex", "trainer": "Green", "work_center": "Repair 2",
        "start_day": "2026-07-14", "workdays": 5,
    })

    assert resp.status_code == 200
    assert resp.json()["block"]["work_center"] == "Repair 2"
    assert resp.json()["block"]["skill_ids"] == [9]


def test_training_protocol_endpoint_rejects_unknown_work_center(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    monkeypatch.setattr(
        rotations.db,
        "query",
        lambda sql, params=None: [{"id": 1}] if "FROM people" in sql else [],
    )

    resp = client.post("/api/rotations/training-blocks", json={
        "trainee": "Alex", "trainer": "Green", "work_center": "Nope",
        "start_day": "2026-07-14", "workdays": 5,
    })

    assert resp.status_code == 422
    assert "work center" in resp.json()["error"].lower()


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
            "work_center": "Repair 1",
            "start_day": "2026-07-14",
            "workdays": 5,
        },
    )

    assert resp.status_code == 422
    assert resp.json()["ok"] is False
    assert resp.json()["error"] == "Day-one trainer must be level 3 for the target skill."


def test_training_block_endpoint_success(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    monkeypatch.setattr(
        rotations.db, "query",
        lambda sql, params=None: (
            [{"id": 1}] if "FROM people" in sql else []
        ),
    )
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: None)

    monkeypatch.setattr(
        rotations.rotation_store,
        "create_block",
        lambda **kw: protocol_block("Repair 1"),
    )

    resp = client.post(
        "/api/rotations/training-blocks",
        json={
            "trainee": "Alex", "trainer": "Green", "work_center": "Repair 1",
            "start_day": "2026-07-14", "workdays": 5,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["block"]["id"] == 42
    assert body["block"]["work_center"] == "Repair 1"
    assert body["block"]["skill_ids"] == [9]
    assert body["block"]["trainer"] == "Green"


def test_training_block_endpoint_unknown_person_422(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    monkeypatch.setattr(rotations.db, "query", lambda sql, params=None: [])

    resp = client.post(
        "/api/rotations/training-blocks",
        json={
            "trainee": "Ghost", "trainer": "Green", "work_center": "Repair 1",
            "start_day": "2026-07-14", "workdays": 5,
        },
    )
    assert resp.status_code == 422
    assert "Ghost" in resp.json()["error"]


def test_training_block_endpoint_bad_date_422(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    resp = client.post(
        "/api/rotations/training-blocks",
        json={
            "trainee": "Alex", "trainer": "Green", "work_center": "Repair 1",
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
            "trainee": "Alex", "trainer": "Green", "work_center": "Repair 1",
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
    from zira_dashboard import rotation_suggestions, scheduler_time_off

    monkeypatch.setattr(staffing_route.rotation_store, "load_preferences_by_name", lambda: {})
    monkeypatch.setattr(
        rotation_suggestions, "_load_recycled_history",
        lambda d, group_locations=None, user_group_centers=None: (
            rotation_suggestions.RecycledHistory()
        ),
    )
    monkeypatch.setattr(staffing_route.rotation_training, "reconcile_blocks", lambda as_of: [])
    monkeypatch.setattr(staffing_route.app_settings, "get_setting", lambda key: ["Repair 1"])
    monkeypatch.setattr(staffing_route.rotation_store, "active_blocks_for_day", lambda d: [])
    monkeypatch.setattr(staffing_route, "_safe_time_off_entries", lambda d: [])
    monkeypatch.setattr(
        staffing_route,
        "_enabled_auto_work_centers",
        lambda d: {"Repair 1"},
    )
    monkeypatch.setattr(staffing_route.work_centers_store, "min_ops", lambda loc: loc.min_ops)
    monkeypatch.setattr(
        staffing_route.work_centers_store,
        "max_ops",
        lambda loc: 2 if loc.name == "Trim Saw 1" else 3,
    )
    monkeypatch.setattr(staffing_route.work_centers_store, "default_people", lambda loc: [])
    monkeypatch.setattr(staffing_route.work_centers_store, "group_defaults_map", lambda: {})
    monkeypatch.setattr(
        staffing_route.work_centers_store,
        "members",
        lambda kind, name: [],
    )
    monkeypatch.setattr(
        scheduler_time_off,
        "time_off_entries_for_day",
        lambda _d: [],
    )
    return staffing_route


def test_current_minimum_coverage_uses_displayed_safe_assignments(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_route

    minimums = {"Loading/Jockeying": 1, "Tablets": 4}
    required = {
        "Loading/Jockeying": ("Loading", "CPUs/VDOs", "Trailer Jockeying"),
        "Tablets": ("Tablets",),
    }
    monkeypatch.setattr(
        staffing_route,
        "_effective_minimum",
        lambda loc: minimums.get(loc.name, loc.min_ops),
    )
    monkeypatch.setattr(
        staffing_route.work_centers_store,
        "required_skills",
        lambda loc: list(required.get(loc.name, staffing.required_skills_for(loc))),
    )
    roster = [
        staffing.Person(
            "Jesus Moreno",
            skills={"Loading": 1, "CPUs/VDOs": 1, "Trailer Jockeying": 1},
        ),
        *[
            staffing.Person(name, skills={"Tablets": 1})
            for name in (
                "Trent Iverson",
                "Francisco Ramirez",
                "Iban Penaloza",
                "Isidro Moctezuma",
                "Lauro Benitez",
            )
        ],
    ]

    issues = staffing_route._current_minimum_coverage_issues(
        roster=roster,
        assignments={
            "Loading/Jockeying": ["Jesus Moreno"],
            "Tablets": [
                "Trent Iverson",
                "Francisco Ramirez",
                "Iban Penaloza",
                "Isidro Moctezuma",
                "Lauro Benitez",
            ],
        },
        time_off_entries=[],
        enabled_centers={"Loading/Jockeying", "Tablets"},
    )

    assert issues == ()


def test_current_minimum_coverage_respects_effective_zero(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_route

    loc = next(loc for loc in staffing.LOCATIONS if loc.name == "Repair 1")
    monkeypatch.setattr(
        staffing_route.work_centers_store,
        "effective",
        lambda _loc: staffing_route.work_centers_store._shape_effective(
            loc,
            {"min_ops": 0, "max_ops": loc.max_ops},
            ["Repair"],
            [],
        ),
    )

    issues = staffing_route._current_minimum_coverage_issues(
        roster=[],
        assignments={"Repair 1": []},
        time_off_entries=[],
        enabled_centers={"Repair 1"},
    )

    assert issues == ()


def test_current_minimum_coverage_excludes_people_who_cannot_cover(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_route

    monkeypatch.setattr(
        staffing_route,
        "_effective_minimum",
        lambda loc: 5 if loc.name == "Repair 1" else loc.min_ops,
    )
    monkeypatch.setattr(
        staffing_route.work_centers_store,
        "required_skills",
        lambda loc: ["Repair"] if loc.name == "Repair 1" else list(
            staffing.required_skills_for(loc)
        ),
    )
    roster = [
        staffing.Person("Qualified", skills={"Repair": 1}),
        staffing.Person("Inactive", active=False, skills={"Repair": 3}),
        staffing.Person("Reserve", reserve=True, skills={"Repair": 3}),
        staffing.Person("Unqualified", skills={"Repair": 0}),
        staffing.Person("Absent", skills={"Repair": 3}),
    ]

    issues = staffing_route._current_minimum_coverage_issues(
        roster=roster,
        assignments={
            "Repair 1": [
                "Qualified", "Inactive", "Reserve", "Unqualified", "Absent", "Unknown",
            ],
        },
        time_off_entries=[{"name": "Absent", "hours": None}],
        enabled_centers={"Repair 1"},
    )

    assert len(issues) == 1
    assert issues[0].code == "center_minimum_unmet"
    assert issues[0].centers == ("Repair 1",)
    assert issues[0].message == (
        "Repair 1 is below its minimum staffing level: "
        "1 qualified and present, minimum 5."
    )


def test_rebuild_infeasible_applies_empty_partial_schedule_and_reports_unplaced(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    staffing_route = _stub_recommendation_inputs(monkeypatch)
    saved = []
    monkeypatch.setattr(rotations.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(
        rotations.staffing,
        "load_roster",
        lambda: [_person("Gerardo Garcia", 1)],
    )
    monkeypatch.setattr(
        rotations.staffing,
        "load_schedule",
        lambda day: staffing.Schedule(day=day),
    )
    monkeypatch.setattr(
        staffing_route,
        "_recycled_suggestion_for_day",
        lambda *args, **kwargs: rotation_suggestions.RecycledSuggestion(
            assignments={},
            sources={},
            reasons={},
            warnings=(),
            complete=False,
            available_people=("Gerardo Garcia",),
            placed_people=(),
            unused_people=("Gerardo Garcia",),
            placement_issues=(schedule_solver.PlacementIssue(
                code="person_no_enabled_qualified_center",
                person="Gerardo Garcia",
                centers=(),
                message=(
                    "Gerardo Garcia has no qualified enabled work center. "
                    "Previous schedule kept."
                ),
            ),),
        ),
    )

    response = client.post(
        "/api/rotations/rebuild",
        json={"day": "2026-07-15", "mode": "normal"},
    )

    assert response.status_code == 200
    assert response.json()["applied"] is True
    assert response.json()["unplaced"] == ["Gerardo Garcia"]
    assert len(saved) == 1


def test_rebuild_complete_result_saves_once_and_preserves_metadata(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    staffing_route = _stub_recommendation_inputs(monkeypatch)
    prior = staffing.Schedule(
        day=TARGET_DAY,
        published=True,
        assignments={"Truck Driver": ["Manual Driver"]},
        notes="keep",
        wc_notes={"Truck Driver": "keep"},
        custom_hours={"start": "06:00", "end": "14:30", "breaks": []},
        published_delivery={"version": "v1"},
    )
    saved = []
    monkeypatch.setattr(
        staffing_route,
        "_configured_center_capacities",
        lambda enabled, strict=False: {name: 3 for name in enabled},
    )
    monkeypatch.setattr(
        staffing_route,
        "_effective_minimum",
        lambda loc: 0,
    )
    monkeypatch.setattr(
        rotations.staffing,
        "load_roster",
        lambda: [_person("A", 1), _person("B", 1), _person("C", 1)],
    )
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda day: prior)
    monkeypatch.setattr(rotations.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(
        staffing_route,
        "_recycled_suggestion_for_day",
        lambda *args, **kwargs: rotation_suggestions.RecycledSuggestion(
            assignments={"Repair 1": ["A", "B", "C"]},
            sources={
                "Repair 1": {name: "generated" for name in ("A", "B", "C")}
            },
            reasons={
                "Repair 1": {
                    name: "complete assignment" for name in ("A", "B", "C")
                }
            },
            warnings=(),
            complete=True,
            available_people=("A", "B", "C"),
            placed_people=("A", "B", "C"),
            unused_people=(),
        ),
    )

    response = client.post(
        "/api/rotations/rebuild",
        json={"day": TARGET_DAY.isoformat(), "mode": "normal"},
    )

    assert response.status_code == 200
    assert len(saved) == 1
    assert saved[0].assignments["Truck Driver"] == ["Manual Driver"]
    assert saved[0].notes == "keep"
    assert saved[0].wc_notes == {"Truck Driver": "keep"}
    assert saved[0].custom_hours == prior.custom_hours
    assert saved[0].published is False
    assert saved[0].published_snapshot == staffing.snapshot_of(prior)
    assert response.json()["placement"]["unplaced_people"] == []


def test_normal_rebuild_uses_enabled_auto_centers_to_distribute_defaults(
    monkeypatch,
):
    client, rotations = _rotations_client(monkeypatch)
    staffing_route = _stub_recommendation_inputs(monkeypatch)
    monkeypatch.setattr(
        staffing_route,
        "_enabled_auto_work_centers",
        lambda _day: {"Repair 1", "Repair 2", "Repair 3"},
    )
    prior = staffing.Schedule(
        day=TARGET_DAY,
        assignments={
            "Repair 1": ["Manual Inside"],
            "Truck Driver": ["Outside Auto"],
        },
        assignment_sources={
            "Repair 1": {"Manual Inside": "manual"},
            "Truck Driver": {"Outside Auto": "manual"},
        },
    )
    captured = {}
    saved = []
    monkeypatch.setattr(staffing_route, "_effective_minimum", lambda _loc: 0)
    monkeypatch.setattr(
        staffing_route,
        "_configured_center_capacities",
        lambda enabled, strict=False: {name: 3 for name in enabled},
    )
    monkeypatch.setattr(
        staffing_route,
        "_recycled_suggestion_for_day",
        lambda *args, **kwargs: (
            captured.update(kwargs)
            or rotation_suggestions.RecycledSuggestion(
                assignments={"Repair 2": ["First Repair"], "Repair 3": ["Second Repair"]},
                sources={
                    "Repair 2": {"First Repair": "generated"},
                    "Repair 3": {"Second Repair": "generated"},
                },
                reasons={}, warnings=(),
                group_locations={"Repair": ("Repair 1", "Repair 2", "Repair 3")},
                complete=True,
                available_people=("First Repair", "Second Repair"),
                placed_people=("First Repair", "Second Repair"),
            )
        ),
    )
    monkeypatch.setattr(
        rotations.staffing,
        "load_roster",
        lambda: [
            _person("First Repair", 3),
            _person("Second Repair", 3),
            _person("Outside Auto", 3),
        ],
    )
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda _day: prior)
    monkeypatch.setattr(rotations.staffing, "save_schedule", saved.append)

    response = client.post(
        "/api/rotations/rebuild",
        json={
            "day": TARGET_DAY.isoformat(),
            "mode": "normal",
        },
    )

    assert response.status_code == 200
    assert captured["enabled_work_centers"] == ["Repair 1", "Repair 2", "Repair 3"]
    assert captured["locked_assignments"] == {"Repair 1": ["Manual Inside"]}
    assert saved[0].assignments == {
        "Repair 2": ["First Repair"],
        "Repair 3": ["Second Repair"],
        "Truck Driver": ["Outside Auto"],
    }


def test_defaults_only_assignments_pins_exact_and_rotates_group_defaults():
    from zira_dashboard.routes import staffing as staffing_route

    assignments, sources = staffing_route._defaults_only_assignments(
        roster=[_person("Pinned", 1), _person("Ana", 1), _person("Bob", 1)],
        full_day_off_names=set(),
        exact_defaults={"Repair 1": ("Pinned",)},
        group_defaults={"Repair": ("Ana", "Bob")},
        user_group_centers={"Repair": ("Repair 1", "Repair 2", "Repair 3")},
        enabled_centers={"Repair 1", "Repair 2", "Repair 3"},
        center_capacities={"Repair 1": 1, "Repair 2": 1, "Repair 3": 1},
        history=rotation_suggestions.RecycledHistory(),
    )

    assert assignments == {
        "Repair 1": ["Pinned"],
        "Repair 2": ["Ana"],
        "Repair 3": ["Bob"],
    }
    assert sources == {
        "Repair 1": {"Pinned": "default"},
        "Repair 2": {"Ana": "default"},
        "Repair 3": {"Bob": "default"},
    }


def test_reset_to_defaults_replaces_assignments_and_never_runs_auto_solver(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    staffing_route = _stub_recommendation_inputs(monkeypatch)
    prior = staffing.Schedule(
        day=TARGET_DAY,
        assignments={"Repair 1": ["Old Auto"], "Truck Driver": ["Manual Driver"]},
        assignment_sources={"Repair 1": {"Old Auto": "generated"}, "Truck Driver": {"Manual Driver": "manual"}},
        notes="keep", wc_notes={"Repair 1": "keep"}, testing_day=True,
        published_snapshot={"assignments": {"Repair 1": ["Old Auto"]}},
        custom_hours={"start": "06:00", "end": "14:30", "breaks": []}, rotation_mode="training",
    )
    saved = []
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda _day: prior)
    monkeypatch.setattr(rotations.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Exact", 1), _person("Rotate", 1), _person("Absent", 1)])
    monkeypatch.setattr(rotations.scheduler_time_off, "time_off_entries_for_day", lambda _day: [{"name": "Absent", "hours": None}])
    monkeypatch.setattr(staffing_route, "_enabled_auto_work_centers", lambda _day: {"Repair 2"})
    monkeypatch.setattr(staffing_route, "_default_inputs", lambda strict=False: ({"Truck Driver": ("Exact",), "Repair 1": ("Absent",)}, {"Repair": ("Rotate",)}, {"Repair": ("Repair 1", "Repair 2")}))
    monkeypatch.setattr(rotation_suggestions, "_load_recycled_history", lambda *_args, **_kwargs: rotation_suggestions.RecycledHistory())
    monkeypatch.setattr(staffing_route, "_recycled_suggestion_for_day", lambda *_args, **_kwargs: pytest.fail("reset must not run automatic scheduling"))
    monkeypatch.setattr(rotations, "_validate_complete_rebuild", lambda *_args, **_kwargs: pytest.fail("reset must not validate a complete rebuild"))
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)

    response = client.post("/api/rotations/rebuild", json={"day": TARGET_DAY.isoformat(), "mode": "normal", "reset_to_defaults": True})

    assert response.status_code == 200
    assert len(saved) == 1
    assert saved[0].assignments == {"Truck Driver": ["Exact"], "Repair 2": ["Rotate"]}
    assert saved[0].assignment_sources == {"Truck Driver": {"Exact": "default"}, "Repair 2": {"Rotate": "default"}}
    assert saved[0].notes == prior.notes
    assert saved[0].wc_notes == prior.wc_notes
    assert saved[0].testing_day is True
    assert saved[0].published_snapshot == prior.published_snapshot
    assert saved[0].custom_hours == prior.custom_hours
    assert saved[0].rotation_mode == prior.rotation_mode
    assert response.json()["assignments"] == saved[0].assignments
    assert response.json()["sources"] == saved[0].assignment_sources
    assert response.json()["enabled_work_centers"] == ["Repair 2"]
    assert response.json()["coverage"]["issues"] == []
    assert response.json()["placement"]["issues"] == []


def test_reset_to_defaults_spreads_group_people_across_enabled_auto_centers(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    staffing_route = _stub_recommendation_inputs(monkeypatch)
    saved = []
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda _day: staffing.Schedule(day=TARGET_DAY))
    monkeypatch.setattr(rotations.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Ana", 1), _person("Bob", 1), _person("Cara", 1)])
    monkeypatch.setattr(rotations.scheduler_time_off, "time_off_entries_for_day", lambda _day: [])
    monkeypatch.setattr(staffing_route, "_enabled_auto_work_centers", lambda _day: {"Repair 1", "Repair 2", "Repair 3"})
    monkeypatch.setattr(staffing_route, "_default_inputs", lambda strict=False: ({}, {"Repair": ("Ana", "Bob", "Cara")}, {"Repair": ("Repair 1", "Repair 2", "Repair 3")}))
    # Capacities must not force this distribution: least-load selection should.
    monkeypatch.setattr(staffing_route, "_configured_center_capacities", lambda centers, strict=False: {center: 3 for center in centers})
    monkeypatch.setattr(rotations.rotation_suggestions, "_load_recycled_history", lambda *_args, **_kwargs: rotation_suggestions.RecycledHistory())
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)

    response = client.post("/api/rotations/rebuild", json={"day": TARGET_DAY.isoformat(), "mode": "normal", "reset_to_defaults": True})

    assert response.status_code == 200
    assert saved[0].assignments == {"Repair 1": ["Ana"], "Repair 2": ["Bob"], "Repair 3": ["Cara"]}


def test_reset_to_defaults_counts_exact_defaults_and_skips_full_group_centers(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    staffing_route = _stub_recommendation_inputs(monkeypatch)
    saved = []
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda _day: staffing.Schedule(day=TARGET_DAY))
    monkeypatch.setattr(rotations.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Pinned", 1), _person("Ana", 1), _person("Bob", 1)])
    monkeypatch.setattr(rotations.scheduler_time_off, "time_off_entries_for_day", lambda _day: [])
    monkeypatch.setattr(staffing_route, "_enabled_auto_work_centers", lambda _day: {"Repair 1", "Repair 2"})
    monkeypatch.setattr(staffing_route, "_default_inputs", lambda strict=False: ({"Repair 1": ("Pinned",)}, {"Repair": ("Ana", "Bob")}, {"Repair": ("Repair 1", "Repair 2")}))
    monkeypatch.setattr(staffing_route, "_configured_center_capacities", lambda centers, strict=False: {"Repair 1": 1, "Repair 2": 1})
    monkeypatch.setattr(rotations.rotation_suggestions, "_load_recycled_history", lambda *_args, **_kwargs: rotation_suggestions.RecycledHistory())
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)

    response = client.post("/api/rotations/rebuild", json={"day": TARGET_DAY.isoformat(), "mode": "normal", "reset_to_defaults": True})

    assert response.status_code == 200
    assert saved[0].assignments == {"Repair 1": ["Pinned"], "Repair 2": ["Ana"]}
    assert "Bob" not in {name for names in saved[0].assignments.values() for name in names}


def test_reset_to_defaults_skips_group_default_without_enabled_member(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    staffing_route = _stub_recommendation_inputs(monkeypatch)
    saved = []
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda _day: staffing.Schedule(day=TARGET_DAY))
    monkeypatch.setattr(rotations.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Rotate", 1)])
    monkeypatch.setattr(rotations.scheduler_time_off, "time_off_entries_for_day", lambda _day: [])
    monkeypatch.setattr(staffing_route, "_enabled_auto_work_centers", lambda _day: {"Junior #1"})
    monkeypatch.setattr(staffing_route, "_default_inputs", lambda strict=False: ({}, {"Repair": ("Rotate",)}, {"Repair": ("Repair 1", "Repair 2")}))
    monkeypatch.setattr(rotation_suggestions, "_load_recycled_history", lambda *_args, **_kwargs: rotation_suggestions.RecycledHistory())
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)

    response = client.post("/api/rotations/rebuild", json={"day": TARGET_DAY.isoformat(), "mode": "normal", "reset_to_defaults": True})

    assert response.status_code == 200
    assert saved[0].assignments == {}
    assert saved[0].assignment_sources == {}


def test_rebuild_rejects_non_boolean_reset_flag(monkeypatch):
    client, _rotations = _rotations_client(monkeypatch)

    response = client.post(
        "/api/rotations/rebuild",
        json={
            "day": TARGET_DAY.isoformat(),
            "mode": "normal",
            "reset_to_defaults": "yes",
        },
    )

    assert response.status_code == 422
    assert "boolean" in response.json()["error"]


@pytest.mark.parametrize("failed_read", ["time_off", "defaults", "minimum", "maximum"])
def test_rebuild_fails_closed_before_solving_when_authoritative_input_read_fails(
    monkeypatch, failed_read,
):
    client, rotations = _rotations_client(monkeypatch, raise_server_exceptions=False)
    staffing_route = _stub_recommendation_inputs(monkeypatch)
    saved = []
    invalidated = []
    solver_calls = []

    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Green", 3)])
    monkeypatch.setattr(
        rotations.staffing,
        "load_schedule",
        lambda _d: staffing.Schedule(day=TARGET_DAY),
    )
    monkeypatch.setattr(rotations.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(
        rotations._http_cache,
        "invalidate_today_cache",
        lambda: invalidated.append("today"),
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError(f"{failed_read} unavailable")

    failing_reader = {
        "time_off": (rotations.scheduler_time_off, "time_off_entries_for_day"),
        "defaults": (staffing_route.work_centers_store, "default_people"),
        "minimum": (staffing_route.work_centers_store, "min_ops"),
        "maximum": (staffing_route.work_centers_store, "max_ops"),
    }[failed_read]
    monkeypatch.setattr(*failing_reader, fail)
    real_solver = rotation_suggestions.suggest_recycled_assignments

    def counting_solver(*args, **kwargs):
        solver_calls.append((args, kwargs))
        return real_solver(*args, **kwargs)

    monkeypatch.setattr(rotation_suggestions, "suggest_recycled_assignments", counting_solver)

    response = client.post(
        "/api/rotations/rebuild",
        json={"day": "2026-07-14", "mode": "normal"},
    )

    assert response.status_code == 503
    assert solver_calls == []
    assert saved == []
    assert invalidated == []


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
    # A goal rebuild staffs only the enabled centers' minimums. Extra people
    # remain in the unassigned list so the operator can turn another center on.
    assert len(generated) == 1
    assert generated <= {"Green One", "Green Two"}
    assert len(body["unplaced"]) == 1
    # Every generated placement carries a source. Green/proficient placements
    # intentionally do not render a redundant visible reason badge.
    for wc, sources in body["sources"].items():
        for name, src in sources.items():
            assert src in ("generated", "manual")
    assert "green coverage" not in str(body["reasons"])


def test_rebuild_applies_safe_partial_assignments_and_reports_unplaced(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    saved = []
    issue = schedule_solver.CoverageIssue(
        center="Dismantler 1",
        group="Dismantler",
        code="training_required",
        message="Dismantler 1 could not be staffed. Training is required for Dismantler.",
    )
    suggestion = rotation_suggestions.RecycledSuggestion(
        assignments={"Repair 1": ["Qualified"]},
        sources={"Repair 1": {"Qualified": "generated"}},
        reasons={"Repair 1": {"Qualified": "Assigned to meet minimum coverage."}},
        warnings=(issue.message,),
        group_locations={
            "Repair": ("Repair 1",),
            "Dismantler": ("Dismantler 1",),
        },
        reason_codes={"Repair 1": {"Qualified": "minimum_coverage"}},
        staffed_centers=("Repair 1",),
        unresolved_centers=("Dismantler 1",),
        issues=(issue,),
        complete=False,
        available_people=("Qualified", "Missing"),
        placed_people=("Qualified",),
        unused_people=("Missing",),
        placement_issues=(schedule_solver.PlacementIssue(
            code="person_no_enabled_qualified_center",
            person="Missing",
            message="Missing cannot be safely assigned. Previous schedule kept.",
        ),),
    )
    sched = staffing.Schedule(
        day=TARGET_DAY,
        published=True,
        assignments={"Dismantler 1": ["Stale Generated"]},
        notes="Keep the daily handoff",
        wc_notes={"Repair 1": "Keep the work-center handoff"},
        testing_day=True,
        published_snapshot={"assignments": {"Repair 1": ["Published Person"]}},
        custom_hours={"shift_start": "06:30", "shift_end": "15:00"},
        assignment_sources={"Dismantler 1": {"Stale Generated": "generated"}},
    )

    monkeypatch.setattr(
        rotations.staffing_route,
        "_enabled_auto_work_centers",
        lambda _d: {"Repair 1", "Dismantler 1"},
    )
    monkeypatch.setattr(rotations.staffing_route.work_centers_store, "default_people", lambda _loc: [])
    monkeypatch.setattr(
        rotations.staffing_route.work_centers_store, "group_defaults_map", lambda: {}
    )
    monkeypatch.setattr(rotations.scheduler_time_off, "time_off_entries_for_day", lambda _d: [])
    monkeypatch.setattr(
        rotations.staffing_route.work_centers_store,
        "min_ops",
        lambda loc: loc.min_ops,
    )
    monkeypatch.setattr(
        rotations.staffing_route.work_centers_store,
        "max_ops",
        lambda loc: loc.max_ops,
    )
    monkeypatch.setattr(
        rotations.staffing_route,
        "_recycled_suggestion_for_day",
        lambda *args, **kwargs: suggestion,
    )
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Qualified", 3)])
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda _d: sched)
    monkeypatch.setattr(rotations.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)

    response = client.post(
        "/api/rotations/rebuild",
        json={"day": TARGET_DAY.isoformat(), "mode": "normal"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["applied"] is True
    assert body["assignments"]["Repair 1"] == ["Qualified"]
    assert body["unplaced"] == ["Missing"]
    assert body["placement"]["issues"][0]["code"] == (
        "person_no_enabled_qualified_center"
    )
    assert saved[0].assignments["Repair 1"] == ["Qualified"]


















def test_recycled_context_reports_invalid_minimum_above_maximum(monkeypatch):
    staffing_route = _stub_recommendation_inputs(monkeypatch)
    monkeypatch.setattr(
        staffing_route,
        "_auto_group_maps",
        lambda _enabled: ({"Repair": ("Repair 1",)}, {"Repair": ("Repair",)}),
    )
    monkeypatch.setattr(staffing_route.work_centers_store, "min_ops", lambda _loc: 2)
    monkeypatch.setattr(staffing_route.work_centers_store, "max_ops", lambda _loc: 1)

    context = staffing_route._recycled_context_for_day(
        TARGET_DAY,
        roster=[_person("Green A", 3), _person("Green B", 3)],
        mode="normal",
        base_assignments={},
        locked_assignments={},
        time_off_entries=[],
        enabled_work_centers={"Repair 1"},
        assignment_sources={},
        work_weekdays=frozenset({0, 1, 2, 3, 4}),
    )

    assert context["rotation_issues"][0]["code"] == "invalid_center_configuration"
    assert "minimum of 2 but a maximum of 1" in context["rotation_issues"][0]["message"]


def test_recycled_context_uses_current_staffing_instead_of_auto_preview_shortage(
    monkeypatch,
):
    staffing_route = _stub_recommendation_inputs(monkeypatch)
    preview_message = "Repair 1 is below its minimum Auto staffing level."
    monkeypatch.setattr(
        staffing_route,
        "_auto_group_maps",
        lambda _enabled: ({"Repair": ("Repair 1",)}, {"Repair": ("Repair",)}),
    )
    monkeypatch.setattr(
        staffing_route.work_centers_store,
        "required_skills",
        lambda loc: ["Repair"] if loc.name == "Repair 1" else list(
            staffing.required_skills_for(loc)
        ),
    )
    monkeypatch.setattr(
        rotation_suggestions,
        "suggest_recycled_assignments",
        lambda **_kwargs: rotation_suggestions.RecycledSuggestion(
            assignments={},
            sources={},
            reasons={},
            warnings=(preview_message, "Keep this training warning."),
            group_locations={"Repair": ("Repair 1",)},
            placement_issues=(
                schedule_solver.PlacementIssue(
                    code="center_minimum_unmet",
                    centers=("Repair 1",),
                    message=preview_message,
                ),
                schedule_solver.PlacementIssue(
                    code="person_unplaced",
                    person="Preview Person",
                    message=(
                        "Preview Person could not be placed in an enabled Auto work center."
                    ),
                ),
            ),
        ),
    )

    context = staffing_route._recycled_context_for_day(
        TARGET_DAY,
        roster=[_person("Qualified", 3)],
        mode="normal",
        base_assignments={},
        locked_assignments={},
        time_off_entries=[],
        enabled_work_centers={"Repair 1"},
        assignment_sources={},
        current_assignments={"Repair 1": ["Qualified"]},
        work_weekdays=frozenset({0, 1, 2, 3, 4}),
    )

    assert context["rotation_issues"] == []
    assert context["rotation_warnings"] == ["Keep this training warning."]


def test_page_placement_issues_hide_only_disabled_defaults_on_off_saturday():
    from zira_dashboard.routes import staffing as staffing_route

    disabled_default = schedule_solver.PlacementIssue(
        code="exact_default_center_disabled", person="Ana", centers=("Repair 1",),
        message="Ana's default work center Repair 1 is not enabled.",
    )
    unrelated = schedule_solver.PlacementIssue(
        code="exact_default_unqualified", person="Ben", centers=("Repair 2",),
        message="Ben is not qualified for default work center Repair 2.",
    )

    assert staffing_route._page_placement_issues_for_day(
        date(2026, 7, 18), frozenset({0, 1, 2, 3, 4}),
        (disabled_default, unrelated),
    ) == (unrelated,)


@pytest.mark.parametrize(
    ("day", "work_weekdays"),
    [
        (date(2026, 7, 17), frozenset({0, 1, 2, 3, 4})),
        (date(2026, 7, 18), frozenset({0, 1, 2, 3, 4, 5})),
    ],
)
def test_page_placement_issues_keep_disabled_defaults_on_working_days(
    day, work_weekdays,
):
    from zira_dashboard.routes import staffing as staffing_route

    issue = schedule_solver.PlacementIssue(
        code="exact_default_center_disabled", person="Ana", centers=("Repair 1",),
        message="Ana's default work center Repair 1 is not enabled.",
    )

    assert staffing_route._page_placement_issues_for_day(
        day, work_weekdays, (issue,),
    ) == (issue,)


def test_recycled_context_defers_current_minimum_shortage_until_an_action(monkeypatch):
    staffing_route = _stub_recommendation_inputs(monkeypatch)
    monkeypatch.setattr(
        staffing_route,
        "_effective_minimum",
        lambda loc: 1 if loc.name == "Repair 1" else loc.min_ops,
    )
    monkeypatch.setattr(
        staffing_route.work_centers_store,
        "required_skills",
        lambda loc: ["Repair"] if loc.name == "Repair 1" else list(
            staffing.required_skills_for(loc)
        ),
    )

    def preview_unavailable(**_kwargs):
        raise RuntimeError("preview unavailable")

    monkeypatch.setattr(
        rotation_suggestions,
        "suggest_recycled_assignments",
        preview_unavailable,
    )

    context = staffing_route._recycled_context_for_day(
        TARGET_DAY,
        roster=[_person("Qualified", 3)],
        mode="normal",
        base_assignments={},
        locked_assignments={},
        time_off_entries=[],
        enabled_work_centers={"Repair 1"},
        assignment_sources={},
        current_assignments={"Repair 1": []},
        work_weekdays=frozenset({0, 1, 2, 3, 4}),
    )

    assert context["rotation_issues"] == []




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


def test_rebuild_treats_default_people_as_exact_generated_anchors(monkeypatch):
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
    assert "Default Green" in body["assignments"]["Repair 1"]
    assert body["sources"]["Repair 1"]["Default Green"] == "generated"
    assert not any(
        issue["code"] in {"exact_default_violation", "exact_default_unqualified"}
        for issue in body["placement"]["issues"]
    )
    assert "Default Green" in saved[-1].assignments["Repair 1"]
    assert saved[-1].assignment_sources["Repair 1"]["Default Green"] == "generated"


def test_auto_work_centers_endpoint_saves_global_setting(monkeypatch):
    from zira_dashboard import scheduler_time_off

    client, rotations = _rotations_client(monkeypatch)
    _stub_recommendation_inputs(monkeypatch)
    saved: dict[str, list[str]] = {}
    invalidated: list[str] = []

    monkeypatch.setattr(
        rotations.staffing_route.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: saved.setdefault(key, value),
    )
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [])
    monkeypatch.setattr(
        rotations.staffing,
        "load_schedule",
        lambda d: staffing.Schedule(day=d),
    )
    monkeypatch.setattr(scheduler_time_off, "time_off_entries_for_day", lambda d: [])
    monkeypatch.setattr(
        rotations.staffing_route,
        "_recycled_suggestion_for_day",
        lambda *args, **kwargs: rotation_suggestions.RecycledSuggestion({}, {}, {}, ()),
    )
    monkeypatch.setattr(rotations.staffing_route.work_centers_store, "default_people", lambda _loc: [])
    monkeypatch.setattr(rotations.db, "cursor", lambda: nullcontext(object()))
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
    assert resp.json()["ok"] is True
    assert resp.json()["enabled_work_centers"] == ["Repair 1", "Junior #1"]
    assert resp.json()["placement"]["issues"] == []
    assert saved[rotations.staffing_route.AUTO_SCHEDULE_WC_SETTING] == ["Repair 1", "Junior #1"]
    assert invalidated == ["today", "stable"]


def test_auto_work_centers_endpoint_removes_non_empty_turn_off_selection(monkeypatch):
    from zira_dashboard import scheduler_time_off

    client, rotations = _rotations_client(monkeypatch)
    _stub_recommendation_inputs(monkeypatch)
    saved: list[tuple[str, ...]] = []

    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [])
    monkeypatch.setattr(
        rotations.staffing,
        "load_schedule",
        lambda d: staffing.Schedule(day=d),
    )
    monkeypatch.setattr(scheduler_time_off, "time_off_entries_for_day", lambda d: [])
    monkeypatch.setattr(
        rotations.staffing_route,
        "_save_enabled_auto_work_centers",
        lambda centers, *, cur=None: saved.append(tuple(centers)) or list(centers),
    )
    monkeypatch.setattr(rotations.db, "cursor", lambda: nullcontext(object()))
    monkeypatch.setattr(rotations.staffing_route.work_centers_store, "default_people", lambda _loc: [])
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: None)

    response = client.post(
        "/api/rotations/auto-work-centers",
        json={
            "day": "2026-07-14",
            "work_centers": ["Repair 1", "Repair 2", "Dismantler 1"],
            "turn_off": ["Repair 2"],
        },
    )

    assert response.status_code == 200
    assert response.json()["enabled_work_centers"] == ["Repair 1", "Dismantler 1"]
    assert saved == [("Repair 1", "Dismantler 1")]


def test_auto_work_centers_turning_off_a_populated_center_clears_its_draft_assignments(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    _stub_recommendation_inputs(monkeypatch)
    schedule = staffing.Schedule(
        day=TARGET_DAY,
        assignments={"Repair 1": ["Jordan"], "Work Orders": ["Juan"]},
    )
    saved_schedules = []
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [])
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda _day: schedule)
    monkeypatch.setattr(rotations.scheduler_time_off, "time_off_entries_for_day", lambda _day: [])
    monkeypatch.setattr(
        rotations.staffing,
        "save_schedule",
        lambda changed, *, cur=None: saved_schedules.append((changed, cur)),
    )
    monkeypatch.setattr(
        rotations.staffing_route,
        "_save_enabled_auto_work_centers",
        lambda names, *, cur=None: list(names),
    )
    monkeypatch.setattr(rotations.db, "cursor", lambda: nullcontext(object()))
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: None)

    response = client.post("/api/rotations/auto-work-centers", json={
        "day": TARGET_DAY.isoformat(),
        "work_centers": ["Repair 1"],
        "turn_off": ["Work Orders"],
    })

    assert response.status_code == 200
    assert response.json()["assignments"] == {"Repair 1": ["Jordan"]}
    assert len(saved_schedules) == 1
    assert saved_schedules[0][0].assignments == {"Repair 1": ["Jordan"]}
    assert saved_schedules[0][1] is not None
    assert schedule.assignments == {"Repair 1": ["Jordan"], "Work Orders": ["Juan"]}


def test_auto_center_selection_saves_quietly_without_solver_preview(monkeypatch):
    from zira_dashboard import scheduler_time_off

    client, rotations = _rotations_client(monkeypatch)
    _stub_recommendation_inputs(monkeypatch)
    saved = []
    monkeypatch.setattr(
        rotations.staffing_route,
        "_recycled_suggestion_for_day",
        lambda *args, **kwargs: pytest.fail("toggle must not build a solver preview"),
    )
    monkeypatch.setattr(
        rotations.staffing_route,
        "_save_enabled_auto_work_centers",
        lambda centers, *, cur=None: saved.append(tuple(centers)) or list(centers),
    )
    monkeypatch.setattr(rotations.staffing_route.work_centers_store, "default_people", lambda _loc: [])
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Qualified", 3)])
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda d: staffing.Schedule(day=d))
    monkeypatch.setattr(
        rotations.staffing,
        "save_schedule",
        lambda _schedule, *, cur=None: (_ for _ in ()).throw(
            AssertionError("Auto-center selection must not save a schedule")
        ),
    )
    monkeypatch.setattr(rotations.db, "cursor", lambda: nullcontext(object()))
    monkeypatch.setattr(scheduler_time_off, "time_off_entries_for_day", lambda d: [])

    resp = client.post("/api/rotations/auto-work-centers", json={
        "day": "2026-07-14",
        "work_centers": ["Repair 1", "Dismantler 1"],
        "turn_off": [],
    })

    assert resp.status_code == 200
    assert saved[-1] == ("Repair 1", "Dismantler 1")
    assert resp.json()["coverage"] == {
        "staffed_centers": [],
        "unresolved_centers": [],
        "issues": [],
    }
    assert resp.json()["warnings"] == []
    assert resp.json()["placement"]["issues"] == []
    assert resp.json()["enabled_work_centers"] == ["Repair 1", "Dismantler 1"]


def test_auto_center_selection_persists_posted_schedule_as_draft_first(monkeypatch):
    """A current Posted page must land on its persisted Draft after a toggle."""
    from zira_dashboard import scheduler_time_off

    client, rotations = _rotations_client(monkeypatch)
    _stub_recommendation_inputs(monkeypatch)
    events: list[str] = []
    saved_schedules: list[staffing.Schedule] = []
    posted = staffing.Schedule(
        day=TARGET_DAY,
        published=True,
        assignments={"Repair 1": ["Qualified"]},
        published_delivery={"version": "posted-v1"},
    )

    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Qualified", 3)])
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda _day: posted)
    monkeypatch.setattr(
        rotations.staffing,
        "save_schedule",
        lambda schedule, *, cur=None: events.append("schedule") or saved_schedules.append(schedule),
    )
    monkeypatch.setattr(scheduler_time_off, "time_off_entries_for_day", lambda _day: [])
    monkeypatch.setattr(
        rotations.staffing_route,
        "_save_enabled_auto_work_centers",
        lambda centers, *, cur=None: events.append("centers") or list(centers),
    )
    monkeypatch.setattr(rotations.db, "cursor", lambda: nullcontext(object()))
    monkeypatch.setattr(rotations.staffing_route.work_centers_store, "default_people", lambda _loc: [])
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: None)

    response = client.post("/api/rotations/auto-work-centers", json={
        "day": "2026-07-14",
        "work_centers": ["Repair 1"],
        "turn_off": [],
    })

    assert response.status_code == 200
    assert events == ["schedule", "centers"]
    assert len(saved_schedules) == 1
    assert saved_schedules[0].published is False
    assert saved_schedules[0].published_snapshot["assignments"] == {"Repair 1": ["Qualified"]}
    assert saved_schedules[0].published_delivery == {}


def test_auto_center_endpoint_saves_when_minimum_lookup_fails(monkeypatch):
    from zira_dashboard import scheduler_time_off

    client, rotations = _rotations_client(monkeypatch, raise_server_exceptions=False)
    _stub_recommendation_inputs(monkeypatch)
    saved = []
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Green", 3, "Hand Build")])
    monkeypatch.setattr(
        rotations.staffing,
        "load_schedule",
        lambda d: staffing.Schedule(day=d),
    )
    monkeypatch.setattr(scheduler_time_off, "time_off_entries_for_day", lambda d: [])
    monkeypatch.setattr(
        rotations.staffing_route.work_centers_store,
        "min_ops",
        lambda loc: (_ for _ in ()).throw(RuntimeError("settings unavailable")),
    )
    monkeypatch.setattr(
        rotations.staffing_route,
        "_save_enabled_auto_work_centers",
        lambda names, *, cur=None: saved.append(names),
    )
    monkeypatch.setattr(rotations.db, "cursor", lambda: nullcontext(object()))

    resp = client.post("/api/rotations/auto-work-centers", json={
        "day": "2026-07-14",
        "work_centers": ["Hand Build #2"],
        "turn_off": [],
    })

    assert resp.status_code == 200
    assert saved == [["Hand Build #2"]]


def test_auto_center_endpoint_saves_when_training_effect_read_fails(monkeypatch):
    from zira_dashboard import scheduler_time_off

    client, rotations = _rotations_client(monkeypatch, raise_server_exceptions=False)
    _stub_recommendation_inputs(monkeypatch)
    saved = []
    invalidated = []
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Green", 3)])
    monkeypatch.setattr(
        rotations.staffing,
        "load_schedule",
        lambda d: staffing.Schedule(day=d),
    )
    monkeypatch.setattr(scheduler_time_off, "time_off_entries_for_day", lambda d: [])
    monkeypatch.setattr(
        rotations.staffing_route.rotation_store,
        "active_blocks_for_day",
        lambda _d: (_ for _ in ()).throw(RuntimeError("blocks unavailable")),
    )
    monkeypatch.setattr(rotations.staffing_route.work_centers_store, "min_ops", lambda loc: loc.min_ops)
    monkeypatch.setattr(
        rotations.staffing_route,
        "_save_enabled_auto_work_centers",
        lambda names, *, cur=None: saved.append(names),
    )
    monkeypatch.setattr(rotations.db, "cursor", lambda: nullcontext(object()))
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

    assert resp.status_code == 200
    assert saved == [["Repair 1"]]
    assert invalidated == ["today", "stable"]


def test_auto_center_endpoint_fails_closed_when_time_off_read_fails(monkeypatch):
    from zira_dashboard import scheduler_time_off

    client, rotations = _rotations_client(monkeypatch, raise_server_exceptions=False)
    saved = []
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Green", 3)])
    monkeypatch.setattr(
        rotations.staffing,
        "load_schedule",
        lambda d: staffing.Schedule(day=d),
    )
    monkeypatch.setattr(
        scheduler_time_off,
        "time_off_entries_for_day",
        lambda d: (_ for _ in ()).throw(RuntimeError("time off unavailable")),
    )
    monkeypatch.setattr(
        rotations.staffing_route,
        "_save_enabled_auto_work_centers",
        lambda names, *, cur=None: saved.append(names),
    )
    monkeypatch.setattr(rotations.db, "cursor", lambda: nullcontext(object()))

    resp = client.post("/api/rotations/auto-work-centers", json={
        "day": "2026-07-14",
        "work_centers": ["Repair 1"],
        "turn_off": [],
    })

    assert resp.status_code == 503
    assert saved == []


def test_auto_center_endpoint_saves_when_default_read_fails(monkeypatch):
    from zira_dashboard import scheduler_time_off

    client, rotations = _rotations_client(monkeypatch, raise_server_exceptions=False)
    saved = []
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Green", 3)])
    monkeypatch.setattr(
        rotations.staffing,
        "load_schedule",
        lambda d: staffing.Schedule(day=d),
    )
    monkeypatch.setattr(scheduler_time_off, "time_off_entries_for_day", lambda d: [])
    monkeypatch.setattr(
        rotations.staffing_route.work_centers_store,
        "default_people",
        lambda loc: (_ for _ in ()).throw(RuntimeError("defaults unavailable")),
    )
    monkeypatch.setattr(
        rotations.staffing_route,
        "_save_enabled_auto_work_centers",
        lambda names, *, cur=None: saved.append(names),
    )
    monkeypatch.setattr(rotations.db, "cursor", lambda: nullcontext(object()))

    resp = client.post("/api/rotations/auto-work-centers", json={
        "day": "2026-07-14",
        "work_centers": ["Repair 1"],
        "turn_off": [],
    })

    assert resp.status_code == 200
    assert saved == [["Repair 1"]]


def test_auto_work_centers_persists_selection_on_saturday(monkeypatch):
    from zira_dashboard import scheduler_time_off

    client, rotations = _rotations_client(monkeypatch)
    _stub_recommendation_inputs(monkeypatch)
    saved: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        rotations.staffing,
        "load_roster",
        lambda: [],
    )
    monkeypatch.setattr(
        rotations.staffing,
        "load_schedule",
        lambda d: staffing.Schedule(day=d),
    )
    monkeypatch.setattr(scheduler_time_off, "time_off_entries_for_day", lambda d: [])
    monkeypatch.setattr(
        rotations.staffing_route,
        "_recycled_suggestion_for_day",
        lambda *args, **kwargs: rotation_suggestions.RecycledSuggestion({}, {}, {}, ()),
    )
    monkeypatch.setattr(
        rotations.staffing_route,
        "_save_enabled_auto_work_centers",
        lambda centers, *, cur: saved.append(tuple(centers)) or list(centers),
    )
    monkeypatch.setattr(rotations.db, "cursor", _RouteTransaction)
    monkeypatch.setattr(rotations.saturday_recruiting_store, "get", lambda day, *, cur: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: None)

    response = client.post("/api/rotations/auto-work-centers", json={
        "day": "2026-07-18",
        "work_centers": ["Repair 1"],
        "turn_off": [],
    })

    assert response.status_code == 200
    assert response.json()["enabled_work_centers"] == ["Repair 1"]
    assert response.json()["saturday_recruiting"] is None
    assert saved == [("Repair 1",)]


def test_auto_work_centers_updates_open_saturday_recruiting_demand(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    bundle = SimpleNamespace(
        recruitment=SimpleNamespace(status="recruiting", shift_start=time(6), shift_end=time(12)),
    )
    updated = []
    monkeypatch.setattr(rotations.db, "cursor", _RouteTransaction)
    monkeypatch.setattr(rotations.saturday_recruiting_store, "get", lambda day, *, cur: bundle)
    monkeypatch.setattr(rotations.staffing_route, "_saturday_recruit_requested_counts", lambda enabled: {17: 2, 18: 1})
    monkeypatch.setattr(rotations.saturday_recruiting_store, "update_openings", lambda **kwargs: updated.append(kwargs) or bundle)
    monkeypatch.setattr(rotations.saturday_recruiting_store, "serialize_bundle", lambda bundle: {"updated": True})
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [])
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda d: staffing.Schedule(day=d))
    monkeypatch.setattr(rotations.scheduler_time_off, "time_off_entries_for_day", lambda d: [])
    monkeypatch.setattr(rotations.staffing_route, "_save_enabled_auto_work_centers", lambda centers, *, cur: list(centers))
    monkeypatch.setattr(rotations.staffing_route, "_minimum_crew_balance_for_day", lambda **kwargs: ())
    monkeypatch.setattr(rotations.staffing_route, "_minimum_crew_balance_payload", lambda balance: {})
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: None)

    response = client.post("/api/rotations/auto-work-centers", json={
        "day": "2026-07-18", "work_centers": ["Repair 1", "Repair 2"], "turn_off": [],
    })

    assert response.status_code == 200
    assert updated[0]["requested_counts"] == {17: 2, 18: 1}
    assert updated[0]["shift_start"] == time(6)
    assert response.json()["saturday_recruiting"] == {"updated": True}


def test_auto_work_centers_allows_toggling_after_saturday_recruiting_closes(monkeypatch):
    """A closed volunteer round must not prevent an internal center toggle."""
    client, rotations = _rotations_client(monkeypatch)
    bundle = SimpleNamespace(
        recruitment=SimpleNamespace(status="closed", shift_start=time(6), shift_end=time(12)),
    )
    saved = []
    monkeypatch.setattr(rotations.db, "cursor", _RouteTransaction)
    monkeypatch.setattr(rotations.saturday_recruiting_store, "get", lambda day, *, cur: bundle)
    monkeypatch.setattr(
        rotations.saturday_recruiting_store,
        "update_openings",
        lambda **kwargs: pytest.fail("closed recruiting must not be expanded by a center toggle"),
    )
    monkeypatch.setattr(rotations.saturday_recruiting_store, "serialize_bundle", lambda _: {"closed": True})
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [])
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda d: staffing.Schedule(day=d))
    monkeypatch.setattr(rotations.scheduler_time_off, "time_off_entries_for_day", lambda d: [])
    monkeypatch.setattr(
        rotations.staffing_route,
        "_save_enabled_auto_work_centers",
        lambda centers, *, cur: saved.append(tuple(centers)) or list(centers),
    )
    monkeypatch.setattr(rotations.staffing_route, "_minimum_crew_balance_for_day", lambda **kwargs: ())
    monkeypatch.setattr(rotations.staffing_route, "_minimum_crew_balance_payload", lambda balance: {})
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: None)

    response = client.post("/api/rotations/auto-work-centers", json={
        "day": "2026-07-18", "work_centers": ["Junior #3"], "turn_off": [],
    })

    assert response.status_code == 200
    assert saved == [("Junior #3",)]
    assert response.json()["saturday_recruiting"] == {"closed": True}


def test_auto_work_centers_rejects_saturday_toggle_that_breaks_commitments(monkeypatch):
    client, rotations = _rotations_client(monkeypatch, raise_server_exceptions=False)
    saved = []
    bundle = SimpleNamespace(
        recruitment=SimpleNamespace(status="recruiting", shift_start=time(6), shift_end=time(12)),
    )
    monkeypatch.setattr(rotations.db, "cursor", _RouteTransaction)
    monkeypatch.setattr(rotations.saturday_recruiting_store, "get", lambda day, *, cur: bundle)
    monkeypatch.setattr(rotations.saturday_recruiting_store, "serialize_bundle", lambda bundle: {"updated": True})
    monkeypatch.setattr(rotations.staffing_route, "_saturday_recruit_requested_counts", lambda enabled: {})
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [])
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda d: staffing.Schedule(day=d))
    monkeypatch.setattr(rotations.scheduler_time_off, "time_off_entries_for_day", lambda d: [])
    monkeypatch.setattr(
        rotations.staffing_route,
        "_save_enabled_auto_work_centers",
        lambda centers, *, cur: saved.append(centers) or list(centers),
    )
    monkeypatch.setattr(rotations.staffing_route, "_minimum_crew_balance_for_day", lambda **kwargs: ())
    monkeypatch.setattr(rotations.staffing_route, "_minimum_crew_balance_payload", lambda balance: {})
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: None)
    monkeypatch.setattr(
        rotations.saturday_recruiting_store, "update_openings",
        lambda **kwargs: (_ for _ in ()).throw(
            rotations.saturday_recruiting_store.LifecycleConflict(
                "Requested openings cannot drop below committed Saturday coverage"
            )
        ),
    )

    response = client.post("/api/rotations/auto-work-centers", json={
        "day": "2026-07-18", "work_centers": [], "turn_off": [],
    })

    assert response.status_code == 409
    assert "committed Saturday coverage" in response.json()["error"]
    assert saved == []


def test_auto_work_centers_rolls_back_recruiting_when_settings_persist_fails(monkeypatch):
    """Saturday demand and the enabled-center setting commit as one unit."""
    client, rotations = _rotations_client(monkeypatch, raise_server_exceptions=False)
    persisted = {"recruiting": {17: 1}, "enabled": ["Repair 1"]}

    class Transaction:
        def __enter__(self):
            self.before = {key: value.copy() for key, value in persisted.items()}
            return self

        def __exit__(self, exc_type, exc, traceback):
            if exc_type is not None:
                persisted.clear()
                persisted.update(self.before)
            return False

    transaction = Transaction()
    bundle = SimpleNamespace(
        recruitment=SimpleNamespace(status="recruiting", shift_start=time(6), shift_end=time(12)),
    )
    monkeypatch.setattr(rotations.db, "cursor", lambda: transaction)
    monkeypatch.setattr(rotations.saturday_recruiting_store, "get", lambda day, *, cur: bundle)
    monkeypatch.setattr(
        rotations.saturday_recruiting_store,
        "update_openings",
        lambda **kwargs: persisted.update(recruiting=kwargs["requested_counts"]) or bundle,
    )
    monkeypatch.setattr(rotations.staffing_route, "_saturday_recruit_requested_counts", lambda enabled: {17: 3})
    monkeypatch.setattr(
        rotations.staffing_route,
        "_save_enabled_auto_work_centers",
        lambda centers, *, cur: (_ for _ in ()).throw(OSError("settings unavailable")),
    )
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [])
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda d: staffing.Schedule(day=d))
    monkeypatch.setattr(rotations.scheduler_time_off, "time_off_entries_for_day", lambda d: [])

    response = client.post("/api/rotations/auto-work-centers", json={
        "day": "2026-07-18", "work_centers": ["Repair 1"], "turn_off": [],
    })

    assert response.status_code == 503
    assert persisted == {"recruiting": {17: 1}, "enabled": ["Repair 1"]}


def test_auto_work_centers_uses_real_saturday_demand_derivation(monkeypatch):
    """The route filters enabled centers and maps their configured crew demand."""
    from zira_dashboard import saturday_recruiting_store as store

    client, rotations = _rotations_client(monkeypatch)
    transaction = _RouteTransaction()
    bundle = SimpleNamespace(
        recruitment=SimpleNamespace(status="recruiting", shift_start=time(6), shift_end=time(12)),
    )
    repair_1 = staffing.Location("Repair 1", "Repair", "Bay", "Recycled", None, min_ops=1, max_ops=4)
    repair_2 = staffing.Location("Repair 2", "Repair", "Bay", "Recycled", None, min_ops=1, max_ops=4)
    repair_3 = staffing.Location("Repair 3", "Repair", "Bay", "Recycled", None, min_ops=1, max_ops=4)
    captured = {}
    monkeypatch.setattr(rotations.db, "cursor", lambda: transaction)
    monkeypatch.setattr(rotations.saturday_recruiting_store, "get", lambda day, *, cur: bundle)
    monkeypatch.setattr(
        rotations.saturday_recruiting_store, "available_positions", lambda: (
            store.AvailablePosition(17, "Repair 1", ("Repair",)),
            store.AvailablePosition(18, "Repair 2", ("Repair",)),
            store.AvailablePosition(19, "Repair 3", ("Repair",)),
        ),
    )
    monkeypatch.setattr(rotations.staffing, "LOCATIONS", (repair_1, repair_2, repair_3))
    monkeypatch.setattr(
        rotations.staffing_route.work_centers_store, "min_ops",
        lambda loc: {"Repair 1": 3, "Repair 2": 0, "Repair 3": 5}[loc.name],
    )
    monkeypatch.setattr(
        rotations.saturday_recruiting_store, "update_openings",
        lambda **kwargs: captured.update(kwargs) or bundle,
    )
    monkeypatch.setattr(rotations.saturday_recruiting_store, "serialize_bundle", lambda _: {"updated": True})
    monkeypatch.setattr(
        rotations.staffing_route, "_save_enabled_auto_work_centers", lambda centers, *, cur: list(centers),
    )
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [])
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda d: staffing.Schedule(day=d))
    monkeypatch.setattr(rotations.scheduler_time_off, "time_off_entries_for_day", lambda d: [])
    monkeypatch.setattr(rotations.staffing_route, "_minimum_crew_balance_for_day", lambda **kwargs: ())
    monkeypatch.setattr(rotations.staffing_route, "_minimum_crew_balance_payload", lambda balance: {})
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: None)

    response = client.post("/api/rotations/auto-work-centers", json={
        "day": "2026-07-18", "work_centers": ["Repair 1", "Repair 2"], "turn_off": [],
    })

    assert response.status_code == 200
    assert captured["requested_counts"] == {17: 3}
    assert captured["cur"] is transaction


def test_rebuild_persists_schedule_on_saturday(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    staffing_route = _stub_recommendation_inputs(monkeypatch)
    saturday = date(2026, 7, 18)
    saved: list[staffing.Schedule] = []
    prior = staffing.Schedule(day=saturday)
    monkeypatch.setattr(
        staffing_route,
        "_configured_center_capacities",
        lambda enabled, strict=False: {name: 3 for name in enabled},
    )
    monkeypatch.setattr(staffing_route, "_effective_minimum", lambda loc: 0)
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Green", 3)])
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda d: prior)
    monkeypatch.setattr(rotations.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(
        staffing_route,
        "_recycled_suggestion_for_day",
        lambda *args, **kwargs: rotation_suggestions.RecycledSuggestion(
            assignments={"Repair 1": ["Green"]},
            sources={"Repair 1": {"Green": "generated"}},
            reasons={},
            warnings=(),
            complete=True,
            available_people=("Green",),
            placed_people=("Green",),
            unused_people=(),
        ),
    )
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)

    response = client.post("/api/rotations/rebuild", json={
        "day": saturday.isoformat(),
        "mode": "normal",
    })

    assert response.status_code == 200
    assert response.json()["applied"] is True
    assert [schedule.day for schedule in saved] == [saturday]
    assert saved[0].assignments == {"Repair 1": ["Green"]}


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


def test_staffing_exposes_unified_training_setup_and_removes_row_toggles():
    html = (ROOT / "src/zira_dashboard/templates/staffing.html").read_text()
    js = (ROOT / "src/zira_dashboard/static/staffing.js").read_text()
    print_css = (ROOT / "src/zira_dashboard/static/staffing-print.css").read_text()

    assert 'id="training-protocol-open"' in html
    assert 'id="training-protocol-modal"' in html
    assert 'class="wc-training-cb"' not in html
    assert "setWcTraining" not in js
    assert ".wc-training-toggle" not in print_css
    assert "/api/rotations/training-blocks" in js


def test_people_matrix_no_longer_renders_training_block_form():
    html = (ROOT / "src/zira_dashboard/templates/skills.html").read_text()

    assert 'id="rotation-block-form"' not in html
    assert "Start Recycled level-0 training block" not in html


def test_staffing_has_rotation_mode_controls_without_automated_person_notes():
    html = (ROOT / "src/zira_dashboard/templates/staffing.html").read_text()
    js = (ROOT / "src/zira_dashboard/static/staffing.js").read_text()
    css = (ROOT / "src/zira_dashboard/static/staffing.css").read_text()
    assert 'data-rotation-mode="optimized"' in html
    assert 'data-rotation-mode="normal"' in html
    assert 'data-rotation-mode="training"' in html
    assert 'aria-label="Optimized schedule goal"' in html
    assert 'aria-label="Normal schedule goal"' in html
    assert 'aria-label="Training schedule goal"' in html
    assert 'title="Optimized: strongest coverage"' in html
    assert 'title="Normal: balanced coverage and fair rotation"' in html
    assert 'title="Training: develop operator skills"' in html
    assert 'title="Optimized: strongest coverage">⚡</button>' in html
    assert '⚡⚡⚡' not in html
    assert '⚖' in html
    assert '🎓' in html
    assert 'data-work-center-toggle' in html
    assert 'role="switch"' in html
    assert 'aria-checked="{{ _center_on|tojson }}"' in html
    assert 'class="wc-auto-cb"' not in html
    assert 'class="wc-on-off-label"' not in html
    assert "rotation_reasons" not in html
    assert "ROTATION_REASONS" not in html
    assert "ROTATION_REASONS" not in js
    assert "appendReasonBadge" not in js
    assert "rotation-reason" not in html
    assert "rotation-reason" not in js
    assert "rotation-reason" not in css
    assert 'id="rotation-warnings"' in html
    assert 'name="notes"' in html
    assert 'name="wc_note__{{ row.loc.name }}"' in html
    assert "/api/rotations/rebuild" in js
    assert "/api/rotations/auto-work-centers" in js
    assert "function postAutoCenters(workCenters, turnOff)" in js
    assert "JSON.stringify({ day, work_centers: workCenters, turn_off: turnOff })" in js
    assert 'rotation-reset-btn' not in html
    assert 'Reset auto assignments' not in html
    assert "const resetBtn" not in js
    assert "modeBtns.forEach(btn => {" in js
    assert "btn.addEventListener('click', () => rebuild(btn.dataset.rotationMode));" in js
    assert "data.unplaced" in js
    assert "could not be placed in an enabled Auto work center" in js
    assert 'id="rotation-auto-summary"' in html
    assert "data-minimum-crew-balance='{{ minimum_crew_balance|default({}, true)|tojson }}'" in html
    assert 'id="minimum-crew-waiting"' not in html
    assert 'id="minimum-crew-slots"' not in html
    assert 'id="minimum-crew-action"' in html
    assert 'class="ops-range-full"' in html
    assert 'class="ops-range-min"' in html
    assert "function renderMinimumCrewBalance(balance)" in js
    assert "function renderMinimumCrewBalanceFromGrid()" in js
    assert "summary.classList.toggle('is-balanced', balance?.direction === 'ready');" in js
    assert "summary.classList.toggle('is-unbalanced', balance?.direction !== 'ready');" in js
    assert "setWorkCenterOnState(name, !enabled);\n      renderMinimumCrewBalanceFromGrid();\n      saveAutoCenters();" in js
    assert "const waitingEl = document.getElementById('minimum-crew-waiting');" not in js
    assert "const slotsEl = document.getElementById('minimum-crew-slots');" not in js
    assert "function clearStaleAutoWarnings()" in js
    assert ".rotation-mode {\n    display: flex; flex-wrap: nowrap;" in css
    assert "width: 100%;" in css
    assert ".minimum-crew-balance { gap: 0.45rem; margin-left: auto; }" in css
    assert ".minimum-crew-balance.is-balanced #minimum-crew-action { color: var(--accent); }" in css
    assert ".minimum-crew-balance.is-unbalanced #minimum-crew-action { color: var(--bad); }" in css
    assert ".work-center-off" in css
    assert '.ops-range-min { display: none; }' in css
    assert 'tr.work-center-off .ops-range-full { display: none; }' in css
    assert 'tr.work-center-off .ops-range-min { display: inline; }' in css
    assert 'tr[data-loc][data-on="true"] td { background: var(--accent-dim); }' in css
    assert '.wc-auto-toggle' not in css
    assert "tr.work-center-off td { background: var(--panel-2); }" in css
    assert "tr.work-center-off .sched-cell > *," in css
    assert "tr.work-center-off .wc-note-cell > * { display: none; }" in css
    assert "tr.work-center-off .dept," not in css
    assert ".day-context .rotation-controls {" in css
    assert "position: fixed; right: 1.25rem; bottom: 1.25rem; z-index: 20;" in css
    assert "box-shadow: 0 16px 36px rgba(31, 41, 55, 0.18);" in css
    assert "background: linear-gradient(135deg, var(--panel), color-mix(in srgb, var(--accent-dim) 32%, var(--panel)));" in css
    assert ".day-context .rotation-mode { flex-wrap: wrap; width: auto; padding: 0; border: 0; background: transparent; }" in css
    assert ".day-context .minimum-crew-balance { display: block; flex: 0 0 100%; margin: 0.55rem 0 0; white-space: normal; }" in css
    assert ".day-context .rotation-mode-label::before" in css
    assert "content: '•';" in css
    assert "@media (max-width: 1100px)" in css
    assert ".day-context .rotation-controls { position: static; width: auto; }" in css


def test_staffing_keeps_automation_controls_in_the_notes_sidebar():
    html = (ROOT / "src/zira_dashboard/templates/staffing.html").read_text()

    sidebar_start = html.index('<aside class="day-context">')
    sidebar_end = html.index('</aside>', sidebar_start)
    sidebar = html[sidebar_start:sidebar_end]
    main_start = html.index('<main class="panel">')
    main_end = html.index('</main>', main_start)
    main = html[main_start:main_end]

    assert 'class="day-notes"' in sidebar
    assert 'class="rotation-controls" data-day="{{ day }}"' in sidebar
    assert 'id="rotation-auto-summary"' in sidebar
    assert 'id="reset-schedule-btn"' in sidebar
    assert 'id="clear-schedule-btn"' in sidebar
    assert 'id="rotation-warnings"' in main
    assert 'class="rotation-controls" data-day="{{ day }}"' not in main
    assert 'id="reset-schedule-btn"' not in main
    assert 'id="clear-schedule-btn"' not in main


def test_staffing_notes_sidebar_is_sticky_and_mobile_safe():
    css = (ROOT / "src/zira_dashboard/static/staffing.css").read_text()

    assert ".day-context { min-width: 0; position: sticky;" in css
    assert "top: 1rem; align-self: start;" in css
    assert ".sidebar-schedule-actions { display: flex; gap: 0.45rem; margin-top: 0.6rem; }" in css
    assert ".sidebar-schedule-actions .clear-btn { flex: 1 1 0; }" in css
    assert "@media (max-width: 1100px)" in css
    assert ".day-context { order: 3; position: static; }" in css


def test_skills_matrix_exposes_scheduling_preferences_without_training_controls():
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
    assert 'id="rotation-pref-grid"' in html
    assert "dataset.rotationPreference" in js
    assert "/api/rotations/preferences" in js
    assert "/api/rotations/training-blocks" not in js


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
    assert out.complete is False
    assert out.placement_issues[0].code == "no_safe_complete_crew"


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

    _stub_recommendation_inputs(monkeypatch)

    monkeypatch.setattr(staffing_route.rotation_store, "load_preferences_by_name", lambda: {})
    monkeypatch.setattr(
        rotation_suggestions, "_load_recycled_history",
        lambda d, group_locations=None, user_group_centers=None: (
            rotation_suggestions.RecycledHistory()
        ),
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
        issue = schedule_solver.CoverageIssue(
            center="Dismantler 1",
            group="Dismantler",
            code="training_required",
            message=(
                "Dismantler 1 could not be staffed. "
                "Training is required for Dismantler."
            ),
            rejections=(
                schedule_solver.CandidateRejection(
                    person="Learner",
                    code="not_qualified",
                    detail="Needs Dismantler training before assignment.",
                ),
            ),
        )
        return rotation_suggestions.RecycledSuggestion(
            assignments={"Repair 1": ["Green"]},
            sources={"Repair 1": {"Green": "generated"}},
            reasons={},
            warnings=("Trim Saw 1 short",),
            group_locations={"Repair": ("Repair 1",)},
            reason_codes={"Repair 1": {"Green": "minimum_coverage"}},
            staffed_centers=("Repair 1",),
            unresolved_centers=("Dismantler 1",),
            issues=(issue,),
        )

    monkeypatch.setattr(rotation_suggestions, "suggest_recycled_assignments", fake_engine)

    ctx = staffing_route._recycled_context_for_day(
        TARGET_DAY, roster=[_person("Green", 3)], mode="training",
        base_assignments={}, locked_assignments={}, time_off_entries=[],
        work_weekdays=frozenset({0, 1, 2, 3, 4}),
    )

    assert ctx["recycled_rotation_mode"] == "training"
    assert ctx["rotation_reasons"] == {}
    assert ctx["rotation_reason_codes"] == {
        "Repair 1": {"Green": "minimum_coverage"},
    }
    assert ctx["rotation_issues"] == [{
        "center": "Dismantler 1",
        "group": "Dismantler",
        "code": "training_required",
        "message": (
            "Dismantler 1 could not be staffed. "
            "Training is required for Dismantler."
        ),
        "rejections": [{
            "person": "Learner",
            "code": "not_qualified",
            "detail": "Needs Dismantler training before assignment.",
        }],
    }]
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

    _stub_recommendation_inputs(monkeypatch)

    def boom():
        raise RuntimeError("preferences unavailable")

    monkeypatch.setattr(staffing_route.rotation_store, "load_preferences_by_name", boom)
    monkeypatch.setattr(
        rotation_suggestions,
        "_load_recycled_history",
        lambda _d, group_locations=None, user_group_centers=None: (
            rotation_suggestions.RecycledHistory()
        ),
    )
    monkeypatch.setattr(staffing_route.rotation_training, "reconcile_blocks", lambda _as_of: [])
    monkeypatch.setattr(staffing_route.rotation_store, "active_blocks_for_day", lambda _d: [])
    monkeypatch.setattr(staffing_route.work_centers_store, "min_ops", lambda loc: loc.min_ops)

    captured = {}
    sentinel = rotation_suggestions.RecycledSuggestion(
        assignments={"Repair 1": ["Green"]},
        sources={"Repair 1": {"Green": "generated"}},
        reasons={},
        warnings=(),
        group_locations={"Repair": ("Repair 1",)},
    )

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

    _stub_recommendation_inputs(monkeypatch)

    def boom():
        raise RuntimeError("preferences unavailable")

    monkeypatch.setattr(staffing_route.rotation_store, "load_preferences_by_name", boom)
    monkeypatch.setattr(
        rotation_suggestions,
        "_load_recycled_history",
        lambda _d, group_locations=None, user_group_centers=None: (
            rotation_suggestions.RecycledHistory()
        ),
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
        work_weekdays=frozenset({0, 1, 2, 3, 4}),
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


def test_auto_solver_base_assignments_keeps_only_centers_outside_auto_scope():
    from zira_dashboard.routes import staffing as staffing_route

    assert staffing_route._auto_solver_base_assignments(
        {
            "Work Orders": ["Default Mechanic"],
            "Tablets": ["Old Generated"],
            "Repair 1": ["Manual Repair"],
            "Truck Driver": ["Outside Auto"],
        },
        {"Work Orders", "Tablets", "Repair 1"},
    ) == {"Truck Driver": ["Outside Auto"]}


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


def _render_staffing_page(
    monkeypatch,
    *,
    saved_schedule=None,
    day=None,
    schedule_revision="test",
    roster=None,
    time_off_entries=None,
    strict_time_off_entries=None,
    schedule_loader=None,
    default_inputs=None,
    center_capacities=None,
    history=None,
    saved_schedules=None,
    conditional_create=None,
    smart_defaults=None,
    auto_centers=None,
    default_people=None,
    recycled_context=None,
    bay_model=None,
):
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
    monkeypatch.setattr(
        staffing_routes.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    monkeypatch.setattr(staffing_routes._http_cache, "get_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(staffing_routes._http_cache, "set_cache_headers", lambda *a, **k: None)
    monkeypatch.setattr(staffing_routes._http_cache, "store_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(
        staffing_routes.app_settings,
        "get_setting",
        lambda key: list(auto_centers or []),
    )
    monkeypatch.setattr(cert_lookup, "load_person_certs", lambda: {})
    monkeypatch.setattr(staffing_mod, "load_roster", lambda: list(roster or []))
    monkeypatch.setattr(
        staffing_mod, "load_schedule",
        schedule_loader or (lambda d: saved_schedule or staffing_mod.Schedule(day=d, published=False, assignments={})),
    )
    monkeypatch.setattr(
        staffing_mod,
        "schedule_revision",
        lambda d: schedule_revision(d) if callable(schedule_revision) else schedule_revision,
    )
    monkeypatch.setattr(staffing_routes, "_safe_time_off_entries", lambda _day: list(time_off_entries or []))
    monkeypatch.setattr(
        staffing_routes,
        "_time_off_entries_cached",
        strict_time_off_entries or (lambda _day: list(time_off_entries or [])),
    )
    monkeypatch.setattr(staffing_routes, "_default_inputs", default_inputs or (lambda strict=False: ({}, {}, {})))
    monkeypatch.setattr(
        staffing_routes,
        "_configured_center_capacities",
        center_capacities or (lambda centers, strict=False: {center: None for center in centers}),
    )
    monkeypatch.setattr(
        staffing_routes.rotation_suggestions,
        "_load_recycled_history",
        history or (lambda *_args, **_kwargs: rotation_suggestions.RecycledHistory()),
    )
    monkeypatch.setattr(
        staffing_mod,
        "save_schedule",
        lambda schedule: saved_schedules.append(schedule) if saved_schedules is not None else None,
    )
    def create_schedule_if_absent(schedule):
        if conditional_create is not None:
            return conditional_create(schedule)
        if saved_schedules is not None:
            saved_schedules.append(schedule)
        return True

    monkeypatch.setattr(staffing_mod, "create_schedule_if_absent", create_schedule_if_absent)
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
    monkeypatch.setattr(
        staffing_routes.work_centers_store,
        "default_people",
        default_people or (lambda loc: []),
    )
    monkeypatch.setattr(
        staffing_routes, "_smart_defaults_for_day",
        smart_defaults
        or (lambda d, roster, defaults, time_off, mode="normal", **kwargs: {k: list(v) for k, v in defaults.items()}),
    )
    if recycled_context is not None:
        monkeypatch.setattr(staffing_routes, "_recycled_context_for_day", recycled_context)

    def fake_build_staffing_bays(
        roster, sched, time_off_entries, publish_blocked, **_kwargs,
    ):
        default_model = {
            "bays": [], "publish_block_reasons": [], "defaults_by_loc": {},
            "unassigned": [], "reserves": [], "time_off_names": [], "time_off_entries": [],
            "partial_hours_by_name": {}, "partial_range_by_name": {},
            "partial_clear_by_name": {}, "people_meta": {}, "all_active_people": [],
        }
        return bay_model or default_model

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


def test_staffing_context_exposes_auto_summary_counts(monkeypatch):
    ctx = _render_staffing_page(
        monkeypatch,
        auto_centers={"Repair 1", "Dismantler 1"},
        bay_model={
            "bays": [], "publish_block_reasons": [], "defaults_by_loc": {},
            "unassigned": ["A", "B", "C"], "reserves": [],
            "time_off_names": [], "time_off_entries": [],
            "partial_hours_by_name": {}, "partial_range_by_name": {},
            "partial_clear_by_name": {}, "people_meta": {}, "all_active_people": [],
        },
    )

    assert ctx["rotation_auto_summary"] == {
        "unscheduled_count": 3,
        "auto_on_count": 2,
        "delta": -1,
    }


def test_first_future_staffing_view_saves_exact_and_group_defaults(monkeypatch):
    saved = []
    ctx = _render_staffing_page(
        monkeypatch,
        schedule_revision=None,
        roster=[_person("Pinned", 1), _person("Ana", 1), _person("Bob", 1)],
        auto_centers={"Repair 1", "Repair 2", "Repair 3"},
        default_inputs=lambda strict=False: (
            {"Repair 1": ("Pinned",)},
            {"Repair": ("Ana", "Bob")},
            {"Repair": ("Repair 1", "Repair 2", "Repair 3")},
        ),
        center_capacities=lambda centers, strict=False: {center: 1 for center in centers},
        saved_schedules=saved,
    )

    assert len(saved) == 1
    assert saved[0].published is False
    assert saved[0].assignments == {
        "Repair 1": ["Pinned"],
        "Repair 2": ["Ana"],
        "Repair 3": ["Bob"],
    }
    assert saved[0].assignment_sources == {
        "Repair 1": {"Pinned": "default"},
        "Repair 2": {"Ana": "default"},
        "Repair 3": {"Bob": "default"},
    }
    assert ctx["sched"] is saved[0]


def test_saved_blank_future_draft_is_not_reseeded(monkeypatch):
    saved = []
    blank = staffing.Schedule(day=TARGET_DAY, published=False, assignments={})

    ctx = _render_staffing_page(
        monkeypatch,
        saved_schedule=blank,
        schedule_revision="already-saved",
        roster=[_person("Pinned", 1)],
        auto_centers={"Repair 1"},
        default_inputs=lambda strict=False: ({"Repair 1": ("Pinned",)}, {}, {}),
        saved_schedules=saved,
    )

    assert saved == []
    assert ctx["sched"] is blank
    assert ctx["sched"].assignments == {}


def test_first_future_staffing_view_keeps_blank_draft_when_defaults_fail(monkeypatch):
    saved = []
    ctx = _render_staffing_page(
        monkeypatch,
        schedule_revision=None,
        roster=[_person("Pinned", 1)],
        default_inputs=lambda strict=False: (_ for _ in ()).throw(RuntimeError("settings offline")),
        saved_schedules=saved,
    )

    assert saved == []
    assert ctx["sched"].assignments == {}


def test_future_draft_is_not_overwritten_when_revision_lookup_fails(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_route

    blank = staffing.Schedule(day=TARGET_DAY, published=False, assignments={})
    monkeypatch.setattr(
        staffing_route.staffing,
        "schedule_revision",
        lambda _day: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    assert staffing_route._seed_new_future_draft(
        TARGET_DAY,
        date(2026, 7, 13),
        blank,
        [],
        [],
    ) is blank


def test_first_future_staffing_view_does_not_seed_when_time_off_read_fails(monkeypatch):
    saved = []

    ctx = _render_staffing_page(
        monkeypatch,
        schedule_revision=None,
        roster=[_person("Pinned", 1)],
        auto_centers={"Repair 1"},
        default_inputs=lambda strict=False: ({"Repair 1": ("Pinned",)}, {}, {}),
        strict_time_off_entries=lambda _day: (_ for _ in ()).throw(RuntimeError("time off unavailable")),
        saved_schedules=saved,
    )

    assert saved == []
    assert ctx["sched"].assignments == {}


def test_first_future_staffing_view_returns_existing_draft_after_create_conflict(monkeypatch):
    blank = staffing.Schedule(day=TARGET_DAY, published=False, assignments={})
    existing = staffing.Schedule(
        day=TARGET_DAY,
        published=False,
        assignments={"Repair 1": ["Existing"]},
        notes="manager draft",
        assignment_sources={"Repair 1": {"Existing": "manual"}},
    )
    load_count = 0

    def schedule_loader(_day):
        nonlocal load_count
        load_count += 1
        return blank if load_count == 1 else existing

    ctx = _render_staffing_page(
        monkeypatch,
        schedule_revision=None,
        schedule_loader=schedule_loader,
        roster=[_person("Pinned", 1)],
        auto_centers={"Repair 1"},
        default_inputs=lambda strict=False: ({"Repair 1": ("Pinned",)}, {}, {}),
        conditional_create=lambda _schedule: False,
    )

    assert ctx["sched"] is existing
    assert ctx["sched"].assignments == {"Repair 1": ["Existing"]}
    assert ctx["sched"].notes == "manager draft"
    assert ctx["sched"].assignment_sources == {"Repair 1": {"Existing": "manual"}}


def test_staffing_page_renders_blank_draft_when_display_revision_read_fails(monkeypatch):
    blank = staffing.Schedule(day=TARGET_DAY, published=False, assignments={})
    reads = iter(("saved", RuntimeError("database unavailable")))

    def schedule_revision(_day):
        result = next(reads)
        if isinstance(result, Exception):
            raise result
        return result

    ctx = _render_staffing_page(
        monkeypatch,
        saved_schedule=blank,
        schedule_revision=schedule_revision,
    )

    assert ctx["sched"] is blank
    assert ctx["sched"].assignments == {}


@pytest.mark.parametrize("day", [
    date(2026, 7, 13),  # Monday
    date(2026, 7, 18),  # Saturday
    date(2026, 7, 19),  # Sunday
])
def test_staffing_context_enables_auto_scheduler_every_day(monkeypatch, day):
    ctx = _render_staffing_page(monkeypatch, day=day)

    assert ctx["auto_scheduler_available"] is True


def test_staffing_template_renders_auto_controls_from_the_available_context():
    html = (ROOT / "src/zira_dashboard/templates/staffing.html").read_text()

    assert "{% if auto_scheduler_available %}" in html
    assert 'class="rotation-controls"' in html
    assert 'data-work-center-toggle' in html


def test_saved_staffing_day_context_hydrates_stored_mode(monkeypatch):
    sched = staffing.Schedule(
        day=TARGET_DAY, published=False,
        assignments={"Repair 1": ["Someone"]},
        rotation_mode="optimized",
        assignment_sources={"Repair 1": {"Someone": "manual"}},
    )
    ctx = _render_staffing_page(monkeypatch, saved_schedule=sched)
    assert ctx["recycled_rotation_mode"] == "optimized"


def test_staffing_context_does_not_treat_exact_default_as_duplicate_lock(monkeypatch):
    captured = {}
    schedule = staffing.Schedule(
        day=TARGET_DAY,
        assignments={"Repair 2": ["Default Green"]},
        assignment_sources={"Repair 2": {"Default Green": "generated"}},
    )

    def fake_recycled_context(*args, **kwargs):
        captured.update(kwargs)
        return {
            "recycled_rotation_mode": "normal",
            "rotation_reasons": {},
            "rotation_reason_codes": {},
            "rotation_warnings": [],
            "rotation_issues": [],
            "active_training_blocks": [],
        }

    _render_staffing_page(
        monkeypatch,
        saved_schedule=schedule,
        auto_centers={"Repair 1", "Repair 2"},
        default_people=lambda loc: ["Default Green"] if loc.name == "Repair 1" else [],
        recycled_context=fake_recycled_context,
    )

    assert captured["base_assignments"] == {}
    assert captured["locked_assignments"] == {}
    assert captured["current_assignments"] == {
        "Repair 2": ["Default Green"],
    }


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


def test_rebuild_does_not_restore_person_after_clear_removes_manual_source(monkeypatch):
    """A cleared manual source cannot become a stale rebuild lock."""
    from zira_dashboard.routes import staffing as staffing_route

    client, rotations = _rotations_client(monkeypatch)
    schedule_before_clear = staffing.Schedule(
        day=TARGET_DAY,
        assignments={"Repair 1": ["Manual Person"]},
        assignment_sources={"Repair 1": {"Manual Person": "manual"}},
    )
    cleared: list = []
    locations = staffing.LOCATIONS
    monkeypatch.setattr(staffing_route.staffing, "LOCATIONS", ())
    monkeypatch.setattr(
        staffing_route.staffing, "load_schedule", lambda _day: schedule_before_clear,
    )
    monkeypatch.setattr(staffing_route.staffing, "save_schedule", cleared.append)
    monkeypatch.setattr(staffing_route.staffing, "schedule_revision", lambda _day: "test")
    monkeypatch.setattr(staffing_route._http_cache, "invalidate_today_cache", lambda: None)

    staffing_route._staffing_save_work(
        SimpleNamespace(headers={}), TARGET_DAY, 1, FormData({"action": "save"}),
    )

    cleared_schedule = cleared[-1]
    assert cleared_schedule.assignments == {}
    assert cleared_schedule.assignment_sources == {}

    monkeypatch.setattr(staffing_route.staffing, "LOCATIONS", locations)
    _stub_recommendation_inputs(monkeypatch)

    rebuilt: list = []
    monkeypatch.setattr(
        rotations.staffing, "load_roster", lambda: [_person("Manual Person", 3, active=False)],
    )
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda _day: cleared_schedule)
    monkeypatch.setattr(rotations.staffing, "save_schedule", rebuilt.append)
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)

    resp = client.post("/api/rotations/rebuild", json={"day": "2026-07-14", "mode": "normal"})

    assert resp.status_code == 200
    assert resp.json()["applied"] is True
    assert resp.json()["unplaced"] == []
    assert len(rebuilt) == 1


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


def test_staffing_skills_context_includes_scheduling_preferences_only(monkeypatch):
    """The People Matrix keeps scheduling preferences, not training-form data."""
    from types import SimpleNamespace
    from zira_dashboard import odoo_sync, cert_lookup, skill_matrix_views_store as views_store
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
    monkeypatch.setattr(
        skills_routes.rotation_store,
        "active_blocks",
        lambda: (_ for _ in ()).throw(AssertionError("People Matrix must not load training blocks")),
    )

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

    assert ctx["rotation_preference_options"] == ["primary", "regular", "occasional", "never"]
    assert ctx["rotation_preferences"] == {"Alex": {"Repair": "primary"}}
    assert "active_training_blocks" not in ctx
    assert "rotation_levels" not in ctx
    assert "rotation_active_people" not in ctx


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


def test_staffing_skills_context_degrades_when_preference_load_fails(monkeypatch):
    """A preferences outage leaves the People Matrix renderable rather than 500ing."""
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

    captured: dict = {}

    class FakeTemplates:
        def TemplateResponse(self, request, template, context):
            captured["context"] = context
            return SimpleNamespace(context=context, headers={})

    monkeypatch.setattr(skills_routes, "templates", FakeTemplates())

    skills_routes.staffing_skills(request=object())
    ctx = captured["context"]

    assert ctx["rotation_preferences"] == {}
    assert "active_training_blocks" not in ctx
