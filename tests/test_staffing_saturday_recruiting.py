"""Saturday recruiting staffing derivation and publication guardrails."""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import FormData

from zira_dashboard import saturday_recruiting as sr
from zira_dashboard import saturday_recruiting_store as recruiting_store
from zira_dashboard import staffing, staffing_view, work_centers_store
from zira_dashboard.routes import staffing as staffing_routes
from zira_dashboard.shift_config import SITE_TZ


SATURDAY = date(2026, 7, 25)


def _person(name, *, reserve=False, active=True, **skills):
    return SimpleNamespace(
        name=name,
        reserve=reserve,
        active=active,
        skills=skills,
        level=lambda skill, _skills=skills: int(_skills.get(skill, 0)),
    )


def _sched(assignments=None):
    return SimpleNamespace(assignments=dict(assignments or {}), wc_notes={})


@pytest.fixture
def patch_wcs(monkeypatch):
    loc = staffing.Location(
        "Repair 1", "Repair", "Bay 1", "Recycled", None,
        min_ops=1, max_ops=2, required_skills=("Repair",),
    )
    monkeypatch.setattr(staffing, "LOCATIONS", (loc,))
    monkeypatch.setattr(work_centers_store, "required_skills", lambda _loc: ["Repair"])
    monkeypatch.setattr(work_centers_store, "min_ops", lambda _loc: 1)
    monkeypatch.setattr(work_centers_store, "max_ops", lambda _loc: 2)
    monkeypatch.setattr(work_centers_store, "default_people", lambda _loc: [])


def test_only_commitments_enter_saturday_unassigned(patch_wcs):
    model = staffing_view.build_staffing_bays(
        roster=[_person("Ana", Repair=3), _person("Bob", Repair=2), _person("Cara", Repair=3)],
        sched=_sched(), time_off_entries=[], publish_blocked=0,
        saturday_commitments={
            "Ana": {"start": time(6, 0), "end": time(12, 0)},
            "Bob": {"start": time(7, 0), "end": time(11, 30)},
        },
    )

    assert model["unassigned"] == ["Ana", "Bob"]
    assert model["off"] == ["Cara"]
    assert model["saturday_availability_by_name"]["Bob"] == "7:00 AM–11:30 AM"
    assert model["is_saturday_recruiting"] is True


def test_closed_plant_saturday_puts_every_active_person_off(patch_wcs):
    model = staffing_view.build_staffing_bays(
        roster=[_person("Ana", Repair=3), _person("Bob", Repair=2)],
        sched=_sched({"Repair 1": ["Ana"]}), time_off_entries=[], publish_blocked=0,
        saturday_commitments={},
    )

    assert model["unassigned"] == []
    assert model["off"] == ["Ana", "Bob"]
    assert all(
        assignment["name"] != "Ana"
        for bay in model["bays"]
        for row in bay["rows"]
        for assignment in row["assigned"]
    )


def test_full_day_time_off_wins_over_commitment(patch_wcs):
    model = staffing_view.build_staffing_bays(
        roster=[_person("Ana", Repair=3)], sched=_sched(),
        time_off_entries=[{"name": "Ana", "hours": None}], publish_blocked=0,
        saturday_commitments={"Ana": {"start": time(6, 0), "end": time(12, 0)}},
    )

    assert model["unassigned"] == []
    assert model["off"] == []
    assert model["time_off_names"] == ["Ana"]


def test_partial_commitment_keeps_availability_after_assignment(patch_wcs):
    model = staffing_view.build_staffing_bays(
        roster=[_person("Bob", Repair=2)],
        sched=_sched({"Repair 1": ["Bob"]}), time_off_entries=[], publish_blocked=0,
        saturday_commitments={"Bob": {"start": time(7, 0), "end": time(11, 30)}},
    )

    assert model["unassigned"] == []
    assert model["bays"][0]["rows"][0]["assigned"][0]["name"] == "Bob"
    assert model["saturday_availability_by_name"]["Bob"] == "7:00 AM–11:30 AM"


