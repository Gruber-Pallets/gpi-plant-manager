"""Fail-soft cache refreshes after durable absence-PTO state changes."""

from __future__ import annotations

from datetime import date
import logging

from . import _http_cache, staffing


_log = logging.getLogger(__name__)


def invalidate_for_absence(absence_day: date) -> None:
    """Refresh Staffing and every rendered response bucket without raising.

    Callers invoke this only after their database transition committed. Cache
    trouble must never roll back that durable truth or block later Odoo work.
    """
    try:
        staffing.invalidate_schedule_cache(absence_day)
    except Exception:  # noqa: BLE001 - committed domain state stays successful
        _log.warning("absence PTO schedule cache refresh failed", exc_info=True)
    try:
        _http_cache.invalidate_all_cache()
    except Exception:  # noqa: BLE001 - committed domain state stays successful
        _log.warning("absence PTO response cache refresh failed", exc_info=True)


__all__ = ["invalidate_for_absence"]
