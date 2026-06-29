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

from datetime import date, datetime, time

from . import app_settings, forklift_client, forklift_ingest, forklift_store, shift_config


def _day_start_ms(day: date) -> int:
    """Epoch milliseconds at 00:00 plant-local on `day`."""
    start = datetime.combine(day, time.min, tzinfo=shift_config.SITE_TZ)
    return int(start.timestamp() * 1000)


def snapshot_today(client, day: date) -> dict:
    since = _day_start_ms(day)
    items = forklift_client.fetch_completions(since)
    drivers = forklift_client.fetch_drivers()
    id2name = {str(d.get("id")): d.get("name")
               for d in (drivers or []) if d.get("id") is not None}

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

    backups = [d.get("name") for d in (drivers or [])
               if d.get("isOverloadResponder") and d.get("name")]
    app_settings.set_setting("forklift_overload_responders", backups)

    return {"day": day.isoformat(), "calls": calls_row["total_calls"], "drivers": n}
