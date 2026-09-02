from __future__ import annotations

from contextlib import contextmanager

from scripts import reconcile_feedback_task_lifecycle as reconcile


class Cursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []
        self.current = None

    def execute(self, sql, params=None):
        self.calls.append((" ".join(str(sql).split()), params))
        self.current = self.rows.pop(0) if self.rows else None

    def fetchone(self):
        return self.current


def use_cursor(monkeypatch, cursor):
    @contextmanager
    def opened():
        yield cursor

    monkeypatch.setattr(reconcile.db, "cursor", opened)


def test_preview_is_read_only_and_reports_safe_bounded_count(monkeypatch):
    cursor = Cursor([{"eligible": 1}])
    use_cursor(monkeypatch, cursor)

    assert reconcile.run(apply=False) == {"eligible": 1, "queued": 0, "applied": False}

    assert len(cursor.calls) == 1
    assert "LIMIT 100" in cursor.calls[0][0]
    assert not cursor.calls[0][0].startswith("UPDATE")


def test_apply_queues_existing_exact_task_relationships(monkeypatch):
    cursor = Cursor([{"eligible": 1}, {"queued": 1}])
    use_cursor(monkeypatch, cursor)

    assert reconcile.run(apply=True) == {"eligible": 1, "queued": 1, "applied": True}

    update = cursor.calls[1][0]
    assert "FOR UPDATE OF td SKIP LOCKED" in update
    assert "LIMIT 100" in update
    assert "td.odoo_task_id IS NOT NULL" in update
    assert "f.lifecycle_origin = 'local'" in update
    assert "td.state <> 'blocked'" in update
    assert "desired_version = candidates.projection_version" in update
    assert "desired_status = candidates.status" in update
    assert "odoo_task_id" not in update.split(" SET ", 1)[1].split(" FROM ", 1)[0]
