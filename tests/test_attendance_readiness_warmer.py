"""Thirty-second readiness/cutover warmer contracts."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from contextlib import contextmanager
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from zira_dashboard import app as app_module


def test_attendance_readiness_warmer_is_registered_once_at_thirty_seconds():
    entries = [
        warmer
        for warmer in app_module._WARMERS
        if warmer[1] is app_module._tick_attendance_readiness
    ]

    assert entries == [("attendance readiness", app_module._tick_attendance_readiness, 30)]


def test_attendance_readiness_tick_runs_blocking_work_off_event_loop(monkeypatch):
    calls = []
    fixed = datetime(2026, 9, 1, 15, tzinfo=UTC)

    class FixedDateTime:
        @staticmethod
        def now(_tz):
            return fixed

    async def fake_to_thread(fn, *args):
        calls.append((fn, args))
        return "not_due"

    monkeypatch.setattr(app_module, "datetime", FixedDateTime)
    monkeypatch.setattr(app_module.asyncio, "to_thread", fake_to_thread)

    asyncio.run(app_module._tick_attendance_readiness())

    from zira_dashboard import attendance_readiness

    assert calls == [
        (attendance_readiness.run_warmer_tick, (fixed, app_module._zira_client()))
    ]


def test_active_live_warmer_keeps_persisted_readiness_current(monkeypatch):
    from zira_dashboard import attendance_readiness

    fixed = datetime(2026, 9, 1, 15, tzinfo=UTC)
    report = SimpleNamespace(ready=True)
    persisted = []
    config = SimpleNamespace(
        mode="live",
        cutover_at=fixed,
        live_gate=SimpleNamespace(activated_at=fixed),
    )

    @contextmanager
    def claimed():
        yield True

    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config_strict",
        lambda: config,
    )
    monkeypatch.setattr(attendance_readiness, "activate_due_cutover", lambda *_args, **_kwargs: "not_due")
    monkeypatch.setattr(attendance_readiness, "_shadow_refresh_claim", claimed)
    monkeypatch.setattr(
        attendance_readiness,
        "build_report",
        lambda now, production_client=None: report,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_persist_readiness_report",
        lambda value, *, checked_at, **_kwargs: persisted.append((value, checked_at)),
    )

    assert attendance_readiness.run_warmer_tick(fixed, object()) == "not_due"
    assert persisted == [(report, fixed)]


def test_active_live_with_future_rollback_keeps_readiness_current(monkeypatch):
    from zira_dashboard import attendance_readiness

    fixed = datetime(2026, 9, 1, 15, tzinfo=UTC)
    report = SimpleNamespace(ready=True)
    persisted = []
    config = SimpleNamespace(
        mode="shadow",
        cutover_at=datetime(2026, 9, 2, 12, tzinfo=UTC),
        live_gate=SimpleNamespace(activated_at=fixed),
    )

    @contextmanager
    def claimed():
        yield True

    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config_strict",
        lambda: config,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_settle_due_rollback",
        lambda *_args, **_kwargs: "not_due",
    )
    monkeypatch.setattr(attendance_readiness, "_shadow_refresh_claim", claimed)
    monkeypatch.setattr(
        attendance_readiness,
        "refresh_shadow_comparison",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pending rollback is still active Live")
        ),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "build_report",
        lambda now, production_client=None: report,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_persist_readiness_report",
        lambda value, *, checked_at, **_kwargs: persisted.append((value, checked_at)),
    )

    assert attendance_readiness.run_warmer_tick(fixed, object()) == "not_due"
    assert persisted == [(report, fixed)]


def test_pending_live_keeps_shadow_proof_and_readiness_current(monkeypatch):
    from zira_dashboard import attendance_readiness

    fixed = datetime(2026, 9, 1, 15, tzinfo=UTC)
    report = SimpleNamespace(ready=True)
    refreshed = []
    persisted = []
    config = SimpleNamespace(
        mode="live",
        cutover_at=datetime(2026, 9, 2, 12, tzinfo=UTC),
        live_gate=SimpleNamespace(activated_at=None),
    )

    @contextmanager
    def claimed():
        yield True

    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config_strict",
        lambda: config,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "activate_due_cutover",
        lambda *_args, **_kwargs: "not_due",
    )
    monkeypatch.setattr(attendance_readiness, "_shadow_refresh_claim", claimed)
    monkeypatch.setattr(
        attendance_readiness,
        "_previous_complete_workday",
        lambda _now: fixed.date(),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_epoch_entered_at",
        lambda: fixed,
    )
    def refresh(day, client, **kwargs):
        refreshed.append((day, client, kwargs))
        return object()

    monkeypatch.setattr(attendance_readiness, "refresh_shadow_comparison", refresh)
    monkeypatch.setattr(
        attendance_readiness,
        "build_report",
        lambda *_args, **_kwargs: report,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_persist_readiness_report",
        lambda value, *, checked_at, **_kwargs: persisted.append((value, checked_at)),
    )

    meter = object()
    assert attendance_readiness.run_warmer_tick(fixed, meter) == "not_due"
    assert refreshed == [
        (
            fixed.date(),
            meter,
            {"now_utc": fixed, "shadow_entered_at": fixed},
        )
    ]
    assert persisted == [(report, fixed)]


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_off_transition_wins_against_inflight_shadow_warmer(monkeypatch):
    from zira_dashboard import app_settings, attendance_readiness, db, production_history

    fixed = datetime(2098, 1, 7, 21, tzinfo=UTC)
    day = fixed.date()
    source_started = Event()
    release_source = Event()
    result = []
    keys = (
        "odoo_attendance_location",
        "odoo_attendance_shadow_epoch",
        "odoo_attendance_shadow_health",
        "odoo_attendance_readiness_report",
    )
    original = {key: app_settings.get_setting(key) for key in keys}

    @contextmanager
    def claimed():
        yield True

    @contextmanager
    def source_view():
        yield object()

    def freeze(*_args, **_kwargs):
        source_started.set()
        assert release_source.wait(5)
        return object(), ()

    monkeypatch.setattr(attendance_readiness, "_shadow_refresh_claim", claimed)
    monkeypatch.setattr(attendance_readiness, "_previous_complete_workday", lambda _now: day)
    monkeypatch.setattr(attendance_readiness, "_freeze_leaderboard_rows", freeze)
    monkeypatch.setattr(attendance_readiness.db, "read_snapshot", source_view)
    frozen_day = production_history.StrictSourceSnapshot(
        day=day,
        shift_start_utc=datetime(2098, 1, 7, 13, tzinfo=UTC),
        shift_end_utc=datetime(2098, 1, 7, 21, tzinfo=UTC),
        break_windows=(),
        shift_by_day={},
        stations=(),
        work_center_by_odoo_id={},
        source_fingerprint="warmer-source",
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_snapshot_production_day",
        lambda _day: frozen_day,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_lock_production_config_sources_cur",
        lambda _cur: None,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_source_fingerprint",
        lambda *_args, **_kwargs: "local-source",
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_snapshot_work_center_mapper",
        lambda: lambda _odoo_id: None,
    )
    monkeypatch.setattr(
        attendance_readiness.attendance_mirror,
        "health_snapshot",
        lambda: SimpleNamespace(
            last_incremental_observed_at=fixed,
            baseline_completed_at=fixed,
        ),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "compute_shadow_comparison",
        lambda *_args, **_kwargs: attendance_readiness.ShadowComparison(
            day=day,
            checked_at=fixed,
            complete=True,
            changed_worker_units=0.0,
            comparison_keys=0,
            strict_worker_units=0.0,
            current_worker_units=0.0,
            error=None,
        ),
    )
    monkeypatch.setattr(
        attendance_readiness.shift_config,
        "shift_start_for",
        lambda _day: datetime.min.time().replace(hour=7),
    )
    monkeypatch.setattr(
        attendance_readiness.shift_config,
        "shift_end_for",
        lambda _day: datetime.min.time().replace(hour=15),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "build_report",
        lambda *_args, **_kwargs: pytest.fail(
            "superseded Shadow warmer must not persist readiness"
        ),
    )

    try:
        with db.cursor() as cur:
            app_settings.set_setting(
                "odoo_attendance_location",
                {"mode": "shadow", "cutover_at": None, "live_gate": None},
                cur=cur,
            )
            attendance_readiness.start_shadow_epoch_cur(cur, entered_at=fixed)

        thread = Thread(
            target=lambda: result.append(
                attendance_readiness.run_warmer_tick(fixed, object())
            )
        )
        thread.start()
        assert source_started.wait(5)
        with db.cursor() as cur:
            attendance_readiness.attendance_location_policy.lock_rollout_decision_cur(cur)
            app_settings.set_setting(
                "odoo_attendance_location",
                {"mode": "off", "cutover_at": None, "live_gate": None},
                cur=cur,
            )
            attendance_readiness.clear_shadow_evidence_cur(cur)
        release_source.set()
        thread.join(5)

        assert not thread.is_alive()
        assert result == ["not_due"]
        assert app_settings.get_setting("odoo_attendance_shadow_health") is None
        assert app_settings.get_setting("odoo_attendance_readiness_report") is None
    finally:
        release_source.set()
        with db.cursor() as cur:
            cur.execute("DELETE FROM app_settings WHERE key = ANY(%s)", (list(keys),))
            for key, value in original.items():
                if value is not None:
                    app_settings.set_setting(key, value, cur=cur)
