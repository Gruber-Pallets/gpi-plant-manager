"""Unit tests for ribbon_announce — first-workday gate + podium payload."""
from __future__ import annotations

from datetime import date



def test_previous_month_rolls_year():
    from zira_dashboard import ribbon_announce
    assert ribbon_announce.previous_month(date(2026, 1, 5)) == (2025, 12)
    assert ribbon_announce.previous_month(date(2026, 8, 3)) == (2026, 7)


def test_is_ribbon_announce_day_when_first_is_workday(monkeypatch):
    from zira_dashboard import ribbon_announce, shift_config
    monkeypatch.setattr(shift_config, "is_workday", lambda d: d.weekday() < 5)
    assert ribbon_announce.is_ribbon_announce_day(date(2026, 7, 1)) is True  # Wed
    assert ribbon_announce.is_ribbon_announce_day(date(2026, 7, 2)) is False


def test_is_ribbon_announce_day_skips_weekend_to_monday(monkeypatch):
    from zira_dashboard import ribbon_announce, shift_config
    monkeypatch.setattr(shift_config, "is_workday", lambda d: d.weekday() < 5)
    # Aug 2026: Sat 1, Sun 2, Mon 3
    assert ribbon_announce.is_ribbon_announce_day(date(2026, 8, 1)) is False
    assert ribbon_announce.is_ribbon_announce_day(date(2026, 8, 2)) is False
    assert ribbon_announce.is_ribbon_announce_day(date(2026, 8, 3)) is True
    assert ribbon_announce.is_ribbon_announce_day(date(2026, 8, 4)) is False


def test_is_ribbon_announce_day_mid_month_false(monkeypatch):
    from zira_dashboard import ribbon_announce, shift_config
    monkeypatch.setattr(shift_config, "is_workday", lambda d: True)
    assert ribbon_announce.is_ribbon_announce_day(date(2026, 8, 15)) is False


def test_is_ribbon_announce_day_propagates_is_workday_error(monkeypatch):
    import pytest

    from zira_dashboard import ribbon_announce, shift_config

    def _raising_is_workday(d):
        if d.day == 1:
            raise RuntimeError("shift config unavailable")
        return d.weekday() < 5

    monkeypatch.setattr(shift_config, "is_workday", _raising_is_workday)
    with pytest.raises(RuntimeError, match="shift config unavailable"):
        ribbon_announce.is_ribbon_announce_day(date(2026, 8, 1))
    with pytest.raises(RuntimeError, match="shift config unavailable"):
        ribbon_announce.ribbon_announce_payload(date(2026, 8, 1))


def test_payload_none_off_announce_day(monkeypatch):
    from zira_dashboard import ribbon_announce, shift_config
    monkeypatch.setattr(shift_config, "is_workday", lambda d: d.weekday() < 5)
    assert ribbon_announce.ribbon_announce_payload(date(2026, 8, 4)) is None


def _stub_groups_and_records(monkeypatch, *, groups, members_map, records):
    from zira_dashboard import production_history, work_centers_store, awards

    class _FakeLoc:
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(work_centers_store, "registered_groups", lambda: list(groups))
    monkeypatch.setattr(
        work_centers_store,
        "members",
        lambda kind, name: [_FakeLoc(n) for n in members_map.get(name, [])],
    )
    monkeypatch.setattr(
        production_history,
        "daily_records",
        lambda s, e, c=None: [r for r in records if s <= r["day"] <= e],
    )
    monkeypatch.setattr(awards, "apply_overrides", lambda slots, **kw: slots)
    awards._GOAT_CACHE.clear()


def test_payload_omits_zero_production_groups(monkeypatch):
    from zira_dashboard import ribbon_announce, shift_config

    monkeypatch.setattr(shift_config, "is_workday", lambda d: d.weekday() < 5)
    # Aug 3 2026 is first workday → July winners
    _stub_groups_and_records(
        monkeypatch,
        groups=["Repairs", "EmptyGroup"],
        members_map={
            "Repairs": ["Repair 1"],
            "EmptyGroup": ["Ghost 1"],
        },
        records=[
            {"day": date(2026, 7, 10), "person": "Alice", "wc": "Repair 1",
             "units": 100.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 7, 11), "person": "Bob", "wc": "Repair 1",
             "units": 90.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 7, 12), "person": "Cara", "wc": "Repair 1",
             "units": 80.0, "hours": 8.0, "downtime": 0.0},
            # EmptyGroup has only zero-unit rows → omit
            {"day": date(2026, 7, 10), "person": "Zed", "wc": "Ghost 1",
             "units": 0.0, "hours": 8.0, "downtime": 0.0},
        ],
    )
    payload = ribbon_announce.ribbon_announce_payload(date(2026, 8, 3))
    assert payload is not None
    assert payload["year"] == 2026
    assert payload["month"] == 7
    assert payload["label"] == "July 2026"
    assert [g["group"] for g in payload["groups"]] == ["Repairs"]
    assert [e["name"] for e in payload["groups"][0]["entries"]] == ["Alice", "Bob", "Cara"]
    assert [e["position"] for e in payload["groups"][0]["entries"]] == [1, 2, 3]


def test_payload_omits_group_when_apply_overrides_empty(monkeypatch):
    from zira_dashboard import awards, ribbon_announce, shift_config

    monkeypatch.setattr(shift_config, "is_workday", lambda d: d.weekday() < 5)
    _stub_groups_and_records(
        monkeypatch,
        groups=["Repairs", "StrippedGroup"],
        members_map={
            "Repairs": ["Repair 1"],
            "StrippedGroup": ["Strip 1"],
        },
        records=[
            {"day": date(2026, 7, 10), "person": "Alice", "wc": "Repair 1",
             "units": 100.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 7, 11), "person": "Bob", "wc": "Repair 1",
             "units": 90.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 7, 12), "person": "Cara", "wc": "Repair 1",
             "units": 80.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 7, 10), "person": "Dan", "wc": "Strip 1",
             "units": 50.0, "hours": 8.0, "downtime": 0.0},
        ],
    )

    def _strip_stripped_group(slots, **kw):
        if kw.get("group_name") == "StrippedGroup":
            return []
        return slots

    monkeypatch.setattr(awards, "apply_overrides", _strip_stripped_group)

    payload = ribbon_announce.ribbon_announce_payload(date(2026, 8, 3))
    assert payload is not None
    assert [g["group"] for g in payload["groups"]] == ["Repairs"]


def test_payload_none_when_all_groups_empty(monkeypatch):
    from zira_dashboard import ribbon_announce, shift_config

    monkeypatch.setattr(shift_config, "is_workday", lambda d: True)
    _stub_groups_and_records(
        monkeypatch,
        groups=["EmptyGroup"],
        members_map={"EmptyGroup": ["Ghost 1"]},
        records=[],
    )
    assert ribbon_announce.ribbon_announce_payload(date(2026, 8, 1)) is None
