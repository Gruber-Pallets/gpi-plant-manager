"""Unit tests for the quick-punch fixer tick. All I/O is monkeypatched."""

from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from zira_dashboard import (
    attendance_corrections,
    attendance_location_snapshot,
    inbox_log,
    live_cache,
    odoo_client,
    plant_day,
    production_history,
    quick_punch_fix_settings,
    quick_punch_fixer as fixer,
    quick_punch_fixes,
    shift_config,
    staffing_hours,
)
from zira_dashboard.attendance_location_snapshot import LocationSnapshot
from zira_dashboard.quick_punch_fixes import FixScan, PlannedFix, SourceStint, item_key_for
from zira_dashboard.schedule_store import Break

CT = ZoneInfo("America/Chicago")
DAY = date(2026, 9, 18)


def ct(hour, minute=0, second=0):
    return datetime(2026, 9, 18, hour, minute, second, tzinfo=CT).astimezone(UTC)


def christian_fix(now=None):
    now = now or ct(7, 7)
    before = (
        SourceStint("Dismantler 3", ct(7), ct(7, 2, 10), False, (6190,)),
        SourceStint("Dismantler 2", ct(7, 2, 10), ct(7, 4, 27), False, (6207,)),
        SourceStint("Dismantler 3", ct(7, 4, 27), now, True, (6208,)),
    )
    ids = (6190, 6207, 6208)
    return PlannedFix(
        employee_odoo_id=8,
        person_name="Christian C.",
        wc_name="Dismantler 3",
        start_utc=ct(7),
        end_utc=None,
        source_attendance_ids=ids,
        before=before,
        item_key=item_key_for(8, ids),
    )


def ana_fix():
    ids = (21, 22)
    return PlannedFix(
        employee_odoo_id=9,
        person_name="Ana R.",
        wc_name="Dismantler 1",
        start_utc=ct(7, 30),
        end_utc=None,
        source_attendance_ids=ids,
        before=(
            SourceStint("Dismantler 1", ct(7, 30), ct(8, 1), False, (21,)),
            SourceStint("Dismantler 1", ct(8, 4), ct(8, 30), True, (22,)),
        ),
        item_key=item_key_for(9, ids),
    )


def _policy(*, owned=True, available=True, stale=False):
    return live_cache.AttendanceReadPolicy(
        mirror_owned=owned,
        available=available,
        refreshed_at=ct(7, 6),
        stale=stale,
        mode="live",
    )


def _snapshot(spans=(), **policy_kw):
    return LocationSnapshot(
        policy=_policy(**policy_kw),
        attendance_source=None,
        spans=tuple(spans),
        verified_cap_utc=ct(7, 7),
        current_attendance_ids=frozenset(),
    )


def _total(name, samples=(), last_reading_at=None, truncated=False):
    return SimpleNamespace(
        station=SimpleNamespace(name=name),
        samples=samples,
        last_reading_at=last_reading_at or ct(23),
        truncated=truncated,
    )


CHRISTIAN_ROWS = (
    {"odoo_attendance_id": 6190, "check_in_utc": ct(7), "check_out_utc": ct(7, 2, 10)},
    {"odoo_attendance_id": 6207, "check_in_utc": ct(7, 2, 10), "check_out_utc": ct(7, 4, 27)},
    {"odoo_attendance_id": 6208, "check_in_utc": ct(7, 4, 27), "check_out_utc": None},
)


def _preview(rows=CHRISTIAN_ROWS, operations=(object(),)):
    return SimpleNamespace(
        plans=(SimpleNamespace(operations=operations, source_intervals=rows),),
        employee_odoo_ids=(8,),
        item_key=christian_fix().item_key,
    )


