"""Kiosk time-off route tests.

The route surface is HMAC-token-gated like the rest of the kiosk; the
two tests below cover the easy gate-fail case (bogus token → redirect)
and stub a placeholder for the happy-path test once the suite gets a
seeded-person fixture (Task 16 in the plan promises to wire it).
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# Import after conftest sets AUTH_DISABLED
from zira_dashboard.app import app
from zira_dashboard.routes.kiosk import _mint_token


def _token_for(person_id: int) -> str:
    return _mint_token(person_id)


def test_landing_route_redirects_when_token_invalid(monkeypatch):
    monkeypatch.setenv("KIOSK_TIME_OFF_ENABLED", "1")
    client = TestClient(app)
    r = client.get("/kiosk/time-off/bogus.token", follow_redirects=False)
    assert r.status_code in (302, 303, 307)


def test_landing_route_renders_when_token_valid(monkeypatch):
    """Token valid + person exists → 200 with the landing HTML."""
    monkeypatch.setenv("KIOSK_TIME_OFF_ENABLED", "1")
    # Need to seed a person row. If the test DB isn't available, skip.
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("Requires DATABASE_URL")
    # Implementer: insert a test person, then:
    # token = _token_for(<person_id>)
    # client = TestClient(app)
    # r = client.get(f"/kiosk/time-off/{token}")
    # assert r.status_code == 200
    # assert "Request Time Off" in r.text
    pytest.skip("Needs test fixture for seeded person row")


def test_request_shape_picker_redirects_on_bad_token():
    """Bogus token on the shape picker should bounce to /kiosk — same
    HMAC gate as the rest of the kiosk."""
    client = TestClient(app)
    r = client.get("/kiosk/time-off/request/bogus", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
