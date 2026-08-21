"""inbox_log: write/read the Exception Inbox activity log + best-effort wrapper."""
from contextlib import contextmanager
import json
import os
from datetime import date, datetime, UTC

import pytest

from zira_dashboard import db, inbox_log

_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")

KEY = "test:inbox-log:1"


@pytest.fixture(autouse=True)
def _clean():
    if os.environ.get("DATABASE_URL"):
        db.bootstrap_schema()
        db.execute("DELETE FROM inbox_events WHERE item_key = %s", (KEY,))
    yield
    if os.environ.get("DATABASE_URL"):
        db.execute("DELETE FROM inbox_events WHERE item_key = %s", (KEY,))


def test_record_event_with_cursor_returns_real_dict_id_and_serializes_detail():
    class Cursor:
        def execute(self, sql, params=None):
            self.sql = " ".join(sql.split())
            self.params = params

        def fetchone(self):
            return {"id": 41}

    cursor = Cursor()

    event_id = inbox_log.record_event_with_cursor(
        cursor,
        item_kind="breakdown",
        item_key=KEY,
        person_name="Maria",
        category_label="Machine Breakdown",
        action="auto_resolved",
        source="auto",
        detail={"day": date(2026, 8, 20)},
    )

    assert event_id == 41
    assert cursor.sql.startswith("INSERT INTO inbox_events")
    assert cursor.params[0:5] == (
        "breakdown",
        KEY,
        "Maria",
        "Machine Breakdown",
        "auto_resolved",
    )
    assert json.loads(cursor.params[-1]) == {"day": "2026-08-20"}


def test_record_event_delegates_to_cursor_aware_insert(monkeypatch):
    sentinel_cursor = object()
    calls = []

    @contextmanager
    def opened_cursor():
        yield sentinel_cursor

    def insert(cursor, **event):
        calls.append((cursor, event))
        return 42

    monkeypatch.setattr(inbox_log.db, "cursor", opened_cursor)
    monkeypatch.setattr(inbox_log, "record_event_with_cursor", insert)

    event_id = inbox_log.record_event(
        item_kind="missing_wc",
        item_key=KEY,
        person_name="Maria",
        category_label="Missing WC",
        action="assign",
    )

    assert event_id == 42
    assert calls == [
        (
            sentinel_cursor,
            {
                "item_kind": "missing_wc",
                "item_key": KEY,
                "person_name": "Maria",
                "category_label": "Missing WC",
                "action": "assign",
                "outcome": None,
                "before_value": None,
                "after_value": None,
                "reason": None,
                "actor_upn": None,
                "actor_name": None,
                "source": "inbox",
                "reversible": False,
                "detail": None,
            },
        )
    ]


@_db
def test_record_event_round_trips_with_actor():
    eid = inbox_log.record_event(
        item_kind="missing_wc",
        item_key=KEY,
        person_name="Maria",
        category_label="Missing WC",
        action="assign",
        outcome="Assigned to Saw 1",
        after_value="Saw 1",
        actor_upn="dale@gruberpallets.com",
        actor_name="Dale Gruber",
        source="inbox",
        reversible=True,
    )
    assert isinstance(eid, int)
    rows = [r for r in inbox_log.recent_events(days=1) if r["item_key"] == KEY]
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "assign"
    assert r["after_value"] == "Saw 1"
    assert r["actor_upn"] == "dale@gruberpallets.com"
    assert r["reversible"] is True
    assert r["undone_at"] is None


@_db
def test_record_event_allows_null_actor_for_auto_resolved():
    inbox_log.record_event(
        item_kind="late",
        item_key=KEY,
        person_name="Tomas",
        category_label="Late",
        action="auto_resolved",
        outcome="Auto-resolved",
        actor_upn=None,
        actor_name=None,
        source="auto",
    )
    rows = [r for r in inbox_log.recent_events(days=1) if r["item_key"] == KEY]
    assert rows[0]["actor_upn"] is None


@_db
def test_record_event_detail_with_real_datetime_and_date_round_trips():
    """Regression: detail payloads built from real DB rows (e.g. breakdown
    dismiss's wc_attributions.for_day() snapshot) carry actual datetime/date
    objects, not ISO strings. json.dumps can't serialize those natively --
    record_event must not raise (it would be swallowed by log_event_safe's
    blanket except, silently dropping the event and making it non-undoable)."""
    detail = {
        "incident_id": 1,
        "rows": [{
            "day": date(2026, 7, 8),
            "wc_name": "Dismantler 2",
            "person_name": "Juan",
            "start_utc": datetime(2026, 7, 8, 18, 2, tzinfo=UTC),
            "end_utc": None,
        }],
    }
    eid = inbox_log.record_event(
        item_kind="breakdown",
        item_key=KEY,
        person_name=None,
        category_label="Machine Breakdown",
        action="dismiss",
        outcome="Not a breakdown",
        source="inbox",
        reversible=True,
        detail=detail,
    )
    assert isinstance(eid, int)

    ev = inbox_log.get_event(eid)
    assert ev is not None
    got = ev["detail"]
    if isinstance(got, str):
        import json
        got = json.loads(got)
    assert got["incident_id"] == 1
    row = got["rows"][0]
    assert row["wc_name"] == "Dismantler 2"
    assert row["person_name"] == "Juan"
    # datetime/date fall back to str() -- not natively JSON serializable.
    assert row["day"] == str(date(2026, 7, 8))
    assert row["start_utc"] == str(datetime(2026, 7, 8, 18, 2, tzinfo=UTC))
    assert row["end_utc"] is None


def test_log_event_safe_swallows_errors(monkeypatch):
    def boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(inbox_log, "record_event", boom)
    # Best-effort: a logging failure must never raise into the caller.
    result = inbox_log.log_event_safe(
        item_kind="missing_wc",
        item_key="x",
        person_name=None,
        category_label="Missing WC",
        action="assign",
    )
    assert result is None
