"""Transactional persistence for optional Saturday-work recruiting."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time

from . import saturday_recruiting as sr


class LifecycleConflict(sr.SaturdayRecruitingError):
    """Raised when a recruiting lifecycle operation is no longer allowed."""


SaturdayRecruitingError = sr.SaturdayRecruitingError
InvalidAvailability = sr.InvalidAvailability


class RecruitingClosed(sr.SaturdayRecruitingError):
    """Raised when an employee response arrives after recruiting has closed."""


class NoCompatibleOpening(sr.SaturdayRecruitingError):
    """Raised when an employee cannot safely fill a remaining opening."""


@dataclass(frozen=True)
class AvailablePosition:
    wc_id: int
    wc_name: str
    required_skills: tuple[str, ...]


@dataclass(frozen=True)
class Recruitment:
    day: date
    status: str
    shift_start: time
    shift_end: time
    response_deadline: datetime


@dataclass(frozen=True)
class StoredCommitment:
    person_id: int
    person_odoo_id: int | None
    person_name: str
    status: str
    availability_start: time | None
    availability_end: time | None
    eligible_wc_ids: frozenset[int]


@dataclass(frozen=True)
class RecruitmentBundle:
    recruitment: Recruitment
    openings: tuple[sr.Opening, ...]
    commitments: tuple[StoredCommitment, ...]


@dataclass(frozen=True)
class Offer:
    day: date
    shift_start: time
    shift_end: time
    response_deadline: datetime
    eligible_wc_ids: frozenset[int]


@dataclass(frozen=True)
class HomeBanner:
    day: date
    response_deadline: datetime
    remaining_count: int


@dataclass(frozen=True)
class CommitmentStatus:
    day: date
    availability_start: time
    availability_end: time
    response_deadline: datetime
    can_employee_cancel: bool


@dataclass(frozen=True)
class DecisionResult:
    status: str
    bundle: RecruitmentBundle


def _on_half_hour(value: time) -> bool:
    return value.minute in (0, 30) and value.second == 0 and value.microsecond == 0


def _validate_shift(shift_start: time, shift_end: time) -> None:
    if not _on_half_hour(shift_start) or not _on_half_hour(shift_end) or shift_end <= shift_start:
        raise LifecycleConflict("Saturday shift hours must use 30-minute increments")


def _normalize_counts(requested_counts: Mapping[int, int]) -> dict[int, int]:
    if not requested_counts:
        raise LifecycleConflict("Choose at least one requested Saturday opening")
    normalized: dict[int, int] = {}
    for raw_wc_id, raw_count in requested_counts.items():
        if type(raw_wc_id) is not int or type(raw_count) is not int or raw_count <= 0:
            raise LifecycleConflict("Requested Saturday opening counts must be positive integers")
        normalized[raw_wc_id] = raw_count
    return normalized


def _row_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise LifecycleConflict("Saturday response deadline must be a datetime")
    if value.tzinfo is None:
        raise LifecycleConflict("Saturday response deadline must include a timezone")
    return value


def _required_positions(cur, wc_ids: tuple[int, ...]) -> dict[int, AvailablePosition]:
    if not wc_ids:
        return {}
    cur.execute(
        "SELECT wc.id AS wc_id, wc.name AS wc_name, "
        "array_agg(s.name ORDER BY s.name) AS required_skills "
        "FROM work_centers wc "
        "JOIN work_center_required_skills wrs ON wrs.wc_id = wc.id "
        "JOIN skills s ON s.id = wrs.skill_id "
        "WHERE wc.id = ANY(%s) "
        "GROUP BY wc.id, wc.name",
        (list(wc_ids),),
    )
    return {
        int(row["wc_id"]): AvailablePosition(
            int(row["wc_id"]), str(row["wc_name"]), tuple(row["required_skills"] or ())
        )
        for row in cur.fetchall()
    }


def _validate_positions(cur, requested_counts: Mapping[int, int]) -> dict[int, AvailablePosition]:
    positions = _required_positions(cur, tuple(requested_counts))
    missing = sorted(set(requested_counts).difference(positions))
    if missing:
        raise LifecycleConflict("Every requested Saturday work center needs at least one required skill")
    return positions


def _json_ids(value) -> frozenset[int]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = []
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if type(item) is int)


def _load_bundle(cur, day: date) -> RecruitmentBundle | None:
    cur.execute(
        "SELECT day, status, shift_start, shift_end, response_deadline "
        "FROM saturday_recruitments WHERE day = %s",
        (day,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    recruitment = Recruitment(
        day=row["day"],
        status=str(row["status"]),
        shift_start=row["shift_start"],
        shift_end=row["shift_end"],
        response_deadline=row["response_deadline"],
    )
    cur.execute(
        "SELECT o.wc_id, wc.name AS wc_name, o.requested_count, "
        "array_agg(s.name ORDER BY s.name) AS required_skills "
        "FROM saturday_recruitment_openings o "
        "JOIN work_centers wc ON wc.id = o.wc_id "
        "JOIN work_center_required_skills wrs ON wrs.wc_id = o.wc_id "
        "JOIN skills s ON s.id = wrs.skill_id "
        "WHERE o.day = %s "
        "GROUP BY o.wc_id, wc.name, o.requested_count ORDER BY o.wc_id",
        (day,),
    )
    openings = tuple(
        sr.Opening(
            int(item["wc_id"]),
            str(item["wc_name"]),
            int(item["requested_count"]),
            tuple(item["required_skills"] or ()),
        )
        for item in cur.fetchall()
    )
    cur.execute(
        "SELECT r.person_id, p.odoo_id AS person_odoo_id, p.name AS person_name, r.status, "
        "r.availability_start, r.availability_end, r.eligible_wc_ids "
        "FROM saturday_work_responses r JOIN people p ON p.id = r.person_id "
        "WHERE r.day = %s ORDER BY r.person_id",
        (day,),
    )
    commitments = tuple(
        StoredCommitment(
            person_id=int(item["person_id"]),
            person_odoo_id=item["person_odoo_id"],
            person_name=str(item["person_name"]),
            status=str(item["status"]),
            availability_start=item["availability_start"],
            availability_end=item["availability_end"],
            eligible_wc_ids=_json_ids(item["eligible_wc_ids"]),
        )
        for item in cur.fetchall()
    )
    return RecruitmentBundle(recruitment, openings, commitments)


def get(day: date) -> RecruitmentBundle | None:
    """Return one persisted recruitment, including every response, if present."""
    from . import db

    with db.cursor() as cur:
        return _load_bundle(cur, day)


def available_positions() -> tuple[AvailablePosition, ...]:
    """Return locally identified work centers that have required skills."""
    from . import db

    with db.cursor() as cur:
        cur.execute(
            "SELECT wc.id AS wc_id, wc.name AS wc_name, "
            "array_agg(s.name ORDER BY s.name) AS required_skills "
            "FROM work_centers wc "
            "JOIN work_center_required_skills wrs ON wrs.wc_id = wc.id "
            "JOIN skills s ON s.id = wrs.skill_id "
            "GROUP BY wc.id, wc.name ORDER BY wc.id"
        )
        return tuple(
            AvailablePosition(
                int(row["wc_id"]), str(row["wc_name"]), tuple(row["required_skills"] or ())
            )
            for row in cur.fetchall()
        )


def _lock_recruitment(cur, day: date) -> Recruitment:
    cur.execute(
        "SELECT day, status, shift_start, shift_end, response_deadline "
        "FROM saturday_recruitments WHERE day = %s FOR UPDATE",
        (day,),
    )
    row = cur.fetchone()
    if row is None:
        raise LifecycleConflict("No Saturday recruiting round exists for this date")
    return Recruitment(
        day=row["day"],
        status=str(row["status"]),
        shift_start=row["shift_start"],
        shift_end=row["shift_end"],
        response_deadline=row["response_deadline"],
    )


def _require_open(recruitment: Recruitment, now: datetime) -> None:
    if recruitment.status != "recruiting" or recruitment.response_deadline <= now:
        raise RecruitingClosed("Saturday work sign-up is closed. Contact a manager to make a change.")


def _existing_response(cur, day: date, person_id: int) -> dict | None:
    cur.execute(
        "SELECT status, availability_start, availability_end, committed_at, cancelled_at "
        "FROM saturday_work_responses WHERE day = %s AND person_id = %s",
        (day, person_id),
    )
    return cur.fetchone()


def _person_can_volunteer(cur, person_id: int, day: date) -> bool:
    cur.execute(
        "SELECT odoo_id FROM people "
        "WHERE id = %s AND active = TRUE AND excluded = FALSE "
        "AND COALESCE(wage_type, 'hourly') <> 'monthly'",
        (person_id,),
    )
    person = cur.fetchone()
    if person is None:
        return False
    if person["odoo_id"] is None:
        return True
    cur.execute(
        "SELECT 1 FROM time_off_requests "
        "WHERE person_odoo_id = %s AND shape = 'full_day' "
        "AND state = ANY(%s) AND date_from <= %s AND date_to >= %s LIMIT 1",
        (person["odoo_id"], ["confirm", "validate1", "validate"], day, day),
    )
    return cur.fetchone() is None


def _eligible_wc_ids_for_person(
    cur, person_id: int, openings: tuple[sr.Opening, ...], day: date
) -> frozenset[int]:
    if not _person_can_volunteer(cur, person_id, day):
        return frozenset()
    cur.execute(
        "SELECT s.name, ps.level FROM person_skills ps "
        "JOIN skills s ON s.id = ps.skill_id WHERE ps.person_id = %s",
        (person_id,),
    )
    levels = {str(row["name"]): int(row["level"]) for row in cur.fetchall()}
    return sr.eligible_work_centers(levels, openings)


def _coverage_with_candidate(
    bundle: RecruitmentBundle, person_id: int, eligible_wc_ids: frozenset[int]
) -> sr.Coverage | None:
    commitments = [
        sr.Commitment(item.person_id, item.eligible_wc_ids)
        for item in bundle.commitments
        if item.status == "committed" and item.person_id != person_id
    ]
    return sr.match_commitments(
        bundle.openings, [*commitments, sr.Commitment(person_id, eligible_wc_ids)]
    )


def _remaining_count(bundle: RecruitmentBundle) -> int:
    coverage = sr.match_commitments(
        bundle.openings,
        [
            sr.Commitment(item.person_id, item.eligible_wc_ids)
            for item in bundle.commitments
            if item.status == "committed"
        ],
    )
    if coverage is None:
        return 0
    return sum(opening.requested_count for opening in bundle.openings) - coverage.total


def _insert_response(
    cur,
    day: date,
    person_id: int,
    status: str,
    now: datetime,
    *,
    availability_start: time | None = None,
    availability_end: time | None = None,
    eligible_wc_ids: frozenset[int] = frozenset(),
) -> None:
    cur.execute(
        "INSERT INTO saturday_work_responses "
        "(day, person_id, status, availability_start, availability_end, eligible_wc_ids, responded_at, committed_at, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s) "
        "ON CONFLICT (day, person_id) DO UPDATE SET "
        "status = EXCLUDED.status, availability_start = EXCLUDED.availability_start, "
        "availability_end = EXCLUDED.availability_end, eligible_wc_ids = EXCLUDED.eligible_wc_ids, "
        "responded_at = EXCLUDED.responded_at, committed_at = EXCLUDED.committed_at, updated_at = EXCLUDED.updated_at",
        (
            day,
            person_id,
            status,
            availability_start,
            availability_end,
            json.dumps(sorted(eligible_wc_ids)),
            now,
            now if status == "committed" else None,
            now,
            now,
        ),
    )


def _result(cur, day: date, status: str) -> DecisionResult:
    bundle = _load_bundle(cur, day)
    assert bundle is not None
    return DecisionResult(status, bundle)


def offer_for_person(person_id: int, now: datetime) -> Offer | None:
    """Return the next compatible open Saturday offer for one employee."""
    from . import db

    with db.cursor() as cur:
        cur.execute(
            "SELECT day FROM saturday_recruitments "
            "WHERE status = 'recruiting' AND response_deadline > %s ORDER BY day LIMIT 1",
            (now,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        bundle = _load_bundle(cur, row["day"])
        assert bundle is not None
        existing = next((item for item in bundle.commitments if item.person_id == person_id), None)
        if existing is not None and existing.status in {"declined", "committed", "cancelled"}:
            return None
        eligible_wc_ids = _eligible_wc_ids_for_person(cur, person_id, bundle.openings, bundle.recruitment.day)
        if not eligible_wc_ids or _coverage_with_candidate(bundle, person_id, eligible_wc_ids) is None:
            return None
        return Offer(
            bundle.recruitment.day,
            bundle.recruitment.shift_start,
            bundle.recruitment.shift_end,
            bundle.recruitment.response_deadline,
            eligible_wc_ids,
        )


def home_banner(now: datetime) -> HomeBanner | None:
    """Return the next recruiting Saturday while at least one slot remains."""
    from . import db

    with db.cursor() as cur:
        cur.execute(
            "SELECT day FROM saturday_recruitments "
            "WHERE status = 'recruiting' AND response_deadline > %s ORDER BY day",
            (now,),
        )
        for row in cur.fetchall():
            bundle = _load_bundle(cur, row["day"])
            assert bundle is not None
            remaining_count = _remaining_count(bundle)
            if remaining_count > 0:
                return HomeBanner(
                    bundle.recruitment.day, bundle.recruitment.response_deadline, remaining_count
                )
        return None


def commitment_for_person(person_id: int, now: datetime) -> CommitmentStatus | None:
    """Return an employee's next firm Saturday commitment, including after cutoff."""
    from . import db

    with db.cursor() as cur:
        cur.execute(
            "SELECT r.day, r.availability_start, r.availability_end, s.status, s.response_deadline "
            "FROM saturday_work_responses r "
            "JOIN saturday_recruitments s ON s.day = r.day "
            "WHERE r.person_id = %s AND r.status = 'committed' AND r.day >= %s "
            "ORDER BY r.day LIMIT 1",
            (person_id, now.date()),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return CommitmentStatus(
            row["day"],
            row["availability_start"],
            row["availability_end"],
            row["response_deadline"],
            row["status"] == "recruiting" and row["response_deadline"] > now,
        )


def record_later(day: date, person_id: int, now: datetime) -> DecisionResult:
    """Record a non-reserving Later response without allowing stale downgrades."""
    from . import db

    with db.cursor() as cur:
        recruitment = _lock_recruitment(cur, day)
        _require_open(recruitment, now)
        existing = _existing_response(cur, day, person_id)
        if existing is not None:
            if existing["status"] == "later":
                return _result(cur, day, "later")
            raise LifecycleConflict("Your Saturday response has already been finalized")
        bundle = _load_bundle(cur, day)
        assert bundle is not None
        eligible_wc_ids = _eligible_wc_ids_for_person(cur, person_id, bundle.openings, day)
        if not eligible_wc_ids or _coverage_with_candidate(bundle, person_id, eligible_wc_ids) is None:
            raise NoCompatibleOpening("That opening was just filled. You have not been scheduled.")
        _insert_response(cur, day, person_id, "later", now)
        return _result(cur, day, "later")


def decline(day: date, person_id: int, now: datetime) -> DecisionResult:
    """Record a No response, permanently suppressing this Saturday's offer."""
    from . import db

    with db.cursor() as cur:
        recruitment = _lock_recruitment(cur, day)
        _require_open(recruitment, now)
        existing = _existing_response(cur, day, person_id)
        if existing is not None:
            if existing["status"] == "declined":
                return _result(cur, day, "declined")
            if existing["status"] != "later":
                raise LifecycleConflict("Your Saturday response has already been finalized")
        _insert_response(cur, day, person_id, "declined", now)
        return _result(cur, day, "declined")


def commit(
    day: date, person_id: int, start: time, end: time, now: datetime
) -> DecisionResult:
    """Atomically claim one compatible Saturday opening for an employee."""
    from . import db

    with db.cursor() as cur:
        recruitment = _lock_recruitment(cur, day)
        _require_open(recruitment, now)
        sr.validate_availability(start, end, recruitment.shift_start, recruitment.shift_end)
        existing = _existing_response(cur, day, person_id)
        if existing is not None:
            if (
                existing["status"] == "committed"
                and existing["availability_start"] == start
                and existing["availability_end"] == end
            ):
                return _result(cur, day, "committed")
            if existing["status"] != "later":
                raise LifecycleConflict("Your Saturday response has already been finalized")
        bundle = _load_bundle(cur, day)
        assert bundle is not None
        eligible_wc_ids = _eligible_wc_ids_for_person(cur, person_id, bundle.openings, day)
        if not eligible_wc_ids or _coverage_with_candidate(bundle, person_id, eligible_wc_ids) is None:
            raise NoCompatibleOpening("That opening was just filled. You have not been scheduled.")
        _insert_response(
            cur,
            day,
            person_id,
            "committed",
            now,
            availability_start=start,
            availability_end=end,
            eligible_wc_ids=eligible_wc_ids,
        )
        return _result(cur, day, "committed")


def _cancel(
    cur, day: date, person_id: int, now: datetime, actor: str | None, reason: str | None
) -> DecisionResult:
    existing = _existing_response(cur, day, person_id)
    if existing is None:
        raise LifecycleConflict("No Saturday commitment exists to cancel")
    if existing["status"] == "cancelled":
        return _result(cur, day, "cancelled")
    if existing["status"] != "committed":
        raise LifecycleConflict("No Saturday commitment exists to cancel")
    cur.execute(
        "UPDATE saturday_work_responses SET status = 'cancelled', cancelled_at = %s, "
        "cancelled_by = %s, cancellation_reason = %s, updated_at = %s "
        "WHERE day = %s AND person_id = %s",
        (now, actor, reason, now, day, person_id),
    )
    return _result(cur, day, "cancelled")


def cancel_by_employee(day: date, person_id: int, now: datetime) -> DecisionResult:
    """Allow an employee to cancel their own commitment before the cutoff."""
    from . import db

    with db.cursor() as cur:
        recruitment = _lock_recruitment(cur, day)
        _require_open(recruitment, now)
        return _cancel(cur, day, person_id, now, None, None)


def cancel_by_manager(
    day: date, person_id: int, actor: str | None, reason: str, now: datetime
) -> DecisionResult:
    """Allow a manager to cancel a commitment even after the employee cutoff."""
    from . import db

    if not reason.strip():
        raise LifecycleConflict("A cancellation reason is required")
    with db.cursor() as cur:
        _lock_recruitment(cur, day)
        return _cancel(cur, day, person_id, now, actor, reason.strip())


def activate(
    day: date,
    shift_start: time,
    shift_end: time,
    response_deadline: datetime,
    requested_counts: Mapping[int, int],
    actor: str | None,
    now: datetime,
) -> RecruitmentBundle:
    """Create a recruiting round, safely rejecting non-volunteer schedules."""
    from . import db

    if day.weekday() != 5:
        raise SaturdayRecruitingError("Saturday recruiting requires a Saturday")
    _validate_shift(shift_start, shift_end)
    response_deadline = _row_datetime(response_deadline)
    if response_deadline <= now:
        raise LifecycleConflict("Saturday response deadline has already passed")
    requested_counts = _normalize_counts(requested_counts)
    with db.cursor() as cur:
        # A missing row cannot be protected by SELECT ... FOR UPDATE. Serialize
        # activation attempts for this one calendar day before checking it so
        # concurrent identical requests see the first inserted round and take
        # the idempotent branch rather than racing its primary-key insert.
        cur.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (day.toordinal(),))
        positions = _validate_positions(cur, requested_counts)
        cur.execute(
            "SELECT published FROM schedules WHERE day = %s FOR UPDATE",
            (day,),
        )
        schedule = cur.fetchone()
        if schedule and schedule["published"]:
            raise LifecycleConflict("A published Saturday schedule cannot enter recruiting")
        cur.execute("SELECT 1 FROM schedule_assignments WHERE day = %s LIMIT 1", (day,))
        if cur.fetchone() is not None:
            raise LifecycleConflict("Clear existing Saturday assignments before activating recruiting.")
        cur.execute("SELECT day FROM saturday_recruitments WHERE day = %s FOR UPDATE", (day,))
        if cur.fetchone() is not None:
            existing = _load_bundle(cur, day)
            assert existing is not None
            same_payload = (
                existing.recruitment.status == "recruiting"
                and existing.recruitment.shift_start == shift_start
                and existing.recruitment.shift_end == shift_end
                and existing.recruitment.response_deadline == response_deadline
                and {opening.wc_id: opening.requested_count for opening in existing.openings}
                == requested_counts
            )
            if same_payload:
                return existing
            raise LifecycleConflict("Saturday recruiting has already been activated with different details")
        cur.execute(
            "INSERT INTO saturday_recruitments "
            "(day, status, shift_start, shift_end, response_deadline, activated_by, activated_at, created_at, updated_at) "
            "VALUES (%s, 'recruiting', %s, %s, %s, %s, %s, %s, %s)",
            (day, shift_start, shift_end, response_deadline, actor, now, now, now),
        )
        for wc_id, requested_count in requested_counts.items():
            # _validate_positions above proves this local id has requirements.
            assert wc_id in positions
            cur.execute(
                "INSERT INTO saturday_recruitment_openings (day, wc_id, requested_count) "
                "VALUES (%s, %s, %s)",
                (day, wc_id, requested_count),
            )
        bundle = _load_bundle(cur, day)
        assert bundle is not None
        return bundle


