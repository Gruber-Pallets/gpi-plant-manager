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
            and (bundle.recruitment.status != "recruiting" or any(
                item.status == "committed" for item in bundle.commitments
            ))
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
