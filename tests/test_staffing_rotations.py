"""Task 4 — rotation JSON APIs + Staffing recommendation wiring.

DB-free route/orchestration tests. The FastAPI endpoints are exercised through
a TestClient with only ``rotations.router`` mounted; all DB and store calls are
monkeypatched. The Staffing orchestration helpers are called directly with the
recommendation inputs stubbed, so nothing here touches Postgres or the clock.
"""

from __future__ import annotations

from datetime import date, time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zira_dashboard import staffing


TARGET_DAY = date(2026, 7, 14)


def _person(name: str, level: int, group: str = "Repair", *, active: bool = True, reserve: bool = False):
    return staffing.Person(name=name, active=active, reserve=reserve, skills={group: level})


def _rotations_client(monkeypatch):
    from zira_dashboard.routes import rotations

    app = FastAPI()
    app.include_router(rotations.router)
    return TestClient(app), rotations


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
        lambda d: rotation_suggestions.RecycledHistory(),
    )
    monkeypatch.setattr(staffing_route.rotation_training, "reconcile_blocks", lambda as_of: [])
    monkeypatch.setattr(staffing_route.rotation_store, "active_blocks_for_day", lambda d: [])
    monkeypatch.setattr(staffing_route, "_safe_time_off_entries", lambda d: [])
    return staffing_route


def test_rebuild_preserves_manual_assignment(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    staffing_route = _stub_recommendation_inputs(monkeypatch)

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
    # Every generated placement carries a source + reason.
    for wc, sources in body["sources"].items():
        for name, src in sources.items():
            assert src in ("generated", "manual")
            assert body["reasons"].get(wc, {}).get(name)


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


def test_smart_defaults_merges_recycled_and_keeps_non_recycled(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_route
    from zira_dashboard import rotation_suggestions

    _stub_recommendation_inputs(monkeypatch)

    def fake_engine(**kwargs):
        return rotation_suggestions.RecycledSuggestion(
            assignments={"Junior #1": ["Keep Me"], "Repair 1": ["Rotated"]},
            sources={"Repair 1": {"Rotated": "generated"}},
            reasons={"Repair 1": {"Rotated": "green coverage"}},
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
        TARGET_DAY, roster=roster, defaults=defaults, time_off_entries=[]
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
        lambda d: rotation_suggestions.RecycledHistory(),
    )
    monkeypatch.setattr(staffing_route.rotation_training, "reconcile_blocks", lambda as_of: [])

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
            reasons={"Repair 1": {"Green": "green coverage"}},
            warnings=("Trim Saw 1 short",),
            group_locations={"Repair": ("Repair 1",)},
        )

    monkeypatch.setattr(rotation_suggestions, "suggest_recycled_assignments", fake_engine)

    ctx = staffing_route._recycled_context_for_day(
        TARGET_DAY, roster=[_person("Green", 3)], mode="training",
        base_assignments={}, locked_assignments={}, time_off_entries=[],
    )

    assert ctx["recycled_rotation_mode"] == "training"
    assert ctx["rotation_reasons"]["Repair 1"]["Green"] == "green coverage"
    assert "Trim Saw 1 short" in ctx["rotation_warnings"]
    assert len(ctx["active_training_blocks"]) == 1
    tb = ctx["active_training_blocks"][0]
    assert tb["trainee"] == "Learner"
    assert tb["trainer"] == "Green"
    assert tb["group"] == "Repair"
    assert tb["remaining_attended_days"] == 3  # 5 planned - 2 attended


def test_recycled_context_degrades_to_safe_defaults(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_route

    def boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(staffing_route.rotation_store, "load_preferences_by_name", boom)

    ctx = staffing_route._recycled_context_for_day(
        TARGET_DAY, roster=[], mode="normal",
        base_assignments={}, locked_assignments={}, time_off_entries=[],
    )
    assert ctx == {
        "recycled_rotation_mode": "normal",
        "rotation_reasons": {},
        "rotation_warnings": [],
        "active_training_blocks": [],
    }


def test_manual_locks_from_sources_extracts_manual_only():
    from zira_dashboard.routes import staffing as staffing_route

    sources = {
        "Repair 1": {"Manual Person": "manual", "Bot": "generated"},
        "Repair 2": {"Bot Two": "generated"},
    }
    assignments = {"Repair 1": ["Manual Person", "Bot"], "Repair 2": ["Bot Two"]}
    locks = staffing_route._manual_locks_from_sources(sources, assignments)
    assert locks == {"Repair 1": ["Manual Person"]}


# --------------------------------------------------------------------------- #
# GET /staffing context wiring (rendered via a fake TemplateResponse)
# --------------------------------------------------------------------------- #


def _render_staffing_page(monkeypatch, *, saved_schedule=None, day=None):
    """Render the staffing page with all I/O stubbed, returning the captured
    template context. Mirrors the harness in test_staffing_trim_saw_defaults."""
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
        lambda d, roster, defaults, time_off: {k: list(v) for k, v in defaults.items()},
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


def test_saved_staffing_day_context_hydrates_stored_mode(monkeypatch):
    sched = staffing.Schedule(
        day=TARGET_DAY, published=False,
        assignments={"Repair 1": ["Someone"]},
        rotation_mode="optimized",
        assignment_sources={"Repair 1": {"Someone": "manual"}},
    )
    ctx = _render_staffing_page(monkeypatch, saved_schedule=sched)
    assert ctx["recycled_rotation_mode"] == "optimized"