def update_openings(
    day: date,
    requested_counts: Mapping[int, int],
    shift_start: time,
    shift_end: time,
    actor: str | None,
    now: datetime,
) -> RecruitmentBundle:
    """Replace unfilled openings while preserving committed volunteer coverage."""
    del actor  # Audit columns for per-opening edits are intentionally not part of the schema.
    from . import db

    _validate_shift(shift_start, shift_end)
    requested_counts = _normalize_counts(requested_counts)
    with db.cursor() as cur:
        cur.execute("SELECT day FROM saturday_recruitments WHERE day = %s FOR UPDATE", (day,))
        if cur.fetchone() is None:
            raise LifecycleConflict("No Saturday recruiting round exists for this date")
        bundle = _load_bundle(cur, day)
        assert bundle is not None
        if bundle.recruitment.status not in {"recruiting", "closed"}:
            raise LifecycleConflict("Saturday recruiting openings can no longer be changed")
        if (
            (shift_start != bundle.recruitment.shift_start or shift_end != bundle.recruitment.shift_end)
            and any(item.status == "committed" for item in bundle.commitments)
        ):
            raise LifecycleConflict("Saturday shift hours lock after the first commitment")
        positions = _validate_positions(cur, requested_counts)
        old_counts = {opening.wc_id: opening.requested_count for opening in bundle.openings}
        if bundle.recruitment.status == "closed" and (
            not set(requested_counts).issubset(old_counts)
            or any(requested_counts[wc_id] > old_counts[wc_id] for wc_id in requested_counts)
        ):
            raise LifecycleConflict("Closed recruiting can only reduce unfilled openings")
        proposed_openings = tuple(
            sr.Opening(
                wc_id,
                positions[wc_id].wc_name,
                requested_count,
                positions[wc_id].required_skills,
            )
            for wc_id, requested_count in sorted(requested_counts.items())
        )
        coverage = sr.match_commitments(
            proposed_openings,
            tuple(
                sr.Commitment(item.person_id, item.eligible_wc_ids)
                for item in bundle.commitments
                if item.status == "committed"
            ),
        )
        if coverage is None:
            raise LifecycleConflict("Requested openings cannot drop below committed Saturday coverage")
        cur.execute(
            "UPDATE saturday_recruitments SET shift_start = %s, shift_end = %s, updated_at = %s "
            "WHERE day = %s",
            (shift_start, shift_end, now, day),
        )
        cur.execute("DELETE FROM saturday_recruitment_openings WHERE day = %s", (day,))
        for wc_id, requested_count in requested_counts.items():
            cur.execute(
                "INSERT INTO saturday_recruitment_openings (day, wc_id, requested_count) "
                "VALUES (%s, %s, %s)",
                (day, wc_id, requested_count),
            )
        updated = _load_bundle(cur, day)
        assert updated is not None
        return updated


