"""Backfill the full forklift history from the authenticated external API.

Pulls every completed call (the external completions feed exposes all history,
not just today), aggregates it into the snapshot tables, and UPSERTs. Re-runnable
and idempotent: each (day) and (day, driver) row is overwritten, so re-runs and
overlapping windows never double-count.

Used by scripts/backfill_forklift_history.py for a one-shot historical load.
"""
from __future__ import annotations

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
