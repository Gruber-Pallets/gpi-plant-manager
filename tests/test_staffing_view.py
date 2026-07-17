"""Characterization tests for ``staffing_view.build_staffing_bays``.

These pin the behavior of the per-work-center render model that used to
live inline in the GET /staffing handler (bands A+B), extracted verbatim
into ``staffing_view``. They assert the REAL returned structure so any
future change to the derivation is caught.

Like ``test_staffing_options_color.py``, these exercise the pure builder
directly — no HTTP layer. ``staffing.LOCATIONS`` and the per-WC config
pass-throughs on ``work_centers_store`` are monkeypatched so nothing
touches Postgres.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from zira_dashboard import staffing, staffing_view, work_centers_store

# skill_color sentinels (from staffing.SKILL_COLORS) — pinned so a palette
# change is a deliberate, test-visible decision.
GREEN = "#4ade80"   # level 3
COMPETENT = "#e6edf3"  # level 2
ORANGE = "#fb923c"  # level 1
RED = "#ef4444"     # level 0


def _person(name, *, reserve=False, active=True, **skills):
    """Minimal Person stand-in matching staffing.Person's attrs."""
    return SimpleNamespace(
        name=name,
        reserve=reserve,
        active=active,
        skills=skills,
        level=lambda s, _skills=skills: int(_skills.get(s, 0)),
    )


def _sched(assignments=None, wc_notes=None):
    return SimpleNamespace(
        assignments=dict(assignments or {}),
        wc_notes=dict(wc_notes or {}),
    )


@pytest.fixture
def patch_wcs(monkeypatch):
    """Patch staffing.LOCATIONS + work_centers_store config pass-throughs.

    Returns a setter taking a list of (Location, cfg) pairs where cfg is
    {required, min, max, defaults}. Mirrors the production DB-backed
    helpers without standing up Postgres.
    """

    def _apply(pairs):
        locs = tuple(loc for loc, _ in pairs)
        cfg = {loc.name: c for loc, c in pairs}
        monkeypatch.setattr(staffing, "LOCATIONS", locs)
        monkeypatch.setattr(
            work_centers_store, "required_skills",
            lambda loc: list(cfg[loc.name]["required"]),
        )
        monkeypatch.setattr(
            work_centers_store, "min_ops", lambda loc: cfg[loc.name]["min"],
        )
        monkeypatch.setattr(
            work_centers_store, "max_ops", lambda loc: cfg[loc.name]["max"],
        )
        monkeypatch.setattr(
            work_centers_store, "default_people",
            lambda loc: list(cfg[loc.name]["defaults"]),
        )

    return _apply


def _loc(name, bay="Bay 1", *, skill="Repair", min_ops=1, max_ops=1, required=()):
    return staffing.Location(
        name, skill, bay, "Recycled", None,
        min_ops=min_ops, max_ops=max_ops, required_skills=required,
    )


# --------------------------------------------------------------------------
# Return shape
# --------------------------------------------------------------------------

def test_return_keys_are_exactly_the_bands_ab_context_keys(patch_wcs):
    patch_wcs([(_loc("Repair 1", required=("Repair",)),
                {"required": ("Repair",), "min": 1, "max": 1, "defaults": []})])
    m = staffing_view.build_staffing_bays(
        roster=[_person("Alice", Repair=3)],
        sched=_sched({"Repair 1": ["Alice"]}),
        time_off_entries=[],
        publish_blocked=0,
    )
    assert set(m.keys()) == {
        "bays",
        "publish_block_reasons",
        "time_off_names",
        "time_off_entries",
        "partial_hours_by_name",
        "partial_range_by_name",
        "partial_clear_by_name",
        "unassigned",
        "off",
        "saturday_committed_names",
        "saturday_availability_by_name",
        "is_saturday_recruiting",
        "reserves",
        "people_meta",
        "defaults_by_loc",
        "all_active_people",
    }


# --------------------------------------------------------------------------
# (a) skill color
# --------------------------------------------------------------------------

