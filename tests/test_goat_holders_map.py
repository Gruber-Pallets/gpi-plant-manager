"""Tests for awards.goat_holders_map().

The helper inverts per-group goat() calls into {name: [groups...]}.
Tests stub goat() + registered_groups() + apply_overrides_single
via monkeypatch so they run without a DB.
"""
from __future__ import annotations

from datetime import date, timedelta


def _stub(monkeypatch, *, groups, goat_by_group, overrides=None):
    """groups: list of group names; goat_by_group: {group: {name, ...} | None};
    overrides: optional {group: replacement_slot | None}.

    overrides=None means apply_overrides_single is a no-op passthrough.
    """
    from zira_dashboard import awards, work_centers_store

    monkeypatch.setattr(
        work_centers_store, "registered_groups", lambda: list(groups)
    )

    def _fake_goat(g):
        return goat_by_group.get(g)

    monkeypatch.setattr(awards, "goat", _fake_goat)

    def _fake_apply(slot, *, scope, group_name=None, **kw):
        if overrides is None:
            return slot
        if group_name in overrides:
            return overrides[group_name]
        return slot

    monkeypatch.setattr(awards, "apply_overrides_single", _fake_apply)
    # Default: no roster aliasing. Individual tests override when needed.
    monkeypatch.setattr(awards, "_people_name_rows", lambda: [])
    # Bust the in-process TTL cache between tests.
    awards._GOAT_HOLDERS_CACHE.clear()


def test_empty_groups_returns_empty_map(monkeypatch):
    from zira_dashboard import awards
    _stub(monkeypatch, groups=[], goat_by_group={})
    assert awards.goat_holders_map() == {}


def test_single_group_single_goat(monkeypatch):
    from zira_dashboard import awards
    _stub(
        monkeypatch,
        groups=["Repairs"],
        goat_by_group={"Repairs": {"name": "Alice", "units": 100}},
    )
    assert awards.goat_holders_map() == {"Alice": ["Repairs"]}


def test_multi_group_same_person(monkeypatch):
    """Alice holds GOAT in both Repairs and Juniors — two badges."""
    from zira_dashboard import awards
    _stub(
        monkeypatch,
        groups=["Repairs", "Juniors"],
        goat_by_group={
            "Repairs": {"name": "Alice", "units": 100},
            "Juniors": {"name": "Alice", "units": 60},
        },
    )
    out = awards.goat_holders_map()
    assert out == {"Alice": ["Repairs", "Juniors"]}


def test_group_with_no_goat_contributes_nothing(monkeypatch):
    """goat() returns None for a group with no qualifying data."""
    from zira_dashboard import awards
    _stub(
        monkeypatch,
        groups=["Repairs", "Empty"],
        goat_by_group={
            "Repairs": {"name": "Alice", "units": 100},
            "Empty": None,
        },
    )
    assert awards.goat_holders_map() == {"Alice": ["Repairs"]}


def test_override_replaces_name(monkeypatch):
    """Manual override re-points the GOAT slot to a different person."""
    from zira_dashboard import awards
    _stub(
        monkeypatch,
        groups=["Repairs"],
        goat_by_group={"Repairs": {"name": "Alice", "units": 100}},
        overrides={"Repairs": {"name": "Bob"}},
    )
    assert awards.goat_holders_map() == {"Bob": ["Repairs"]}


def test_override_deletes_slot(monkeypatch):
    """Override action='delete' → apply_overrides_single returns None → no entry."""
    from zira_dashboard import awards
    _stub(
        monkeypatch,
        groups=["Repairs"],
        goat_by_group={"Repairs": {"name": "Alice", "units": 100}},
        overrides={"Repairs": None},
    )
    assert awards.goat_holders_map() == {}


def test_hand_build_badge_and_override_wait_for_30_days(monkeypatch):
    from zira_dashboard import (
        awards,
        goat_categories,
        production_history,
        work_centers_store,
    )

    awards._GOAT_HOLDERS_CACHE.clear()
    goat_calls = []
    monkeypatch.setattr(
        work_centers_store,
        "registered_groups",
        lambda: ["Hand Builds"],
    )
    monkeypatch.setattr(
        goat_categories,
        "work_center_names",
        lambda _: {"Hand Build #1"},
    )
    monkeypatch.setattr(
        production_history,
        "daily_records",
        lambda *_: [
            {
                "day": date(2026, 7, 1) + timedelta(days=offset),
                "person": "Builder",
                "wc": "Hand Build #1",
                "units": 100,
                "hours": 7,
            }
            for offset in range(29)
        ],
    )
    monkeypatch.setattr(
        awards,
        "goat",
        lambda group: goat_calls.append(group) or None,
    )
    monkeypatch.setattr(
        awards,
        "apply_overrides_single",
        lambda *_args, **_kwargs: {"name": "Manual Builder"},
    )
    monkeypatch.setattr(awards, "_people_name_rows", lambda: [])

    assert awards.goat_holders_map() == {}
    assert goat_calls == []


