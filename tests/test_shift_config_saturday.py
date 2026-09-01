"""Saturday-default resolution in shift_config. Fully stubbed — no DB."""
from datetime import date, datetime, time
import pytest
from zira_dashboard import (
    optional_workday,
    schedule_store,
    shift_config,
    staffing,
    saturday_schedule_store,
)
from zira_dashboard.saturday_schedule_store import SaturdaySchedule
from zira_dashboard.schedule_store import Break
from zira_dashboard.shift_config import SITE_TZ

SAT = date(2026, 5, 16)   # Saturday (weekday 5)
TUE = date(2026, 5, 19)   # Tuesday (weekday 1)
HOLIDAY_DAY = date(2026, 11, 27)  # Friday
HOLIDAY = optional_workday.OptionalWorkday(
    HOLIDAY_DAY, "holiday", "Black Friday", 42
)

SAT_DEFAULT = SaturdaySchedule(
    time(6, 0), time(12, 0),
    (Break(time(8, 0), time(8, 15), "Morning break"),
     Break(time(10, 0), time(10, 30), "Lunch")),
)
WEEKDAY = schedule_store.Schedule(
    time(7, 0), time(15, 30), frozenset({0, 1, 2, 3, 4}),
    (Break(time(9, 0), time(9, 15), "AM"), Break(time(11, 0), time(11, 30), "Lunch")),
)


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    # No DB: stub both stores and the work-week.
    monkeypatch.setattr(saturday_schedule_store, "current", lambda: SAT_DEFAULT)
    monkeypatch.setattr(schedule_store, "current", lambda: WEEKDAY)


def _load(published, custom=None):
    return lambda d: staffing.Schedule(
        day=d, published=published, assignments={}, custom_hours=custom
    )