def test_required_skill_colors_assigned_and_pool_by_level(patch_wcs):
    """required=('Repair',): level 0 → red, level>=1 → staffing.skill_color."""
    patch_wcs([(_loc("Repair 1", required=("Repair",)),
                {"required": ("Repair",), "min": 1, "max": 1, "defaults": []})])
    roster = [
        _person("Ace", Repair=3),   # green
        _person("Practicer", Repair=1),  # orange
        _person("Comp", Repair=2),  # competent foreground
        _person("Zero", Repair=0),  # red
    ]
    m = staffing_view.build_staffing_bays(
        roster=roster,
        sched=_sched({"Repair 1": ["Ace", "Zero"]}),
        time_off_entries=[],
        publish_blocked=0,
    )
    row = m["bays"][0]["rows"][0]
    assigned = {a["name"]: a for a in row["assigned"]}
    assert assigned["Ace"]["level"] == 3 and assigned["Ace"]["color"] == GREEN
    assert assigned["Zero"]["level"] == 0 and assigned["Zero"]["color"] == RED

    pool = {r["name"]: r for r in row["pool"]}
    assert pool["Ace"]["color"] == GREEN
    assert pool["Practicer"]["level"] == 1 and pool["Practicer"]["color"] == ORANGE
    assert pool["Comp"]["level"] == 2 and pool["Comp"]["color"] == COMPETENT
    assert pool["Zero"]["level"] == 0 and pool["Zero"]["color"] == RED
    # Trained = level >= 1 in all required skills.
    assert pool["Zero"]["trained"] is False
    assert pool["Practicer"]["trained"] is True


def test_blank_required_renders_neutral_level_2_for_assigned_and_pool(patch_wcs):
    """No required skills → level 2, color 'neutral', trained=True everywhere."""
    patch_wcs([(_loc("Trim Saw 1", bay="Bay 4", skill="Trim Saw",
                     min_ops=2, max_ops=2, required=()),
                {"required": (), "min": 2, "max": 2, "defaults": []})])
    roster = [_person("Ace", Repair=3), _person("Zero", Repair=0),
              _person("Res", reserve=True)]
    m = staffing_view.build_staffing_bays(
        roster=roster,
        sched=_sched({"Trim Saw 1": ["Ace", "Zero"]}),
        time_off_entries=[],
        publish_blocked=0,
    )
    row = m["bays"][0]["rows"][0]
    for a in row["assigned"]:
        assert a["level"] == 2 and a["color"] == "neutral"
    for r in row["pool"]:
        assert r["level"] == 2 and r["color"] == "neutral" and r["trained"] is True


# --------------------------------------------------------------------------
# (b) headcount thresholds
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "assigned_names, min_ops, max_ops, expected_status, expected_badge",
    [
        ([], 1, 1, "empty", ""),
        (["Ace"], 2, 2, "under", "needs 2"),
        (["Ace", "Practicer", "Comp"], 1, 2, "over", "max 2"),
        (["Ace"], 1, 1, "ok", ""),
    ],
)
def test_headcount_status_thresholds(
    patch_wcs, assigned_names, min_ops, max_ops, expected_status, expected_badge,
):
    patch_wcs([(_loc("WC", required=("Repair",), min_ops=min_ops, max_ops=max_ops),
                {"required": ("Repair",), "min": min_ops, "max": max_ops, "defaults": []})])
    roster = [_person("Ace", Repair=3), _person("Practicer", Repair=1),
              _person("Comp", Repair=2)]
    m = staffing_view.build_staffing_bays(
        roster=roster,
        sched=_sched({"WC": list(assigned_names)}),
        time_off_entries=[],
        publish_blocked=0,
    )
    row = m["bays"][0]["rows"][0]
    assert row["hc_status"] == expected_status
    assert row["hc_badge"] == expected_badge


def test_unlimited_max_ops_never_reads_over_and_labels_infinity(patch_wcs):
    patch_wcs([(_loc("Tablets", required=("Repair",), min_ops=1, max_ops=None),
                {"required": ("Repair",), "min": 1, "max": None, "defaults": []})])
    roster = [_person(f"P{i}", Repair=2) for i in range(5)]
    m = staffing_view.build_staffing_bays(
        roster=roster,
        sched=_sched({"Tablets": [p.name for p in roster]}),
        time_off_entries=[],
        publish_blocked=0,
    )
    row = m["bays"][0]["rows"][0]
    assert row["hc_status"] == "ok"
    assert row["max_ops_label"] == "∞"


# --------------------------------------------------------------------------
# (c) reserve pool sort + flags
# --------------------------------------------------------------------------

def test_pool_sort_reserves_last_then_level_desc_then_name(patch_wcs):
    """Sort key is (reserve, -level, name.lower())."""
    patch_wcs([(_loc("Repair 1", required=("Repair",)),
                {"required": ("Repair",), "min": 1, "max": 1, "defaults": []})])
    roster = [
        _person("zoe", Repair=3),
        _person("amy", Repair=3),
        _person("bob", Repair=1),
        _person("ResHigh", reserve=True, Repair=3),
        _person("ResLow", reserve=True, Repair=0),
    ]
    m = staffing_view.build_staffing_bays(
        roster=roster,
        sched=_sched({"Repair 1": []}),
        time_off_entries=[],
        publish_blocked=0,
    )
    pool = m["bays"][0]["rows"][0]["pool"]
    # Non-reserves first (level 3 amy/zoe by name, then level 1 bob),
    # then reserves (level 3 ResHigh, then level 0 ResLow).
    assert [r["name"] for r in pool] == ["amy", "zoe", "bob", "ResHigh", "ResLow"]
    assert [r["reserve"] for r in pool] == [False, False, False, True, True]


