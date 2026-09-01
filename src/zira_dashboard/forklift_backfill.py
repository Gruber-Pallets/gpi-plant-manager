"""Backfill the full forklift history from the authenticated external API.

Pulls every completed call (the external completions feed exposes all history,
not just today), aggregates it into the snapshot tables, and UPSERTs. Re-runnable
and idempotent: each (day) and (day, driver) row is overwritten, so re-runs and
overlapping windows never double-count.

Used by scripts/backfill_forklift_history.py for a one-shot historical load.
"""
from __future__ import annotations

import datetime as dt
import logging

from . import (
    app_settings,
    forklift_client,
    forklift_event_store,
    forklift_ingest,
    forklift_store,
    shift_config,
)

_log = logging.getLogger(__name__)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _zero_calls_row(day: dt.date) -> dict:
    return {
        "day": day,
        "total_calls": 0,
        "urgent_calls": 0,
        "overload_count": 0,
        "neglected_count": 0,
        "by_hour": {},
        "by_station": {},
        "by_skill": {},
    }


def backfill_history(client=None, since: int = 0) -> dict:
    """Pull all completions from the external API, aggregate, and UPSERT into
    forklift_calls_daily + forklift_driver_daily. Re-runnable / idempotent.

    `client` is accepted for symmetry with the warmer/snapshot path but unused —
    forklift_client reads its config from env per-call. Returns a
    {days, drivers, calls} summary. A failure is logged and reported, not raised,
    so a missing key or transient API error degrades to "nothing written".
    """
    try:
        # Coverage ends when the request begins, not after local processing:
        # calls arriving during the fetch/write window were not observed.
        fetched_through = _utc_now()
        items = forklift_client.fetch_completions(since)
        drivers = forklift_client.fetch_drivers()
        id2name = {str(d.get("id")): d.get("name")
                   for d in (drivers or []) if d.get("id") is not None}
        events = forklift_ingest.completion_events(items, id2name)
        forklift_ingest.require_complete_event_transform(items, events)

        calls_rows, driver_rows = forklift_ingest.aggregate_completions(
            items, id2name, shift_config.SITE_TZ)

        rows_by_day = {row["day"]: row for row in calls_rows}
        coverage_start_day = None
        if since > 0:
            local_since = dt.datetime.fromtimestamp(
                since / 1000,
                tz=dt.UTC,
            ).astimezone(shift_config.SITE_TZ)
            coverage_start_day = local_since.date()
            if local_since.time() != dt.time.min:
                coverage_start_day += dt.timedelta(days=1)
        elif events:
            coverage_start_day = min(
                event.created_at_utc.astimezone(shift_config.SITE_TZ).date()
                for event in events
            )

        coverage_days: list[dt.date] = []
        coverage_end_day = fetched_through.astimezone(shift_config.SITE_TZ).date()
        if coverage_start_day is None and since == 0:
            # An empty all-history response proves Today is empty through this
            # fetch, but supplies no trustworthy historical inception date.
            coverage_start_day = coverage_end_day
        if coverage_start_day is not None:
            cursor = coverage_start_day
            while cursor <= coverage_end_day:
                rows_by_day.setdefault(cursor, _zero_calls_row(cursor))
                coverage_days.append(cursor)
                cursor += dt.timedelta(days=1)
        complete_calls_rows = [rows_by_day[key] for key in sorted(rows_by_day)]
        total_calls = 0
        for row in complete_calls_rows:
            forklift_store.upsert_calls_daily(row)
            total_calls += row["total_calls"]
        n_drivers = forklift_store.upsert_driver_daily(driver_rows)
        forklift_event_store.upsert_completion_events(events)
        event_counts: dict[dt.date, int] = {}
        for event in events:
            local_day = event.created_at_utc.astimezone(shift_config.SITE_TZ).date()
            event_counts[local_day] = event_counts.get(local_day, 0) + 1
        for covered_day in coverage_days:
            forklift_event_store.record_completion_coverage(
                covered_day,
                covered_through_utc=fetched_through,
                raw_event_count=event_counts.get(covered_day, 0),
            )

        # External /drivers may omit isOverloadResponder — only overwrite the
        # saved backup list when the payload actually carries that flag.
        if any("isOverloadResponder" in d for d in (drivers or []) if isinstance(d, dict)):
            backups = [d.get("name") for d in (drivers or [])
                       if d.get("isOverloadResponder") and d.get("name")]
            app_settings.set_setting("forklift_overload_responders", backups)

        summary = {
            "days": len(complete_calls_rows),
            "drivers": n_drivers,
            "calls": total_calls,
        }
        _log.info("forklift backfill complete: %s", summary)
        return summary
    except Exception as e:  # noqa: BLE001 - never fatal; degrade to no-op
        _log.warning("forklift backfill failed: %s", e)
        return {"days": 0, "drivers": 0, "calls": 0, "error": str(e)}


