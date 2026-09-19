"""Dry-run-first pay-period cleanup for the quick-punch fixer."""

from datetime import UTC, date, datetime
from io import StringIO
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from scripts import quick_punch_backfill
from zira_dashboard.quick_punch_fixes import (
    FixScan,
    PlannedFix,
    SkippedFix,
    SourceStint,
    item_key_for,
)
from zira_dashboard.quick_punch_fixer import FixRun

CT = ZoneInfo("America/Chicago")


def ct(hour, minute=0):
    return datetime(2026, 9, 18, hour, minute, tzinfo=CT).astimezone(UTC)


def _fix():
    before = (
        SourceStint("Dismantler 3", ct(7), ct(7, 2), False, (1,)),
        SourceStint("Dismantler 3", ct(7, 4), ct(8), True, (2,)),
    )
    return PlannedFix(
        employee_odoo_id=8,
        person_name="Christian C.",
        wc_name="Dismantler 3",
        start_utc=ct(7),
        end_utc=None,
        source_attendance_ids=(1, 2),
        before=before,
        item_key=item_key_for(8, (1, 2)),
    )


def _skip():
    return SkippedFix(
        8,
        "Christian C.",
        "meter_not_current",
        (SourceStint("Dismantler 2", ct(7, 2), ct(7, 4), False, (9,)),),
    )


def _empty_run(**overrides):
    values = dict(
        scan=FixScan((), ()),
        payroll_skipped=frozenset(),
        active_job_skipped=frozenset(),
        deduped_item_keys=frozenset(),
        eligible=(),
        applied_item_keys=frozenset(),
    )
    values.update(overrides)
    return FixRun(**values)


def _stub(monkeypatch, *, mode="live", runs=None):
    calls = []

    def run_for_day(day, **kwargs):
        calls.append({"day": day, **kwargs})
        if runs is None:
            return _empty_run(
                scan=FixScan((_fix(),), (_skip(),)),
                eligible=(_fix(),),
            )
        return runs[day]

    monkeypatch.setattr(
        quick_punch_backfill.staffing_hours,
        "current_pay_period_bounds",
        lambda today: (date(2026, 9, 14), date(2026, 9, 27)),
    )
    monkeypatch.setattr(quick_punch_backfill.plant_day, "today", lambda: date(2026, 9, 19))
    monkeypatch.setattr(
        quick_punch_backfill.quick_punch_fix_settings,
        "current",
        lambda: SimpleNamespace(mode=mode),
    )
    monkeypatch.setattr(quick_punch_backfill.quick_punch_fixer, "run_for_day", run_for_day)
    monkeypatch.setattr(quick_punch_backfill.deps, "client", "CLIENT")
    monkeypatch.setattr(
        quick_punch_backfill, "_spans_for_past_day", lambda day: ("spans", day)
    )
    return calls


def test_dry_run_prints_fixes_and_skips_and_never_applies(monkeypatch):
    calls = _stub(monkeypatch)
    out = StringIO()
    code = quick_punch_backfill.run(["--start", "2026-09-18", "--end", "2026-09-18"], out=out)
    assert code == 0
    text = out.getvalue()
    assert "Christian C." in text
    assert "→" in text
    assert "skip meter_not_current" in text
    assert "Dry run — nothing changed. Re-run with --yes to apply." in text
    assert calls[0]["apply"] is False
    assert calls[0]["mode"] == "live"
    assert calls[0]["client"] == "CLIENT"
    assert calls[0]["spans"] == ("spans", date(2026, 9, 18))


def test_yes_refuses_unless_live(monkeypatch):
    for mode in ("preview", "off"):
        calls = _stub(monkeypatch, mode=mode)
        out = StringIO()
        code = quick_punch_backfill.run(["--yes"], out=out)
        assert code == 2
        assert "not Live" in out.getvalue()
        assert calls == []


def test_yes_in_live_applies(monkeypatch):
    calls = _stub(monkeypatch, mode="live")
    out = StringIO()
    code = quick_punch_backfill.run(
        ["--start", "2026-09-18", "--end", "2026-09-18", "--yes"], out=out
    )
    assert code == 0
    assert calls[0]["apply"] is True
    assert "Dry run" not in out.getvalue()


def test_start_before_the_pay_period_is_clamped(monkeypatch):
    calls = _stub(monkeypatch)
    quick_punch_backfill.run(["--start", "2026-09-01", "--end", "2026-09-14"], out=StringIO())
    assert [call["day"] for call in calls] == [date(2026, 9, 14)]
