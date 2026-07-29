from contextlib import contextmanager
from datetime import date, datetime, time
from types import SimpleNamespace

from zira_dashboard import (
    db,
    optional_workday,
    saturday_recruiting_store as store,
    staffing,
)
from zira_dashboard.shift_config import SITE_TZ


DAY = date(2026, 7, 25)
NOW = datetime(2026, 7, 20, 12, tzinfo=SITE_TZ)
DEADLINE = datetime(2026, 7, 24, 7, tzinfo=SITE_TZ)


class _Cursor:
    def __init__(self, rows=()):
        self._rows = iter(rows)
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchone(self):
        return next(self._rows)

    def fetchall(self):
        return []


def _cursor_context(monkeypatch, cursor, events):
    @contextmanager
    def context():
        events.append("begin")
        yield cursor
        events.append("commit")

    monkeypatch.setattr(db, "cursor", context)


def test_activation_invalidates_publication_cache_after_commit(monkeypatch):
    events = []
    cursor = _Cursor((None, None))
    bundle = SimpleNamespace(
        recruitment=SimpleNamespace(status="recruiting"),
        openings=(),
        commitments=(),
    )
    _cursor_context(monkeypatch, cursor, events)
    monkeypatch.setattr(
        store,
        "_validate_positions",
        lambda _cur, _counts: {17: object()},
    )
    monkeypatch.setattr(store, "_load_bundle", lambda _cur, _day: bundle)
    monkeypatch.setattr(
        optional_workday,
        "invalidate",
        lambda day: events.append(("invalidate", day)),
    )

    assert (
        store.activate(
            day=DAY,
            shift_start=time(6),
            shift_end=time(12),
            response_deadline=DEADLINE,
            requested_counts={17: 1},
            actor="manager@example.com",
            now=NOW,
        )
        is bundle
    )
    assert events[-2:] == ["commit", ("invalidate", DAY)]


def test_whole_day_cancel_invalidates_schedule_and_publication_after_commit(
    monkeypatch,
):
    events = []
    cursor = _Cursor()
    recruitment = SimpleNamespace(status="published")
    bundle = SimpleNamespace(commitments=())
    _cursor_context(monkeypatch, cursor, events)
    monkeypatch.setattr(store, "_lock_recruitment", lambda _cur, _day: recruitment)
    monkeypatch.setattr(store, "_load_bundle", lambda _cur, _day: bundle)
    monkeypatch.setattr(
        staffing,
        "invalidate_schedule_cache",
        lambda day: events.append(("invalidate-schedule", day)),
    )

    assert store.cancel_recruitment(DAY, "manager@example.com", NOW) == ()
    assert events[-2:] == ["commit", ("invalidate-schedule", DAY)]


def test_publication_invalidates_publication_cache_after_commit(monkeypatch):
    events = []
    cursor = _Cursor(({"day": DAY},))
    closed = SimpleNamespace(recruitment=SimpleNamespace(status="closed"))
    published = SimpleNamespace(recruitment=SimpleNamespace(status="published"))
    bundles = iter((closed, published))
    _cursor_context(monkeypatch, cursor, events)
    monkeypatch.setattr(store, "_load_bundle", lambda _cur, _day: next(bundles))
    monkeypatch.setattr(
        optional_workday,
        "invalidate",
        lambda day: events.append(("invalidate", day)),
    )

    assert store.mark_published(DAY, NOW) is published
    assert events[-2:] == ["commit", ("invalidate", DAY)]