def _stub_io(monkeypatch, *, fixes=None, mode="preview"):
    fix = christian_fix() if fixes is None else None
    planned = tuple(fixes) if fixes is not None else (fix,)
    calls = {
        "snapshot": 0,
        "leaderboard": 0,
        "find_fixes": 0,
        "payroll": 0,
        "events": [],
        "previews": [],
        "jobs": [],
        "existing_jobs": set(),
        "previewed": set(),
        "active": set(),
    }

    def read_snapshot(day, *, as_of_utc):
        calls["snapshot"] += 1
        return _snapshot()

    def leaderboard(client, day, *, now_utc=None):
        calls["leaderboard"] += 1
        return [_total("Dismantler 3")]

    def find_fixes(spans, **kwargs):
        calls["find_fixes"] += 1
        return FixScan(planned, ())

    def record_event(**kwargs):
        calls["events"].append(kwargs)
        calls["previewed"].add(kwargs["item_key"])
        return 1

    def correction_preview(**kwargs):
        calls["previews"].append(kwargs)
        return _preview()

    def create_job(*, preview, actor_email, actor_name, audit_summary=None):
        calls["jobs"].append(
            {
                "preview": preview,
                "actor_email": actor_email,
                "actor_name": actor_name,
                "audit_summary": audit_summary,
            }
        )
        calls["existing_jobs"].add(preview.item_key)
        return 1

    monkeypatch.setattr(quick_punch_fix_settings, "current", lambda: SimpleNamespace(mode=mode))
    monkeypatch.setattr(attendance_location_snapshot, "read_location_snapshot", read_snapshot)
    monkeypatch.setattr(production_history, "_metered_leaderboard", leaderboard)
    monkeypatch.setattr(quick_punch_fixes, "find_fixes", find_fixes)
    monkeypatch.setattr(shift_config, "breaks_for", lambda day: ())
    monkeypatch.setattr(
        staffing_hours,
        "current_pay_period_bounds",
        lambda today: (date(2026, 9, 14), date(2026, 9, 27)),
    )
    monkeypatch.setattr(odoo_client, "fetch_payroll_work_entries", lambda ids, start, end: [])
    monkeypatch.setattr(
        attendance_corrections, "active_job_employee_ids", lambda: set(calls["active"])
    )
    monkeypatch.setattr(
        attendance_corrections,
        "existing_job_item_keys",
        lambda keys: calls["existing_jobs"] & set(keys),
    )
    monkeypatch.setattr(
        inbox_log,
        "item_keys_with_action",
        lambda keys, action: calls["previewed"] & set(keys),
    )
    monkeypatch.setattr(inbox_log, "record_event", record_event)
    monkeypatch.setattr(attendance_corrections, "correction_preview", correction_preview)
    monkeypatch.setattr(attendance_corrections, "create_job_from_preview", create_job)
    return calls


def test_off_does_nothing_and_calls_nothing(monkeypatch):
    calls = _stub_io(monkeypatch, mode="off")
    seen = {"run": 0}
    monkeypatch.setattr(
        fixer, "run_for_day", lambda *a, **k: seen.__setitem__("run", seen["run"] + 1)
    )
    fixer.tick(now_utc=ct(7, 7))
    assert seen["run"] == 0
    assert calls["snapshot"] == 0
    assert calls["leaderboard"] == 0
    assert calls["events"] == []
    assert calls["previews"] == []


def test_preview_records_one_event_per_item_key_and_never_touches_odoo(monkeypatch):
    calls = _stub_io(monkeypatch, mode="preview")
    first = fixer.run_for_day(DAY, now_utc=ct(7, 7), mode="preview", client=object(), apply=True)
    second = fixer.run_for_day(DAY, now_utc=ct(7, 8), mode="preview", client=object(), apply=True)
    key = christian_fix().item_key
    assert first.applied_item_keys == frozenset({key})
    assert second.applied_item_keys == frozenset()
    assert second.deduped_item_keys == frozenset({key})
    assert len(calls["events"]) == 1
    event = calls["events"][0]
    assert event["item_kind"] == "quick_punch_fix"
    assert event["item_key"] == key
    assert event["person_name"] == "Christian C."
    assert event["category_label"] == "Quick-punch auto-fix"
    assert event["action"] == "quick_punch_would_merge"
    assert event["outcome"] == "Preview — Odoo not changed"
    assert event["before_value"] == "D3 7:00–7:02 · D2 7:02–7:04 · D3 7:04–now"
    assert event["after_value"] == "D3 7:00–now"
    assert event["actor_upn"] == attendance_corrections.QUICK_PUNCH_ACTOR_UPN
    assert event["actor_name"] == attendance_corrections.QUICK_PUNCH_ACTOR_NAME
    assert event["source"] == "auto"
    assert calls["previews"] == []
    assert calls["jobs"] == []


