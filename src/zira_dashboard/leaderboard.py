"""Fetch and aggregate Zira readings into per-station daily totals."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from time import monotonic as _monotonic
from typing import Any

from zira_probe.client import ZiraClient

from ._cache import TTLCache
from .shift_config import SITE_TZ, breaks_for, is_workday, shift_end_for, shift_start_for
from .stations import Station

PAGE_SIZE = 500
MAX_PAGES = 20
WORKING_STATUS = "Working"

# "Transfer rule": if a station hasn't produced a unit for this long, we treat
# the operator as transferred away. From the last unit forward, time stops
# counting against downtime. Production resumes when a new unit is logged.
TRANSFER_GAP = timedelta(minutes=60)


@dataclass(frozen=True)
class StationTotal:
    station: Station
    units: int
    reading_count: int
    truncated: bool
    downtime_minutes: int
    active_minutes: int  # total minutes the station was "on" per the transfer rule
    last_reading_at: datetime | None
    last_status: str | None
    samples: tuple[tuple[datetime, int], ...]  # (event_dt_utc, units) for shift rows with units > 0
    active_intervals: tuple[tuple[datetime, datetime], ...]  # (start_utc, end_utc) per the transfer rule


def day_window_utc(day: date) -> tuple[str, str]:
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    end = datetime.combine(day, time.max, tzinfo=timezone.utc)
    return _iso_z(start), _iso_z(end)


def _iso_z(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _envelope(payload: Any) -> tuple[list[dict], str | None]:
    if isinstance(payload, dict):
        rows = payload.get("data") or []
        cursor = payload.get("lastValue") or None
        return (rows if isinstance(rows, list) else []), cursor
    if isinstance(payload, list):
        return payload, None
    return [], None


def _parse_event_date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _active_intervals(
    samples: list[tuple[datetime, int]],
    end_of_day: datetime,
) -> list[tuple[datetime, datetime]]:
    """Returns the (start, end) windows where the station counts as 'active'.

    A window is active iff:
    - It sits between two consecutive units whose gap is <= TRANSFER_GAP, OR
    - It's the tail after the last unit, capped at min(last + TRANSFER_GAP,
      end_of_day) — once TRANSFER_GAP elapses with no new unit, the station
      is treated as having been transferred away from the last unit time.

    Time before the first unit, time inside long inter-unit gaps, and time
    beyond TRANSFER_GAP after the last unit are all NOT active and are
    excluded from downtime.
    """
    if not samples:
        return []
    intervals: list[tuple[datetime, datetime]] = []
    for i in range(len(samples) - 1):
        a = samples[i][0]
        b = samples[i + 1][0]
        if b - a <= TRANSFER_GAP:
            intervals.append((a, b))
    last_t = samples[-1][0]
    tail_end = min(last_t + TRANSFER_GAP, end_of_day)
    if tail_end > last_t:
        intervals.append((last_t, tail_end))
    return intervals


def _minutes_in_breaks(
    start_utc: datetime,
    end_utc: datetime,
    breaks_by_day: dict[date, Any] | None = None,
) -> float:
    """Sum minutes between [start_utc, end_utc] that fall inside a break
    window on the local date(s) covered. Returns 0 when start >= end.

    Used by `_adjusted_downtime` so the lunch/break portion of a downtime
    event that bleeds into a break (or sits inside an active interval that
    spans a break) is subtracted off — the report should only credit
    productive minutes against downtime.

    `breaks_by_day` is an optional {local_date: breaks} memo shared across
    calls (see `_adjusted_downtime`) so the day's schedule isn't re-resolved
    for every overlap window in a nested loop.
    """
    if end_utc <= start_utc:
        return 0.0
    s_local = start_utc.astimezone(SITE_TZ)
    e_local = end_utc.astimezone(SITE_TZ)
    total = 0.0
    cur_day = s_local.date()
    last_day = e_local.date()
    while cur_day <= last_day:
        if breaks_by_day is not None and cur_day in breaks_by_day:
            day_breaks = breaks_by_day[cur_day]
        else:
            try:
                day_breaks = breaks_for(cur_day) or []
            except Exception:
                day_breaks = []
            if breaks_by_day is not None:
                breaks_by_day[cur_day] = day_breaks
        for b in day_breaks:
            b_start = datetime.combine(cur_day, b.start, tzinfo=SITE_TZ)
            b_end = datetime.combine(cur_day, b.end, tzinfo=SITE_TZ)
            lo = max(b_start, s_local)
            hi = min(b_end, e_local)
            if hi > lo:
                total += (hi - lo).total_seconds() / 60.0
        cur_day += timedelta(days=1)
    return total


def _adjusted_downtime(
    downtime_rows: list[tuple[datetime, int]],
    samples: list[tuple[datetime, int]],
    end_of_day: datetime,
) -> int:
    """Sum downtime that overlaps with active intervals; the rest is dropped.

    Break/lunch time inside the overlap is subtracted — a downtime event that
    starts pre-lunch and spans into lunch (and gets bracketed by an active
    interval that itself spans lunch, because pallets bracketing lunch sit
    within TRANSFER_GAP) would otherwise have its lunch portion incorrectly
    counted as downtime. The leaderboard's `in_shift_on` filter only catches
    events whose START is in a break — it doesn't help when the DURATION
    bleeds into one.
    """
    intervals = _active_intervals(samples, end_of_day)
    if not intervals:
        return 0
    breaks_by_day: dict[date, Any] = {}  # shared memo — one breaks_for() per local date
    total_minutes = 0.0
    for event_end, duration_min in downtime_rows:
        # The meter stamps each reading at the END of the interval it covers,
        # and `duration` is that interval's length in minutes -- so the event
        # spans [event_end - duration, event_end], looking BACKWARD. Projecting
        # it forward instead laid the overnight idle hour (the hourly Stop
        # stamped 07:00 with duration=60, which describes 06:00->07:00) on top
        # of the morning's production, inflating every producing station's
        # downtime by ~an hour at shift start.
        event_start = event_end - timedelta(minutes=duration_min)
        for ai_start, ai_end in intervals:
            overlap_start = max(event_start, ai_start)
            overlap_end = min(event_end, ai_end)
            if overlap_end > overlap_start:
                window_min = (overlap_end - overlap_start).total_seconds() / 60.0
                break_min = _minutes_in_breaks(overlap_start, overlap_end, breaks_by_day)
                total_minutes += max(0.0, window_min - break_min)
    return int(total_minutes)


def fetch_station_day(
    client: ZiraClient,
    station: Station,
    start_iso: str,
    end_iso: str,
    now_utc: datetime | None = None,
) -> StationTotal:
    total = 0
    count = 0
    last_reading_at: datetime | None = None
    last_status: str | None = None
    last_value: str | None = None
    truncated = False
    samples: list[tuple[datetime, int]] = []
    downtime_rows: list[tuple[datetime, int]] = []

    # Resolve the shift window ONCE per local date (a UTC day window spans at
    # most 2 local dates) instead of re-resolving the schedule for every
    # reading row (up to PAGE_SIZE * MAX_PAGES rows, refetched every 30s).
    # Mirrors shift_config.in_shift_on exactly, including the published-
    # Saturday / per-day custom_hours handling inside is_workday & friends.
    shift_by_day: dict[date, tuple[bool, time, time, tuple]] = {}

    def _in_shift_local(local_dt: datetime) -> bool:
        d = local_dt.date()
        info = shift_by_day.get(d)
        if info is None:
            info = (is_workday(d), shift_start_for(d), shift_end_for(d), breaks_for(d))
            shift_by_day[d] = info
        workday, s_start, s_end, s_breaks = info
        if not workday:
            return False
        t = local_dt.time()
        if t < s_start or t >= s_end:
            return False
        for b in s_breaks:
            if b.start <= t < b.end:
                return False
        return True

    for _ in range(MAX_PAGES):
        payload = client.get_readings(
            meter_id=station.meter_id,
            end_time=end_iso,
            start_time=start_iso,
            limit=PAGE_SIZE,
            last_value=last_value,
        )
        rows, cursor = _envelope(payload)
        if not rows:
            break
        if last_reading_at is None:
            first = rows[0]
            last_reading_at = _parse_event_date(first.get("event_date"))
            last_status = first.get("status")
        for r in rows:
            u = r.get("units")
            u_int = int(u) if isinstance(u, (int, float)) else 0
            total += u_int
            status = r.get("status")
            duration = r.get("duration")
            event_dt = _parse_event_date(r.get("event_date"))
            event_local = event_dt.astimezone(SITE_TZ) if event_dt else None
            in_shift_now = event_local is not None and _in_shift_local(event_local)
            if status and status != WORKING_STATUS and isinstance(duration, (int, float)) and in_shift_now:
                downtime_rows.append((event_dt, int(duration)))
            if u_int > 0 and event_dt is not None and in_shift_now:
                samples.append((event_dt, u_int))
        count += len(rows)
        if not cursor or len(rows) < PAGE_SIZE:
            break
        last_value = cursor
    else:
        truncated = True
    samples.sort(key=lambda s: s[0])
    # Cap the active-interval tail at the *shift end* of this day, not the
    # UTC end of day. Otherwise the 60-min grace window can extend past the
    # actual workday and inflate every active_minutes (and every per-WC
    # expected on the bar widgets) by up to 60 min. Also cap at `now` when
    # provided, so the in-progress shift's transfer-rule tail can't bill
    # future minutes as productive.
    end_of_day = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    day_local = end_of_day.astimezone(SITE_TZ).date()
    shift_end_local = datetime.combine(day_local, shift_end_for(day_local), tzinfo=SITE_TZ)
    eval_end = min(shift_end_local.astimezone(timezone.utc), end_of_day)
    if now_utc is not None:
        eval_end = min(eval_end, now_utc)
    intervals = _active_intervals(samples, eval_end)
    active_minutes = int(sum((b - a).total_seconds() / 60.0 for a, b in intervals))
    downtime = _adjusted_downtime(downtime_rows, samples, eval_end)
    return StationTotal(
        station=station,
        units=total,
        reading_count=count,
        truncated=truncated,
        downtime_minutes=downtime,
        active_minutes=active_minutes,
        last_reading_at=last_reading_at,
        last_status=last_status,
        samples=tuple(samples),
        active_intervals=tuple(intervals),
    )


# Module-level pool shared by every leaderboard fetch. Persistent worker
# threads mean the Zira client's thread-local requests.Sessions (and their
# TLS connections) are reused across calls instead of being rebuilt by a
# per-call executor every 30s.
_FETCH_POOL = ThreadPoolExecutor(max_workers=10, thread_name_prefix="zira-fetch")


def leaderboard(
    client: ZiraClient,
    stations: list[Station],
    day: date,
    now_utc: datetime | None = None,
) -> list[StationTotal]:
    start_iso, end_iso = day_window_utc(day)
    results = list(
        _FETCH_POOL.map(
            lambda s: fetch_station_day(client, s, start_iso, end_iso, now_utc),
            stations,
        )
    )
    results.sort(key=lambda r: (-r.units, r.station.name))
    return results


# In-process cache for per-station day totals, keyed by (meter_id, day).
# Per-meter keying lets overlapping station sets (the recycling set, the
# all-metered set, per-WC singletons) share one Zira fetch per meter
# instead of each set refetching the same meters independently. For today,
# TTL is short (30s) so up-to-the-minute production stays visible. For
# past days, results are immutable; we still TTL them to bound cache size,
# but longer (1h). Cache is per-process — a Railway redeploy resets it.
_TODAY_CACHE = TTLCache(ttl_seconds=30.0, max_entries=64)
_PAST_CACHE = TTLCache(ttl_seconds=3600.0, max_entries=256)

# Throttle for persisting TODAY's rows to Postgres: a given (meter, day) is
# upserted at most every _PERSIST_INTERVAL_SECONDS. Today's snapshot only
# needs to survive a redeploy / the midnight rollover — not mirror every
# 30s cache refresh. Past-day persists are not throttled (they happen once,
# on first fetch).
_PERSIST_INTERVAL_SECONDS = 300.0
_LAST_PERSIST: dict[tuple[str, str], float] = {}  # (meter_id, day_iso) -> monotonic ts


def _persist_day(totals: list[StationTotal], day: date, is_today: bool) -> None:
    """Persist freshly fetched rows — past (so next time we don't re-pay
    the Zira round-trip) AND today (so a Railway redeploy or the midnight
    day-rollover doesn't lose the snapshot). save_day is idempotent
    (ON CONFLICT DO UPDATE), so the most recent today-fetch becomes the
    eventual past-day record without any extra orchestration."""
    day_key = day.isoformat()
    now_mono = _monotonic()
    if is_today:
        totals = [
            t for t in totals
            if now_mono - _LAST_PERSIST.get((t.station.meter_id, day_key), float("-inf"))
            >= _PERSIST_INTERVAL_SECONDS
        ]
        if not totals:
            return
    try:
        from . import _zira_persist
        _zira_persist.save_day(totals, day)
    except Exception:
        return
    if is_today:
        for t in totals:
            _LAST_PERSIST[(t.station.meter_id, day_key)] = now_mono
        # Prune entries from previous days so the map stays bounded.
        for k in [k for k in _LAST_PERSIST if k[1] != day_key]:
            _LAST_PERSIST.pop(k, None)


def cached_leaderboard(
    client: ZiraClient,
    stations: list[Station],
    day: date,
    now_utc: datetime | None = None,
) -> list[StationTotal]:
    """Same contract as `leaderboard()`, but caches per-station results so
    repeated requests within the TTL skip the Zira API round-trip
    (~1 call per station, paginated). Entries are keyed per (meter_id, day),
    so only the missing/expired meters of a request are fetched. For 'today'
    the TTL is 30s; for past days it's 1h. Past-day results are also
    persisted to Postgres so they survive Railway redeploys."""
    today = datetime.now(timezone.utc).date()
    is_today = day == today
    cache = _TODAY_CACHE if is_today else _PAST_CACHE
    day_key = day.isoformat()

    by_meter: dict[str, StationTotal] = {}
    missing: list[Station] = []
    for s in stations:
        if s.meter_id in by_meter or any(m.meter_id == s.meter_id for m in missing):
            continue
        hit = cache.peek((s.meter_id, day_key))
        if hit is not None:
            by_meter[s.meter_id] = hit
        else:
            missing.append(s)

    if missing:
        fetched: list[StationTotal] | None = None
        # For past days, check Postgres first.
        if not is_today:
            try:
                from . import _zira_persist
                fetched = _zira_persist.load_day(missing, day)
            except Exception:
                # If Postgres is unavailable or the table doesn't exist
                # yet, fall through to the API. Don't fail the request.
                fetched = None
        if fetched is None:
            # Cache miss — call Zira for ONLY the missing meters.
            fetched = leaderboard(client, missing, day, now_utc)
            if fetched:
                _persist_day(fetched, day, is_today)
        for r in fetched:
            cache.set((r.station.meter_id, day_key), r)
            by_meter[r.station.meter_id] = r

    out: list[StationTotal] = []
    for s in stations:
        r = by_meter.get(s.meter_id)
        if r is None:
            continue
        # A cached entry may have been fetched via a different call site's
        # Station object (same meter; possibly different category/cell
        # labels). Return it under the *requested* station so callers'
        # name-matching and grouping are unaffected by who warmed the cache.
        if r.station != s:
            r = replace(r, station=s)
        out.append(r)
    out.sort(key=lambda r: (-r.units, r.station.name))
    return out


def station_total_for(
    client: ZiraClient,
    station: Station,
    day: date,
    now_utc: datetime | None = None,
) -> StationTotal | None:
    """StationTotal for a single station via `cached_leaderboard`, or None
    when the fetch fails or the station is absent.

    Shared by the per-WC dashboards and GOAT watch — with per-meter cache
    keying this is a dict hit whenever any other page already fetched the
    meter within the TTL."""
    try:
        results = cached_leaderboard(client, [station], day, now_utc)
    except Exception:
        return None
    for r in results:
        if r.station.name == station.name:
            return r
    return None
