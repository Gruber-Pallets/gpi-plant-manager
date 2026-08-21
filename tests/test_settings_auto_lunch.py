"""Auto-Lunch settings panel.

The mode-mapping helper is pure and runs locally. The GET render + POST save
are Postgres-backed (skip without DATABASE_URL; run in CI).
"""
import os
from pathlib import Path

import pytest

from zira_dashboard.routes import settings as settings_routes

db_required = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres")

TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "src" / "zira_dashboard" / "templates" / "settings.html"
)


@pytest.fixture(autouse=True)
def _reset_auto_lunch():
    """Reset the singleton to defaults around each DB-backed test. No-op
    locally (no DATABASE_URL) so the pure tests below still run."""
    if not os.environ.get("DATABASE_URL"):
        yield
        return
    from zira_dashboard import db, auto_lunch_settings as als
    db.bootstrap_schema()
    db.execute("UPDATE auto_lunch_settings SET enabled=FALSE, observe_only=TRUE, "
               "flex_after_hours=5.0, flex_minutes=30 WHERE id=1")
    als.reload()
    yield
    db.execute("UPDATE auto_lunch_settings SET enabled=FALSE, observe_only=TRUE, "
               "flex_after_hours=5.0, flex_minutes=30 WHERE id=1")
    als.reload()


# ---- pure mode mapping (runs locally) ----

def test_mode_flags_off_observe_live():
    assert settings_routes._auto_lunch_mode_flags("off", True, False) == (False, True)
    assert settings_routes._auto_lunch_mode_flags("observe", False, False) == (True, True)
    assert settings_routes._auto_lunch_mode_flags("live", False, True) == (True, False)


def test_mode_flags_unknown_keeps_current():
    assert settings_routes._auto_lunch_mode_flags("", True, False) == (True, False)
    assert settings_routes._auto_lunch_mode_flags(None, False, True) == (False, True)
    assert settings_routes._auto_lunch_mode_flags("bogus", True, True) == (True, True)


def test_auto_lunch_template_has_recent_history_block():
    html = TEMPLATE.read_text()
    assert 'id="auto-lunch-history"' in html
    assert "Recent Auto-Lunch changes" in html


# ---- GET render + POST save (DB-backed, CI) ----

@db_required
def test_get_renders_auto_lunch_panel():
    from fastapi.testclient import TestClient
    from zira_dashboard import auto_lunch_settings as als
    from zira_dashboard.app import app
    als.save(
        als.Settings(True, False, 5.0, 30),
        actor_upn="manager@gruberpallets.com",
        actor_name="Plant Manager",
    )
    client = TestClient(app)
    r = client.get("/settings?section=timeclock")
    assert r.status_code == 200
    assert "Auto-Lunch" in r.text
    assert 'name="mode"' in r.text
    assert "Recent Auto-Lunch changes" in r.text
    assert "Live" in r.text
    assert "Plant Manager" in r.text


@db_required
def test_post_passes_request_actor_to_audited_save(monkeypatch):
    from fastapi.testclient import TestClient
    from zira_dashboard import auto_lunch_settings, inbox_log
    from zira_dashboard.app import app

    captured = {}
    real_save = auto_lunch_settings.save

    def recording_save(updated, **kwargs):
        captured["kwargs"] = kwargs
        return real_save(updated, **kwargs)

    monkeypatch.setattr(
        inbox_log,
        "actor_from",
        lambda _request: ("manager@gruberpallets.com", "Plant Manager"),
    )
    monkeypatch.setattr(auto_lunch_settings, "save", recording_save)

    response = TestClient(app).post(
        "/settings/auto_lunch",
        data={"mode": "live", "flex_after_hours": "5", "flex_minutes": "30"},
        headers={"accept": "application/json"},
    )

    assert response.status_code == 200
    assert captured["kwargs"] == {
        "actor_upn": "manager@gruberpallets.com",
        "actor_name": "Plant Manager",
    }


@db_required
def test_post_live_then_off_round_trips():
    from fastapi.testclient import TestClient
    from zira_dashboard.app import app
    from zira_dashboard import auto_lunch_settings as als
    client = TestClient(app)
    r = client.post("/settings/auto_lunch", data={
        "mode": "live", "flex_after_hours": "6", "flex_minutes": "45",
    }, headers={"accept": "application/json"})
    assert r.status_code == 200
    als.reload()
    s = als.current()
    assert s.enabled is True and s.observe_only is False
    assert s.flex_after_hours == 6.0 and s.flex_minutes == 45

    client.post("/settings/auto_lunch", data={"mode": "off"},
                headers={"accept": "application/json"})
    als.reload()
    assert als.current().enabled is False


@db_required
def test_post_clamps_flex_values():
    from fastapi.testclient import TestClient
    from zira_dashboard.app import app
    from zira_dashboard import auto_lunch_settings as als
    client = TestClient(app)
    client.post("/settings/auto_lunch", data={
        "mode": "observe", "flex_after_hours": "999", "flex_minutes": "-5",
    }, headers={"accept": "application/json"})
    als.reload()
    s = als.current()
    assert s.flex_after_hours == 24.0   # clamped to max
    assert s.flex_minutes == 0          # clamped to min