def diff_day(day_key: str, next_key: str, cum: dict) -> list[dict]:
    """Per-driver metrics for `day_key` = cumulative(day_key) - cumulative(next_key).
    Cumulative counts run from `since` to now, so the older day's cumulative minus
    the next day's cumulative isolates that single day. Clamps negatives at 0."""
    today_c = cum.get(day_key, {})
    next_c = cum.get(next_key, {})
    rows = []
    for did, t in today_c.items():
        n = next_c.get(did, {})
        on_time = max(0, int(t.get("on_time", 0)) - int(n.get("on_time", 0)))
        late = max(0, int(t.get("late", 0)) - int(n.get("late", 0)))
        on_call = max(0, int(t.get("on_call_ms", 0)) - int(n.get("on_call_ms", 0)))
        avail = max(0, int(t.get("available_ms", 0)) - int(n.get("available_ms", 0)))
        util = round(on_call / avail * 100, 2) if avail else 0.0
        rows.append({"driver_id": did, "on_time": on_time, "late": late,
                     "on_call_ms": on_call, "available_ms": avail,
                     "utilization_pct": util})
    return rows


def reconstruct_ontime_history(client=None, days_back: int = 120) -> dict:
    """Fetch one cumulative dashboard per day boundary, difference consecutive
    days, and upsert per-day on-time/util into forklift_driver_daily. Idempotent;
    best-effort (logs + swallows). Returns a small outcome dict."""
    client = client or forklift_client

    today = dt.datetime.now(shift_config.SITE_TZ).date()
    days = [today - dt.timedelta(days=i) for i in range(days_back, -1, -1)]
    boundaries = days + [today + dt.timedelta(days=1)]  # need day+1 for the newest diff

    id_to_name = {str(d.get("id")): d.get("name")
                  for d in (client.fetch_drivers() or [])
                  if d.get("id") is not None}
    cum: dict = {}
    for d in boundaries:
        try:
            ms = int(dt.datetime.combine(d, dt.time.min, tzinfo=shift_config.SITE_TZ).timestamp() * 1000)
            dash = client.fetch_dashboard(since=ms)
            rows = forklift_ingest.driver_metrics_from_dashboard(dash, id_to_name)
            cum[d.isoformat()] = {r["driver_id"]: r for r in rows}
        except Exception as exc:  # noqa: BLE001 - best-effort per boundary
            _log.warning("forklift reconstruct: fetch failed for %s: %s", d, exc)

    total = 0
    for d in days:
        day_rows = diff_day(d.isoformat(), (d + dt.timedelta(days=1)).isoformat(), cum)
        for r in day_rows:
            r["day"] = d
            r["name"] = id_to_name.get(r["driver_id"], r["driver_id"])
        try:
            total += forklift_store.upsert_driver_metrics(day_rows)
        except Exception as exc:  # noqa: BLE001 - best-effort per day
            _log.warning("forklift reconstruct: upsert failed for %s: %s", d, exc)

    out = {"days": len(days), "rows": total}
    _log.warning("forklift reconstruct on-time history -> %s", out)
    return out