def close_due(now: datetime) -> int:
    """Close every recruiting round whose snapshotted deadline is due."""
    from . import db

    with db.cursor() as cur:
        cur.execute(
            "UPDATE saturday_recruitments "
            "SET status = 'closed', closed_at = %s, updated_at = %s "
            "WHERE status = 'recruiting' AND response_deadline <= %s",
            (now, now, now),
        )
        return cur.rowcount


def mark_published(day: date, now: datetime) -> RecruitmentBundle:
    """Move a closed recruiting round into its published terminal state."""
    from . import db

    with db.cursor() as cur:
        cur.execute("SELECT day FROM saturday_recruitments WHERE day = %s FOR UPDATE", (day,))
        if cur.fetchone() is None:
            raise LifecycleConflict("No Saturday recruiting round exists for this date")
        bundle = _load_bundle(cur, day)
        assert bundle is not None
        if bundle.recruitment.status == "published":
            return bundle
        if bundle.recruitment.status != "closed":
            raise LifecycleConflict("Saturday recruiting must close before publishing")
        cur.execute(
            "UPDATE saturday_recruitments "
            "SET status = 'published', published_at = %s, updated_at = %s WHERE day = %s",
            (now, now, day),
        )
        published = _load_bundle(cur, day)
        assert published is not None
        return published
