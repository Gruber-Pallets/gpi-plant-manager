"""Persisted Auto-Lunch observation and Exception Inbox alert shaping."""

from __future__ import annotations

from dataclasses import dataclass
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


class AutoLunchSourceError(RuntimeError):
    """A fresh reader-facing error for a failed persisted observation."""


@dataclass(frozen=True, slots=True)
class _FailureSnapshot:
    message: str = "Auto-Lunch settings unavailable."


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
    if isinstance(alert, _FailureSnapshot):
        raise AutoLunchSourceError(alert.message) from None
    return dict(alert) if isinstance(alert, dict) else None


def _publish(alert: object) -> None:
    global _published_alert

    with _state_lock:
        _published_alert = alert


def _observe_and_publish() -> dict | None:
    try:
        observed_alert = _alert_for(observe())
    except Exception:
        _publish(_FailureSnapshot())
        raise
    _publish(observed_alert)
    return _copy_alert(observed_alert)


def refresh() -> dict | None:
    """Observe persisted settings and atomically publish the resulting alert."""
    with _refresh_lock:
        return _observe_and_publish()


def current_alert() -> dict | None:
    """Return the warmed alert, observing once if this process is still cold."""
    with _state_lock:
        published = _published_alert
    if published is not _UNSET:
        return _copy_alert(published)

    with _refresh_lock:
        with _state_lock:
            published = _published_alert
        if published is not _UNSET:
            return _copy_alert(published)
        return _observe_and_publish()
