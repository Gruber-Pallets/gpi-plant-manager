"""Persisted Auto-Lunch observation and Exception Inbox alert shaping."""

from __future__ import annotations

import logging

from . import auto_lunch_settings
from .auto_lunch_settings import Settings

_log = logging.getLogger(__name__)
_DETAIL = "Lunch deductions are not being written. Restore Live mode."


def observe() -> Settings:
    persisted = auto_lunch_settings.reload()
    try:
        return auto_lunch_settings.reconcile_external_change()
    except Exception:
        _log.warning("Auto-Lunch external change audit failed", exc_info=True)
        return persisted


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
    return {
        "name": "Auto-Lunch",
        "label": label,
        "detail": _DETAIL,
        "priority": "urgent",
        "badge": "Timeclock",
        "href": "/settings?section=timeclock#auto-lunch-form",
        "row_key": "auto_lunch:setting",
        "item_key": "auto_lunch:setting",
    }
