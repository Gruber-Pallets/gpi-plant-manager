import os
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ZIRA_ADMIN_SECRET", "test-secret")
    from zira_dashboard.app import app
    return TestClient(app)


def test_precompute_run_rejects_missing_secret(client):
    r = client.post("/admin/precompute-run")
    assert r.status_code == 401


def test_precompute_run_rejects_wrong_secret(client):
    r = client.post("/admin/precompute-run", headers={"X-Admin-Secret": "nope"})
    assert r.status_code == 401


def test_precompute_run_default_does_yesterday(client, monkeypatch):
    calls = []

    def fake_precompute_day(day, client_):
        calls.append(day)
        return {"day": day.isoformat(), "rows_written": 5}

    monkeypatch.setattr(
        "zira_dashboard.precompute.precompute_day", fake_precompute_day
    )

    r = client.post(
        "/admin/precompute-run",
        headers={"X-Admin-Secret": "test-secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["days_processed"] == 1
    assert body["rows_written"] == 5
    yesterday = date.today() - timedelta(days=1)
    assert calls == [yesterday]


def test_precompute_run_with_range(client, monkeypatch):
    calls = []

    def fake_precompute_day(day, client_):
        calls.append(day)
        return {"day": day.isoformat(), "rows_written": 3}

    monkeypatch.setattr(
        "zira_dashboard.precompute.precompute_day", fake_precompute_day
    )

    r = client.post(
        "/admin/precompute-run?from=2026-05-01&to=2026-05-03",
        headers={"X-Admin-Secret": "test-secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["days_processed"] == 3
    assert body["rows_written"] == 9
    assert calls == [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]


def test_precompute_run_continues_on_per_day_error(client, monkeypatch):
    def fake_precompute_day(day, client_):
        if day == date(2026, 5, 2):
            raise RuntimeError("boom")
        return {"day": day.isoformat(), "rows_written": 1}

    monkeypatch.setattr(
        "zira_dashboard.precompute.precompute_day", fake_precompute_day
    )

    r = client.post(
        "/admin/precompute-run?from=2026-05-01&to=2026-05-03",
        headers={"X-Admin-Secret": "test-secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["days_processed"] == 3
    assert body["rows_written"] == 2
    assert body["errors"] == [{"day": "2026-05-02", "error": "boom"}]
