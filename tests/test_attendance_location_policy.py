"""Behavior tests for the attendance-location rollout policy boundary."""

from datetime import UTC, date, datetime, time, timedelta

import pytest

from zira_dashboard import attendance_location_policy as policy
from zira_dashboard import shift_config


@pytest.fixture(autouse=True)
def _fixed_workday_boundary(monkeypatch):
    """Keep policy tests independent of Postgres-backed schedule overrides."""
    monkeypatch.setattr(shift_config, "shift_start_for", lambda _day: time(7, 0))


def _cutover_utc(day: date) -> datetime:
    return datetime.combine(
        day,
        shift_config.shift_start_for(day),
        tzinfo=shift_config.SITE_TZ,
    ).astimezone(UTC)


def _stored_live_config(cutover_utc: datetime, activated_at: datetime | None):
    return {
        "mode": "live",
        "cutover_at": cutover_utc.isoformat(),
        "live_gate": {
            "checked_at": (cutover_utc - timedelta(minutes=1)).isoformat(),
            "report_digest": "b617a1c0" * 8,
            "activated_at": activated_at.isoformat() if activated_at else None,
        },
    }


def test_rollout_defaults_to_off_for_missing_or_invalid_setting(monkeypatch):
    monkeypatch.setattr(policy.app_settings, "get_setting", lambda _key: None)
    assert policy.get_rollout_config() == policy.RolloutConfig(
        mode="off", cutover_at=None, live_gate=None
    )

    monkeypatch.setattr(
        policy.app_settings,
        "get_setting",
        lambda _key: {"mode": "broken", "cutover_at": "not-a-date"},
    )
    assert policy.get_rollout_config().mode == "off"


def test_shadow_config_round_trips_as_one_app_setting(monkeypatch):
    saved = {}
    monkeypatch.setattr(
        policy.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: saved.update(
            {"key": key, "value": value, "cur": cur}
        ),
    )
    marker = object()

    policy.set_rollout_config(
        policy.RolloutConfig(mode="shadow", cutover_at=None, live_gate=None),
        cur=marker,
    )

    assert saved == {
        "key": "odoo_attendance_location",
        "value": {"mode": "shadow", "cutover_at": None, "live_gate": None},
        "cur": marker,
    }


def test_live_config_requires_a_fresh_readiness_gate(monkeypatch):
    monkeypatch.setattr(
        policy.app_settings,
        "set_setting",
        lambda *_args, **_kwargs: pytest.fail("invalid live config was persisted"),
    )
    cutover = _cutover_utc(datetime.now(shift_config.SITE_TZ).date() + timedelta(days=1))

    with pytest.raises(ValueError, match="^live_readiness_required$"):
        policy.set_rollout_config(
            policy.RolloutConfig(mode="live", cutover_at=cutover, live_gate=None)
        )

    stale_gate = policy.LiveGate(
        checked_at=datetime.now(UTC) - timedelta(minutes=6),
        report_digest="b617a1c0" * 8,
        activated_at=None,
    )
    with pytest.raises(ValueError, match="^live_readiness_required$"):
        policy.set_rollout_config(
            policy.RolloutConfig(
                mode="live", cutover_at=cutover, live_gate=stale_gate
            )
        )


def test_live_cutover_must_be_timezone_aware_and_on_workday_boundary():
    gate = policy.LiveGate(
        checked_at=datetime.now(UTC),
        report_digest="b617a1c0" * 8,
        activated_at=None,
    )
    day = datetime.now(shift_config.SITE_TZ).date() + timedelta(days=1)

    with pytest.raises(ValueError, match="^cutover_timezone_required$"):
        policy.set_rollout_config(
            policy.RolloutConfig(
                mode="live",
                cutover_at=datetime.combine(day, shift_config.shift_start_for(day)),
                live_gate=gate,
            )
        )

    wrong_boundary = _cutover_utc(day) + timedelta(minutes=5)
    with pytest.raises(ValueError, match="^cutover_boundary_required$"):
        policy.set_rollout_config(
            policy.RolloutConfig(
                mode="live", cutover_at=wrong_boundary, live_gate=gate
            )
        )


