from __future__ import annotations

from datetime import date, time
from pathlib import Path

from zira_dashboard.deps import templates


def _globals_script() -> str:
    html = Path("src/zira_dashboard/templates/staffing.html").read_text()
    marker = "window.DEFAULTS_BY_LOC"
    start = html.rindex("<script>", 0, html.index(marker))
    end = html.index("</script>", html.index(marker)) + len("</script>")
    return html[start:end]


def test_staffing_template_exposes_smart_defaults():
    rendered = templates.env.from_string(_globals_script()).render(
        person_certs={},
        cert_icon_data=lambda: {},
        day="2026-07-06",
        today="2026-07-06",
        view_mode="draft",
        published=False,
        viewing_posted=False,
        defaults_by_loc={"Trim Saw 1": ["Stored"]},
        smart_defaults_by_loc={"Trim Saw 1": ["Smart"]},
        people_meta={},
        partial_range_by_name={},
        partial_hours_by_name={},
        time_off_names=[],
        forklift_live_model={"available": False},
    )

    assert "window.SMART_DEFAULTS_BY_LOC" in rendered
    assert '"Smart"' in rendered


def test_staffing_page_seeds_empty_day_with_smart_defaults(monkeypatch):
    from zira_dashboard import cert_lookup
    from zira_dashboard import staffing as staffing_mod, staffing_view
    from zira_dashboard.routes import staffing as staffing_routes

    target_day = date(2026, 7, 7)
    captured = {}

    monkeypatch.setattr(staffing_routes, "plant_today", lambda: date(2026, 7, 6))
    monkeypatch.setattr(staffing_routes, "_next_working_day", lambda today: target_day)
    monkeypatch.setattr(staffing_routes._http_cache, "get_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(staffing_routes._http_cache, "set_cache_headers", lambda *a, **k: None)
    monkeypatch.setattr(staffing_routes._http_cache, "store_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(cert_lookup, "load_person_certs", lambda: {})
    monkeypatch.setattr(staffing_mod, "load_roster", lambda: [])
    monkeypatch.setattr(
        staffing_mod,
        "load_schedule",
        lambda d: staffing_mod.Schedule(day=d, published=False, assignments={}),
    )
    monkeypatch.setattr(staffing_routes, "_safe_time_off_entries", lambda d: [])
    monkeypatch.setattr(
        staffing_routes,
        "_safe_attendance",
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
        lambda loc: ["Stored"] if loc.name == "Trim Saw 1" else [],
    )
    monkeypatch.setattr(
        staffing_routes,
        "_smart_defaults_for_day",
        lambda d, roster, defaults, time_off: {"Trim Saw 1": ["Smart"]},
    )

    def fake_build_staffing_bays(roster, sched, time_off_entries, publish_blocked):
        captured["assignments"] = dict(sched.assignments)
        return {
            "bays": [],
            "publish_block_reasons": [],
            "defaults_by_loc": {"Trim Saw 1": ["Stored"]},
            "unassigned": [],
            "reserves": [],
            "time_off_names": [],
            "time_off_entries": [],
            "partial_hours_by_name": {},
            "partial_range_by_name": {},
            "partial_clear_by_name": {},
            "people_meta": {},
            "all_active_people": [],
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

    staffing_routes.staffing_page(request=object(), day=None, publish_blocked=0, view="draft")

    assert captured["assignments"] == {"Trim Saw 1": ["Smart"]}
    assert captured["context"]["smart_defaults_by_loc"]["Trim Saw 1"] == ["Smart"]


def test_staffing_page_preserves_saved_manual_trim_saw_assignments(monkeypatch):
    from zira_dashboard import cert_lookup
    from zira_dashboard import staffing as staffing_mod, staffing_view
    from zira_dashboard.routes import staffing as staffing_routes

    target_day = date(2026, 7, 7)
    manual_assignments = {"Trim Saw 1": ["Manual One", "Manual Two"]}
    captured = {}

    monkeypatch.setattr(staffing_routes, "plant_today", lambda: date(2026, 7, 6))
    monkeypatch.setattr(staffing_routes, "_next_working_day", lambda today: target_day)
    monkeypatch.setattr(staffing_routes._http_cache, "get_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(staffing_routes._http_cache, "set_cache_headers", lambda *a, **k: None)
    monkeypatch.setattr(staffing_routes._http_cache, "store_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(cert_lookup, "load_person_certs", lambda: {})
    monkeypatch.setattr(staffing_mod, "load_roster", lambda: [])
    monkeypatch.setattr(
        staffing_mod,
        "load_schedule",
        lambda d: staffing_mod.Schedule(
            day=d,
            published=False,
            assignments={k: list(v) for k, v in manual_assignments.items()},
        ),
    )
    monkeypatch.setattr(staffing_routes, "_safe_time_off_entries", lambda d: [])
    monkeypatch.setattr(
        staffing_routes,
        "_safe_attendance",
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
        lambda loc: ["Stored"] if loc.name == "Trim Saw 1" else [],
    )

    def fake_smart_defaults(d, roster, defaults, time_off, mode="normal"):
        captured["smart_defaults_input"] = defaults
        return {"Trim Saw 1": ["Smart"]}

    monkeypatch.setattr(staffing_routes, "_smart_defaults_for_day", fake_smart_defaults)

    def fake_build_staffing_bays(roster, sched, time_off_entries, publish_blocked):
        captured["assignments"] = {k: list(v) for k, v in sched.assignments.items()}
        return {
            "bays": [],
            "publish_block_reasons": [],
            "defaults_by_loc": {"Trim Saw 1": ["Stored"]},
            "unassigned": [],
            "reserves": [],
            "time_off_names": [],
            "time_off_entries": [],
            "partial_hours_by_name": {},
            "partial_range_by_name": {},
            "partial_clear_by_name": {},
            "people_meta": {},
            "all_active_people": [],
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

    staffing_routes.staffing_page(
        request=object(),
        day=target_day.isoformat(),
        publish_blocked=0,
        view="draft",
    )

    assert captured["assignments"] == manual_assignments
    assert captured["smart_defaults_input"] == {"Trim Saw 1": ["Stored"]}
    assert captured["context"]["smart_defaults_by_loc"] == {"Trim Saw 1": ["Smart"]}


def test_publish_prefills_next_day_with_smart_defaults(monkeypatch):
    from zira_dashboard import staffing as staffing_mod
    from zira_dashboard.routes import staffing as staffing_routes

    current_day = date(2026, 7, 6)
    next_day = date(2026, 7, 7)
    next_schedule = staffing_mod.Schedule(
        day=next_day,
        published=False,
        assignments={},
        rotation_mode="training",
        assignment_sources={"Trim Saw 1": {"Jordan": "manual"}},
    )
    saved = []
    smart_calls = []

    class FakeForm:
        def getlist(self, key):
            return []

        def get(self, key, default=None):
            if key == "action":
                return "publish"
            return default

    class FakeRequest:
        headers = {}

    monkeypatch.setattr(staffing_routes, "_next_working_day", lambda d: next_day)
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(
        staffing_mod,
        "load_schedule",
        lambda d: next_schedule if d == next_day else staffing_mod.Schedule(
            day=d, published=False, assignments={},
        ),
    )
    monkeypatch.setattr(staffing_mod, "save_schedule", lambda sched: saved.append(sched))
    monkeypatch.setattr(staffing_mod, "load_roster", lambda: [])
    monkeypatch.setattr(staffing_routes, "_safe_time_off_entries", lambda d: [])
    monkeypatch.setattr(staffing_routes.work_centers_store, "min_ops", lambda loc: loc.min_ops)
    monkeypatch.setattr(
        staffing_routes.work_centers_store,
        "default_people",
        lambda loc: ["Stored"] if loc.name == "Trim Saw 1" else [],
    )

    def fake_smart(d, roster, defaults, time_off, mode="normal"):
        smart_calls.append((d, defaults))
        return {"Trim Saw 1": ["Smart"]}

    monkeypatch.setattr(staffing_routes, "_smart_defaults_for_day", fake_smart)

    response = staffing_routes._staffing_save_work(FakeRequest(), current_day, 0, FakeForm())

    assert response.status_code == 303
    assert smart_calls == [(next_day, {"Trim Saw 1": ["Stored"]})]
    assert saved[-1].day == next_day
    assert saved[-1].assignments == {"Trim Saw 1": ["Smart"]}
    assert saved[-1].rotation_mode == "training"
    assert saved[-1].assignment_sources == {"Trim Saw 1": {"Jordan": "manual"}}


def test_route_smart_defaults_falls_back_to_raw_defaults(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_routes
    from zira_dashboard import rotation_suggestions

    # _smart_defaults_for_day now runs the Recycled rotation engine. Let the
    # (mocked) recommendation-data reads succeed but make the engine itself
    # raise; the route must still fall back to the raw stored defaults.
    monkeypatch.setattr(staffing_routes.rotation_store, "load_preferences_by_name", lambda: {})
    monkeypatch.setattr(
        rotation_suggestions, "_load_recycled_history",
        lambda d: rotation_suggestions.RecycledHistory(),
    )
    monkeypatch.setattr(staffing_routes.rotation_training, "reconcile_blocks", lambda as_of: [])
    monkeypatch.setattr(staffing_routes.rotation_store, "active_blocks_for_day", lambda d: [])

    def boom(*args, **kwargs):
        raise RuntimeError("engine unavailable")

    monkeypatch.setattr(rotation_suggestions, "suggest_recycled_assignments", boom)

    out = staffing_routes._smart_defaults_for_day(
        date(2026, 7, 6),
        roster=[],
        defaults={"Trim Saw 1": ["Stored"]},
        time_off_entries=[],
    )

    assert out == {"Trim Saw 1": ["Stored"]}