def test_live_creates_one_merge_job_per_key_across_two_ticks(monkeypatch):
    calls = _stub_io(monkeypatch, mode="live")
    first = fixer.run_for_day(DAY, now_utc=ct(7, 7), mode="live", client=object(), apply=True)
    second = fixer.run_for_day(DAY, now_utc=ct(7, 8), mode="live", client=object(), apply=True)
    key = christian_fix().item_key
    assert first.applied_item_keys == frozenset({key})
    assert second.applied_item_keys == frozenset()
    assert second.deduped_item_keys == frozenset({key})
    assert len(calls["previews"]) == 1
    assert calls["previews"][0]["merge"] is True
    assert calls["previews"][0]["item_key"] == key
    assert calls["previews"][0]["employee_odoo_ids"] == [8]
    assert calls["previews"][0]["target_work_center_name"] == "Dismantler 3"
    assert calls["previews"][0]["start_utc"] == ct(7)
    assert calls["previews"][0]["end_utc"] is None
    assert len(calls["jobs"]) == 1
    job = calls["jobs"][0]
    assert job["actor_email"] == attendance_corrections.QUICK_PUNCH_ACTOR_UPN
    assert job["actor_name"] == attendance_corrections.QUICK_PUNCH_ACTOR_NAME
    assert job["audit_summary"] == {
        "person_name": "Christian C.",
        "before": "D3 7:00–7:02 · D2 7:02–7:04 · D3 7:04–now",
        "after": "D3 7:00–now",
    }
    assert calls["events"] == []


def test_payroll_validated_employee_is_skipped(monkeypatch):
    _stub_io(monkeypatch, mode="live")
    monkeypatch.setattr(
        odoo_client,
        "fetch_payroll_work_entries",
        lambda ids, start, end: [{"employee_id": 8, "state": "validated", "conflict": False}],
    )
    run = fixer.run_for_day(DAY, now_utc=ct(7, 7), mode="live", client=object(), apply=True)
    assert run.payroll_skipped == frozenset({8})
    assert run.applied_item_keys == frozenset()


def test_payroll_error_skips_everyone(monkeypatch):
    _stub_io(monkeypatch, mode="live")

    def boom(*_args, **_kwargs):
        raise RuntimeError("odoo down")

    monkeypatch.setattr(odoo_client, "fetch_payroll_work_entries", boom)
    run = fixer.run_for_day(DAY, now_utc=ct(7, 7), mode="live", client=object(), apply=True)
    assert run.payroll_skipped == frozenset({8})
    assert run.eligible == ()
    assert run.applied_item_keys == frozenset()


def test_active_job_skips_the_employee(monkeypatch):
    calls = _stub_io(monkeypatch, mode="live")
    calls["active"].add(8)
    run = fixer.run_for_day(DAY, now_utc=ct(7, 7), mode="live", client=object(), apply=True)
    assert run.active_job_skipped == frozenset({8})
    assert run.applied_item_keys == frozenset()
    assert calls["jobs"] == []


def test_stale_or_unowned_mirror_is_an_empty_run(monkeypatch):
    for policy in (
        {"owned": False},
        {"available": False},
        {"stale": True},
    ):
        calls = _stub_io(monkeypatch, mode="live")
        monkeypatch.setattr(
            attendance_location_snapshot,
            "read_location_snapshot",
            lambda day, *, as_of_utc, _policy=policy: _snapshot(**_policy),
        )
        run = fixer.run_for_day(DAY, now_utc=ct(7, 7), mode="live", client=object(), apply=True)
        assert run == fixer._empty_run()
        assert calls["find_fixes"] == 0
        assert calls["leaderboard"] == 0


def test_leaderboard_error_is_an_empty_run(monkeypatch):
    calls = _stub_io(monkeypatch, mode="live")

    def boom(*_args, **_kwargs):
        raise RuntimeError("zira down")

    monkeypatch.setattr(production_history, "_metered_leaderboard", boom)
    run = fixer.run_for_day(DAY, now_utc=ct(7, 7), mode="live", client=object(), apply=True)
    assert run == fixer._empty_run()
    assert calls["find_fixes"] == 0


