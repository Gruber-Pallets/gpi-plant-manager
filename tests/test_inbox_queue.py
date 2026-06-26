"""build_snapshot's flat queue: urgency-sorted, empty categories dropped."""
from zira_dashboard import exception_inbox


SECTIONS = [
    {"id": "assignments", "title": "Assignments To Do", "tone": "warn",
     "rows": [{"name": "Saw 1", "priority": "warn", "item_key": "assignment:Saw 1:x"}]},
    {"id": "late", "title": "Late / Absence", "tone": "bad",
     "rows": [
         {"name": "Maria", "priority": "urgent", "item_key": "late:1:d"},
         {"name": "Snoozed Sam", "priority": "muted", "item_key": "late:2:d"},
     ]},
    {"id": "missing_wc", "title": "Missing Work Center", "tone": "bad", "rows": []},
    {"id": "time_off", "title": "Pending Time Off", "tone": "info",
     "rows": [{"name": "Caleb", "priority": "info", "item_key": "time_off:9"}]},
]


def test_queue_orders_by_tier_then_section_order():
    q = exception_inbox._queue_from_sections(SECTIONS)
    assert [r["item_key"] for r in q] == [
        "late:1:d",            # urgent
        "assignment:Saw 1:x",  # warn
        "time_off:9",          # info
        "late:2:d",            # muted (follow-up sinks to the bottom)
    ]


def test_queue_drops_empty_categories_and_tags_rows():
    q = exception_inbox._queue_from_sections(SECTIONS)
    assert all(r["section_id"] != "missing_wc" for r in q)
    first = q[0]
    assert first["category_label"] == "Late / Absence"
    assert first["tone"] == "bad"
    assert first["section_id"] == "late"


import os
import pytest


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_build_snapshot_includes_queue_and_item_keys():
    from zira_dashboard import db
    db.bootstrap_schema()
    snap = exception_inbox.build_snapshot()
    assert "queue" in snap and isinstance(snap["queue"], list)
    for section in snap["sections"]:
        for row in section.get("rows") or []:
            assert row.get("item_key"), f"row missing item_key in {section['id']}"
    for row in snap["queue"]:
        assert row.get("item_key") and row.get("category_label") and row.get("section_id")
