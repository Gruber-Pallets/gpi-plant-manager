"""machine_breakdowns / breakdown_snoozes tables + production_daily.excluded_minutes
(Postgres). Mirrors tests/test_inbox_open_items.py's fixture pattern."""
import os
from datetime import datetime, timezone

import pytest

from zira_dashboard import db

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")


@pytest.fixture(autouse=True)
def _clean():
    db.bootstrap_schema()
    db.execute("DELETE FROM breakdown_snoozes WHERE person_name = 'Test Person'")
    db.execute("DELETE FROM machine_breakdowns WHERE wc_name = 'Test WC'")
    db.execute("DELETE FROM production_daily WHERE wc_name = 'Test WC'")
    yield
    db.execute("DELETE FROM breakdown_snoozes WHERE person_name = 'Test Person'")
    db.execute("DELETE FROM machine_breakdowns WHERE wc_name = 'Test WC'")
    db.execute("DELETE FROM production_daily WHERE wc_name = 'Test WC'")


def test_machine_breakdowns_round_trips():
    now = datetime.now(timezone.utc)
    rows = db.query(
        "INSERT INTO machine_breakdowns (wc_name, day, detected_stop_utc, source) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        ("Test WC", now.date(), now, "auto"),
    )
    incident_id = rows[0]["id"]
    fetched = db.query(
        "SELECT wc_name, source, resolved_at, resolution, resume_utc "
        "FROM machine_breakdowns WHERE id = %s",
        (incident_id,),
    )
    assert fetched[0]["wc_name"] == "Test WC"
    assert fetched[0]["source"] == "auto"
    assert fetched[0]["resolved_at"] is None
    assert fetched[0]["resolution"] is None


def test_breakdown_snoozes_round_trips():
    now = datetime.now(timezone.utc)
    rows = db.query(
        "INSERT INTO machine_breakdowns (wc_name, day, detected_stop_utc, source) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        ("Test WC", now.date(), now, "auto"),
    )
    incident_id = rows[0]["id"]
    db.execute(
        "INSERT INTO breakdown_snoozes (breakdown_id, person_name, until_utc) "
        "VALUES (%s, %s, %s)",
        (incident_id, "Test Person", now),
    )
    fetched = db.query(
        "SELECT person_name FROM breakdown_snoozes WHERE breakdown_id = %s",
        (incident_id,),
    )
    assert fetched[0]["person_name"] == "Test Person"


def test_same_name_breakdown_snoozes_round_trip_by_odoo_identity():
    now = datetime.now(timezone.utc)
    incident_id = db.query(
        "INSERT INTO machine_breakdowns (wc_name, day, detected_stop_utc, source) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        ("Test WC", now.date(), now, "auto"),
    )[0]["id"]
    db.execute(
        "INSERT INTO breakdown_snoozes "
        "(breakdown_id, person_name, employee_odoo_id, until_utc) "
        "VALUES (%s, %s, %s, %s), (%s, %s, %s, %s)",
        (incident_id, "Test Person", 101, now, incident_id, "Test Person", 202, now),
    )

    fetched = db.query(
        "SELECT employee_odoo_id FROM breakdown_snoozes WHERE breakdown_id = %s "
        "ORDER BY employee_odoo_id",
        (incident_id,),
    )

    assert fetched == [{"employee_odoo_id": 101}, {"employee_odoo_id": 202}]


def test_production_daily_has_excluded_minutes_column():
    db.execute(
        "INSERT INTO production_daily (day, emp_id, name, wc_name, units, downtime, "
        "hours, days_worked, excluded_minutes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (day, emp_id, wc_name) DO UPDATE SET excluded_minutes = EXCLUDED.excluded_minutes",
        (datetime.now(timezone.utc).date(), "test-emp", "Test Person", "Test WC",
         10.0, 0.0, 7.0, 1.0, 42.5),
    )
    fetched = db.query(
        "SELECT excluded_minutes FROM production_daily WHERE wc_name = 'Test WC'"
    )
    assert float(fetched[0]["excluded_minutes"]) == 42.5


def test_wc_time_attributions_has_breakdown_id_column():
    db.execute("DELETE FROM wc_time_attributions WHERE wc_name = 'Test WC'")


def test_breakdown_identity_columns_support_same_display_name():
    db.bootstrap_schema()
    columns = db.query(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' "
        "AND table_name IN ('wc_time_attributions', 'breakdown_snoozes') "
        "AND column_name = 'employee_odoo_id' ORDER BY table_name"
    )

    assert columns == [
        {"table_name": "breakdown_snoozes", "column_name": "employee_odoo_id"},
        {"table_name": "wc_time_attributions", "column_name": "employee_odoo_id"},
    ]
    db.execute(
        "INSERT INTO wc_time_attributions (day, wc_name, person_name, start_utc, "
        "source, breakdown_id) VALUES (%s, %s, %s, %s, %s, %s)",
        (datetime.now(timezone.utc).date(), "Test WC", "Test Person",
         datetime.now(timezone.utc), "breakdown", 999),
    )
    fetched = db.query(
        "SELECT breakdown_id FROM wc_time_attributions WHERE wc_name = 'Test WC' "
        "AND person_name = 'Test Person'"
    )
    assert fetched[0]["breakdown_id"] == 999
    db.execute("DELETE FROM wc_time_attributions WHERE wc_name = 'Test WC'")