def test_broken_group_does_not_poison_map(monkeypatch):
    """A goat() call that raises must not break the rest of the map."""
    from zira_dashboard import awards, work_centers_store

    awards._GOAT_HOLDERS_CACHE.clear()
    monkeypatch.setattr(
        work_centers_store, "registered_groups", lambda: ["Repairs", "Broken"]
    )

    def _fake_goat(g):
        if g == "Broken":
            raise RuntimeError("boom")
        return {"name": "Alice", "units": 100}

    monkeypatch.setattr(awards, "goat", _fake_goat)
    monkeypatch.setattr(
        awards, "apply_overrides_single", lambda slot, **kw: slot
    )
    monkeypatch.setattr(awards, "_people_name_rows", lambda: [])
    out = awards.goat_holders_map()
    assert out == {"Alice": ["Repairs"]}


def test_ttl_cache_returns_same_object(monkeypatch):
    """Two calls within TTL → goat() invoked only once."""
    from zira_dashboard import awards
    calls = {"n": 0}

    def _counting_goat(g):
        calls["n"] += 1
        return {"name": "Alice", "units": 100}

    awards._GOAT_HOLDERS_CACHE.clear()
    from zira_dashboard import work_centers_store
    monkeypatch.setattr(
        work_centers_store, "registered_groups", lambda: ["Repairs"]
    )
    monkeypatch.setattr(awards, "goat", _counting_goat)
    monkeypatch.setattr(
        awards, "apply_overrides_single", lambda slot, **kw: slot
    )
    monkeypatch.setattr(awards, "_people_name_rows", lambda: [])

    awards.goat_holders_map()
    awards.goat_holders_map()
    assert calls["n"] == 1  # second call hit the cache


def test_ttl_cache_recomputes_after_expiry(monkeypatch):
    """After TTL elapses, the cache rebuilds — goat() invoked twice."""
    from zira_dashboard import awards
    calls = {"n": 0}

    def _counting_goat(g):
        calls["n"] += 1
        return {"name": "Alice", "units": 100}

    awards._GOAT_HOLDERS_CACHE.clear()
    from zira_dashboard import work_centers_store
    monkeypatch.setattr(
        work_centers_store, "registered_groups", lambda: ["Repairs"]
    )
    monkeypatch.setattr(awards, "goat", _counting_goat)
    monkeypatch.setattr(
        awards, "apply_overrides_single", lambda slot, **kw: slot
    )
    monkeypatch.setattr(awards, "_people_name_rows", lambda: [])

    # First call populates cache.
    awards.goat_holders_map()
    # Force expire: rewrite the cached entry to be already-expired.
    awards._GOAT_HOLDERS_CACHE["value"] = (awards._GOAT_HOLDERS_CACHE["value"][0], 0.0)

    awards.goat_holders_map()
    assert calls["n"] == 2


def test_badge_name_aliases_maps_production_name_to_roster():
    """Historical production labels must resolve to today's roster label.

    GOAT rows can still say "Jose Luis" while staffing shows "Jose L.".
    Badges look up by the name rendered on the page, so both must key the map.
    """
    from zira_dashboard import awards

    people = [
        {"name": "Jose L.", "full_name": "Jose Luis Hernandez Alvarez"},
        {"name": "Jose O.", "full_name": "Jose Ochoa"},
    ]
    assert awards.badge_name_aliases("Jose Luis", people) == [
        "Jose Luis",
        "Jose L.",
        "Jose Luis Hernandez Alvarez",
    ]


def test_badge_name_aliases_keeps_ambiguous_prefix_unaliased():
    """If more than one person matches, do not guess — keep the award name only."""
    from zira_dashboard import awards

    people = [
        {"name": "Jose L.", "full_name": "Jose Luis Hernandez Alvarez"},
        {"name": "Jose C.", "full_name": "Jose Cabezas"},
    ]
    assert awards.badge_name_aliases("Jose", people) == ["Jose"]


def test_goat_holders_map_keys_roster_alias(monkeypatch):
    """Staffing pages look up 'Jose L.'; map must include that key."""
    from zira_dashboard import awards

    _stub(
        monkeypatch,
        groups=["Dismantlers"],
        goat_by_group={
            "Dismantlers": {"name": "Jose Luis", "units": 775},
        },
    )
    monkeypatch.setattr(
        awards,
        "_people_name_rows",
        lambda: [
            {"name": "Jose L.", "full_name": "Jose Luis Hernandez Alvarez"},
        ],
    )
    out = awards.goat_holders_map()
    assert out["Jose L."] == ["Dismantlers"]
    assert out["Jose Luis"] == ["Dismantlers"]
