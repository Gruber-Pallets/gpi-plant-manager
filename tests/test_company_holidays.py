from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime
import logging
import os

import pytest

from zira_dashboard import company_holidays


def _odoo_row(
    *,
    odoo_id: object = 81,
    name: object = "Black Friday",
    date_from: object = "2026-11-27 06:00:00",
    date_to: object = "2026-11-28 05:59:59",
) -> dict:
    return {
        "id": odoo_id,
        "name": name,
        "date_from": date_from,
        "date_to": date_to,
    }


def _mirror_row(
    *,
    odoo_id: int = 81,
    name: str = "Plant break",
    date_from: date = date(2026, 7, 3),
    date_to: date = date(2026, 7, 5),
) -> dict:
    return {
        "odoo_id": odoo_id,
        "name": name,
        "date_from": date_from,
        "date_to": date_to,
        "odoo_date_from": f"{date_from.isoformat()} 05:00:00",
        "odoo_date_to": f"{date_to.isoformat()} 05:00:00",
    }


def test_normalize_odoo_utc_to_plant_dates():
    holiday = company_holidays.normalize_odoo_row(_odoo_row())

    assert holiday == company_holidays.CompanyHoliday(
        odoo_id=81,
        name="Black Friday",
        date_from=date(2026, 11, 27),
        date_to=date(2026, 11, 27),
        odoo_date_from="2026-11-27 06:00:00",
        odoo_date_to="2026-11-28 05:59:59",
    )


@pytest.mark.parametrize("odoo_id", [None, False, True, 0, -1, "81"])
def test_normalize_rejects_missing_boolean_and_nonpositive_ids(odoo_id):
    with pytest.raises(company_holidays.InvalidHolidayRow):
        company_holidays.normalize_odoo_row(_odoo_row(odoo_id=odoo_id))


@pytest.mark.parametrize("name", [None, False, "", "   "])
def test_normalize_rejects_blank_names(name):
    with pytest.raises(company_holidays.InvalidHolidayRow):
        company_holidays.normalize_odoo_row(_odoo_row(name=name))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("date_from", None),
        ("date_from", False),
        ("date_from", "2026-02-30 05:00:00"),
        ("date_from", "2026-07-03T05:00:00"),
        ("date_to", None),
        ("date_to", "not a datetime"),
    ],
)
def test_normalize_rejects_invalid_datetimes(field, value):
    row = _odoo_row()
    row[field] = value

    with pytest.raises(company_holidays.InvalidHolidayRow):
        company_holidays.normalize_odoo_row(row)


def test_normalize_rejects_end_before_start_even_on_same_local_day():
    with pytest.raises(company_holidays.InvalidHolidayRow):
        company_holidays.normalize_odoo_row(
            _odoo_row(
                date_from="2026-07-03 12:00:00",
                date_to="2026-07-03 11:59:59",
            )
        )


def test_reload_expands_multiday_holiday_and_range_returns_one_compat_row(
    monkeypatch,
):
    monkeypatch.setattr(
        company_holidays.db,
        "query",
        lambda *args, **kwargs: [_mirror_row()],
    )

    loaded = company_holidays.reload()

    assert set(loaded) == {
        date(2026, 7, 3),
        date(2026, 7, 4),
        date(2026, 7, 5),
    }
    assert company_holidays.for_day(date(2026, 7, 4)).odoo_id == 81
    assert company_holidays.for_range(date(2026, 7, 4), date(2026, 7, 6)) == [
        {
            "id": 81,
            "name": "Plant break",
            "date_from": "2026-07-03",
            "date_to": "2026-07-05",
            "calendar_id": False,
        }
    ]


def test_reload_overlap_chooses_lowest_odoo_id_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(
        company_holidays.db,
        "query",
        lambda *args, **kwargs: [
            _mirror_row(
                odoo_id=90,
                name="Higher",
                date_from=date(2026, 7, 4),
                date_to=date(2026, 7, 4),
            ),
            _mirror_row(
                odoo_id=12,
                name="Lower",
                date_from=date(2026, 7, 4),
                date_to=date(2026, 7, 4),
            ),
        ],
    )

    with caplog.at_level(logging.WARNING):
        company_holidays.reload()

    assert company_holidays.for_day(date(2026, 7, 4)).odoo_id == 12
    assert "overlap" in caplog.text.lower()