def _bundle(*, status="closed"):
    return recruiting_store.RecruitmentBundle(
        recruiting_store.Recruitment(
            SATURDAY, status, time(6), time(12),
            datetime(2026, 7, 24, 7, tzinfo=SITE_TZ),
        ),
        (
            sr.Opening(1, "Repair 1", 1, ("Repair",)),
            sr.Opening(2, "Dismantle", 1, ("Dismantle",)),
        ),
        (
            recruiting_store.StoredCommitment(1, 101, "Ana", "committed", time(6), time(12), frozenset({1})),
            recruiting_store.StoredCommitment(2, 102, "Bob", "committed", time(6), time(12), frozenset({2})),
        ),
    )


def _people(*, ana_active=True, ana_repair=3):
    return {
        "Ana": _person("Ana", active=ana_active, Repair=ana_repair),
        "Bob": _person("Bob", Dismantle=3),
        "Cara": _person("Cara", Repair=3),
    }


def _repair_only_bundle(*, status="closed"):
    return recruiting_store.RecruitmentBundle(
        recruiting_store.Recruitment(
            SATURDAY, status, time(6), time(12),
            datetime(2026, 7, 24, 7, tzinfo=SITE_TZ),
        ),
        (sr.Opening(1, "Repair 1", 1, ("Repair",)),),
        (
            recruiting_store.StoredCommitment(
                1, 101, "Ana", "committed", time(6), time(12), frozenset({1}),
            ),
        ),
    )


def test_publish_requires_commitments_and_requested_coverage():
    reasons = sr.validate_publish(_bundle(), {"Repair 1": ["Ana"]}, _people(), set())

    assert "Bob committed to Saturday but is not assigned." in reasons
    assert "Dismantle requires 1 qualified operator — currently 0." in reasons


@pytest.mark.parametrize(
    ("assignments", "people", "full_day_off_names", "expected"),
    [
        ({"Repair 1": ["Ana", "Ana"], "Dismantle": ["Bob"]}, _people(), set(), "Ana is assigned more than once."),
        ({"Repair 1": ["Cara"], "Dismantle": ["Bob"]}, _people(), set(), "Cara is not committed to Saturday."),
        ({"Repair 1": ["Ana"], "Dismantle": ["Bob"]}, _people(ana_repair=1), set(), "Ana is no longer qualified for Repair."),
        ({"Repair 1": ["Ana"], "Dismantle": ["Bob"]}, _people(ana_active=False), set(), "Ana is inactive."),
        ({"Repair 1": ["Ana"], "Dismantle": ["Bob"]}, _people(), {"Ana"}, "Ana has approved full-day time off."),
    ],
)
def test_publish_validation_reports_each_saturday_blocker(
    assignments, people, full_day_off_names, expected,
):
    assert expected in sr.validate_publish(_bundle(), assignments, people, full_day_off_names)


def test_publish_before_deadline_is_blocked(monkeypatch):
    bundle = _bundle(status="recruiting")
    saved = []
    repair = staffing.Location("Repair 1", "Repair", "Bay 1", "Recycled", None, min_ops=1, max_ops=2)
    dismantle = staffing.Location("Dismantle", "Dismantle", "Bay 1", "Recycled", None, min_ops=1, max_ops=2)
    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", (repair, dismantle))
    monkeypatch.setattr(staffing_routes.work_centers_store, "min_ops", lambda loc: loc.min_ops)
    monkeypatch.setattr(staffing_routes.staffing, "load_schedule", lambda _day: staffing.Schedule(day=SATURDAY, assignments={}))
    monkeypatch.setattr(staffing_routes.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(staffing_routes.saturday_recruiting_store, "get", lambda _day: bundle)
    monkeypatch.setattr(staffing_routes, "plant_now", lambda: datetime(2026, 7, 23, 8, tzinfo=SITE_TZ))

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), SATURDAY, 0,
        FormData([("action", "publish"), ("loc__Repair 1", "Ana"), ("loc__Dismantle", "Bob")]),
    )

    assert "publish_blocked=1" in response.headers["location"]
    assert saved[0].published is False


