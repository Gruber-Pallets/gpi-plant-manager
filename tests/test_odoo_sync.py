import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from zira_dashboard import odoo_sync, staffing


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    monkeypatch.setattr(odoo_sync, "ROSTER_PATH", tmp_path / "roster.json")
    monkeypatch.setattr(odoo_sync, "LAST_SYNC_PATH", tmp_path / ".odoo_last_sync")
    monkeypatch.setattr(staffing, "ROSTER_PATH", tmp_path / "roster.json")
    return tmp_path


def test_sync_skips_when_within_ttl(tmp_env, monkeypatch):
    (tmp_env / "roster.json").write_text("[]")
    (tmp_env / ".odoo_last_sync").write_text(datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employees",
                        lambda: (_ for _ in ()).throw(AssertionError("should not call")))
    result = odoo_sync.sync(force=False)
    assert result.refreshed is False
    assert result.ok is True


def test_sync_force_refreshes_even_within_ttl(tmp_env, monkeypatch):
    (tmp_env / ".odoo_last_sync").write_text(datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employees",
                        lambda: [{"id": 1, "name": "Alice", "active": True, "work_email": False}])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skills_for",
                        lambda ids: {1: [{"skill_id": 10, "skill_name": "Repair", "level_id": 103}]})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_columns",
                        lambda: ["Repair", "Dismantler"])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_level_buckets",
                        lambda: {103: 3})
    result = odoo_sync.sync(force=True)
    assert result.refreshed is True
    assert result.employee_count == 1
    assert result.skill_column_count == 2
    roster = json.loads((tmp_env / "roster.json").read_text())
    assert roster[0]["name"] == "Alice"
    assert roster[0]["skills"]["Repair"] == 3
    assert roster[0]["skills"]["Dismantler"] == 0
    assert roster[0]["employee_id"] == 1


def test_sync_preserves_local_reserve_flag(tmp_env, monkeypatch):
    (tmp_env / "roster.json").write_text(json.dumps([
        {"name": "Alice", "active": True, "reserve": True,
         "skills": {"Repair": 0}, "employee_id": 1},
    ]))
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employees",
                        lambda: [{"id": 1, "name": "Alice", "active": True, "work_email": False}])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skills_for",
                        lambda ids: {1: []})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_columns",
                        lambda: ["Repair"])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_level_buckets",
                        lambda: {})
    odoo_sync.sync(force=True)
    roster = json.loads((tmp_env / "roster.json").read_text())
    assert roster[0]["reserve"] is True


def test_sync_returns_error_on_odoo_failure(tmp_env, monkeypatch):
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employees",
                        lambda: (_ for _ in ()).throw(odoo_sync.odoo_client.OdooAuthError("nope")))
    result = odoo_sync.sync(force=True)
    assert result.ok is False
    assert "nope" in (result.error or "")
    assert result.refreshed is False