def test_reserve_in_left_rail_and_people_meta(patch_wcs):
    patch_wcs([(_loc("Repair 1", required=("Repair",)),
                {"required": ("Repair",), "min": 1, "max": 1, "defaults": []})])
    roster = [_person("Worker", Repair=2), _person("Boss", reserve=True, Repair=3)]
    m = staffing_view.build_staffing_bays(
        roster=roster,
        sched=_sched({"Repair 1": []}),
        time_off_entries=[],
        publish_blocked=0,
    )
    assert m["reserves"] == ["Boss"]
    assert m["unassigned"] == ["Worker"]
    assert m["people_meta"] == {
        "Worker": {"reserve": False},
        "Boss": {"reserve": True},
    }


# --------------------------------------------------------------------------
# (d) currently-assigned safety net
# --------------------------------------------------------------------------

def test_safety_net_readds_inactive_assignee_to_pool(patch_wcs):
    """An inactive (or deleted) person still assigned historically is
    re-added to the pool with reserve=False and trained=(level>=1),
    so dirty data isn't silently dropped from the picker."""
    patch_wcs([(_loc("Repair 1", required=("Repair",)),
                {"required": ("Repair",), "min": 1, "max": 1, "defaults": []})])
    roster = [
        _person("Active", Repair=2),
        _person("Ghost", active=False, Repair=3),  # inactive → not in options_for
    ]
    m = staffing_view.build_staffing_bays(
        roster=roster,
        sched=_sched({"Repair 1": ["Ghost"]}),
        time_off_entries=[],
        publish_blocked=0,
    )
    row = m["bays"][0]["rows"][0]
    pool_by_name = {r["name"]: r for r in row["pool"]}
    # Ghost is inactive so options_for skips them; the safety net re-adds.
    assert "Ghost" in pool_by_name
    ghost = pool_by_name["Ghost"]
    assert ghost["reserve"] is False
    assert ghost["level"] == 3 and ghost["trained"] is True
    # Active person isn't assigned here, so not in left-rail unassigned? They
    # have no station → unassigned. Ghost is inactive → never in unassigned.
    assert m["unassigned"] == ["Active"]
    assert "Ghost" not in m["all_active_people"]


# --------------------------------------------------------------------------
# (e) time-off exclusion from pool/headcount, retention in assigned
# --------------------------------------------------------------------------

def test_full_day_off_excluded_from_pool_and_headcount(patch_wcs):
    """A full-day-off person who is NOT assigned anywhere vanishes from
    every WC's pool and the Unscheduled list, and is not counted."""
    patch_wcs([(_loc("Repair 1", required=("Repair",), min_ops=1, max_ops=1),
                {"required": ("Repair",), "min": 1, "max": 1, "defaults": []})])
    roster = [_person("Here", Repair=2), _person("Gone", Repair=3)]
    m = staffing_view.build_staffing_bays(
        roster=roster,
        sched=_sched({"Repair 1": ["Here"]}),
        time_off_entries=[{"name": "Gone", "hours": None}],
        publish_blocked=0,
    )
    row = m["bays"][0]["rows"][0]
    pool_names = [r["name"] for r in row["pool"]]
    assert "Gone" not in pool_names          # excluded from picker
    assert "Gone" not in m["unassigned"]     # excluded from left rail
    assert m["time_off_names"] == ["Gone"]   # shown in Time Off panel
    assert row["hc_status"] == "ok"          # Here covers it


def test_full_day_off_retained_in_assigned_but_dropped_from_headcount(patch_wcs):
    """An assigned person who goes full-day off stays in `assigned` (so the
    saved schedule is preserved) but is pulled from `present_assigned`, so
    the slot reads as needing coverage."""
    patch_wcs([(_loc("Trim Saw 1", bay="Bay 4", skill="Trim Saw",
                     min_ops=2, max_ops=2, required=("Repair",)),
                {"required": ("Repair",), "min": 2, "max": 2, "defaults": []})])
    roster = [_person("Stay", Repair=3), _person("Off", Repair=2)]
    m = staffing_view.build_staffing_bays(
        roster=roster,
        sched=_sched({"Trim Saw 1": ["Stay", "Off"]}),
        time_off_entries=[{"name": "Off", "hours": None}],
        publish_blocked=0,
    )
    row = m["bays"][0]["rows"][0]
    assert {a["name"] for a in row["assigned"]} == {"Stay", "Off"}  # retained
    assert [a["name"] for a in row["present_assigned"]] == ["Stay"]  # dropped
    assert row["hc_status"] == "under"  # 1 present < min 2