def test_legacy_breakdown_identity_schema_migrates_in_place():
    from zira_dashboard._schema import SCHEMA_DDL

    class RollBackMigrationFixture(Exception):
        pass

    with pytest.raises(RollBackMigrationFixture):
        with db.cursor() as cur:
            cur.execute("CREATE SCHEMA task12_legacy_migration")
            cur.execute("SET LOCAL search_path TO task12_legacy_migration")
            cur.execute(
                "CREATE TABLE wc_time_attributions ("
                "id BIGSERIAL PRIMARY KEY, day DATE NOT NULL, wc_name TEXT NOT NULL, "
                "person_name TEXT NOT NULL, start_utc TIMESTAMPTZ NOT NULL, "
                "end_utc TIMESTAMPTZ, source TEXT NOT NULL DEFAULT 'manual', "
                "breakdown_id BIGINT, employee_odoo_id INTEGER, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT now()); "
                "CREATE TABLE breakdown_snoozes ("
                "breakdown_id BIGINT NOT NULL, person_name TEXT NOT NULL, "
                "until_utc TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                "PRIMARY KEY (breakdown_id, person_name))"
            )
            cur.execute(
                "INSERT INTO wc_time_attributions "
                "(day, wc_name, person_name, employee_odoo_id, start_utc, "
                "end_utc, source, breakdown_id) VALUES "
                "('2098-08-31', 'Dismantler 2', 'Alex', 101, "
                "'2098-08-31 13:02+00', '2098-08-31 13:40+00', 'breakdown', 7), "
                "('2098-08-31', 'Dismantler 2', 'Alex', 101, "
                "'2098-08-31 13:02+00', '2098-08-31 13:30+00', 'breakdown', 7)"
            )

            cur.execute(SCHEMA_DDL)

            cur.execute(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = 'task12_legacy_migration' "
                "AND table_name IN ('wc_time_attributions', 'breakdown_snoozes') "
                "AND column_name = 'employee_odoo_id' ORDER BY table_name"
            )
            assert cur.fetchall() == [
                {"table_name": "breakdown_snoozes"},
                {"table_name": "wc_time_attributions"},
            ]
            cur.execute(
                "SELECT employee_odoo_id, end_utc FROM wc_time_attributions "
                "WHERE breakdown_id = 7"
            )
            assert cur.fetchall() == [{
                "employee_odoo_id": 101,
                "end_utc": datetime(2098, 8, 31, 13, 30, tzinfo=timezone.utc),
            }]
            cur.execute(
                "INSERT INTO breakdown_snoozes "
                "(breakdown_id, person_name, employee_odoo_id, until_utc) VALUES "
                "(1, 'Alex', 101, now()), (1, 'Alex', 202, now())"
            )
            raise RollBackMigrationFixture


def test_partially_deployed_snooze_schema_dedupes_and_bootstraps_twice():
    from zira_dashboard._schema import SCHEMA_DDL

    class RollBackMigrationFixture(Exception):
        pass

    with pytest.raises(RollBackMigrationFixture):
        with db.cursor() as cur:
            cur.execute("CREATE SCHEMA task12_partial_snooze_migration")
            cur.execute("SET LOCAL search_path TO task12_partial_snooze_migration")
            cur.execute(
                "CREATE TABLE breakdown_snoozes ("
                "breakdown_id BIGINT NOT NULL, person_name TEXT NOT NULL, "
                "employee_odoo_id INTEGER, until_utc TIMESTAMPTZ NOT NULL, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            cur.execute(
                "INSERT INTO breakdown_snoozes "
                "(breakdown_id, person_name, employee_odoo_id, until_utc, created_at) "
                "VALUES "
                "(41, 'Alex Old', 101, '2098-08-31 13:20+00', '2098-08-31 13:00+00'), "
                "(41, 'Alex New', 101, '2098-08-31 13:50+00', '2098-08-31 13:05+00'), "
                "(41, 'Legacy Alex', NULL, '2098-08-31 13:15+00', '2098-08-31 13:00+00'), "
                "(41, 'Legacy Alex', NULL, '2098-08-31 13:45+00', '2098-08-31 13:05+00')"
            )

            # Startup may rerun the whole idempotent bootstrap after a partial
            # deploy. Both passes must preserve one longest snooze per durable
            # effective identity and leave the canonical key installed.
            cur.execute(SCHEMA_DDL)
            cur.execute(SCHEMA_DDL)

            cur.execute(
                "SELECT person_name, employee_odoo_id, until_utc "
                "FROM breakdown_snoozes WHERE breakdown_id = 41 "
                "ORDER BY employee_odoo_id NULLS LAST"
            )
            assert cur.fetchall() == [
                {
                    "person_name": "Alex New",
                    "employee_odoo_id": 101,
                    "until_utc": datetime(2098, 8, 31, 13, 50, tzinfo=timezone.utc),
                },
                {
                    "person_name": "Legacy Alex",
                    "employee_odoo_id": None,
                    "until_utc": datetime(2098, 8, 31, 13, 45, tzinfo=timezone.utc),
                },
            ]
            cur.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'task12_partial_snooze_migration' "
                "AND indexname = 'breakdown_snoozes_operator_identity_idx'"
            )
            assert cur.fetchall() == [
                {"indexname": "breakdown_snoozes_operator_identity_idx"}
            ]
            raise RollBackMigrationFixture


def test_wc_time_attributions_round_trips_breakdown_employee_identity():
    now = datetime.now(timezone.utc)
    db.execute("DELETE FROM wc_time_attributions WHERE wc_name = 'Test WC'")
    db.execute(
        "INSERT INTO wc_time_attributions "
        "(day, wc_name, person_name, employee_odoo_id, start_utc, source, breakdown_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (now.date(), "Test WC", "Test Person", 101, now, "breakdown", 999),
    )

    fetched = db.query(
        "SELECT person_name, employee_odoo_id FROM wc_time_attributions "
        "WHERE wc_name = 'Test WC'"
    )

    assert fetched == [{"person_name": "Test Person", "employee_odoo_id": 101}]
    db.execute("DELETE FROM wc_time_attributions WHERE wc_name = 'Test WC'")
