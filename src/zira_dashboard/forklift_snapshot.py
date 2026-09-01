"""Orchestrate one day's forklift snapshot: fetch -> ingest -> store.

Reads the authenticated external completions feed (full history; we ask only for
calls created since the start of `day` in plant time), aggregates with the same
plant-local clock-hour bucketing as the history backfill, and UPSERTs. This
unifies the today + history paths on one source.

Called by the background warmer (and usable from a backfill script). The
`client` arg is accepted for symmetry with precompute_day but unused — the
forklift_client functions read config from env per-call. When FORKLIFT_API_KEY
is absent, fetch_completions raises ForkliftError, which the warmer swallows ->
no snapshot is written (degrades to "unavailable" rather than 500).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from . import (
    app_settings,
    forklift_client,
    forklift_event_store,
    forklift_ingest,
    forklift_store,
    shift_config,
)


LATE_COMPLETION_WINDOW = timedelta(hours=2)


def day_start_ms(day: date) -> int:
    """Epoch milliseconds at 00:00 plant-local on `day`."""
    start = datetime.combine(day, time.min, tzinfo=shift_config.SITE_TZ)
    return int(start.timestamp() * 1000)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=shift_config.SITE_TZ).astimezone(UTC)
    end = datetime.combine(
        day + timedelta(days=1),
        time.min,
        tzinfo=shift_config.SITE_TZ,
    ).astimezone(UTC)
    return start, end


def snapshot_today(client, day: date) -> dict:
    since = day_start_ms(day)
    # The response can only prove completeness through the instant the fetch
    # began.  Using a later post-write clock would claim unseen calls that
    # arrived while the snapshot was being processed.
    fetched_through = _utc_now()
    items = forklift_client.fetch_completions(since)
    drivers = forklift_client.fetch_drivers()
    id2name = {str(d.get("id")): d.get("name")
               for d in (drivers or []) if d.get("id") is not None}
    events = forklift_ingest.completion_events(items, id2name)
    forklift_ingest.require_complete_event_transform(items, events)

    calls_rows, driver_rows = forklift_ingest.aggregate_completions(
        items, id2name, shift_config.SITE_TZ)
    # Scope to `day` only: `since` is midnight of `day`, but late-night calls can
    # still land on the next plant-local day -> keep just today's buckets.
    calls_rows = [r for r in calls_rows if r["day"] == day]
    driver_rows = [r for r in driver_rows if r["day"] == day]

    calls_row = calls_rows[0] if calls_rows else {
        "day": day, "total_calls": 0, "urgent_calls": 0, "overload_count": 0,
        "neglected_count": 0, "by_hour": {}, "by_station": {}, "by_skill": {},
    }
    forklift_store.upsert_calls_daily(calls_row)
    n = forklift_store.upsert_driver_daily(driver_rows)
    forklift_event_store.upsert_completion_events(events)
    day_events = tuple(
        event
        for event in events
        if event.created_at_utc.astimezone(shift_config.SITE_TZ).date() == day
    )
    day_start, _day_end = _day_bounds_utc(day)
    covered_through = max(day_start, fetched_through)
    forklift_event_store.record_completion_coverage(
        day,
        covered_through_utc=covered_through,
        raw_event_count=len(day_events),
    )

    # External /drivers may omit isOverloadResponder — only overwrite the
    # saved backup list when the payload actually carries that flag.
    if any("isOverloadResponder" in d for d in (drivers or []) if isinstance(d, dict)):
        backups = [d.get("name") for d in (drivers or [])
                   if d.get("isOverloadResponder") and d.get("name")]
        app_settings.set_setting("forklift_overload_responders", backups)

    return {"day": day.isoformat(), "calls": calls_row["total_calls"], "drivers": n}


def day_is_finalized(day: date) -> bool:
    """Whether the source was rechecked after the late-completion window."""
    coverage = forklift_event_store.completion_coverage_for_day(day)
    if coverage is None:
        return False
    _day_start, day_end = _day_bounds_utc(day)
    return coverage.covered_through_utc >= day_end + LATE_COMPLETION_WINDOW
