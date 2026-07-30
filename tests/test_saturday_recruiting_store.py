"""Postgres-backed lifecycle contracts for Saturday recruiting."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from threading import Barrier
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


SATURDAY = date(2026, 7, 25)
NEXT_SATURDAY = date(2026, 8, 1)
HOLIDAY = date(2026, 7, 24)
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=SITE_TZ)
DEADLINE = datetime(2026, 7, 24, 7, 0, tzinfo=SITE_TZ)
NEXT_DEADLINE = datetime(2026, 7, 31, 7, 0, tzinfo=SITE_TZ)
HOLIDAY_DEADLINE = datetime(2026, 7, 23, 7, 0, tzinfo=SITE_TZ)

WC_IDS = (910101, 910102, 910103)
SKILL_IDS = (910101, 910102)
PERSON_IDS = (910101, 910102, 910103, 910104)
PERSON_ID = PERSON_IDS[0]
RECRUITING_DAYS = (SATURDAY, NEXT_SATURDAY)


@pytest.fixture(autouse=True)
def _clean_recruiting_data():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("needs Postgres")
    db.bootstrap_schema()
    db.execute("DELETE FROM saturday_recruitments WHERE day = ANY(%s)", (list(RECRUITING_DAYS),))
    db.execute("DELETE FROM schedule_assignments WHERE day = ANY(%s)", (list(RECRUITING_DAYS),))
    db.execute("DELETE FROM schedules WHERE day = ANY(%s)", (list(RECRUITING_DAYS),))
    db.execute("DELETE FROM work_center_required_skills WHERE wc_id = ANY(%s)", (list(WC_IDS),))
    db.execute("DELETE FROM person_skills WHERE person_id = ANY(%s)", (list(PERSON_IDS),))
    db.execute("DELETE FROM time_off_requests WHERE person_odoo_id = ANY(%s)", (list(PERSON_IDS),))
    db.execute("DELETE FROM skills WHERE id = ANY(%s)", (list(SKILL_IDS),))
    db.execute("DELETE FROM work_centers WHERE id = ANY(%s)", (list(WC_IDS),))
    db.execute("DELETE FROM people WHERE id = ANY(%s)", (list(PERSON_IDS),))
    db.execute(
        "INSERT INTO work_centers (id, name, category) VALUES "
        "(910101, 'Saturday Test Repair', 'Repair'), "
        "(910102, 'Saturday Test Dismantle', 'Dismantler'), "
        "(910103, 'Saturday Test Unqualified', 'Other')"
    )
    db.execute(
        "INSERT INTO skills (id, name, skill_type) VALUES "
        "(910101, 'Saturday Test Repair skill', 'Certification'), "
        "(910102, 'Saturday Test Dismantle skill', 'Certification')"
    )
    db.execute(
        "INSERT INTO work_center_required_skills (wc_id, skill_id) VALUES "
        "(910101, 910101), (910102, 910102)"
    )
    db.execute(
        "INSERT INTO people (id, odoo_id, name, wage_type) VALUES "
        "(910101, 910101, 'Saturday Test Volunteer', 'hourly'), "
        "(910102, 910102, 'Saturday Test Repair', 'hourly'), "
        "(910103, 910103, 'Saturday Test Salaried', 'monthly'), "
        "(910104, 910104, 'Saturday Test Unqualified Person', 'hourly')"
    )
    yield
    db.execute("DELETE FROM saturday_recruitments WHERE day = ANY(%s)", (list(RECRUITING_DAYS),))
    db.execute("DELETE FROM schedule_assignments WHERE day = ANY(%s)", (list(RECRUITING_DAYS),))
    db.execute("DELETE FROM schedules WHERE day = ANY(%s)", (list(RECRUITING_DAYS),))
    db.execute("DELETE FROM work_center_required_skills WHERE wc_id = ANY(%s)", (list(WC_IDS),))
    db.execute("DELETE FROM person_skills WHERE person_id = ANY(%s)", (list(PERSON_IDS),))
    db.execute("DELETE FROM time_off_requests WHERE person_odoo_id = ANY(%s)", (list(PERSON_IDS),))
    db.execute("DELETE FROM skills WHERE id = ANY(%s)", (list(SKILL_IDS),))
    db.execute("DELETE FROM work_centers WHERE id = ANY(%s)", (list(WC_IDS),))
    db.execute("DELETE FROM people WHERE id = ANY(%s)", (list(PERSON_IDS),))


class TestRecruitingStoreWithoutDatabase:
    @pytest.fixture(autouse=True)
    def _clean_recruiting_data(self):
        yield

    @staticmethod
    def _bundle(*, day=HOLIDAY, day_kind="holiday", event_name="Founders Day"):
        recruitment = store.Recruitment(
            day=day,
            status="recruiting",
            shift_start=time(6),
            shift_end=time(12),
            response_deadline=HOLIDAY_DEADLINE,
            day_kind=day_kind,
            event_name=event_name,
            holiday_odoo_id=42 if day_kind == "holiday" else None,
        )
        return store.RecruitmentBundle(
            recruitment,
            (store.sr.Opening(17, "Repair 1", 1, ("Repair",)),),
            (),
        )

    def test_old_recruitment_rows_and_employee_results_default_to_saturday(self):
        recruitment = store.Recruitment(
            SATURDAY,
            "recruiting",
            time(6),
            time(12),
            DEADLINE,
        )

        assert (recruitment.day_kind, recruitment.event_name, recruitment.holiday_odoo_id) == (
            "saturday",
            None,
            None,
        )
        assert (
            store.Offer(SATURDAY, time(6), time(12), DEADLINE, frozenset({17})).day_kind
            == "saturday"
        )
        assert (
            store.HomeBanner(SATURDAY, DEADLINE, 1, "available", time(6), time(12)).day_kind
            == "saturday"
        )
        assert (
            store.CommitmentStatus(SATURDAY, time(6), time(12), DEADLINE, True).day_kind
            == "saturday"
        )

    def test_load_and_serialize_round_trip_holiday_metadata(self):
        class Cursor:
            def __init__(self):
                self.query_number = 0

            def execute(self, _sql, _params=None):
                self.query_number += 1

            def fetchone(self):
                assert self.query_number == 1
                return {
                    "day": HOLIDAY,
                    "day_kind": "holiday",
                    "event_name": "Founders Day",
                    "holiday_odoo_id": 42,
                    "status": "recruiting",
                    "shift_start": time(6),
                    "shift_end": time(12),
                    "response_deadline": HOLIDAY_DEADLINE,
                    "staffing_prepared_at": None,
                }

            def fetchall(self):
                if self.query_number == 2:
                    return [
                        {
                            "wc_id": 17,
                            "wc_name": "Repair 1",
                            "requested_count": 1,
                            "required_skills": ["Repair"],
                        }
                    ]
                if self.query_number == 3:
                    return []
                raise AssertionError(f"unexpected query {self.query_number}")

        bundle = store._load_bundle(Cursor(), HOLIDAY)

        assert bundle is not None
        assert bundle.recruitment.day_kind == "holiday"
        assert bundle.recruitment.event_name == "Founders Day"
        assert bundle.recruitment.holiday_odoo_id == 42
        assert store.serialize_bundle(bundle)["recruitment"] == {
            "day": HOLIDAY.isoformat(),
            "day_kind": "holiday",
            "event_name": "Founders Day",
            "holiday_odoo_id": 42,
            "status": "recruiting",
            "shift_start": "06:00",
            "shift_end": "12:00",
            "response_deadline": HOLIDAY_DEADLINE.isoformat(),
        }

    def test_schedule_mutation_lock_uses_public_recruiting_row_seam(
        self,
        monkeypatch,
    ):
        bundle = self._bundle()

        class Cursor:
            def __init__(self):
                self.statements = []

            def execute(self, sql, params):
                self.statements.append((" ".join(sql.split()), params))

            def fetchone(self):
                return {"day": HOLIDAY}

        cursor = Cursor()
        monkeypatch.setattr(store, "_load_bundle", lambda cur, day: bundle)

        locked = store.lock_for_schedule_mutation(HOLIDAY, cur=cursor)

        assert locked is bundle
        assert cursor.statements == [
            (
                "SELECT pg_advisory_xact_lock(%s::bigint)",
                (HOLIDAY.toordinal(),),
            ),
            (
                "SELECT day FROM saturday_recruitments WHERE day = %s FOR UPDATE",
                (HOLIDAY,),
            ),
        ]

    def test_recruitment_mutations_take_day_lock_before_recruitment_row_lock(self):
        class Cursor:
            def __init__(self):
                self.statements = []

            def execute(self, sql, params):
                self.statements.append((" ".join(sql.split()), params))

            def fetchone(self):
                return {
                    "day": HOLIDAY,
                    "day_kind": "holiday",
                    "event_name": "Founders Day",
                    "holiday_odoo_id": 42,
                    "status": "recruiting",
                    "shift_start": time(6),
                    "shift_end": time(12),
                    "response_deadline": HOLIDAY_DEADLINE,
                    "staffing_prepared_at": None,
                }

        cursor = Cursor()

        store._lock_recruitment(cursor, HOLIDAY)

        assert cursor.statements[:2] == [
            (
                "SELECT pg_advisory_xact_lock(%s::bigint)",
                (HOLIDAY.toordinal(),),
            ),
            (
                "SELECT day, day_kind, event_name, holiday_odoo_id, status, "
                "shift_start, shift_end, response_deadline, staffing_prepared_at "
                "FROM saturday_recruitments WHERE day = %s FOR UPDATE",
                (HOLIDAY,),
            ),
        ]

    def test_employee_offer_and_banner_copy_holiday_metadata(self, monkeypatch):
        bundle = self._bundle()

        class Cursor:
            def execute(self, _sql, _params=None):
                pass

            def fetchall(self):
                return [{"day": HOLIDAY}]

        @contextmanager
        def cursor_context():
            yield Cursor()

        monkeypatch.setattr(db, "cursor", cursor_context)
        monkeypatch.setattr(store, "_load_bundle", lambda _cur, _day: bundle)
        monkeypatch.setattr(
            store,
            "_eligible_wc_ids_for_person",
            lambda _cur, _person_id, _openings, _day: frozenset({17}),
        )
        monkeypatch.setattr(store, "_coverage_with_candidate", lambda *_args: object())

        offer = store.offer_for_person(99, NOW)
        banner = store.home_banner(NOW)

        assert offer is not None
        assert (offer.day_kind, offer.event_name) == ("holiday", "Founders Day")
        assert banner is not None
        assert (banner.day_kind, banner.event_name) == ("holiday", "Founders Day")

    def test_today_banner_copies_holiday_metadata_until_shift_end(self, monkeypatch):
        bundle = self._bundle()

        class Cursor:
            def execute(self, _sql, _params=None):
                pass

            def fetchall(self):
                return [{"day": HOLIDAY}]

        @contextmanager
        def cursor_context():
            yield Cursor()

        monkeypatch.setattr(db, "cursor", cursor_context)
        monkeypatch.setattr(store, "_load_bundle", lambda _cur, _day: bundle)

        banner = store.home_banner(datetime(2026, 7, 24, 8, 0, tzinfo=SITE_TZ))

        assert banner is not None
        assert (banner.phase, banner.day_kind, banner.event_name) == (
            "today",
            "holiday",
            "Founders Day",
        )

    def test_employee_commitment_copies_holiday_metadata(self, monkeypatch):
        class Cursor:
            def execute(self, _sql, _params=None):
                pass

            def fetchone(self):
                return {
                    "day": HOLIDAY,
                    "availability_start": time(7),
                    "availability_end": time(11, 30),
                    "status": "recruiting",
                    "response_deadline": HOLIDAY_DEADLINE,
                    "day_kind": "holiday",
                    "event_name": "Founders Day",
                }

        @contextmanager
        def cursor_context():
            yield Cursor()

        monkeypatch.setattr(db, "cursor", cursor_context)

        commitment = store.commitment_for_person(99, NOW)

        assert commitment is not None
        assert (commitment.day_kind, commitment.event_name) == ("holiday", "Founders Day")

    @pytest.mark.parametrize(
        "mirrored",
        [
            None,
            SimpleNamespace(odoo_id=41, name="Founders Day"),
            SimpleNamespace(odoo_id=42, name="Different Holiday"),
        ],
    )
    def test_holiday_activation_requires_current_matching_mirror(self, monkeypatch, mirrored):
        monkeypatch.setattr(company_holidays, "for_day", lambda _day: mirrored)

        with pytest.raises(store.LifecycleConflict):
            store.activate(
                day=HOLIDAY,
                shift_start=time(6),
                shift_end=time(12),
                response_deadline=HOLIDAY_DEADLINE,
                requested_counts={17: 1},
                actor="manager@example.com",
                now=NOW,
                day_kind="holiday",
                event_name="Founders Day",
                holiday_odoo_id=42,
            )

    def test_holiday_activation_safely_drafts_posted_schedule_and_stores_audit(self, monkeypatch):
        events = []
        statements = []
        posted = staffing.Schedule(
            day=HOLIDAY,
            published=True,
            assignments={"Repair 1": ["Jordan"]},
            notes="Keep this posted note",
            assignment_sources={"Repair 1": {"Jordan": "manual"}},
            saturday_availability_overrides={"Jordan": "off"},
            published_delivery={"version": "posted-v1"},
        )
        expected_snapshot = staffing.snapshot_of(posted)
        bundle = self._bundle()

        class Cursor:
            def execute(self, sql, params=None):
                statements.append((sql, params))

            def fetchone(self):
                return None

        @contextmanager
        def cursor_context():
            events.append("begin")
            yield Cursor()
            events.append("commit")

        monkeypatch.setattr(db, "cursor", cursor_context)
        monkeypatch.setattr(store, "_validate_positions", lambda _cur, _counts: {17: object()})
        monkeypatch.setattr(staffing, "load_schedule_for_update", lambda _day, cur: posted)
        monkeypatch.setattr(store, "_load_bundle", lambda _cur, _day: bundle)
        monkeypatch.setattr(
            company_holidays,
            "for_day",
            lambda _day: SimpleNamespace(odoo_id=42, name="Founders Day"),
        )
        monkeypatch.setattr(
            optional_workday,
            "invalidate",
            lambda day: events.append(("invalidate", day)),
        )

        result = store.activate(
            day=HOLIDAY,
            shift_start=time(6),
            shift_end=time(12),
            response_deadline=HOLIDAY_DEADLINE,
            requested_counts={17: 1},
            actor="manager@example.com",
            now=NOW,
            day_kind="holiday",
            event_name="Founders Day",
            holiday_odoo_id=42,
        )

        assert result is bundle
        schedule_update = next(
            (sql, params)
            for sql, params in statements
            if sql.startswith("UPDATE schedules SET published")
        )
        assert "published_snapshot = %s::jsonb" in schedule_update[0]
        assert "assignment_sources = '{}'::jsonb" in schedule_update[0]
        assert "saturday_availability_overrides = '{}'::jsonb" in schedule_update[0]
        assert schedule_update[1][0] is False
        assert json.loads(schedule_update[1][1]) == expected_snapshot
        assert any(sql.startswith("DELETE FROM schedule_assignments") for sql, _ in statements)
        recruitment_insert = next(
            (sql, params)
            for sql, params in statements
            if sql.startswith("INSERT INTO saturday_recruitments")
        )
        assert "(day, day_kind, event_name, holiday_odoo_id, status" in recruitment_insert[0]
        assert recruitment_insert[1][:4] == (HOLIDAY, "holiday", "Founders Day", 42)
        assert events[-2:] == ["commit", ("invalidate", HOLIDAY)]

    def test_identical_holiday_activation_is_a_schedule_noop(self, monkeypatch):
        bundle = self._bundle()
        statements = []

        class Cursor:
            def execute(self, sql, params=None):
                statements.append((sql, params))

            def fetchone(self):
                return {"day": HOLIDAY}

        @contextmanager
        def cursor_context():
            yield Cursor()

        monkeypatch.setattr(db, "cursor", cursor_context)
        monkeypatch.setattr(store, "_validate_positions", lambda _cur, _counts: {17: object()})
        monkeypatch.setattr(store, "_load_bundle", lambda _cur, _day: bundle)
        monkeypatch.setattr(
            staffing,
            "load_schedule_for_update",
            lambda *_args, **_kwargs: pytest.fail("idempotence must not touch the schedule"),
        )
        monkeypatch.setattr(
            company_holidays,
            "for_day",
            lambda _day: SimpleNamespace(odoo_id=42, name="Founders Day"),
        )

        result = store.activate(
            day=HOLIDAY,
            shift_start=time(6),
            shift_end=time(12),
            response_deadline=HOLIDAY_DEADLINE,
            requested_counts={17: 1},
            actor="manager@example.com",
            now=NOW + timedelta(hours=1),
            day_kind="holiday",
            event_name="Founders Day",
            holiday_odoo_id=42,
        )

        assert result is bundle
        assert not any(
            sql.startswith(("UPDATE schedules", "DELETE FROM schedule_assignments"))
            for sql, _ in statements
        )

    def test_saturday_activation_still_clears_default_only_assignments(self, monkeypatch):
        statements = []
        schedule = staffing.Schedule(
            day=SATURDAY,
            assignments={"Repair 1": ["Jordan"]},
            assignment_sources={"Repair 1": {"Jordan": "default"}},
        )
        bundle = store.RecruitmentBundle(
            store.Recruitment(SATURDAY, "recruiting", time(6), time(12), DEADLINE),
            (store.sr.Opening(17, "Repair 1", 1, ("Repair",)),),
            (),
        )

        class Cursor:
            def execute(self, sql, params=None):
                statements.append((sql, params))

            def fetchone(self):
                return None

        @contextmanager
        def cursor_context():
            yield Cursor()

        monkeypatch.setattr(db, "cursor", cursor_context)
        monkeypatch.setattr(store, "_validate_positions", lambda _cur, _counts: {17: object()})
        monkeypatch.setattr(staffing, "load_schedule_for_update", lambda _day, cur: schedule)
        monkeypatch.setattr(store, "_load_bundle", lambda _cur, _day: bundle)

        assert (
            store.activate(
                day=SATURDAY,
                shift_start=time(6),
                shift_end=time(12),
                response_deadline=DEADLINE,
                requested_counts={17: 1},
                actor="manager@example.com",
                now=NOW,
            )
            is bundle
        )

        assert any(sql.startswith("DELETE FROM schedule_assignments") for sql, _ in statements)
        assert any(
            sql.startswith("UPDATE schedules SET assignment_sources") for sql, _ in statements
        )
        recruitment_insert = next(
            params
            for sql, params in statements
            if sql.startswith("INSERT INTO saturday_recruitments")
        )
        assert recruitment_insert[:4] == (SATURDAY, "saturday", None, None)

    @pytest.mark.parametrize(
        "schedule",
        [
            staffing.Schedule(day=SATURDAY, published=True),
            staffing.Schedule(
                day=SATURDAY,
                assignments={"Repair 1": ["Jordan"]},
                assignment_sources={"Repair 1": {"Jordan": "manual"}},
            ),
        ],
    )
    def test_saturday_activation_still_rejects_unsafe_schedules(self, monkeypatch, schedule):
        statements = []

        class Cursor:
            def execute(self, sql, params=None):
                statements.append((sql, params))

            def fetchone(self):
                return None

        @contextmanager
        def cursor_context():
            yield Cursor()

        monkeypatch.setattr(db, "cursor", cursor_context)
        monkeypatch.setattr(store, "_validate_positions", lambda _cur, _counts: {17: object()})
        monkeypatch.setattr(staffing, "load_schedule_for_update", lambda _day, cur: schedule)

        with pytest.raises(store.LifecycleConflict):
            store.activate(
                day=SATURDAY,
                shift_start=time(6),
                shift_end=time(12),
                response_deadline=DEADLINE,
                requested_counts={17: 1},
                actor="manager@example.com",
                now=NOW,
            )

        assert not any(sql.startswith("INSERT INTO saturday_recruitments") for sql, _ in statements)

    def test_failed_holiday_activation_rolls_back_and_does_not_open_date(self, monkeypatch):
        events = []
        state = {"schedule_published": True, "recruitment_status": None}
        posted = staffing.Schedule(day=HOLIDAY, published=True)

        class Cursor:
            def execute(self, sql, _params=None):
                if sql.startswith("UPDATE schedules SET published"):
                    state["schedule_published"] = False
                if sql.startswith("INSERT INTO saturday_recruitments"):
                    state["recruitment_status"] = "recruiting"
                if sql.startswith("INSERT INTO saturday_recruitment_openings"):
                    raise RuntimeError("simulated insert failure")

            def fetchone(self):
                return None

        @contextmanager
        def cursor_context():
            before = dict(state)
            events.append("begin")
            try:
                yield Cursor()
            except Exception:
                state.clear()
                state.update(before)
                events.append("rollback")
                raise
            else:
                events.append("commit")

        monkeypatch.setattr(db, "cursor", cursor_context)
        monkeypatch.setattr(store, "_validate_positions", lambda _cur, _counts: {17: object()})
        monkeypatch.setattr(staffing, "load_schedule_for_update", lambda _day, cur: posted)
        monkeypatch.setattr(
            company_holidays,
            "for_day",
            lambda _day: SimpleNamespace(odoo_id=42, name="Founders Day"),
        )
        monkeypatch.setattr(
            optional_workday,
            "invalidate",
            lambda day: events.append(("invalidate", day)),
        )

        with pytest.raises(RuntimeError, match="simulated insert failure"):
            store.activate(
                day=HOLIDAY,
                shift_start=time(6),
                shift_end=time(12),
                response_deadline=HOLIDAY_DEADLINE,
                requested_counts={17: 1},
                actor="manager@example.com",
                now=NOW,
                day_kind="holiday",
                event_name="Founders Day",
                holiday_odoo_id=42,
            )

        operational = state["schedule_published"] and state["recruitment_status"] == "published"
        assert operational is False
        assert events == ["begin", "rollback"]

    def test_cancellation_preserves_posted_snapshot_while_clearing_live_schedule(self, monkeypatch):
        statements = []

        class Cursor:
            def execute(self, sql, params=None):
                statements.append((sql, params))

        @contextmanager
        def cursor_context():
            yield Cursor()

        monkeypatch.setattr(db, "cursor", cursor_context)
        monkeypatch.setattr(
            store,
            "_lock_recruitment",
            lambda _cur, _day: SimpleNamespace(status="published"),
        )
        monkeypatch.setattr(
            store,
            "_load_bundle",
            lambda _cur, _day: SimpleNamespace(commitments=()),
        )

        assert store.cancel_recruitment(HOLIDAY, "manager@example.com", NOW) == ()

        schedule_update = next(sql for sql, _ in statements if sql.startswith("UPDATE schedules"))
        assert "published = FALSE" in schedule_update
        assert "published_snapshot" not in schedule_update
        assert "assignment_sources = '{}'::jsonb" in schedule_update
        assert "saturday_availability_overrides = '{}'::jsonb" in schedule_update
        assert any(sql.startswith("DELETE FROM schedule_assignments") for sql, _ in statements)

    def test_whole_cancellation_locks_day_then_recruitment_before_schedule_write(
        self,
        monkeypatch,
    ):
        statements = []

        class Cursor:
            def execute(self, sql, params=None):
                statements.append((" ".join(sql.split()), params))

            def fetchone(self):
                return {
                    "day": HOLIDAY,
                    "day_kind": "holiday",
                    "event_name": "Founders Day",
                    "holiday_odoo_id": 42,
                    "status": "published",
                    "shift_start": time(6),
                    "shift_end": time(12),
                    "response_deadline": HOLIDAY_DEADLINE,
                    "staffing_prepared_at": NOW,
                }

        @contextmanager
        def cursor_context():
            yield Cursor()

        monkeypatch.setattr(db, "cursor", cursor_context)
        monkeypatch.setattr(
            store,
            "_load_bundle",
            lambda _cur, _day: SimpleNamespace(commitments=()),
        )
        monkeypatch.setattr(staffing, "invalidate_schedule_cache", lambda _day: None)

        store.cancel_recruitment(HOLIDAY, "manager@example.com", NOW)

        sql = [statement for statement, _params in statements]
        assert sql[0] == "SELECT pg_advisory_xact_lock(%s::bigint)"
        assert "FROM saturday_recruitments WHERE day = %s FOR UPDATE" in sql[1]
        recruitment_update = next(
            index
            for index, statement in enumerate(sql)
            if statement.startswith("UPDATE saturday_recruitments")
        )
        schedule_update = next(
            index for index, statement in enumerate(sql) if statement.startswith("UPDATE schedules")
        )
        schedule_delete = next(
            index
            for index, statement in enumerate(sql)
            if statement.startswith("DELETE FROM schedule_assignments")
        )
        assert recruitment_update < schedule_update < schedule_delete


def _activate(**changes):
    values = {
        "day": SATURDAY,
        "shift_start": time(6, 0),
        "shift_end": time(12, 0),
        "response_deadline": DEADLINE,
        "requested_counts": {910101: 3, 910102: 2},
        "actor": "manager@gruberpallets.com",
        "now": NOW,
    }
    values.update(changes)
    return store.activate(**values)


def _qualify(person_id, *skill_ids):
    db.execute_many(
        "INSERT INTO person_skills (person_id, skill_id, level) VALUES (%s, %s, 2)",
        [(person_id, skill_id) for skill_id in skill_ids],
    )


def _response(person_id):
    return db.query(
        "SELECT * FROM saturday_work_responses WHERE day = %s AND person_id = %s",
        (SATURDAY, person_id),
    )[0]


def test_available_positions_includes_qualified_rows_and_excludes_unqualified_rows():
    positions = set(store.available_positions())
    assert (
        store.AvailablePosition(910101, "Saturday Test Repair", ("Saturday Test Repair skill",))
        in positions
    )
    assert (
        store.AvailablePosition(
            910102, "Saturday Test Dismantle", ("Saturday Test Dismantle skill",)
        )
        in positions
    )
    assert all(position.wc_id != 910103 for position in positions)


def test_activate_reads_bundle_and_closes_when_deadline_is_due():
    bundle = _activate()
    assert bundle.recruitment.status == "recruiting"
    assert {opening.wc_id: opening.requested_count for opening in bundle.openings} == {
        910101: 3,
        910102: 2,
    }
    assert store.get(SATURDAY) == bundle
    assert store.close_due(DEADLINE) == 1
    assert store.get(SATURDAY).recruitment.status == "closed"


def test_activate_rejects_non_saturday():
    with pytest.raises(store.SaturdayRecruitingError):
        _activate(day=SATURDAY - timedelta(days=1))


def test_activate_rejects_elapsed_deadline():
    with pytest.raises(store.LifecycleConflict):
        _activate(response_deadline=NOW)


def test_activate_rejects_empty_requested_counts():
    with pytest.raises(store.LifecycleConflict):
        _activate(requested_counts={})


def test_activate_rejects_work_center_without_required_skills():
    with pytest.raises(store.LifecycleConflict):
        _activate(requested_counts={910103: 1})


def test_activate_rejects_existing_draft_assignments():
    db.execute("INSERT INTO schedules (day) VALUES (%s)", (SATURDAY,))
    db.execute(
        "INSERT INTO schedule_assignments (day, wc_id, person_id) VALUES (%s, 910101, 910101)",
        (SATURDAY,),
    )
    with pytest.raises(
        store.LifecycleConflict,
        match="Clear existing Saturday assignments before activating recruiting.",
    ):
        _activate()


def test_activate_rejects_already_published_schedule():
    db.execute("INSERT INTO schedules (day, published) VALUES (%s, TRUE)", (SATURDAY,))
    with pytest.raises(store.LifecycleConflict):
        _activate()


def test_repeated_identical_activation_is_idempotent():
    first = _activate()
    activated_at = db.query(
        "SELECT activated_at FROM saturday_recruitments WHERE day = %s", (SATURDAY,)
    )[0]["activated_at"]
    second = _activate(now=NOW + timedelta(hours=1))
    assert second == first
    assert (
        db.query("SELECT activated_at FROM saturday_recruitments WHERE day = %s", (SATURDAY,))[0][
            "activated_at"
        ]
        == activated_at
    )


def test_concurrent_identical_activation_is_idempotent():
    barrier = Barrier(2)

    def activate_together():
        barrier.wait()
        return _activate()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = [
            future.result()
            for future in (
                executor.submit(activate_together),
                executor.submit(activate_together),
            )
        ]

    assert first == second
    assert (
        db.query("SELECT count(*) AS count FROM saturday_recruitments WHERE day = %s", (SATURDAY,))[
            0
        ]["count"]
        == 1
    )


def test_reactivation_with_different_payload_is_rejected():
    _activate()
    with pytest.raises(store.LifecycleConflict):
        _activate(requested_counts={910101: 4, 910102: 2})


def test_update_rejects_positive_openings_that_cannot_match_current_commitments():
    _activate(requested_counts={910101: 2})
    db.execute(
        "INSERT INTO saturday_work_responses "
        "(day, person_id, status, availability_start, availability_end, eligible_wc_ids) "
        "VALUES (%s, 910101, 'committed', '06:00', '12:00', '[910101]'::jsonb)",
        (SATURDAY,),
    )
    with pytest.raises(store.LifecycleConflict):
        store.update_openings(SATURDAY, {910102: 1}, time(6, 0), time(12, 0), None, NOW)


def test_update_rejects_shift_hour_change_after_first_commitment():
    _activate(requested_counts={910101: 2})
    db.execute(
        "INSERT INTO saturday_work_responses "
        "(day, person_id, status, availability_start, availability_end, eligible_wc_ids) "
        "VALUES (%s, 910101, 'committed', '06:00', '12:00', '[910101]'::jsonb)",
        (SATURDAY,),
    )
    with pytest.raises(store.LifecycleConflict):
        store.update_openings(SATURDAY, {910101: 2}, time(6, 30), time(12, 0), None, NOW)


def test_closed_recruitment_can_only_reduce_unfilled_count():
    _activate(requested_counts={910101: 3})
    assert store.close_due(DEADLINE) == 1
    reduced = store.update_openings(SATURDAY, {910101: 2}, time(6, 0), time(12, 0), None, NOW)
    assert reduced.openings[0].requested_count == 2
    with pytest.raises(store.LifecycleConflict):
        store.update_openings(SATURDAY, {910101: 3}, time(6, 0), time(12, 0), None, NOW)
    with pytest.raises(store.LifecycleConflict):
        store.update_openings(SATURDAY, {910101: 2, 910102: 1}, time(6, 0), time(12, 0), None, NOW)


def test_closed_recruitment_allows_shift_change_before_first_commitment():
    _activate(requested_counts={910101: 3})
    assert store.close_due(DEADLINE) == 1
    updated = store.update_openings(SATURDAY, {910101: 3}, time(6, 30), time(12, 0), None, NOW)
    assert updated.recruitment.shift_start == time(6, 30)


def test_commit_rematches_multi_skilled_volunteer_to_preserve_requested_coverage():
    _qualify(910101, 910101, 910102)
    _qualify(910102, 910101)
    _activate(requested_counts={910101: 1, 910102: 1})

    first = store.commit(SATURDAY, 910101, time(6, 0), time(12, 0), NOW)
    second = store.commit(SATURDAY, 910102, time(7, 0), time(11, 30), NOW)

    assert first.status == second.status == "committed"
    coverage = store.sr.match_commitments(
        second.bundle.openings,
        [
            store.sr.Commitment(c.person_id, c.eligible_wc_ids)
            for c in second.bundle.commitments
            if c.status == "committed"
        ],
    )
    assert coverage is not None
    assert coverage.wc_by_person == {910101: 910102, 910102: 910101}


def test_decline_suppresses_future_offer_for_same_saturday():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})

    declined = store.decline(SATURDAY, PERSON_ID, NOW)

    assert declined.status == "declined"
    assert store.offer_for_person(PERSON_ID, NOW) is None


@pytest.mark.parametrize(
    ("earlier_response", "expected_day"),
    [
        # A decline is final for that Saturday — the next one is offered.
        ("declined", NEXT_SATURDAY),
        # A cancellation re-opens the SAME Saturday (ef8a2ee: a mistaken
        # cancel must be recoverable from the kiosk), so the earlier
        # Saturday is offered again ahead of the later one.
        ("cancelled", SATURDAY),
    ],
)
def test_earlier_saturday_response_does_not_suppress_later_offer(earlier_response, expected_day):
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    _activate(
        day=NEXT_SATURDAY,
        response_deadline=NEXT_DEADLINE,
        requested_counts={910101: 1},
    )
    if earlier_response == "declined":
        store.decline(SATURDAY, PERSON_ID, NOW)
    else:
        store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW)
        store.cancel_by_employee(SATURDAY, PERSON_ID, NOW + timedelta(hours=1))

    offer = store.offer_for_person(PERSON_ID, NOW + timedelta(hours=2))

    assert offer is not None
    assert offer.day == expected_day


def test_later_keeps_offer_and_reserves_no_capacity():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})

    later = store.record_later(SATURDAY, PERSON_ID, NOW)

    assert later.status == "later"
    assert [item for item in later.bundle.commitments if item.status == "committed"] == []
    assert store.offer_for_person(PERSON_ID, NOW) is not None


def test_full_day_time_off_has_no_offer():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    db.execute(
        "INSERT INTO time_off_requests "
        "(person_odoo_id, shape, holiday_status_id, date_from, date_to, state) "
        "VALUES (%s, 'full_day', 1, %s, %s, 'validate')",
        (PERSON_ID, SATURDAY, SATURDAY),
    )

    assert store.offer_for_person(PERSON_ID, NOW) is None


def test_salaried_person_has_no_offer():
    _qualify(910103, 910101)
    _activate(requested_counts={910101: 1})

    assert store.offer_for_person(910103, NOW) is None


def test_employee_cancel_before_cutoff_reopens_capacity():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    assert store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW).status == "committed"
    before = store.home_banner(NOW)

    cancelled = store.cancel_by_employee(SATURDAY, PERSON_ID, NOW + timedelta(hours=1))
    after = store.home_banner(NOW + timedelta(hours=1))

    assert cancelled.status == "cancelled"
    assert before is None
    assert after is not None
    assert after.phase == "available"
    assert after.remaining_count == 1
    assert store.offer_for_person(PERSON_ID, NOW + timedelta(hours=1)) == store.Offer(
        SATURDAY, time(6, 0), time(12, 0), DEADLINE, frozenset({910101})
    )


def test_home_banner_becomes_tomorrow_plan_at_the_response_deadline():
    _activate(requested_counts={910101: 1})

    assert store.home_banner(DEADLINE) == store.HomeBanner(
        SATURDAY, DEADLINE, 0, "tomorrow", time(6), time(12)
    )


def test_home_banner_becomes_today_plan_until_the_snapshotted_shift_ends():
    _activate(requested_counts={910101: 1})

    assert store.home_banner(datetime(2026, 7, 25, 11, 59, tzinfo=SITE_TZ)) == store.HomeBanner(
        SATURDAY, DEADLINE, 0, "today", time(6), time(12)
    )
    assert store.home_banner(datetime(2026, 7, 25, 12, 0, tzinfo=SITE_TZ)) is None


def test_home_banner_never_shows_a_cancelled_saturday():
    _activate(requested_counts={910101: 1})
    store.cancel_recruitment(SATURDAY, "scheduler-manager", DEADLINE)

    assert store.home_banner(datetime(2026, 7, 24, 8, tzinfo=SITE_TZ)) is None


def test_cancelled_employee_can_recommit_partial_availability_before_deadline():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    store.commit(SATURDAY, PERSON_ID, time(7, 0), time(11, 30), NOW)
    store.cancel_by_employee(SATURDAY, PERSON_ID, NOW + timedelta(hours=1))

    recommitted = store.commit(
        SATURDAY, PERSON_ID, time(6, 30), time(11, 0), NOW + timedelta(hours=2)
    )

    assert recommitted.status == "committed"
    commitment = next(
        item for item in recommitted.bundle.commitments if item.person_id == PERSON_ID
    )
    assert (commitment.availability_start, commitment.availability_end) == (
        time(6, 30),
        time(11, 0),
    )
    assert (
        _response(PERSON_ID)["availability_start"],
        _response(PERSON_ID)["availability_end"],
    ) == (
        time(6, 30),
        time(11, 0),
    )


def test_employee_cancel_at_or_after_cutoff_is_rejected():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW)

    with pytest.raises(store.RecruitingClosed):
        store.cancel_by_employee(SATURDAY, PERSON_ID, DEADLINE)


def test_commitment_status_keeps_partial_hours_after_cutoff():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    store.commit(SATURDAY, PERSON_ID, time(7, 0), time(11, 30), NOW)

    status = store.commitment_for_person(PERSON_ID, DEADLINE)

    assert status == store.CommitmentStatus(SATURDAY, time(7, 0), time(11, 30), DEADLINE, False)


def test_manager_cancel_after_cutoff_records_actor_and_reason():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW)

    cancelled = store.cancel_by_manager(
        SATURDAY, PERSON_ID, "manager@gruberpallets.com", "Machine maintenance", DEADLINE
    )
    response = _response(PERSON_ID)

    assert cancelled.status == "cancelled"
    assert response["cancelled_by"] == "manager@gruberpallets.com"
    assert response["cancellation_reason"] == "Machine maintenance"
    assert response["cancelled_at"] == DEADLINE


def test_repeated_identical_commit_is_idempotent():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    first = store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW)
    committed_at = _response(PERSON_ID)["committed_at"]

    second = store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW + timedelta(hours=1))

    assert first.status == second.status == "committed"
    assert (
        db.query(
            "SELECT count(*) AS count FROM saturday_work_responses WHERE day = %s AND person_id = %s",
            (SATURDAY, PERSON_ID),
        )[0]["count"]
        == 1
    )
    assert _response(PERSON_ID)["committed_at"] == committed_at


def test_repeated_employee_cancel_is_idempotent():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW)
    first = store.cancel_by_employee(SATURDAY, PERSON_ID, NOW + timedelta(hours=1))
    cancelled_at = _response(PERSON_ID)["cancelled_at"]

    second = store.cancel_by_employee(SATURDAY, PERSON_ID, NOW + timedelta(hours=2))

    assert first.status == second.status == "cancelled"
    assert _response(PERSON_ID)["cancelled_at"] == cancelled_at


def test_stale_decline_cannot_replace_commitment():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW)

    with pytest.raises(store.LifecycleConflict):
        store.decline(SATURDAY, PERSON_ID, NOW + timedelta(minutes=1))

    assert _response(PERSON_ID)["status"] == "committed"


def test_stale_later_cannot_replace_commitment():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW)

    with pytest.raises(store.LifecycleConflict):
        store.record_later(SATURDAY, PERSON_ID, NOW + timedelta(minutes=1))

    assert _response(PERSON_ID)["status"] == "committed"


def test_concurrent_final_slot_allows_exactly_one_commitment():
    _qualify(910101, 910101)
    _qualify(910102, 910101)
    _activate(requested_counts={910101: 1})
    barrier = Barrier(2)

    def commit_together(person_id):
        barrier.wait()
        try:
            return store.commit(SATURDAY, person_id, time(6, 0), time(12, 0), NOW)
        except store.NoCompatibleOpening:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(commit_together, (910101, 910102)))

    assert sum(result is not None for result in results) == 1
    assert (
        db.query(
            "SELECT count(*) AS count FROM saturday_work_responses "
            "WHERE day = %s AND status = 'committed'",
            (SATURDAY,),
        )[0]["count"]
        == 1
    )
