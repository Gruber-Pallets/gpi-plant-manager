"""Placeholder tests for the kiosk Time Off dashboard tile.

These tests are stubs from the Task 14 plan. The kiosk dashboard suite
doesn't have route-level fixtures yet (no seeded `people` row, no
mocked person lookup), so writing real assertions here would require
wiring those first. Task 16 in the plan sets up those route tests; once
that lands, the implementer can fill these in and drop the ``skip``s.

Until then the file documents the expected behavior:
  - flag off  → tile absent from the rendered HTML
  - flag on   → tile present with the right href and badge count
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app


def test_time_off_tile_hidden_when_flag_off(monkeypatch):
    monkeypatch.delenv("KIOSK_TIME_OFF_ENABLED", raising=False)
    TestClient(app)  # smoke import
    pytest.skip(
        "Requires kiosk dashboard test fixtures; "
        "implementer to wire after route tests in Task 16"
    )


def test_time_off_tile_shown_when_flag_on(monkeypatch):
    monkeypatch.setenv("KIOSK_TIME_OFF_ENABLED", "1")
    pytest.skip("Requires kiosk dashboard test fixtures")