def test_published_saturday_uses_saturday_default(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", _load(True))
    assert shift_config.shift_start_for(SAT) == time(6, 0)
    assert shift_config.shift_end_for(SAT) == time(12, 0)
    assert shift_config.breaks_for(SAT) == SAT_DEFAULT.breaks


def test_unpublished_saturday_gated_falls_back_to_weekday(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", _load(False))
    assert shift_config.shift_start_for(SAT) == time(7, 0)
    assert shift_config.shift_end_for(SAT) == time(15, 30)
    assert shift_config.breaks_for(SAT) == WEEKDAY.breaks


def test_configured_saturday_shows_default_even_on_draft(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", _load(False))
    assert shift_config.configured_shift_start_for(SAT) == time(6, 0)
    assert shift_config.configured_shift_end_for(SAT) == time(12, 0)
    assert shift_config.configured_breaks_for(SAT) == SAT_DEFAULT.breaks


def test_published_per_day_custom_overrides_saturday_default(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule",
        _load(True, {"start": "06:00", "end": "14:00", "breaks": []}))
    assert shift_config.shift_start_for(SAT) == time(6, 0)
    assert shift_config.shift_end_for(SAT) == time(14, 0)
    assert shift_config.breaks_for(SAT) == ()   # empty list = no breaks


def test_configured_draft_custom_wins_over_saturday_default(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule",
        _load(False, {"start": "06:00", "end": "13:00", "breaks": []}))
    assert shift_config.configured_shift_end_for(SAT) == time(13, 0)


def test_weekday_unchanged(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", _load(True))
    assert shift_config.shift_start_for(TUE) == time(7, 0)
    assert shift_config.shift_end_for(TUE) == time(15, 30)
    assert shift_config.breaks_for(TUE) == WEEKDAY.breaks


def test_productive_minutes_published_saturday(monkeypatch):
    # 06:00-12:00 = 360 min, minus 15 + 30 = 315.
    monkeypatch.setattr(staffing, "load_schedule", _load(True))
    assert shift_config.productive_minutes_for(SAT) == 315


def test_in_shift_on_published_saturday(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", _load(True))
    assert shift_config.in_shift_on(datetime(2026, 5, 16, 7, 0, tzinfo=SITE_TZ)) is True
    assert shift_config.in_shift_on(datetime(2026, 5, 16, 8, 5, tzinfo=SITE_TZ)) is False
    assert shift_config.in_shift_on(datetime(2026, 5, 16, 12, 30, tzinfo=SITE_TZ)) is False


def test_rounding_snaps_to_saturday_boundaries(monkeypatch):
    """The punch path feeds shift_start_for/shift_end_for into apply_rounding."""
    from zira_dashboard.rounding import apply_rounding, RoundingSettings
    monkeypatch.setattr(staffing, "load_schedule", _load(True))
    windows = RoundingSettings(15, 0, 0, 15)
    start, end = shift_config.shift_start_for(SAT), shift_config.shift_end_for(SAT)
    in_punch = datetime(2026, 5, 16, 5, 52, tzinfo=SITE_TZ)
    out_punch = datetime(2026, 5, 16, 12, 8, tzinfo=SITE_TZ)
    assert apply_rounding("clock_in", in_punch, start, end, windows).astimezone(SITE_TZ).time() == time(6, 0)
    assert apply_rounding("clock_out", out_punch, start, end, windows).astimezone(SITE_TZ).time() == time(12, 0)


def test_scheduler_hours_source():
    assert shift_config.scheduler_hours_source(SAT, False) == "saturday_default"
    assert shift_config.scheduler_hours_source(TUE, False) == "weekday_default"
    assert shift_config.scheduler_hours_source(SAT, True) == "custom"
    assert shift_config.scheduler_hours_source(TUE, True) == "custom"


def _holiday(monkeypatch, *, operational):
    monkeypatch.setattr(
        optional_workday,
        "for_day",
        lambda day: HOLIDAY if day == HOLIDAY_DAY else None,
    )
    monkeypatch.setattr(
        optional_workday,
        "holiday_is_explicitly_published",
        lambda day: operational and day == HOLIDAY_DAY,
    )


def test_worked_holiday_uses_saturday_default_hours_and_breaks(monkeypatch):
    _holiday(monkeypatch, operational=True)
    monkeypatch.setattr(staffing, "load_schedule", _load(True))

    assert shift_config.is_workday(HOLIDAY_DAY) is True
    assert shift_config.shift_start_for(HOLIDAY_DAY) == time(6, 0)
    assert shift_config.shift_end_for(HOLIDAY_DAY) == time(12, 0)
    assert shift_config.breaks_for(HOLIDAY_DAY) == SAT_DEFAULT.breaks


def test_closed_holiday_editor_proposes_saturday_default(monkeypatch):
    _holiday(monkeypatch, operational=False)
    monkeypatch.setattr(staffing, "load_schedule", _load(False))

    assert shift_config.is_workday(HOLIDAY_DAY) is False
    assert shift_config.configured_shift_start_for(HOLIDAY_DAY) == time(6, 0)
    assert shift_config.configured_shift_end_for(HOLIDAY_DAY) == time(12, 0)
    assert shift_config.configured_breaks_for(HOLIDAY_DAY) == SAT_DEFAULT.breaks
    assert (
        shift_config.scheduler_hours_source(HOLIDAY_DAY, False)
        == "saturday_default"
    )


def test_holiday_custom_hours_win_over_saturday_default(monkeypatch):
    _holiday(monkeypatch, operational=True)
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        _load(
            True,
            {
                "start": "08:00",
                "end": "13:30",
                "breaks": [
                    {
                        "start": "10:00",
                        "end": "10:30",
                        "name": "Break",
                    }
                ],
            },
        ),
    )

    assert shift_config.shift_start_for(HOLIDAY_DAY) == time(8, 0)
    assert shift_config.shift_end_for(HOLIDAY_DAY) == time(13, 30)
    assert shift_config.breaks_for(HOLIDAY_DAY) == (
        Break(time(10, 0), time(10, 30), "Break"),
    )


def test_holiday_precedence_closes_posted_weekday_schedule(monkeypatch):
    _holiday(monkeypatch, operational=False)
    monkeypatch.setattr(staffing, "load_schedule", _load(True))

    assert shift_config.is_workday(HOLIDAY_DAY) is False


def test_holiday_publication_lookup_failure_closes_day(monkeypatch):
    monkeypatch.setattr(optional_workday, "for_day", lambda _day: HOLIDAY)
    monkeypatch.setattr(
        optional_workday,
        "holiday_is_explicitly_published",
        lambda _day: (_ for _ in ()).throw(RuntimeError("store unavailable")),
    )

    assert shift_config.is_workday(HOLIDAY_DAY) is False


def test_legacy_published_saturday_remains_active(monkeypatch):
    monkeypatch.setattr(optional_workday, "for_day", lambda _day: None)
    monkeypatch.setattr(staffing, "load_schedule", _load(True))

    assert shift_config.is_workday(SAT) is True


class _SnapshotCursor:
    def __init__(self, row):
        self.row = row

    def execute(self, _sql, _params):
        return None

    def fetchone(self):
        return self.row


def test_canonical_snapshot_uses_full_global_default_when_singleton_missing():
    snapshot = shift_config.snapshot_for(
        TUE,
        cur=_SnapshotCursor(
            {
                "global_id": None,
                "saturday_id": None,
                "day_published": False,
                "holiday_odoo_id": None,
            }
        ),
    )

    assert snapshot.shift_start == schedule_store.DEFAULT_SCHEDULE.shift_start
    assert snapshot.shift_end == schedule_store.DEFAULT_SCHEDULE.shift_end
    assert snapshot.breaks == schedule_store.DEFAULT_SCHEDULE.breaks
    assert snapshot.is_workday is True


def test_canonical_snapshot_uses_full_saturday_default_when_singleton_missing():
    snapshot = shift_config.snapshot_for(
        SAT,
        cur=_SnapshotCursor(
            {
                "global_id": None,
                "saturday_id": None,
                "day_published": True,
                "custom_hours": None,
                "holiday_odoo_id": None,
            }
        ),
    )

    assert snapshot.shift_start == saturday_schedule_store.DEFAULT.shift_start
    assert snapshot.shift_end == saturday_schedule_store.DEFAULT.shift_end
    assert snapshot.breaks == saturday_schedule_store.DEFAULT.breaks
    assert snapshot.is_workday is True
