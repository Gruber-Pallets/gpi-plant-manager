# tests/test_progress.py
from datetime import date, datetime, time, timezone

from zira_dashboard import progress as progress_mod
from zira_dashboard import shift_config, staffing
from zira_dashboard.leaderboard import StationTotal
from zira_dashboard.progress import progress_buckets
from zira_dashboard.stations import Station


def _station(name="Repair-1", category="Repair"):
    return Station(meter_id="m1", name=name, category=category, cell="Recycling")


def _utc(d: date, h: int, m: int) -> datetime:
    """site-local h:m -> UTC datetime (matches StationTotal.samples format)."""
    return datetime.combine(d, time(h, m), tzinfo=shift_config.SITE_TZ).astimezone(timezone.utc)


def _stationtotal(station, samples=(), active_intervals=()):
    return StationTotal(
        station=station,
        units=sum(u for _, u in samples),
        reading_count=len(samples),
        truncated=False,
        downtime_minutes=0,
        active_minutes=0,
        last_reading_at=None,
        last_status=None,
        samples=tuple(samples),
        active_intervals=tuple(active_intervals),
    )


def test_progress_buckets_default_uses_per_day_shift_start(monkeypatch):
    """Regression: default behavior anchors buckets to shift_start_for(day).
    A custom-hours day starting at 07:18 produces a first bucket labeled '07:18'.
    """
    d = date(2026, 4, 30)  # Thursday
    # custom_hours only override the global shift when the day is PUBLISHED
    # (per the 2026-05-15 change). Use published=True so this test's
    # custom 07:18 start is honored.
    monkeypatch.setattr(staffing, "load_schedule", lambda day: staffing.Schedule(
        day=day, published=True,
        custom_hours={"start": "07:18", "end": "15:30", "breaks": []},
    ))
    monkeypatch.setattr(shift_config, "work_weekdays", lambda: frozenset(range(7)))
    # progress.py imports these symbols at module load — patch the bound names there too.
    monkeypatch.setattr(progress_mod, "station_target", lambda station: 0)
    st = _station()
    samples = [(_utc(d, 7, 25), 5)]
    active = [(_utc(d, 7, 18), _utc(d, 8, 0))]
    now = _utc(d, 8, 0)
    buckets = progress_buckets([_stationtotal(st, samples, active)], d, now)
    assert buckets, "expected at least one bucket"
    assert buckets[0]["label"] == "07:18"


def test_progress_buckets_align_to_standard_uses_global_shift_start(monkeypatch):
    """With align_to_standard=True, a custom-hours day starting at 07:18
    still produces a first bucket labeled with the global shift start
    (e.g. '07:00' if that's what shift_start() returns).
    """
    d = date(2026, 4, 30)
    monkeypatch.setattr(staffing, "load_schedule", lambda day: staffing.Schedule(
        day=day, published=False,
        custom_hours={"start": "07:18", "end": "15:30", "breaks": []},
    ))
    monkeypatch.setattr(shift_config, "work_weekdays", lambda: frozenset(range(7)))
    monkeypatch.setattr(progress_mod, "station_target", lambda station: 0)
    monkeypatch.setattr(shift_config, "shift_start", lambda: time(7, 0))
    monkeypatch.setattr(shift_config, "shift_end", lambda: time(15, 30))
    monkeypatch.setattr(shift_config, "breaks", lambda: ())
    st = _station()
    samples = [(_utc(d, 7, 25), 5)]
    active = [(_utc(d, 7, 18), _utc(d, 8, 0))]
    now = _utc(d, 8, 0)
    buckets = progress_buckets([_stationtotal(st, samples, active)], d, now, align_to_standard=True)
    assert buckets, "expected at least one bucket"
    assert buckets[0]["label"] == "07:00"


def test_progress_buckets_align_to_standard_sample_at_0720_lands_in_0715_bucket(monkeypatch):
    """A sample at 07:20 site-local with align_to_standard=True belongs to
    the standard '07:15' bucket [07:15, 07:30).
    """
    d = date(2026, 4, 30)
    monkeypatch.setattr(staffing, "load_schedule", lambda day: staffing.Schedule(
        day=day, published=False,
        custom_hours={"start": "07:18", "end": "15:30", "breaks": []},
    ))
    monkeypatch.setattr(shift_config, "work_weekdays", lambda: frozenset(range(7)))
    monkeypatch.setattr(progress_mod, "station_target", lambda station: 0)
    monkeypatch.setattr(shift_config, "shift_start", lambda: time(7, 0))
    monkeypatch.setattr(shift_config, "shift_end", lambda: time(15, 30))
    monkeypatch.setattr(shift_config, "breaks", lambda: ())
    st = _station()
    samples = [(_utc(d, 7, 20), 5)]
    active = [(_utc(d, 7, 18), _utc(d, 8, 0))]
    now = _utc(d, 8, 0)
    buckets = progress_buckets([_stationtotal(st, samples, active)], d, now, align_to_standard=True)
    by_label = {b["label"]: b for b in buckets}
    assert "07:15" in by_label
    assert by_label["07:15"]["actual"] == 5
    assert by_label.get("07:00", {"actual": 0})["actual"] == 0


def test_progress_buckets_honors_published_schedule_on_saturday(monkeypatch):
    """Regression: A Saturday with a PUBLISHED schedule must produce
    progress buckets. Previously the function returned [] for any day
    outside the standard work_weekdays, blanking out every progress
    report on the recycling VS dashboard on Saturday."""
    saturday = date(2026, 5, 23)  # Saturday (weekday 5)
    monkeypatch.setattr(staffing, "load_schedule", lambda d: staffing.Schedule(
        day=d, published=True,
        custom_hours={"start": "06:00", "end": "10:00", "breaks": []},
    ))
    # Keep the GLOBAL work_weekdays as the default Mon-Fri; only the
    # published-schedule gate should open the day.
    monkeypatch.setattr(shift_config, "work_weekdays", lambda: frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(progress_mod, "station_target", lambda station: 0)
    st = _station()
    samples = [(_utc(saturday, 6, 10), 5), (_utc(saturday, 6, 25), 5)]
    active = [(_utc(saturday, 6, 0), _utc(saturday, 7, 0))]
    now = _utc(saturday, 7, 0)
    buckets = progress_buckets([_stationtotal(st, samples, active)], saturday, now)
    assert buckets, "expected non-empty buckets on a published Saturday"
    assert buckets[0]["label"] == "06:00"


def test_progress_buckets_returns_empty_on_unpublished_saturday(monkeypatch):
    """Symmetric check: an unpublished Saturday (no published schedule)
    still returns []. Only the published-schedule signal opens the gate."""
    saturday = date(2026, 5, 23)
    monkeypatch.setattr(staffing, "load_schedule", lambda d: staffing.Schedule(
        day=d, published=False, assignments={},
    ))
    monkeypatch.setattr(shift_config, "work_weekdays", lambda: frozenset({0, 1, 2, 3, 4}))
    monkeypatch.setattr(progress_mod, "station_target", lambda station: 0)
    st = _station()
    samples = [(_utc(saturday, 6, 10), 5)]
    active = [(_utc(saturday, 6, 0), _utc(saturday, 7, 0))]
    now = _utc(saturday, 7, 0)
    buckets = progress_buckets([_stationtotal(st, samples, active)], saturday, now)
    assert buckets == []