class _RecordingCursor:
    def __init__(self, events: list[object], fail_holiday_write: bool = False):
        self.events = events
        self.fail_holiday_write = fail_holiday_write

    def execute(self, sql, params=None):
        compact_sql = " ".join(sql.split())
        self.events.append(("execute", compact_sql, params))
        if self.fail_holiday_write and compact_sql.startswith("INSERT INTO company_holidays"):
            raise RuntimeError("mirror write failed")


def _cursor_factory(events: list[object], *, fail_first_holiday_write: bool = False):
    call_count = 0

    @contextmanager
    def fake_cursor():
        nonlocal call_count
        call_count += 1
        events.append(("begin", call_count))
        cursor = _RecordingCursor(
            events,
            fail_holiday_write=fail_first_holiday_write and call_count == 1,
        )
        try:
            yield cursor
        except Exception:
            events.append(("rollback", call_count))
            raise
        else:
            events.append(("commit", call_count))

    return fake_cursor


def test_refresh_replaces_set_then_reloads_and_invalidates_after_commit(
    monkeypatch,
):
    events: list[object] = []
    monkeypatch.setattr(company_holidays.db, "cursor", _cursor_factory(events))
    monkeypatch.setattr(
        company_holidays,
        "reload",
        lambda: events.append("reload") or {},
    )
    monkeypatch.setattr(
        company_holidays.staffing,
        "invalidate_all_schedule_caches",
        lambda: events.append("staffing invalidate"),
    )
    monkeypatch.setattr(
        company_holidays._http_cache,
        "invalidate_all_cache",
        lambda: events.append("http invalidate"),
    )
    now = datetime(2026, 7, 29, 15, 30, tzinfo=UTC)

    count = company_holidays.refresh(
        fetcher=lambda: [
            _odoo_row(),
            _odoo_row(
                odoo_id=82,
                name="Christmas",
                date_from="2026-12-25 06:00:00",
                date_to="2026-12-26 05:59:59",
            ),
        ],
        now=now,
    )

    assert count == 2
    statements = [event for event in events if isinstance(event, tuple) and event[0] == "execute"]
    assert sum("INSERT INTO company_holidays " in event[1] for event in statements) == 2
    assert any("DELETE FROM company_holidays" in event[1] for event in statements)
    assert any(
        "INSERT INTO company_holiday_sync_state" in event[1] and event[2] == (now, now)
        for event in statements
    )
    assert events.index(("commit", 1)) < events.index("reload")
    assert events[-3:] == ["reload", "staffing invalidate", "http invalidate"]


def test_valid_empty_refresh_clears_mirror_and_marks_success(monkeypatch):
    events: list[object] = []
    monkeypatch.setattr(company_holidays.db, "cursor", _cursor_factory(events))
    monkeypatch.setattr(company_holidays, "reload", lambda: {})
    monkeypatch.setattr(
        company_holidays.staffing,
        "invalidate_all_schedule_caches",
        lambda: None,
    )
    monkeypatch.setattr(company_holidays._http_cache, "invalidate_all_cache", lambda: None)

    assert company_holidays.refresh(fetcher=lambda: []) == 0

    statements = [event for event in events if isinstance(event, tuple) and event[0] == "execute"]
    assert not any("INSERT INTO company_holidays " in event[1] for event in statements)
    assert any(event[1] == "DELETE FROM company_holidays" for event in statements)
    assert any("INSERT INTO company_holiday_sync_state" in event[1] for event in statements)


@pytest.mark.parametrize(
    "fetcher",
    [
        lambda: (_ for _ in ()).throw(RuntimeError("Odoo unavailable")),
        lambda: [_odoo_row(), _odoo_row(odoo_id=False)],
    ],
)
def test_fetch_and_normalization_failure_preserve_mirror_and_record_error(monkeypatch, fetcher):
    events: list[object] = []
    monkeypatch.setattr(company_holidays.db, "cursor", _cursor_factory(events))
    monkeypatch.setattr(
        company_holidays,
        "reload",
        lambda: pytest.fail("failed refresh must not reload"),
    )

    with pytest.raises((RuntimeError, company_holidays.InvalidHolidayRow)):
        company_holidays.refresh(fetcher=fetcher)

    statements = [event for event in events if isinstance(event, tuple) and event[0] == "execute"]
    assert not any("company_holidays" in event[1] for event in statements)
    assert len(statements) == 1
    assert "INSERT INTO company_holiday_sync_state" in statements[0][1]
    assert "last_success_at" not in statements[0][1]