def test_post_deadline_publish_uses_only_requested_saturday_positions(monkeypatch):
    bundle = _repair_only_bundle()
    saved = []
    marked = []
    repair = staffing.Location("Repair 1", "Repair", "Bay 1", "Recycled", None, min_ops=1, max_ops=2)
    unrelated = staffing.Location("Dismantle", "Dismantle", "Bay 1", "Recycled", None, min_ops=1, max_ops=2)
    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", (repair, unrelated))
    monkeypatch.setattr(staffing_routes.work_centers_store, "min_ops", lambda loc: loc.min_ops)
    monkeypatch.setattr(staffing_routes.staffing, "load_schedule", lambda _day: staffing.Schedule(day=SATURDAY, assignments={}))
    monkeypatch.setattr(staffing_routes.staffing, "load_roster", lambda: [_person("Ana", Repair=3)])
    monkeypatch.setattr(staffing_routes.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(staffing_routes.saturday_recruiting_store, "get", lambda _day: bundle)
    monkeypatch.setattr(staffing_routes.saturday_recruiting_store, "mark_published", lambda day, now: marked.append((day, now)))
    monkeypatch.setattr(staffing_routes, "_safe_time_off_entries", lambda _day: [])
    monkeypatch.setattr(staffing_routes, "plant_now", lambda: datetime(2026, 7, 25, 8, tzinfo=SITE_TZ))

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), SATURDAY, 0,
        FormData([("action", "publish"), ("loc__Repair 1", "Ana")]),
    )

    assert response.headers["location"] == f"/staffing?day={SATURDAY.isoformat()}"
    assert saved[0].published is True
    assert "Dismantle" not in saved[0].assignments
    assert marked and marked[0][0] == SATURDAY


@pytest.mark.parametrize("status", ["recruiting", "closed", "published"])
def test_saturday_save_rejects_noncommitted_assignment_without_persisting(monkeypatch, status):
    bundle = _repair_only_bundle(status=status)
    saved = []
    repair = staffing.Location("Repair 1", "Repair", "Bay 1", "Recycled", None, min_ops=1, max_ops=2)
    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", (repair,))
    monkeypatch.setattr(staffing_routes.staffing, "load_schedule", lambda _day: staffing.Schedule(day=SATURDAY, assignments={"Repair 1": ["Ana"]}))
    monkeypatch.setattr(staffing_routes.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(staffing_routes.saturday_recruiting_store, "get", lambda _day: bundle)

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={"accept": "application/json"}), SATURDAY, 0,
        FormData([("action", "save"), ("loc__Repair 1", "Cara")]),
    )

    assert response.status_code == 409
    assert b"Cara is not committed to Saturday." in response.body
    assert saved == []


@pytest.mark.parametrize("action", ["save", "publish"])
def test_saturday_recruiting_lookup_failure_blocks_schedule_writes(monkeypatch, action):
    saved = []
    default_updates = []
    repair = staffing.Location("Repair 1", "Repair", "Bay 1", "Recycled", None, min_ops=1, max_ops=2)
    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", (repair,))
    monkeypatch.setattr(staffing_routes.staffing, "load_schedule", lambda _day: staffing.Schedule(day=SATURDAY, assignments={}))
    monkeypatch.setattr(staffing_routes.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(staffing_routes.work_centers_store, "save_one", lambda *args: default_updates.append(args))
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)

    def _lookup_failed(_day):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(staffing_routes.saturday_recruiting_store, "get", _lookup_failed)

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), SATURDAY, 0,
        FormData([
            ("action", action),
            ("loc__Repair 1", "Ana"),
            ("defaults_dirty__Repair 1", "1"),
            ("default__Repair 1", "Ana"),
        ]),
    )

    assert response.status_code == 409
    assert b"Saturday recruiting state could not be verified" in response.body
    assert saved == []
    assert default_updates == []


def test_staffing_template_has_saturday_off_availability_and_publish_lock():
    template = Path("src/zira_dashboard/templates/staffing.html").read_text()
    script = Path("src/zira_dashboard/static/staffing.js").read_text()

    assert "'Unassigned' if is_saturday_recruiting else 'Unscheduled'" in template
    assert 'class="section saturday-off"' in template
    assert 'class="saturday-availability-badge"' in template
    assert 'id="saturday-publish-lock"' in template
    assert 'disabled aria-describedby="saturday-publish-lock"' in template
    assert "window.SATURDAY_RECRUITING" in template
    assert "window.SATURDAY_COMMITTED_NAMES" in template
    assert "const __saturdayRecruiting = window.SATURDAY_RECRUITING;" in script
    assert "if (__saturdayRecruiting && !__saturdayCommittedNames.has(name)) return;" in script
