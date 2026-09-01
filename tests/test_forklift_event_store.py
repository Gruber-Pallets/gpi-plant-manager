from datetime import date, datetime, UTC

from zira_dashboard import forklift_event_store
from zira_dashboard._schema import SCHEMA_DDL
from zira_dashboard.forklift_ingest import ForkliftCompletionEvent


def test_schema_has_idempotent_forklift_event_table():
    ddl = " ".join(SCHEMA_DDL.split())
    assert "CREATE TABLE IF NOT EXISTS forklift_completion_events" in ddl
    assert "external_id TEXT PRIMARY KEY" in ddl
    assert "created_at_utc TIMESTAMPTZ NOT NULL" in ddl
    assert "idx_forklift_completion_events_time_driver" in ddl
    assert "CREATE TABLE IF NOT EXISTS forklift_completion_coverage" in ddl
    assert "raw_event_count INTEGER NOT NULL" in ddl


def test_event_upsert_updates_one_external_identity(monkeypatch):
    captured = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(forklift_event_store.db, "cursor", lambda: Cursor())
    monkeypatch.setattr(
        forklift_event_store.db,
        "execute_values",
        lambda cur, sql, rows, template: captured.update(
            sql=sql, rows=rows, template=template
        ),
    )
    event = ForkliftCompletionEvent(
        event_id="c1", driver_id="fk-1", driver_name="Trent",
        created_at_utc=datetime(2026, 6, 26, 14, 30, tzinfo=UTC),
        workstation_name="Prosaw #4", on_time=False, late=True,
        response_ms=120000, handling_ms=300000,
    )

    assert forklift_event_store.upsert_completion_events([event]) == 1
    assert "ON CONFLICT (external_id) DO UPDATE" in captured["sql"]
    assert captured["rows"][0][0] == "c1"


def test_event_range_read_returns_one_updated_external_identity(monkeypatch):
    start = datetime(2026, 6, 26, 0, 0, tzinfo=UTC)
    end = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    changed_at = datetime(2026, 6, 26, 14, 45, tzinfo=UTC)
    captured = {}
    monkeypatch.setattr(
        forklift_event_store.db,
        "query",
        lambda sql, params: captured.update(sql=sql, params=params) or [{
            "external_id": "c1",
            "driver_id": "fk-1",
            "driver_name": "Trent",
            "created_at_utc": changed_at,
            "workstation_name": "New station",
            "on_time": False,
            "late": True,
            "response_ms": 150000,
            "handling_ms": 310000,
        }],
    )

    events = forklift_event_store.completion_events_for_range(start, end)

    assert captured["params"] == (start, end)
    assert "created_at_utc >= %s AND created_at_utc < %s" in captured["sql"]
    assert len(events) == 1
    assert events[0] == ForkliftCompletionEvent(
        event_id="c1", driver_id="fk-1", driver_name="Trent",
        created_at_utc=changed_at, workstation_name="New station",
        on_time=False, late=True, response_ms=150000, handling_ms=310000,
    )


def test_record_completion_coverage_upserts_last_successful_fetch(monkeypatch):
    captured = {}
    day = date(2026, 6, 26)
    through = datetime(2026, 6, 27, 5, 0, tzinfo=UTC)
    monkeypatch.setattr(
        forklift_event_store.db,
        "execute",
        lambda sql, params: captured.update(sql=sql, params=params),
    )

    forklift_event_store.record_completion_coverage(
        day, covered_through_utc=through, raw_event_count=12
    )

    assert "ON CONFLICT (day) DO UPDATE" in captured["sql"]
    assert captured["params"] == (day, through, 12)


def test_completion_coverage_read_distinguishes_missing_from_explicit_zero(monkeypatch):
    day = date(2026, 6, 26)
    through = datetime(2026, 6, 27, 5, 0, tzinfo=UTC)
    monkeypatch.setattr(
        forklift_event_store.db,
        "query",
        lambda sql, params: [
            {
                "day": day,
                "covered_through_utc": through,
                "raw_event_count": 0,
                "successful_at": through,
            }
        ],
    )

    coverage = forklift_event_store.completion_coverage_for_day(day)

    assert coverage == forklift_event_store.ForkliftCompletionCoverage(
        day=day,
        covered_through_utc=through,
        raw_event_count=0,
        successful_at=through,
    )