def test_day_before_the_pay_period_is_skipped(monkeypatch):
    calls = _stub_io(monkeypatch, mode="live")
    run = fixer.run_for_day(
        date(2026, 9, 1), now_utc=ct(7, 7), mode="live", client=object(), apply=True
    )
    assert run.eligible == ()
    assert run.applied_item_keys == frozenset()
    assert calls["jobs"] == []
    assert run.scan.fixes  # still scanned, just not applied


def test_per_fix_exception_does_not_stop_the_others(monkeypatch):
    first, second = christian_fix(), ana_fix()
    calls = _stub_io(monkeypatch, mode="live", fixes=(first, second))

    def preview(**kwargs):
        calls["previews"].append(kwargs)
        if kwargs["employee_odoo_ids"] == [8]:
            raise RuntimeError("christian failed")
        return SimpleNamespace(
            plans=(
                SimpleNamespace(
                    operations=(object(),),
                    source_intervals=(
                        {
                            "odoo_attendance_id": 21,
                            "check_in_utc": ct(7, 30),
                            "check_out_utc": ct(8, 1),
                        },
                        {
                            "odoo_attendance_id": 22,
                            "check_in_utc": ct(8, 4),
                            "check_out_utc": None,
                        },
                    ),
                ),
            ),
            employee_odoo_ids=(9,),
            item_key=second.item_key,
        )

    monkeypatch.setattr(attendance_corrections, "correction_preview", preview)
    run = fixer.run_for_day(DAY, now_utc=ct(7, 7), mode="live", client=object(), apply=True)
    assert run.applied_item_keys == frozenset({second.item_key})
    assert first.item_key not in run.applied_item_keys
    assert len(calls["jobs"]) == 1
    assert calls["jobs"][0]["preview"].item_key == second.item_key


def test_live_skips_when_source_rows_changed(monkeypatch):
    calls = _stub_io(monkeypatch, mode="live")
    moved = (
        {"odoo_attendance_id": 6190, "check_in_utc": ct(7), "check_out_utc": ct(7, 2, 10)},
        {"odoo_attendance_id": 6300, "check_in_utc": ct(7, 2, 10), "check_out_utc": None},
    )
    monkeypatch.setattr(attendance_corrections, "correction_preview", lambda **k: _preview(moved))
    run = fixer.run_for_day(DAY, now_utc=ct(7, 7), mode="live", client=object(), apply=True)
    assert run.applied_item_keys == frozenset()
    assert calls["jobs"] == []


def test_live_skips_when_the_open_row_has_clocked_out(monkeypatch):
    calls = _stub_io(monkeypatch, mode="live")
    closed = (
        {"odoo_attendance_id": 6190, "check_in_utc": ct(7), "check_out_utc": ct(7, 2, 10)},
        {"odoo_attendance_id": 6207, "check_in_utc": ct(7, 2, 10), "check_out_utc": ct(7, 4, 27)},
        {"odoo_attendance_id": 6208, "check_in_utc": ct(7, 4, 27), "check_out_utc": ct(8)},
    )
    monkeypatch.setattr(attendance_corrections, "correction_preview", lambda **k: _preview(closed))
    run = fixer.run_for_day(DAY, now_utc=ct(7, 7), mode="live", client=object(), apply=True)
    assert run.applied_item_keys == frozenset()
    assert calls["jobs"] == []


def test_live_skips_open_merge_value_error(monkeypatch):
    calls = _stub_io(monkeypatch, mode="live")

    def boom(**_kwargs):
        raise ValueError("open merge without the live row in range")

    monkeypatch.setattr(attendance_corrections, "correction_preview", boom)
    run = fixer.run_for_day(DAY, now_utc=ct(7, 7), mode="live", client=object(), apply=True)
    assert run.applied_item_keys == frozenset()
    assert calls["jobs"] == []


def test_already_fixed_plan_creates_no_job(monkeypatch):
    calls = _stub_io(monkeypatch, mode="live")
    monkeypatch.setattr(
        attendance_corrections, "correction_preview", lambda **k: _preview(operations=())
    )
    run = fixer.run_for_day(DAY, now_utc=ct(7, 7), mode="live", client=object(), apply=True)
    assert run.applied_item_keys == frozenset()
    assert calls["jobs"] == []


