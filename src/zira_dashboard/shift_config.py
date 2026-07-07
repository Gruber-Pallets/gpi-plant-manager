"""Shift window helpers and target defaults, backed by schedule_store."""

from __future__ import annotations

from datetime import date, datetime, time, UTC
from zoneinfo import ZoneInfo

from . import schedule_store

SITE_TZ = ZoneInfo("America/Chicago")

# Target throughput per station per DAY (pallets). Edit as needed.
TARGET_PER_DAY = {
    "Dismantler": 325,
    "Repair": 220,
    "Other": 0,
}


def _sched():
    return schedule_store.current()


def shift_start() -> time:
    return _sched().shift_start


def shift_end() -> time:
    return _sched().shift_end


def work_weekdays() -> frozenset[int]:
    return _sched().work_weekdays


def breaks() -> tuple:
    """Tuple of Break objects (start, end, name)."""
    return _sched().breaks


def productive_minutes_per_day() -> int:
    """Shift minutes minus all scheduled breaks (lunch + cleanup included)."""
    def _mins(t): return t.hour * 60 + t.minute
    s, e = shift_start(), shift_end()
    total = _mins(e) - _mins(s)
    for b in breaks():
        total -= _mins(b.end) - _mins(b.start)
    return max(0, total)


SATURDAY = 5  # date.weekday(): Monday=0 .. Sunday=6


def _custom_hours(day: date, *, published_only: bool) -> dict | None:
    """The per-day custom_hours dict for `day`, or None.

    published_only=True (dashboards + punch path): only a PUBLISHED day's
    override applies — the long-standing rule that keeps drafts out of live
    metrics. published_only=False (the scheduler's own editor): the
    configured override applies whether or not it's published, so the Hours
    pill shows what will apply once published.

    Lazy import to avoid the shift_config -> staffing -> schedule_store cycle.
    """
    from . import staffing
    sched = staffing.load_schedule(day)
    if published_only and not getattr(sched, "published", False):
        return None
    ch = sched.custom_hours
    return ch if isinstance(ch, dict) else None


def _saturday_default():
    """The plant Saturday default schedule (cached singleton)."""
    from . import saturday_schedule_store
    return saturday_schedule_store.current()


def is_workday(day: date) -> bool:
    """True if `day` should be treated as a workday.

    A day counts as a workday if either:
      (a) its weekday is in the global `work_weekdays()` set, OR
      (b) a PUBLISHED schedule exists for that day — the explicit signal
          that an otherwise non-standard weekday (e.g. Saturday) is being
          worked.

    Shared by every "is this in shift?" gate (shift_elapsed_minutes,
    in_shift_on, progress_buckets, admin backfill) so the published-Saturday
    escape hatch stays consistent. A drift between these gates is what
    caused the recycling VS dashboard to show empty progress reports and
    100% uptime on Saturdays before this helper existed.
    """
    if day.weekday() in work_weekdays():
        return True
    try:
        from . import staffing
        sched = staffing.load_schedule(day)
        return bool(getattr(sched, "published", False))
    except Exception:
        return False


def _use_saturday_default(day: date, *, published_only: bool) -> bool:
    """Whether `day` resolves from the Saturday default (vs the weekday
    global schedule), assuming no per-day override applies.

    Gated callers (dashboards, punch path) use it only when the Saturday is
    actually being worked — is_workday(day), which for a non-work weekday
    means a published schedule exists. So an unpublished Saturday behaves
    exactly as today (weekday global), staying inert. The scheduler's
    configured view always shows the Saturday default on a Saturday, so a
    fresh Saturday pre-fills 6a-12p before anything is published.
    """
    if day.weekday() != SATURDAY:
        return False
    if not published_only:
        return True
    return is_workday(day)


def _resolve_start(day: date, *, published_only: bool) -> time:
    ch = _custom_hours(day, published_only=published_only)
    if ch and isinstance(ch.get("start"), str):
        try:
            return time.fromisoformat(ch["start"])
        except ValueError:
            pass
    if _use_saturday_default(day, published_only=published_only):
        return _saturday_default().shift_start
    return shift_start()


def _resolve_end(day: date, *, published_only: bool) -> time:
    ch = _custom_hours(day, published_only=published_only)
    if ch and isinstance(ch.get("end"), str):
        try:
            return time.fromisoformat(ch["end"])
        except ValueError:
            pass
    if _use_saturday_default(day, published_only=published_only):
        return _saturday_default().shift_end
    return shift_end()


def _resolve_breaks(day: date, *, published_only: bool) -> tuple:
    from .schedule_store import Break
    ch = _custom_hours(day, published_only=published_only)
    if ch and isinstance(ch.get("breaks"), list):
        out = []
        for b in ch["breaks"]:
            if not isinstance(b, dict):
                continue
            try:
                bs = time.fromisoformat(b["start"])
                be = time.fromisoformat(b["end"])
            except (ValueError, KeyError, TypeError):
                continue
            name = str(b.get("name") or "Break")
            out.append(Break(bs, be, name))
        return tuple(out)
    if _use_saturday_default(day, published_only=published_only):
        return _saturday_default().breaks
    return breaks()