# --------------------------------------------------------------------------
# (f) partial hours for any positive off-span; full-day in time_off_names
# --------------------------------------------------------------------------

def test_partial_hours_window_and_full_day_routing(patch_wcs):
    patch_wcs([(_loc("Repair 1", required=("Repair",)),
                {"required": ("Repair",), "min": 1, "max": 1, "defaults": []})])
    roster = [_person("P", Repair=2)]
    entries = [
        {"name": "Partial", "hours": 4.0, "time_range": "8a-12p",
         "timing_label": "arrives 12:00pm", "request_id": 77},
        {"name": "FullDay", "hours": None},
        {"name": "ZeroHrs", "hours": 0.0},
        {"name": "LongPartial", "hours": 8.0, "time_range": "7a-3p", "request_id": 88},
        {"name": "EmpIdOnly", "hours": 2.0, "time_range": "1p-3p", "emp_id": 555},
    ]
    m = staffing_view.build_staffing_bays(
        roster=roster, sched=_sched({"Repair 1": ["P"]}),
        time_off_entries=entries, publish_blocked=0,
    )
    # Any positive off-span is a partial — including one ≥8h on a longer
    # shift. (Whole-shift windows never get here: the sync layer normalizes
    # them to full_day with hours=None.)
    assert m["partial_hours_by_name"] == {
        "Partial": 4.0, "LongPartial": 8.0, "EmpIdOnly": 2.0}
    # Badge text prefers the shaped timing label; bare range is the fallback.
    assert m["partial_range_by_name"] == {
        "Partial": "arrives 12:00pm", "LongPartial": "7a-3p",
        "EmpIdOnly": "1p-3p"}
    # Clear keys: request_id wins, else emp_id (as str).
    assert m["partial_clear_by_name"] == {
        "Partial": {"request_id": 77},
        "LongPartial": {"request_id": 88},
        "EmpIdOnly": {"emp_id": "555"},
    }
    # Full-day only in the Time Off panel; a zero-hour entry is neither
    # partial nor full-day (hours is not None) so it appears nowhere.
    assert m["time_off_names"] == ["FullDay"]
    assert m["partial_hours_by_name"].get("ZeroHrs") is None


# --------------------------------------------------------------------------
# (g) publish_block_reasons gating
# --------------------------------------------------------------------------

def test_publish_block_reasons_include_every_below_minimum_work_center(patch_wcs):
    pair = _loc("Hand Build #1", bay="Bay 6", min_ops=2, max_ops=2, required=("Repair",))
    solo = _loc("Junior #1", bay="Bay 16", min_ops=1, max_ops=1, required=("Repair",))
    patch_wcs([
        (pair, {"required": ("Repair",), "min": 2, "max": 2, "defaults": []}),
        (solo, {"required": ("Repair",), "min": 1, "max": 1, "defaults": []}),
    ])

    model = staffing_view.build_staffing_bays(
        roster=[_person("Jordan", Repair=3)],
        sched=_sched({"Hand Build #1": ["Jordan"]}),
        time_off_entries=[],
        publish_blocked=1,
    )

    assert model["publish_block_reasons"] == [
        "Hand Build #1 requires 2 operators — currently 1.",
        "Junior #1 requires 1 operators — currently 0.",
    ]


def test_publish_block_reasons_exclude_work_centers_that_are_off(patch_wcs):
    enabled = _loc("Hand Build #1", bay="Bay 6", min_ops=2, max_ops=2, required=("Repair",))
    disabled = _loc("Junior #1", bay="Bay 16", min_ops=1, max_ops=1, required=("Repair",))
    patch_wcs([
        (enabled, {"required": ("Repair",), "min": 2, "max": 2, "defaults": []}),
        (disabled, {"required": ("Repair",), "min": 1, "max": 1, "defaults": []}),
    ])

    model = staffing_view.build_staffing_bays(
        roster=[_person("Jordan", Repair=3)],
        sched=_sched({"Hand Build #1": ["Jordan"]}),
        time_off_entries=[],
        publish_blocked=1,
        enabled_work_centers={"Hand Build #1"},
    )

    assert model["publish_block_reasons"] == [
        "Hand Build #1 requires 2 operators — currently 1.",
    ]
