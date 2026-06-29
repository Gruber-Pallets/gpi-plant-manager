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

from . import app_settings, forklift_client, forklift_ingest, forklift_store, shift_config

_log = logging.getLogger(__name__)


def backfill_history(client=None, since: int = 0) -> dict:
    """Pull all completions from the external API, aggregate, and UPSERT into
    forklift_calls_daily + forklift_driver_daily. Re-runnable / idempotent.

    `client` is accepted for symmetry with the warmer/snapshot path but unused —
    forklift_client reads its config from env per-call. Returns a
    {days, drivers, calls} summary. A failure is logged and reported, not raised,
    so a missing key or transient API error degrades to "nothing written".
    """
    try:
        items = forklift_client.fetch_completions(since)
        drivers = forklift_client.fetch_drivers()
        id2name = {str(d.get("id")): d.get("name")
                   for d in (drivers or []) if d.get("id") is not None}

        calls_rows, driver_rows = forklift_ingest.aggregate_completions(
            items, id2name, shift_config.SITE_TZ)

        total_calls = 0
        for row in calls_rows:
            forklift_store.upsert_calls_daily(row)
            total_calls += row["total_calls"]
        n_drivers = forklift_store.upsert_driver_daily(driver_rows)

        backups = [d.get("name") for d in (drivers or [])
                   if d.get("isOverloadResponder") and d.get("name")]
        app_settings.set_setting("forklift_overload_responders", backups)

        summary = {"days": len(calls_rows), "drivers": n_drivers, "calls": total_calls}
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
        try:
            total += forklift_store.upsert_driver_metrics(day_rows)
        except Exception as exc:  # noqa: BLE001 - best-effort per day
            _log.warning("forklift reconstruct: upsert failed for %s: %s", d, exc)

    out = {"days": len(days), "rows": total}
    _log.warning("forklift reconstruct on-time history -> %s", out)
    return out