def test_live_is_active_only_after_gate_activation(monkeypatch):
    day = date(2026, 8, 31)
    cutover = _cutover_utc(day)
    monkeypatch.setattr(
        policy.app_settings,
        "get_setting",
        lambda _key: _stored_live_config(cutover, cutover),
    )

    assert policy.live_is_active(now_utc=cutover - timedelta(seconds=1)) is False
    assert policy.live_is_active(now_utc=cutover) is True


def test_match_state_tracks_legacy_pending_and_strict(monkeypatch):
    cutover_day = date(2026, 8, 31)
    cutover = _cutover_utc(cutover_day)
    stored = _stored_live_config(cutover, None)
    monkeypatch.setattr(policy.app_settings, "get_setting", lambda _key: stored)
    monkeypatch.setattr(policy, "strict_days", lambda: {date(2026, 8, 20)})

    assert policy.match_state_for_day(
        cutover_day, now_utc=cutover - timedelta(seconds=1)
    ) == "legacy"
    assert policy.match_state_for_day(cutover_day, now_utc=cutover) == "pending"
    assert policy.match_state_for_day(
        date(2026, 8, 20), now_utc=cutover
    ) == "strict"

    stored["live_gate"]["activated_at"] = cutover.isoformat()
    assert policy.match_state_for_day(cutover_day, now_utc=cutover) == "strict"
    assert policy.match_state_for_day(
        date(2026, 8, 30), now_utc=cutover
    ) == "legacy"


def test_day_is_strict_for_live_cutover_or_historical_override(monkeypatch):
    cutover_day = date(2026, 8, 31)
    cutover_utc = datetime.combine(
        cutover_day,
        shift_config.shift_start_for(cutover_day),
        tzinfo=shift_config.SITE_TZ,
    ).astimezone(UTC)
    monkeypatch.setattr(policy.app_settings, "get_setting", lambda key: {
        "mode": "live",
        "cutover_at": cutover_utc.isoformat(),
        "live_gate": {
            "checked_at": (cutover_utc - timedelta(minutes=1)).isoformat(),
            "report_digest": "b617a1c0" * 8,
            "activated_at": cutover_utc.isoformat(),
        },
    })
    monkeypatch.setattr(policy, "strict_days", lambda: {date(2026, 8, 20)})
    monkeypatch.setattr(policy, "_utc_now", lambda: cutover_utc)
    assert policy.day_is_strict(date(2026, 8, 31)) is True
    assert policy.day_is_strict(date(2026, 8, 20)) is True
    assert policy.day_is_strict(date(2026, 8, 19)) is False


def test_department_requirement_uses_explicit_row_or_safe_defaults(monkeypatch):
    rows_by_name = {
        "Assembly": [{"requires_work_center": False}],
        "Maintenance": [],
        "12 Supervisor": [],
        "Unknown": [],
    }
    monkeypatch.setattr(
        policy.db,
        "query",
        lambda _sql, params: rows_by_name.get(params[0], []),
    )

    assert policy.department_requires_work_center("Assembly") is False
    assert policy.department_requires_work_center("Maintenance") is False
    assert policy.department_requires_work_center(" 12 Supervisor ") is False
    assert policy.department_requires_work_center("Unknown") is True
    assert policy.department_requires_work_center(None) is True


def test_set_department_requirement_marks_the_choice_explicit(monkeypatch):
    executed = {}
    monkeypatch.setattr(
        policy.db,
        "execute",
        lambda sql, params: executed.update({"sql": sql, "params": params}),
    )

    policy.set_department_requirement("Maintenance", True)

    assert "requires_work_center_explicit = TRUE" in executed["sql"]
    assert executed["params"] == (True, "Maintenance")
