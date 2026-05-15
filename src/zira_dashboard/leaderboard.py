"""Fetch and aggregate Zira readings into per-station daily totals."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from zira_probe.client import ZiraClient

from ._cache import TTLCache
from .shift_config import SITE_TZ, breaks_for, in_shift_on, shift_end_for
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


def _minutes_in_breaks(start_utc: datetime, end_utc: datetime) -> float:
    """Sum minutes between [start_utc, end_utc] that fall inside a break
    window on the local date(s) covered. Returns 0 when start >= end.

    Used by `_adjusted_downtime` so the lunch/break portion of a downtime
    event that bleeds into a break (or sits inside an active interval that
    spans a break) is subtracted off — the report should only credit
    productive minutes against downtime.
    """
    if end_utc <= start_utc:
        return 0.0
    s_local = start_utc.astimezone(SITE_TZ)
    e_local = end_utc.astimezone(SITE_TZ)
    total = 0.0
    cur_day = s_local.date()
    last_day = e_local.date()
    while cur_day <= last_day:
        try:
            day_breaks = breaks_for(cur_day) or []
        except Exception:
            day_breaks = []
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
    total_minutes = 0.0
    for event_start, duration_min in downtime_rows:
        event_end = event_start + timedelta(minutes=duration_min)
        for ai_start, ai_end in intervals:
            overlap_start = max(event_start, ai_start)
            overlap_end = min(event_end, ai_end)
            if overlap_end > overlap_start:
                window_min = (overlap_end - overlap_start).total_seconds() / 60.0
                break_min = _minutes_in_breaks(overlap_start, overlap_end)
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
            in_shift_now = event_local is not None and in_shift_on(event_local)
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


def leaderboard(
    client: ZiraClient,
    stations: list[Station],
    day: date,
    now_utc: datetime | None = None,
) -> list[StationTotal]:
    start_iso, end_iso = day_window_utc(day)
    with ThreadPoolExecutor(max_workers=min(10, len(stations) or 1)) as pool:
        results = list(
            pool.map(
                lambda s: fetch_station_day(client, s, start_iso, end_iso, now_utc),
                stations,
            )
        )
    results.sort(key=lambda r: (-r.units, r.station.name))
    return results


# In-process cache for full leaderboard results, keyed by
# (sorted meter ids, day, is_today). For today, TTL is short (30s)
# so up-to-the-minute production stays visible. For past days,
# results are immutable; we still TTL them to bound cache size, but
# longer (1h). Cache is per-process — a Railway redeploy resets it.
_TODAY_CACHE = TTLCache(ttl_seconds=30.0, max_entries=16)
_PAST_CACHE = TTLCache(ttl_seconds=3600.0, max_entries=64)


def cached_leaderboard(
    client: ZiraClient,
    stations: list[Station],
    day: date,
    now_utc: datetime | None = None,
) -> list[StationTotal]:
    """Same contract as `leaderboard()`, but caches results so
    repeated requests within the TTL skip the Zira API round-trip
    (~1 call per station, paginated). For 'today' the TTL is 30s;
    for past days it's 1h. Past-day results are also persisted to
    Postgres so they survive Railway redeploys."""
    today = datetime.now(timezone.utc).date()
    is_today = day == today
    key = (tuple(sorted(s.meter_id for s in stations)), day.isoformat(), is_today)
    cache = _TODAY_CACHE if is_today else _PAST_CACHE

    def _fetch_or_load():
        # For past days, check Postgres first.
        if not is_today:
            try:
                from . import _zira_persist
                hit = _zira_persist.load_day(stations, day)
                if hit is not None:
                    return hit
            except Exception:
                # If Postgres is unavailable or the table doesn't exist
                # yet, fall through to the API. Don't fail the request.
                pass
        # Cache miss — call Zira.
        result = leaderboard(client, stations, day, now_utc)
        # Persist results for ANY day with data — past (so next time we
        # don't re-pay the Zira round-trip) AND today (so a Railway
        # redeploy or the midnight day-rollover doesn't lose the
        # snapshot). save_day is idempotent (ON CONFLICT DO UPDATE), so
        # the most recent today-fetch becomes the eventual past-day
        # record without any extra orchestration.
        if result:
            try:
                from . import _zira_persist
                _zira_persist.save_day(result, day)
            except Exception:
                pass
        return result

    return cache.get_or_compute(key, _fetch_or_load)