def test_live_still_creates_a_job_after_a_preview_event(monkeypatch):
    """Switching Preview -> Live must still fix the punches Dale already reviewed."""
    calls = _stub_io(monkeypatch, mode="live")
    calls["previewed"].add(christian_fix().item_key)
    run = fixer.run_for_day(DAY, now_utc=ct(7, 7), mode="live", client=object(), apply=True)
    assert run.applied_item_keys == frozenset({christian_fix().item_key})
    assert len(calls["jobs"]) == 1


def test_apply_false_creates_nothing(monkeypatch):
    calls = _stub_io(monkeypatch, mode="live")
    run = fixer.run_for_day(DAY, now_utc=ct(7, 7), mode="live", client=object(), apply=False)
    assert run.eligible == (christian_fix(),)
    assert run.applied_item_keys == frozenset()
    assert calls["jobs"] == []
    assert calls["events"] == []


def test_tick_logs_and_swallows_exceptions(monkeypatch, caplog):
    monkeypatch.setattr(quick_punch_fix_settings, "current", lambda: SimpleNamespace(mode="preview"))

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(fixer, "run_for_day", boom)
    with caplog.at_level("WARNING"):
        fixer.tick(now_utc=ct(7, 7))
    assert "RuntimeError" in caplog.text


def test_run_for_day_passes_spans_meters_and_breaks_into_find_fixes(monkeypatch):
    captured = {}

    def find_fixes(spans, **kwargs):
        captured["spans"] = spans
        captured.update(kwargs)
        return FixScan((), ())

    _stub_io(monkeypatch, mode="preview")
    monkeypatch.setattr(quick_punch_fixes, "find_fixes", find_fixes)
    monkeypatch.setattr(
        production_history,
        "_metered_leaderboard",
        lambda client, day, *, now_utc=None: [
            _total("Dismantler 2", samples=((ct(7, 3), 1),), last_reading_at=ct(7, 5))
        ],
    )
    monkeypatch.setattr(
        shift_config,
        "breaks_for",
        lambda day: (Break(time(9, 0), time(9, 15), "Morning"),),
    )
    spans = (SimpleNamespace(name="span"),)
    monkeypatch.setattr(
        attendance_location_snapshot,
        "read_location_snapshot",
        lambda day, *, as_of_utc: _snapshot(spans),
    )
    fixer.run_for_day(DAY, now_utc=ct(7, 7), mode="preview", client=object(), apply=True)
    assert captured["spans"] == spans
    assert captured["production_times_by_wc"] == {"Dismantler 2": (ct(7, 3),)}
    assert captured["meter_freshness_by_wc"] == {
        "Dismantler 2": quick_punch_fixes.MeterFreshness(ct(7, 5), False)
    }
    assert captured["breaks"] == ((ct(9), ct(9, 15)),)
    assert captured["now_utc"] == ct(7, 7)


def test_summary_helpers_match_the_preview_text():
    fix = christian_fix()
    assert quick_punch_fixes.before_summary(fix) == "D3 7:00–7:02 · D2 7:02–7:04 · D3 7:04–now"
    assert quick_punch_fixes.after_summary(fix) == "D3 7:00–now"


def test_warmer_is_registered():
    from zira_dashboard import app as app_module

    entry = next(item for item in app_module._WARMERS if item[0] == "quick-punch fixer")
    assert entry[1] is app_module._tick_quick_punch_fixer
    assert entry[2] == 60


def test_tick_uses_plant_today_and_deps_client(monkeypatch):
    seen = {}

    def fake_run(day, *, now_utc, mode, client, apply):
        seen["day"] = day
        seen["now_utc"] = now_utc
        seen["mode"] = mode
        seen["client"] = client
        seen["apply"] = apply
        return fixer._empty_run()

    monkeypatch.setattr(quick_punch_fix_settings, "current", lambda: SimpleNamespace(mode="live"))
    monkeypatch.setattr(fixer, "run_for_day", fake_run)
    import zira_dashboard.deps as deps

    monkeypatch.setattr(deps, "client", "CLIENT")
    fixer.tick(now_utc=ct(7, 7))
    assert seen["day"] == plant_day.today(ct(7, 7))
    assert seen["mode"] == "live"
    assert seen["client"] == "CLIENT"
    assert seen["apply"] is True
