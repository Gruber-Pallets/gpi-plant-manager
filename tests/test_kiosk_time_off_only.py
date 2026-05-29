"""Fixed-wage (salaried) staff are diverted to the time-off flow and
never see punch options. Unit coverage for `_is_time_off_only`, the
classifier that drives that routing across every kiosk punch entry point.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _time_off_enabled(monkeypatch):
    """The diversion only applies when the Time Off feature is live."""
    monkeypatch.setenv("KIOSK_TIME_OFF_ENABLED", "1")


def test_monthly_wage_is_time_off_only():
    from zira_dashboard.routes.kiosk import _is_time_off_only

    # Odoo wage_type 'monthly' == "Fixed Wage".
    assert _is_time_off_only({"wage_type": "monthly"}) is True


def test_hourly_wage_keeps_punch_flow():
    from zira_dashboard.routes.kiosk import _is_time_off_only

    assert _is_time_off_only({"wage_type": "hourly"}) is False


def test_unset_wage_type_keeps_punch_flow():
    # Safe default: only people *explicitly* set to Fixed Wage are
    # diverted, so a mis-tagged hourly worker is never locked out of
    # clocking in.
    from zira_dashboard.routes.kiosk import _is_time_off_only

    assert _is_time_off_only({"wage_type": None}) is False
    assert _is_time_off_only({}) is False
    assert _is_time_off_only(None) is False


def test_feature_flag_off_keeps_dashboard(monkeypatch):
    # With Time Off dark there's no screen to divert to, so even
    # fixed-wage staff fall back to the normal dashboard.
    monkeypatch.delenv("KIOSK_TIME_OFF_ENABLED", raising=False)
    from zira_dashboard.routes.kiosk import _is_time_off_only

    assert _is_time_off_only({"wage_type": "monthly"}) is False
