from contextlib import contextmanager
from datetime import date, datetime, time
from types import SimpleNamespace

import pytest

from zira_dashboard import (
    company_holidays,
    db,
    optional_workday,
    saturday_recruiting_store as store,
    staffing,
)
from zira_dashboard.shift_config import SITE_TZ


DAY = date(2026, 7, 25)
NOW = datetime(2026, 7, 20, 12, tzinfo=SITE_TZ)
DEADLINE = datetime(2026, 7, 24, 7, tzinfo=SITE_TZ)


@pytest.fixture(autouse=True)
def _clear_optional_workday_cache():
    optional_workday.invalidate_all()
    yield
    optional_workday.invalidate_all()


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


def test_close_due_invalidates_primed_publication_state_after_commit(monkeypatch):
    events = []
    current_status = {"value": "recruiting"}
    publication_calls = []

    class CloseCursor(_Cursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            current_status["value"] = "closed"

        def fetchall(self):
            return [{"day": DAY}]

    cursor = CloseCursor()
    _cursor_context(monkeypatch, cursor, events)
    monkeypatch.setattr(
        company_holidays,
        "for_day",
        lambda day: SimpleNamespace(name="Holiday", odoo_id=42) if day == DAY else None,
    )

    def publication_state(day):
        publication_calls.append(day)
        return store.RecruitmentPublicationState(
            day_kind="holiday",
            holiday_odoo_id=42,
            status=current_status["value"],
        )

    monkeypatch.setattr(store, "publication_state", publication_state)
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda day: staffing.Schedule(day=day, published=True),
    )
    real_invalidate = optional_workday.invalidate

    def invalidate(day):
        events.append(("invalidate", day))
        real_invalidate(day)

    monkeypatch.setattr(optional_workday, "invalidate", invalidate)

    assert optional_workday.state_for_day(DAY).recruiting_status == "recruiting"
    assert store.close_due(DEADLINE) == 1
    assert optional_workday.state_for_day(DAY).recruiting_status == "closed"
    assert publication_calls == [DAY, DAY]
    assert events[-2:] == ["commit", ("invalidate", DAY)]
    assert "RETURNING day" in cursor.statements[0][0]
