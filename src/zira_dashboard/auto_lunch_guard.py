"""Persisted Auto-Lunch observation and Exception Inbox alert shaping."""

from __future__ import annotations

import logging
from threading import RLock

from . import auto_lunch_settings, inbox_keys
from .auto_lunch_settings import Settings

_log = logging.getLogger(__name__)
_DETAIL = "Lunch deductions are not being written. Restore Live mode."
_UNSET = object()
_refresh_lock = RLock()
_state_lock = RLock()
_published_alert: object = _UNSET


def observe() -> Settings:
    try:
        return auto_lunch_settings.reconcile_external_change()
    except Exception:
        _log.warning("Auto-Lunch settings reconciliation failed", exc_info=True)
        try:
            return auto_lunch_settings.reload()
        except Exception:
            _log.error("Auto-Lunch settings reload failed", exc_info=True)
            raise


def mode_label(settings: Settings) -> str:
    if not settings.enabled:
        return "Off"
    if settings.observe_only:
        return "Observe only"
    return "Live"


def _alert_for(current: Settings) -> dict | None:
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


def _copy_alert(alert: object) -> dict | None:
    if isinstance(alert, Exception):
        raise alert
    return dict(alert) if isinstance(alert, dict) else None


def refresh() -> dict | None:
    """Observe persisted settings and atomically publish the resulting alert."""
    global _published_alert
    with _refresh_lock:
        try:
            observed_alert = _alert_for(observe())
        except Exception as exc:
            with _state_lock:
                _published_alert = exc
            raise
        with _state_lock:
            _published_alert = observed_alert
            return _copy_alert(_published_alert)


def current_alert() -> dict | None:
    """Return the warmed alert, observing once if this process is still cold."""
    global _published_alert
    with _state_lock:
        if _published_alert is not _UNSET:
            return _copy_alert(_published_alert)

    with _refresh_lock:
        with _state_lock:
            if _published_alert is not _UNSET:
                return _copy_alert(_published_alert)
        observed_alert = _alert_for(observe())
        with _state_lock:
            _published_alert = observed_alert
            return _copy_alert(_published_alert)