def test_database_failure_records_bounded_error_without_reloading(monkeypatch):
    events: list[object] = []
    monkeypatch.setattr(
        company_holidays.db,
        "cursor",
        _cursor_factory(events, fail_first_holiday_write=True),
    )
    monkeypatch.setattr(
        company_holidays,
        "reload",
        lambda: pytest.fail("failed refresh must not reload"),
    )
    with pytest.raises(RuntimeError, match="mirror write failed"):
        company_holidays.refresh(fetcher=lambda: [_odoo_row()])

    failure_statements = [
        event
        for event in events
        if isinstance(event, tuple)
        and event[0] == "execute"
        and "company_holiday_sync_state" in event[1]
    ]
    assert len(failure_statements) == 1
    assert failure_statements[0][2][1] == "mirror write failed"
    assert len(failure_statements[0][2][1]) <= 500


def test_failure_message_is_truncated_to_500_characters(monkeypatch):
    events: list[object] = []
    monkeypatch.setattr(company_holidays.db, "cursor", _cursor_factory(events))
    message = "z" * 800

    with pytest.raises(RuntimeError):
        company_holidays.refresh(fetcher=lambda: (_ for _ in ()).throw(RuntimeError(message)))

    statement = next(
        event for event in events if isinstance(event, tuple) and event[0] == "execute"
    )
    assert statement[2][1] == message[:500]


def test_sync_health_distinguishes_never_synced_from_synced_empty(monkeypatch):
    monkeypatch.setattr(company_holidays.db, "query", lambda *args: [])
    assert company_holidays.sync_health() == company_holidays.HolidaySyncHealth(
        last_success_at=None,
        last_attempt_at=None,
        last_error=None,
    )
    assert company_holidays.has_synced() is False

    success_at = datetime(2026, 7, 29, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(
        company_holidays.db,
        "query",
        lambda *args: [
            {
                "last_success_at": success_at,
                "last_attempt_at": success_at,
                "last_error": None,
            }
        ],
    )
    assert company_holidays.has_synced() is True
    assert company_holidays.sync_health().last_success_at == success_at


needs_postgres = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")


@needs_postgres
def test_refresh_replaces_persisted_mirror_and_valid_empty_clears_it():
    from zira_dashboard import db

    db.bootstrap_schema()
    with db.cursor() as cur:
        cur.execute("DELETE FROM company_holidays")
        cur.execute("DELETE FROM company_holiday_sync_state")
    try:
        assert company_holidays.refresh(fetcher=lambda: [_odoo_row()]) == 1
        assert db.query("SELECT odoo_id FROM company_holidays ORDER BY odoo_id") == [
            {"odoo_id": 81}
        ]
        assert company_holidays.has_synced() is True

        assert company_holidays.refresh(fetcher=lambda: []) == 0
        assert db.query("SELECT odoo_id FROM company_holidays") == []
        assert company_holidays.has_synced() is True
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM company_holidays")
            cur.execute("DELETE FROM company_holiday_sync_state")
        company_holidays.reload()


@needs_postgres
def test_failed_refresh_preserves_last_good_persisted_mirror_and_bounds_error():
    from zira_dashboard import db

    db.bootstrap_schema()
    with db.cursor() as cur:
        cur.execute("DELETE FROM company_holidays")
        cur.execute("DELETE FROM company_holiday_sync_state")
    try:
        company_holidays.refresh(fetcher=lambda: [_odoo_row()])
        first_success = company_holidays.sync_health().last_success_at
        message = "broken " + ("x" * 700)

        with pytest.raises(RuntimeError):
            company_holidays.refresh(fetcher=lambda: (_ for _ in ()).throw(RuntimeError(message)))

        assert db.query("SELECT odoo_id FROM company_holidays ORDER BY odoo_id") == [
            {"odoo_id": 81}
        ]
        health = company_holidays.sync_health()
        assert health.last_success_at == first_success
        assert health.last_error == message[:500]
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM company_holidays")
            cur.execute("DELETE FROM company_holiday_sync_state")
        company_holidays.reload()
