"""inbox_log.archive: filtered, newest-first history for the inbox archive."""
import os

import pytest

from zira_dashboard import db, inbox_log

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")

KP = "test:archive:"


@pytest.fixture(autouse=True)
def _clean():
    db.bootstrap_schema()
    db.execute("DELETE FROM inbox_events WHERE item_key LIKE %s", (KP + "%",))
    yield
    db.execute("DELETE FROM inbox_events WHERE item_key LIKE %s", (KP + "%",))


def _seed():
    inbox_log.record_event(item_kind="time_off", item_key=KP + "1", person_name="A",
                           category_label="Time off", action="approve",
                           actor_upn="dale@gruberpallets.com", actor_name="Dale")
    inbox_log.record_event(item_kind="late", item_key=KP + "2", person_name="B",
                           category_label="Late", action="auto_resolved",
                           actor_upn=None, actor_name=None, source="auto")
    inbox_log.record_event(item_kind="missing_wc", item_key=KP + "3", person_name="C",
                           category_label="Missing WC", action="assign",
                           actor_upn="maria@gruberpallets.com", actor_name="Maria")


def test_archive_hides_auto_by_default():
    _seed()
    rows = [r for r in inbox_log.archive(limit=500) if r["item_key"].startswith(KP)]
    keys = {r["item_key"] for r in rows}
    assert KP + "2" not in keys
    assert {KP + "1", KP + "3"} <= keys


def test_archive_include_auto():
    _seed()
    rows = [r for r in inbox_log.archive(include_auto=True, limit=500) if r["item_key"].startswith(KP)]
    assert KP + "2" in {r["item_key"] for r in rows}


def test_archive_filters_by_actor():
    _seed()
    rows = [r for r in inbox_log.archive(actor_upn="maria@gruberpallets.com", limit=500)
            if r["item_key"].startswith(KP)]
    assert {r["item_key"] for r in rows} == {KP + "3"}


from fastapi.testclient import TestClient

from zira_dashboard.app import app

_client = TestClient(app)


def test_archive_endpoint_groups_by_day_and_hides_auto():
    _seed()
    r = _client.get("/api/exceptions/archive")
    assert r.status_code == 200
    body = r.json()
    assert "groups" in body and "next_before" in body
    events = [e for g in body["groups"] for e in g["events"]
              if str(e.get("category_label") or "")]
    seen = {(e["category_label"], e["action"]) for e in events}
    assert ("Time off", "approve") in seen
    assert ("Missing WC", "assign") in seen
    assert all(e["action"] != "auto_resolved" for e in events)
    g0 = body["groups"][0]
    assert g0["label"] and g0["events"][0]["time_label"]


def test_archive_endpoint_include_auto_flag():
    _seed()
    r = _client.get("/api/exceptions/archive?include_auto=true")
    actions = {e["action"] for g in r.json()["groups"] for e in g["events"]}
    assert "auto_resolved" in actions
