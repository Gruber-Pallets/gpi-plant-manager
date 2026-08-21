"""Persisted Auto-Lunch observation and Exception Inbox alert shaping."""

from __future__ import annotations

import logging

from . import auto_lunch_settings, inbox_keys
from .auto_lunch_settings import Settings

_log = logging.getLogger(__name__)
_DETAIL = "Lunch deductions are not being written. Restore Live mode."


def observe() -> Settings:
    try:
        return auto_lunch_settings.reconcile_external_change()
    except Exception:
        _log.warning("Auto-Lunch settings reconciliation failed", exc_info=True)
        try:
            return auto_lunch_settings.reload()
        except Exception:
            _log.error(
                "Auto-Lunch settings reload failed; using safe defaults",
                exc_info=True,
            )
            return auto_lunch_settings.DEFAULT


def mode_label(settings: Settings) -> str:
    if not settings.enabled:
        return "Off"
    if settings.observe_only:
        return "Observe only"
    return "Live"


def current_alert() -> dict | None:
    current = observe()
    label = mode_label(current)
    if label == "Live":
        return None
    item_key = inbox_keys.auto_lunch_setting()
    return {
        "name": "Auto-Lunch",
        "label": label,
        "detail": _DETAIL,
        "priority": "urgent",
        "badge": "Timeclock",
        "href": "/settings?section=timeclock#auto-lunch-form",
        "row_key": item_key,
        "item_key": item_key,
    }