def shift_start_for(day: date) -> time:
    """Shift start for `day` (gated): published per-day custom_hours, else the
    Saturday default on a worked Saturday, else the weekday global schedule."""
    return _resolve_start(day, published_only=True)


def shift_end_for(day: date) -> time:
    return _resolve_end(day, published_only=True)


def breaks_for(day: date) -> tuple:
    """Breaks for `day` (gated). A per-day custom_hours `breaks` list — even
    empty (= 'no breaks today') — wins; otherwise the Saturday default on a
    worked Saturday, else the weekday global breaks."""
    return _resolve_breaks(day, published_only=True)


def configured_shift_start_for(day: date) -> time:
    """Ungated twin for the scheduler editor: a per-day override applies even
    on a draft; a Saturday with no override shows the Saturday default."""
    return _resolve_start(day, published_only=False)


def configured_shift_end_for(day: date) -> time:
    return _resolve_end(day, published_only=False)


def configured_breaks_for(day: date) -> tuple:
    return _resolve_breaks(day, published_only=False)


def scheduler_hours_source(day: date, has_per_day_override: bool) -> str:
    """Which hours the scheduler is showing for `day`: 'custom' (a per-day
    override exists), 'saturday_default' (a Saturday with no override), or
    'weekday_default'. Drives the Hours-pill styling + banner."""
    if has_per_day_override:
        return "custom"
    if day.weekday() == SATURDAY:
        return "saturday_default"
    return "weekday_default"


def productive_minutes_for(day: date) -> int:
    """Total productive minutes for `day` (shift duration minus breaks),
    honoring custom_hours."""
    def _mins(t): return t.hour * 60 + t.minute
    s, e = shift_start_for(day), shift_end_for(day)
    total = _mins(e) - _mins(s)
    for b in breaks_for(day):
        total -= _mins(b.end) - _mins(b.start)
    return max(0, total)


def in_shift_on(local_dt: datetime) -> bool:
    """Whether local_dt falls inside the day's shift (and outside breaks).
    Derives the day from local_dt and consults per-day custom_hours. Honors
    published Saturdays via is_workday()."""
    day = local_dt.date()
    if not is_workday(day):
        return False
    t = local_dt.time()
    if t < shift_start_for(day) or t >= shift_end_for(day):
        return False
    for b in breaks_for(day):
        if b.start <= t < b.end:
            return False
    return True


def shift_elapsed_minutes(day: date, now: datetime) -> int:
    """Productive shift minutes elapsed on `day` as of `now` (site-local).
    Honors per-day custom_hours and published Saturdays via is_workday()."""
    if not is_workday(day):
        return 0
    s = shift_start_for(day)
    e = shift_end_for(day)
    start = datetime.combine(day, s, tzinfo=SITE_TZ)
    end = datetime.combine(day, e, tzinfo=SITE_TZ)
    effective = min(now.astimezone(SITE_TZ), end)
    if effective <= start:
        return 0
    total = int((effective - start).total_seconds() // 60)
    for b in breaks_for(day):
        bs_dt = datetime.combine(day, b.start, tzinfo=SITE_TZ)
        be_dt = datetime.combine(day, b.end, tzinfo=SITE_TZ)
        lo = max(bs_dt, start)
        hi = min(be_dt, effective)
        if hi > lo:
            total -= int((hi - lo).total_seconds() // 60)
    return max(0, total)


def productive_minutes_in_window(day: date, start_utc: datetime, end_utc: datetime) -> int:
    """Productive minutes in [start_utc, end_utc] on `day`: the span minus any
    scheduled breaks overlapping the window.

    Person-INDEPENDENT by design — it does NOT subtract an operator's time-off.
    This prorates a station PACE GOAL (what the WC should have produced by now
    if it was running), which must not shrink because one operator took partial
    leave. (Crediting / man-hours use effective_minutes_worked, which DOES net
    out time-off — that's a different question.) Honors per-day custom_hours via
    breaks_for(). Inputs must be tz-aware UTC datetimes.
    """
    if end_utc <= start_utc:
        return 0
    total = int((end_utc - start_utc).total_seconds() // 60)
    for b in breaks_for(day):
        bs = datetime.combine(day, b.start, tzinfo=SITE_TZ).astimezone(UTC)
        be = datetime.combine(day, b.end, tzinfo=SITE_TZ).astimezone(UTC)
        lo = max(bs, start_utc)
        hi = min(be, end_utc)
        if hi > lo:
            total -= int((hi - lo).total_seconds() // 60)
    return max(0, total)
