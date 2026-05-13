"""Unit tests for wc_dashboard_data helpers.

Pure functions only — these tests don't need a DB and run unconditionally.
"""
from __future__ import annotations


def test_slug_simple():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("Repair 1") == "repair-1"


def test_slug_lowercases():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("REPAIR 1") == "repair-1"


def test_slug_collapses_punctuation():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("Hand Build #1") == "hand-build-1"


def test_slug_strips_leading_trailing_hyphens():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("  Bay 4  ") == "bay-4"
    assert slug_for_wc("--repair-1--") == "repair-1"


def test_slug_collapses_runs_of_hyphens():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("Repair   1") == "repair-1"
    assert slug_for_wc("Hand // Build") == "hand-build"


def test_slug_keeps_digits():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("Trim Saw 12") == "trim-saw-12"


def test_slug_empty_input():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("") == ""
    assert slug_for_wc("   ") == ""
