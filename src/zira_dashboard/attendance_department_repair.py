"""Durably repair Odoo attendance departments without moving work centers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
import logging
from typing import Literal, TypeAlias

from . import (
    attendance_location_policy,
    attendance_mirror,
    attendance_timeline,
    db,
    odoo_client,
    shift_config,
    work_centers_store,
)


RepairOutcome: TypeAlias = Literal[
    "repaired",
    "adopted_timeout",
    "already_correct",
    "version_refreshed",
    "target_refreshed",
    "discarded",
    "superseded",
    "retrying",
    "failed",
]

MAX_ATTEMPTS = 3
_LEASE = timedelta(minutes=15)
_ERROR_LIMIT = 500
_log = logging.getLogger(__name__)


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _aware_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise TypeError(f"{field_name} must be an aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")
    return value.astimezone(UTC)


def _optional_aware_utc(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _aware_utc(value, field_name)


def _now_utc(value: datetime | None) -> datetime:
    return datetime.now(UTC) if value is None else _aware_utc(value, "now_utc")


def _wall_clock_utc() -> datetime:
    return datetime.now(UTC)


def _error_text(error: object) -> str:
    return (str(error) or type(error).__name__)[:_ERROR_LIMIT]


@dataclass(frozen=True)
class RepairCandidate:
    attendance_id: int
    expected_write_date: datetime
    target_department_id: int
    expected_work_center_id: int
    target_projected_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "attendance_id",
            _positive_int(self.attendance_id, "attendance_id"),
        )
        object.__setattr__(
            self,
            "expected_write_date",
            _aware_utc(self.expected_write_date, "expected_write_date"),
        )
        object.__setattr__(
            self,
            "target_department_id",
            _positive_int(self.target_department_id, "target_department_id"),
        )
        object.__setattr__(
            self,
            "expected_work_center_id",
            _positive_int(self.expected_work_center_id, "expected_work_center_id"),
        )
        object.__setattr__(
            self,
            "target_projected_at",
            _aware_utc(self.target_projected_at, "target_projected_at"),
        )


@dataclass(frozen=True)
class RepairProjectionProof:
    attendance_id: int
    write_date: datetime
    work_center_id: int
    department_id: int | None
    target_department_id: int
    target_projected_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "attendance_id", _positive_int(self.attendance_id, "attendance_id")
        )
        object.__setattr__(
            self,
            "write_date",
            _aware_utc(self.write_date, "write_date"),
        )
        object.__setattr__(
            self,
            "work_center_id",
            _positive_int(self.work_center_id, "work_center_id"),
        )
        if self.department_id is not None:
            object.__setattr__(
                self,
                "department_id",
                _positive_int(self.department_id, "department_id"),
            )
        object.__setattr__(
            self,
            "target_department_id",
            _positive_int(self.target_department_id, "target_department_id"),
        )
        object.__setattr__(
            self,
            "target_projected_at",
            _aware_utc(self.target_projected_at, "target_projected_at"),
        )


@dataclass(frozen=True)
class RepairClaim:
    attendance_id: int
    expected_write_date: datetime
    target_department_id: int
    expected_work_center_id: int | None
    mirror_write_date: datetime | None
    attempt_count: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "attendance_id",
            _positive_int(self.attendance_id, "attendance_id"),
        )
        object.__setattr__(
            self,
            "expected_write_date",
            _aware_utc(self.expected_write_date, "expected_write_date"),
        )
        object.__setattr__(
            self,
            "target_department_id",
            _positive_int(self.target_department_id, "target_department_id"),
        )
        if self.expected_work_center_id is not None:
            object.__setattr__(
                self,
                "expected_work_center_id",
                _positive_int(
                    self.expected_work_center_id,
                    "expected_work_center_id",
                ),
            )
        object.__setattr__(
            self,
            "mirror_write_date",
            _optional_aware_utc(self.mirror_write_date, "mirror_write_date"),
        )
        object.__setattr__(
            self,
            "attempt_count",
            _positive_int(self.attempt_count, "attempt_count"),
        )


@dataclass(frozen=True)
class RepairResult:
    attendance_id: int
    outcome: RepairOutcome
    attempt_count: int
    error: str | None


class _OdooFacade:
    def fetch_attendance_rows_by_ids(self, ids: Sequence[int]):
        return odoo_client.fetch_attendance_rows_by_ids(ids)

    def set_attendance_department_id(self, attendance_id: int, department_id: int) -> None:
        odoo_client.set_attendance_department_id(attendance_id, department_id)

    def target_department_id_for_work_center(
        self,
        odoo_work_center_id: int,
        *,
        force: bool = False,
    ) -> int | None:
        return odoo_client.target_department_id_for_work_center(
            odoo_work_center_id,
            force=force,
        )


def _upsert_verified_cur(
    cur,
    row: Mapping[str, object],
    *,
    now_utc: datetime,
    sync_state: Mapping[str, object] | None = None,
) -> None:
    normalized = attendance_mirror._normalized_rows((row,))  # noqa: SLF001
    state = (
        attendance_mirror._locked_sync_state(cur)  # noqa: SLF001
        if sync_state is None
        else sync_state
    )
    attendance_mirror._upsert_rows_cur(  # noqa: SLF001
        cur,
        normalized,
        sync_completed_at=now_utc,
        observed_at=now_utc,
        baseline_completed=state["baseline_completed_at"] is not None,
    )


class _PostgresBackend:
    @staticmethod
    def _enqueue_cur(cur, candidate: RepairCandidate, *, now_utc: datetime) -> bool:
        cur.execute(
            "INSERT INTO attendance_department_repairs "
            "(odoo_attendance_id, expected_write_date, "
            "target_odoo_department_id, expected_odoo_work_center_id, "
            "target_projected_at, status, attempt_count, updated_at, last_error) "
            "VALUES (%s, %s, %s, %s, %s, 'pending', 0, %s, NULL) "
            "ON CONFLICT (odoo_attendance_id) DO NOTHING "
            "RETURNING odoo_attendance_id",
            (
                candidate.attendance_id,
                candidate.expected_write_date,
                candidate.target_department_id,
                candidate.expected_work_center_id,
                candidate.target_projected_at,
                now_utc,
            ),
        )
        if cur.fetchone() is not None:
            return True
        cur.execute(
            "SELECT expected_write_date, target_odoo_department_id, "
            "expected_odoo_work_center_id, target_projected_at, status, "
            "successor_expected_write_date, successor_target_odoo_department_id, "
            "successor_expected_odoo_work_center_id, successor_target_projected_at "
            "FROM attendance_department_repairs "
            "WHERE odoo_attendance_id = %s FOR UPDATE",
            (candidate.attendance_id,),
        )
        existing = cur.fetchone()
        if existing is None:
            raise RuntimeError("department repair disappeared during enqueue")
        candidate_value = (
            candidate.expected_write_date,
            candidate.target_department_id,
            candidate.expected_work_center_id,
            candidate.target_projected_at,
        )
        active_value = (
            existing["expected_write_date"],
            int(existing["target_odoo_department_id"]),
            (
                int(existing["expected_odoo_work_center_id"])
                if existing["expected_odoo_work_center_id"] is not None
                else None
            ),
            existing["target_projected_at"],
        )
        successor_raw = (
            existing["successor_expected_write_date"],
            existing["successor_target_odoo_department_id"],
            existing["successor_expected_odoo_work_center_id"],
            existing["successor_target_projected_at"],
        )
        if any(value is not None for value in successor_raw) and not all(
            value is not None for value in successor_raw
        ):
            raise RuntimeError("department repair successor is incomplete")
        successor_value = (
            (
                successor_raw[0],
                int(successor_raw[1]),
                int(successor_raw[2]),
                successor_raw[3],
            )
            if successor_raw[0] is not None
            else None
        )
        if existing["status"] == "applying":
            latest_projected_at = (
                successor_value[3] if successor_value is not None else active_value[3]
            )
            if candidate.target_projected_at <= latest_projected_at:
                return False
            newest_version = (
                max(active_value[0], successor_value[0])
                if successor_value is not None
                else active_value[0]
            )
            if candidate.expected_write_date < newest_version:
                cur.execute(
                    "SELECT odoo_write_date, odoo_work_center_id "
                    "FROM odoo_attendance_mirror "
                    "WHERE odoo_attendance_id = %s AND deleted_at IS NULL",
                    (candidate.attendance_id,),
                )
                mirror = cur.fetchone()
                if (
                    mirror is None
                    or mirror["odoo_write_date"] < newest_version
                    or mirror["odoo_work_center_id"] != candidate.expected_work_center_id
                ):
                    return False
                candidate_value = (
                    mirror["odoo_write_date"],
                    candidate.target_department_id,
                    candidate.expected_work_center_id,
                    candidate.target_projected_at,
                )
            if successor_value is None and candidate_value[:3] == active_value[:3]:
                cur.execute(
                    "UPDATE attendance_department_repairs SET target_projected_at = %s "
                    "WHERE odoo_attendance_id = %s",
                    (candidate.target_projected_at, candidate.attendance_id),
                )
                return False
            cur.execute(
                "UPDATE attendance_department_repairs SET "
                "successor_expected_write_date = %s, "
                "successor_target_odoo_department_id = %s, "
                "successor_expected_odoo_work_center_id = %s, "
                "successor_target_projected_at = %s "
                "WHERE odoo_attendance_id = %s",
                (*candidate_value, candidate.attendance_id),
            )
            return cur.rowcount == 1

        if candidate.target_projected_at <= active_value[3]:
            return False
        if candidate.expected_write_date < active_value[0]:
            cur.execute(
                "SELECT odoo_write_date, odoo_work_center_id "
                "FROM odoo_attendance_mirror "
                "WHERE odoo_attendance_id = %s AND deleted_at IS NULL",
                (candidate.attendance_id,),
            )
            mirror = cur.fetchone()
            if (
                mirror is None
                or mirror["odoo_write_date"] < active_value[0]
                or mirror["odoo_work_center_id"] != candidate.expected_work_center_id
            ):
                return False
            candidate_value = (
                mirror["odoo_write_date"],
                candidate.target_department_id,
                candidate.expected_work_center_id,
                candidate.target_projected_at,
            )
        if candidate_value[:3] == active_value[:3]:
            cur.execute(
                "UPDATE attendance_department_repairs SET target_projected_at = %s "
                "WHERE odoo_attendance_id = %s",
                (candidate.target_projected_at, candidate.attendance_id),
            )
            return False
        cur.execute(
            "UPDATE attendance_department_repairs SET "
            "expected_write_date = %s, target_odoo_department_id = %s, "
            "expected_odoo_work_center_id = %s, target_projected_at = %s, "
            "status = 'pending', attempt_count = 0, updated_at = %s, "
            "last_error = NULL, successor_expected_write_date = NULL, "
            "successor_target_odoo_department_id = NULL, "
            "successor_expected_odoo_work_center_id = NULL, "
            "successor_target_projected_at = NULL "
            "WHERE odoo_attendance_id = %s",
            (*candidate_value, now_utc, candidate.attendance_id),
        )
        return cur.rowcount == 1

    def enqueue(
        self,
        candidates: Sequence[RepairCandidate],
        *,
        now_utc: datetime,
    ) -> int:
        changed = 0
        with db.cursor() as cur:
            for candidate in candidates:
                changed += self._enqueue_cur(cur, candidate, now_utc=now_utc)
        return changed

    def reconcile(
        self,
        candidates: Sequence[RepairCandidate],
        projection_proofs: Sequence[RepairProjectionProof],
        *,
        now_utc: datetime,
    ) -> int:
        candidates_by_id = {candidate.attendance_id: candidate for candidate in candidates}
        proofs_by_id = {proof.attendance_id: proof for proof in projection_proofs}
        if len(candidates_by_id) != len(candidates):
            raise ValueError("repair candidates must have unique attendance IDs")
        if len(proofs_by_id) != len(projection_proofs):
            raise ValueError("repair projection proofs must have unique attendance IDs")
        if not candidates_by_id.keys() <= proofs_by_id.keys():
            raise ValueError("repair candidates must belong to the covered projection")
        for attendance_id, candidate in candidates_by_id.items():
            proof = proofs_by_id[attendance_id]
            if (
                candidate.expected_write_date != proof.write_date
                or candidate.expected_work_center_id != proof.work_center_id
                or candidate.target_department_id != proof.target_department_id
                or candidate.target_projected_at != proof.target_projected_at
            ):
                raise ValueError("repair candidate does not match its projection proof")
        changed = 0
        with db.cursor() as cur:
            proof_ids = sorted(proofs_by_id)
            existing_repair_ids: set[int] = set()
            if proof_ids:
                cur.execute(
                    "SELECT odoo_attendance_id FROM attendance_department_repairs "
                    "WHERE odoo_attendance_id = ANY(%s) FOR UPDATE",
                    (proof_ids,),
                )
                existing_repair_ids = {
                    _positive_int(row["odoo_attendance_id"], "repair attendance id")
                    for row in cur.fetchall()
                }
            actionable_ids = sorted(set(candidates_by_id) | existing_repair_ids)
            current_ids: set[int] = set()
            for attendance_id in actionable_ids:
                proof = proofs_by_id[attendance_id]
                cur.execute(
                    "SELECT odoo_write_date, odoo_work_center_id, odoo_department_id "
                    "FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s "
                    "AND deleted_at IS NULL FOR UPDATE",
                    (attendance_id,),
                )
                mirror = cur.fetchone()
                if mirror is None:
                    continue
                exact_projection = (
                    mirror["odoo_write_date"] == proof.write_date
                    and mirror["odoo_work_center_id"] == proof.work_center_id
                    and mirror["odoo_department_id"] == proof.department_id
                )
                delayed_candidate = (
                    attendance_id in candidates_by_id
                    and attendance_id in existing_repair_ids
                    and mirror["odoo_write_date"] > proof.write_date
                    and mirror["odoo_work_center_id"] == proof.work_center_id
                )
                if not exact_projection and not delayed_candidate:
                    continue
                current_ids.add(attendance_id)
            for attendance_id, candidate in candidates_by_id.items():
                if attendance_id not in current_ids:
                    continue
                proof = proofs_by_id[attendance_id]
                if proof.department_id == proof.target_department_id:
                    continue
                changed += self._enqueue_cur(cur, candidate, now_utc=now_utc)
            obsolete_ids = sorted(current_ids - candidates_by_id.keys())
            if obsolete_ids:
                for attendance_id in obsolete_ids:
                    proof = proofs_by_id[attendance_id]
                    if proof.department_id != proof.target_department_id:
                        raise ValueError(
                            "candidate-free projection cannot prove the row is correct"
                        )
                    cur.execute(
                        "UPDATE attendance_department_repairs SET "
                        "expected_write_date = %s, "
                        "target_odoo_department_id = %s, "
                        "expected_odoo_work_center_id = %s, "
                        "target_projected_at = %s, status = 'complete', "
                        "updated_at = %s, last_error = NULL "
                        "WHERE odoo_attendance_id = %s AND status <> 'applying' "
                        "AND %s > target_projected_at "
                        "AND (status <> 'complete' OR last_error IS NOT NULL)",
                        (
                            proof.write_date,
                            proof.target_department_id,
                            proof.work_center_id,
                            proof.target_projected_at,
                            now_utc,
                            attendance_id,
                            proof.target_projected_at,
                        ),
                    )
        return changed

    def claim_next(self, *, now_utc: datetime) -> RepairClaim | None:
        stale_before = now_utc - _LEASE
        with db.cursor() as cur:
            cur.execute(
                "UPDATE attendance_department_repairs SET "
                "expected_write_date = successor_expected_write_date, "
                "target_odoo_department_id = successor_target_odoo_department_id, "
                "expected_odoo_work_center_id = "
                "successor_expected_odoo_work_center_id, "
                "target_projected_at = successor_target_projected_at, "
                "status = 'pending', attempt_count = 0, updated_at = %s, "
                "last_error = NULL, successor_expected_write_date = NULL, "
                "successor_target_odoo_department_id = NULL, "
                "successor_expected_odoo_work_center_id = NULL, "
                "successor_target_projected_at = NULL "
                "WHERE status = 'applying' AND updated_at <= %s "
                "AND attempt_count >= %s "
                "AND successor_expected_write_date IS NOT NULL "
                "AND successor_target_odoo_department_id IS NOT NULL "
                "AND successor_expected_odoo_work_center_id IS NOT NULL "
                "AND successor_target_projected_at IS NOT NULL",
                (now_utc, stale_before, MAX_ATTEMPTS),
            )
            cur.execute(
                "UPDATE attendance_department_repairs SET status = 'failed', "
                "updated_at = %s, last_error = %s "
                "WHERE status = 'applying' AND updated_at <= %s AND attempt_count >= %s",
                (
                    now_utc,
                    "Odoo department repair lease expired after the final attempt",
                    stale_before,
                    MAX_ATTEMPTS,
                ),
            )
            cur.execute(
                "SELECT r.odoo_attendance_id, r.expected_write_date, "
                "r.target_odoo_department_id, r.attempt_count, "
                "r.expected_odoo_work_center_id, "
                "CASE WHEN m.deleted_at IS NULL THEN m.odoo_write_date END "
                "AS mirror_write_date "
                "FROM attendance_department_repairs r "
                "LEFT JOIN odoo_attendance_mirror m "
                "ON m.odoo_attendance_id = r.odoo_attendance_id "
                "WHERE r.attempt_count < %s AND (r.status = 'pending' "
                "OR (r.status = 'applying' AND r.updated_at <= %s)) "
                "ORDER BY r.updated_at, r.odoo_attendance_id "
                "FOR UPDATE OF r SKIP LOCKED LIMIT 1",
                (MAX_ATTEMPTS, stale_before),
            )
            row = cur.fetchone()
            if row is None:
                return None
            attempt_count = int(row["attempt_count"]) + 1
            cur.execute(
                "UPDATE attendance_department_repairs SET "
                "status = 'applying', attempt_count = %s, updated_at = %s "
                "WHERE odoo_attendance_id = %s",
                (attempt_count, now_utc, row["odoo_attendance_id"]),
            )
        return RepairClaim(
            attendance_id=int(row["odoo_attendance_id"]),
            expected_write_date=row["expected_write_date"],
            target_department_id=int(row["target_odoo_department_id"]),
            expected_work_center_id=(
                int(row["expected_odoo_work_center_id"])
                if row["expected_odoo_work_center_id"] is not None
                else None
            ),
            mirror_write_date=row["mirror_write_date"],
            attempt_count=attempt_count,
        )

    @staticmethod
    def _lock_owned(cur, claim: RepairClaim) -> Mapping[str, object] | None:
        cur.execute(
            "SELECT status, attempt_count, expected_write_date, "
            "target_odoo_department_id, expected_odoo_work_center_id, "
            "target_projected_at, "
            "successor_expected_write_date, "
            "successor_target_odoo_department_id, "
            "successor_expected_odoo_work_center_id, "
            "successor_target_projected_at "
            "FROM attendance_department_repairs "
            "WHERE odoo_attendance_id = %s FOR UPDATE",
            (claim.attendance_id,),
        )
        row = cur.fetchone()
        owned = bool(
            row is not None
            and row["status"] == "applying"
            and int(row["attempt_count"]) == claim.attempt_count
            and row["expected_write_date"] == claim.expected_write_date
            and int(row["target_odoo_department_id"]) == claim.target_department_id
            and (
                int(row["expected_odoo_work_center_id"])
                if row["expected_odoo_work_center_id"] is not None
                else None
            )
            == claim.expected_work_center_id
        )
        return row if owned else None

    @staticmethod
    def _successor(
        row: Mapping[str, object],
    ) -> tuple[datetime, int, int, datetime] | None:
        raw = (
            row["successor_expected_write_date"],
            row["successor_target_odoo_department_id"],
            row["successor_expected_odoo_work_center_id"],
            row["successor_target_projected_at"],
        )
        if all(value is None for value in raw):
            return None
        if not all(value is not None for value in raw):
            raise RuntimeError("department repair successor is incomplete")
        return (
            _aware_utc(raw[0], "successor_expected_write_date"),
            _positive_int(raw[1], "successor_target_odoo_department_id"),
            _positive_int(raw[2], "successor_expected_odoo_work_center_id"),
            _aware_utc(raw[3], "successor_target_projected_at"),
        )

    @staticmethod
    def _claim_fence(claim: RepairClaim) -> tuple[object, ...]:
        return (
            claim.attendance_id,
            claim.attempt_count,
            claim.expected_write_date,
            claim.target_department_id,
            claim.expected_work_center_id,
        )

    def _promote_successor(
        self,
        cur,
        claim: RepairClaim,
        owned: Mapping[str, object],
        *,
        now_utc: datetime,
        observed_row: Mapping[str, object] | None = None,
    ) -> bool:
        successor = self._successor(owned)
        if successor is None:
            return False
        if (
            observed_row is not None
            and observed_row["odoo_work_center_id"] == successor[2]
            and observed_row["odoo_write_date"] > successor[0]
        ):
            successor = (
                _aware_utc(observed_row["odoo_write_date"], "odoo_write_date"),
                successor[1],
                successor[2],
                successor[3],
            )
        cur.execute(
            "UPDATE attendance_department_repairs SET "
            "expected_write_date = %s, target_odoo_department_id = %s, "
            "expected_odoo_work_center_id = %s, target_projected_at = %s, "
            "status = 'pending', "
            "attempt_count = 0, updated_at = %s, last_error = NULL, "
            "successor_expected_write_date = NULL, "
            "successor_target_odoo_department_id = NULL, "
            "successor_expected_odoo_work_center_id = NULL, "
            "successor_target_projected_at = NULL "
            "WHERE odoo_attendance_id = %s AND status = 'applying' "
            "AND attempt_count = %s AND expected_write_date = %s "
            "AND target_odoo_department_id = %s "
            "AND expected_odoo_work_center_id IS NOT DISTINCT FROM %s",
            (*successor, now_utc, *self._claim_fence(claim)),
        )
        return cur.rowcount == 1

    def renew_claim(self, claim: RepairClaim, *, now_utc: datetime) -> bool:
        with db.cursor() as cur:
            owned = self._lock_owned(cur, claim)
            if owned is None:
                return False
            if self._promote_successor(
                cur,
                claim,
                owned,
                now_utc=now_utc,
            ):
                return False
            cur.execute(
                "SELECT odoo_write_date, odoo_work_center_id "
                "FROM odoo_attendance_mirror "
                "WHERE odoo_attendance_id = %s AND deleted_at IS NULL",
                (claim.attendance_id,),
            )
            mirror = cur.fetchone()
            if (
                mirror is None
                or mirror["odoo_write_date"] != claim.expected_write_date
                or mirror["odoo_work_center_id"] != claim.expected_work_center_id
            ):
                cur.execute(
                    "UPDATE attendance_department_repairs SET "
                    "status = 'complete', updated_at = %s, last_error = NULL "
                    "WHERE odoo_attendance_id = %s AND status = 'applying' "
                    "AND attempt_count = %s AND expected_write_date = %s "
                    "AND target_odoo_department_id = %s "
                    "AND expected_odoo_work_center_id IS NOT DISTINCT FROM %s",
                    (now_utc, *self._claim_fence(claim)),
                )
                return False
            cur.execute(
                "UPDATE attendance_department_repairs SET updated_at = %s "
                "WHERE odoo_attendance_id = %s AND status = 'applying' "
                "AND attempt_count = %s AND expected_write_date = %s "
                "AND target_odoo_department_id = %s "
                "AND expected_odoo_work_center_id IS NOT DISTINCT FROM %s",
                (now_utc, *self._claim_fence(claim)),
            )
            return cur.rowcount == 1

    def reserve_for_write(self, claim: RepairClaim, *, now_utc: datetime) -> bool:
        return self.renew_claim(claim, now_utc=_aware_utc(now_utc, "now_utc"))

    @staticmethod
    def _verified_row_is_current(cur, claim: RepairClaim, row: Mapping[str, object]) -> bool:
        if row["odoo_work_center_id"] != claim.expected_work_center_id:
            return False
        cur.execute(
            "SELECT odoo_write_date, odoo_work_center_id FROM odoo_attendance_mirror "
            "WHERE odoo_attendance_id = %s FOR UPDATE",
            (claim.attendance_id,),
        )
        mirror = cur.fetchone()
        if mirror is None:
            return True
        if mirror["odoo_write_date"] > row["odoo_write_date"]:
            return False
        return not (
            mirror["odoo_write_date"] == row["odoo_write_date"]
            and mirror["odoo_work_center_id"] != row["odoo_work_center_id"]
        )

    def refresh_expected(
        self,
        claim: RepairClaim,
        row: Mapping[str, object],
        *,
        now_utc: datetime,
    ) -> bool:
        normalized = attendance_mirror._normalize_row(row)  # noqa: SLF001
        if normalized["odoo_attendance_id"] != claim.attendance_id:
            raise ValueError("verified row does not match the repair claim")
        with db.cursor() as cur:
            attendance_location_policy.lock_rollout_decision_cur(cur)
            owned = self._lock_owned(cur, claim)
            if owned is None:
                return False
            sync_state = attendance_mirror._locked_sync_state(cur)  # noqa: SLF001
            if not self._verified_row_is_current(cur, claim, normalized):
                return False
            _upsert_verified_cur(
                cur,
                normalized,
                now_utc=now_utc,
                sync_state=sync_state,
            )
            successor = self._successor(owned)
            if successor is not None and (
                successor[0] >= normalized["odoo_write_date"]
                or successor[2] == normalized["odoo_work_center_id"]
            ):
                return self._promote_successor(
                    cur,
                    claim,
                    owned,
                    now_utc=now_utc,
                    observed_row=normalized,
                )
            cur.execute(
                "UPDATE attendance_department_repairs SET "
                "expected_write_date = %s, status = 'pending', "
                "attempt_count = 0, updated_at = %s, last_error = NULL, "
                "successor_expected_write_date = NULL, "
                "successor_target_odoo_department_id = NULL, "
                "successor_expected_odoo_work_center_id = NULL, "
                "successor_target_projected_at = NULL "
                "WHERE odoo_attendance_id = %s AND status = 'applying' "
                "AND attempt_count = %s AND expected_write_date = %s "
                "AND target_odoo_department_id = %s "
                "AND expected_odoo_work_center_id IS NOT DISTINCT FROM %s",
                (
                    normalized["odoo_write_date"],
                    now_utc,
                    *self._claim_fence(claim),
                ),
            )
            return cur.rowcount == 1

    def refresh_target(
        self,
        claim: RepairClaim,
        target_department_id: int,
        *,
        now_utc: datetime,
    ) -> bool:
        target = _positive_int(target_department_id, "target_department_id")
        with db.cursor() as cur:
            owned = self._lock_owned(cur, claim)
            if owned is None:
                return False
            if self._promote_successor(cur, claim, owned, now_utc=now_utc):
                return True
            cur.execute(
                "UPDATE attendance_department_repairs SET "
                "target_odoo_department_id = %s, target_projected_at = "
                "GREATEST(target_projected_at, %s), status = 'pending', "
                "attempt_count = 0, updated_at = %s, last_error = NULL "
                "WHERE odoo_attendance_id = %s AND status = 'applying' "
                "AND attempt_count = %s AND expected_write_date = %s "
                "AND target_odoo_department_id = %s "
                "AND expected_odoo_work_center_id IS NOT DISTINCT FROM %s",
                (target, now_utc, now_utc, *self._claim_fence(claim)),
            )
            return cur.rowcount == 1

    def discard(
        self,
        claim: RepairClaim,
        *,
        now_utc: datetime,
        reason: str,
    ) -> bool:
        del reason
        with db.cursor() as cur:
            owned = self._lock_owned(cur, claim)
            if owned is None:
                return False
            if self._promote_successor(
                cur,
                claim,
                owned,
                now_utc=now_utc,
            ):
                return True
            cur.execute(
                "UPDATE attendance_department_repairs SET "
                "status = 'complete', updated_at = %s, last_error = NULL "
                "WHERE odoo_attendance_id = %s AND status = 'applying' "
                "AND attempt_count = %s AND expected_write_date = %s "
                "AND target_odoo_department_id = %s "
                "AND expected_odoo_work_center_id IS NOT DISTINCT FROM %s",
                (now_utc, *self._claim_fence(claim)),
            )
            return cur.rowcount == 1

    def finish_verified(
        self,
        claim: RepairClaim,
        row: Mapping[str, object],
        *,
        now_utc: datetime,
    ) -> bool:
        normalized = attendance_mirror._normalize_row(row)  # noqa: SLF001
        if normalized["odoo_attendance_id"] != claim.attendance_id:
            raise ValueError("verified row does not match the repair claim")
        with db.cursor() as cur:
            attendance_location_policy.lock_rollout_decision_cur(cur)
            owned = self._lock_owned(cur, claim)
            if owned is None:
                return False
            sync_state = attendance_mirror._locked_sync_state(cur)  # noqa: SLF001
            if not self._verified_row_is_current(cur, claim, normalized):
                return False
            _upsert_verified_cur(
                cur,
                normalized,
                now_utc=now_utc,
                sync_state=sync_state,
            )
            if self._promote_successor(
                cur,
                claim,
                owned,
                now_utc=now_utc,
                observed_row=normalized,
            ):
                return True
            cur.execute(
                "UPDATE attendance_department_repairs SET "
                "expected_write_date = %s, status = 'complete', "
                "updated_at = %s, last_error = NULL "
                "WHERE odoo_attendance_id = %s AND status = 'applying' "
                "AND attempt_count = %s AND expected_write_date = %s "
                "AND target_odoo_department_id = %s "
                "AND expected_odoo_work_center_id IS NOT DISTINCT FROM %s",
                (
                    normalized["odoo_write_date"],
                    now_utc,
                    *self._claim_fence(claim),
                ),
            )
            return cur.rowcount == 1

    def retry_or_fail(
        self,
        claim: RepairClaim,
        error: str,
        *,
        now_utc: datetime,
        current_row: Mapping[str, object] | None = None,
    ) -> bool | None:
        normalized = (
            attendance_mirror._normalize_row(current_row)  # noqa: SLF001
            if current_row is not None
            else None
        )
        failed = claim.attempt_count >= MAX_ATTEMPTS
        with db.cursor() as cur:
            attendance_location_policy.lock_rollout_decision_cur(cur)
            owned = self._lock_owned(cur, claim)
            if owned is None:
                return None
            if normalized is not None:
                if normalized["odoo_attendance_id"] != claim.attendance_id:
                    raise ValueError("verified row does not match the repair claim")
                sync_state = attendance_mirror._locked_sync_state(cur)  # noqa: SLF001
                if not self._verified_row_is_current(cur, claim, normalized):
                    return None
                _upsert_verified_cur(
                    cur,
                    normalized,
                    now_utc=now_utc,
                    sync_state=sync_state,
                )
            if self._promote_successor(
                cur,
                claim,
                owned,
                now_utc=now_utc,
                observed_row=normalized,
            ):
                return None
            cur.execute(
                "UPDATE attendance_department_repairs SET "
                "expected_write_date = COALESCE(%s, expected_write_date), "
                "status = %s, updated_at = %s, last_error = %s "
                "WHERE odoo_attendance_id = %s AND status = 'applying' "
                "AND attempt_count = %s AND expected_write_date = %s "
                "AND target_odoo_department_id = %s "
                "AND expected_odoo_work_center_id IS NOT DISTINCT FROM %s",
                (
                    normalized["odoo_write_date"] if normalized is not None else None,
                    "failed" if failed else "pending",
                    now_utc,
                    _error_text(error),
                    *self._claim_fence(claim),
                ),
            )
            return failed if cur.rowcount == 1 else None


_facade = _OdooFacade()
_backend = _PostgresBackend()
_live_enabled = attendance_location_policy.live_is_active


def _candidate_for_span(
    span: attendance_timeline.LocationSpan,
    *,
    projected_at_utc: datetime,
) -> RepairCandidate | None:
    if (
        span.status != "valid"
        or span.odoo_work_center_id is None
        or span.app_work_center_name is None
        or span.department_repair is None
    ):
        return None
    attendance_id, target_department_id, expected_write_date = span.department_repair
    return RepairCandidate(
        attendance_id=attendance_id,
        expected_write_date=expected_write_date,
        target_department_id=target_department_id,
        expected_work_center_id=span.odoo_work_center_id,
        target_projected_at=projected_at_utc,
    )


def _candidates_from_spans(
    spans: Sequence[attendance_timeline.LocationSpan],
    *,
    projected_at_utc: datetime,
) -> tuple[RepairCandidate, ...] | None:
    if isinstance(spans, (str, bytes)) or not isinstance(spans, Sequence):
        raise TypeError("spans must be a sequence")
    projected_at = _aware_utc(projected_at_utc, "projected_at_utc")
    by_id: dict[int, RepairCandidate] = {}
    ambiguous: set[int] = set()
    for span in spans:
        if not isinstance(span, attendance_timeline.LocationSpan):
            raise TypeError("spans must contain LocationSpan values")
        covered_ids = {
            _positive_int(attendance_id, "span attendance id")
            for attendance_id in span.attendance_ids
        }
        candidate = _candidate_for_span(span, projected_at_utc=projected_at)
        if candidate is None:
            continue
        if candidate.attendance_id not in covered_ids:
            raise ValueError("department repair must identify a covered attendance row")
        existing = by_id.setdefault(candidate.attendance_id, candidate)
        if existing != candidate:
            ambiguous.add(candidate.attendance_id)
    if ambiguous:
        return None
    return tuple(by_id[attendance_id] for attendance_id in sorted(by_id))


def _enqueue_projected_spans(
    spans: Sequence[attendance_timeline.LocationSpan],
    *,
    projected_at_utc: datetime,
) -> int:
    """Queue one projection using its pre-enqueue observation order token."""
    projected_at = _aware_utc(projected_at_utc, "projected_at_utc")
    candidates = _candidates_from_spans(spans, projected_at_utc=projected_at)
    if not candidates:
        return 0
    return _backend.enqueue(candidates, now_utc=_now_utc(None))


def enqueue_from_spans(
    spans: Sequence[attendance_timeline.LocationSpan],
) -> int:
    """Queue one unambiguous candidate for each source attendance ID."""
    projected_at = _now_utc(None)
    candidates = _candidates_from_spans(spans, projected_at_utc=projected_at)
    if not candidates:
        return 0
    return _backend.enqueue(candidates, now_utc=projected_at)


def _read_one(attendance_id: int) -> dict[str, object] | None:
    raw_rows = _facade.fetch_attendance_rows_by_ids([attendance_id])
    if isinstance(raw_rows, (str, bytes)) or not isinstance(raw_rows, Sequence):
        raise RuntimeError("Odoo attendance reread must be a sequence")
    if len(raw_rows) > 1:
        raise RuntimeError("Odoo attendance reread returned duplicate identity")
    if not raw_rows:
        return None
    row = attendance_mirror._normalize_row(raw_rows[0])  # noqa: SLF001
    if row["odoo_attendance_id"] != attendance_id:
        raise RuntimeError("Odoo attendance reread returned unrelated identity")
    return row


def _result(
    claim: RepairClaim,
    outcome: RepairOutcome,
    error: str | None = None,
) -> RepairResult:
    return RepairResult(claim.attendance_id, outcome, claim.attempt_count, error)


def _discard(claim: RepairClaim, *, now_utc: datetime, reason: str) -> RepairResult:
    if not _backend.discard(claim, now_utc=now_utc, reason=reason):
        return _result(claim, "superseded")
    return _result(claim, "discarded")


def _fail(
    claim: RepairClaim,
    error: object,
    *,
    now_utc: datetime,
    current_row: Mapping[str, object] | None = None,
) -> RepairResult:
    message = _error_text(error)
    failed = _backend.retry_or_fail(
        claim,
        message,
        now_utc=now_utc,
        current_row=current_row,
    )
    if failed is None:
        return _result(claim, "superseded")
    return _result(claim, "failed" if failed else "retrying", message)


def _finish(
    claim: RepairClaim,
    row: Mapping[str, object],
    *,
    now_utc: datetime,
    outcome: Literal["repaired", "adopted_timeout", "already_correct"],
) -> RepairResult:
    if not _backend.finish_verified(claim, row, now_utc=now_utc):
        return _result(claim, "superseded")
    return _result(claim, outcome)


def process_next(*, now_utc: datetime | None = None) -> RepairResult | None:
    """Advance at most one live repair through exact reread/write/verify."""
    now = _now_utc(now_utc)
    if not _live_enabled(now_utc=now):
        return None
    claim = _backend.claim_next(now_utc=now)
    if claim is None:
        return None
    if (
        claim.expected_work_center_id is None
        or claim.mirror_write_date != claim.expected_write_date
    ):
        return _discard(claim, now_utc=now, reason="projection_changed")

    try:
        current = _read_one(claim.attendance_id)
    except Exception as exc:  # noqa: BLE001 - durable retry owns source outages
        return _fail(claim, exc, now_utc=now)
    if current is None:
        return _fail(claim, "Odoo attendance row is missing", now_utc=now)
    if current["odoo_work_center_id"] != claim.expected_work_center_id:
        return _discard(claim, now_utc=now, reason="work_center_changed")
    try:
        fresh_target = _facade.target_department_id_for_work_center(
            claim.expected_work_center_id,
            force=True,
        )
        if fresh_target is None:
            raise RuntimeError("Odoo target department is unavailable")
        fresh_target = _positive_int(fresh_target, "target_department_id")
    except Exception as exc:  # noqa: BLE001 - remote resolver failure is retryable
        return _fail(claim, exc, now_utc=now)
    if fresh_target != claim.target_department_id:
        if not _backend.refresh_target(
            claim,
            fresh_target,
            now_utc=now,
        ):
            return _result(claim, "superseded")
        return _result(claim, "target_refreshed")
    if current["odoo_department_id"] == claim.target_department_id:
        return _finish(claim, current, now_utc=now, outcome="already_correct")
    if claim.attempt_count > MAX_ATTEMPTS:
        return _fail(
            claim,
            "Odoo department repair exceeded its maximum attempts",
            now_utc=now,
            current_row=current,
        )
    if current["odoo_write_date"] != claim.expected_write_date:
        if claim.attempt_count >= MAX_ATTEMPTS:
            return _fail(
                claim,
                "Odoo attendance source version kept changing during department repair",
                now_utc=now,
                current_row=current,
            )
        if not _backend.refresh_expected(claim, current, now_utc=now):
            return _result(claim, "superseded")
        return _result(claim, "version_refreshed")

    if not _backend.reserve_for_write(claim, now_utc=_wall_clock_utc()):
        return _result(claim, "superseded")
    try:
        _facade.set_attendance_department_id(
            claim.attendance_id,
            claim.target_department_id,
        )
    except Exception as write_error:  # noqa: BLE001 - transport outcome may be ambiguous
        try:
            after_error = _read_one(claim.attendance_id)
        except Exception as read_error:  # noqa: BLE001 - preserve both failure facts
            return _fail(
                claim,
                f"{_error_text(write_error)}; reread failed: {_error_text(read_error)}",
                now_utc=now,
            )
        if after_error is not None:
            if after_error["odoo_work_center_id"] != claim.expected_work_center_id:
                return _discard(claim, now_utc=now, reason="work_center_changed")
            if after_error["odoo_department_id"] == claim.target_department_id:
                return _finish(claim, after_error, now_utc=now, outcome="adopted_timeout")
        return _fail(claim, write_error, now_utc=now, current_row=after_error)

    try:
        verified = _read_one(claim.attendance_id)
    except Exception as exc:  # noqa: BLE001 - verification must be durable
        return _fail(claim, exc, now_utc=now)
    if verified is None:
        return _fail(
            claim,
            "Odoo attendance row disappeared after department repair",
            now_utc=now,
        )
    if verified["odoo_work_center_id"] != claim.expected_work_center_id:
        return _discard(claim, now_utc=now, reason="work_center_changed")
    if verified["odoo_department_id"] != claim.target_department_id:
        return _fail(
            claim,
            "Odoo department repair verification failed",
            now_utc=now,
            current_row=verified,
        )
    return _finish(claim, verified, now_utc=now, outcome="repaired")


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=shift_config.SITE_TZ)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=shift_config.SITE_TZ)
    return start.astimezone(UTC), end.astimezone(UTC)


def _projection_rows(
    *,
    affected_days: Sequence[date],
    attendance_ids: Sequence[int],
    now_utc: datetime,
    include_current_day: bool,
    include_baseline: bool,
) -> tuple[Mapping[str, object], ...]:
    if include_baseline:
        return tuple(
            attendance_mirror._utc_database_row(row)  # noqa: SLF001
            for row in db.query(
                "SELECT * FROM odoo_attendance_mirror WHERE deleted_at IS NULL "
                "AND (check_out_utc IS NULL OR check_out_utc > check_in_utc) "
                "ORDER BY check_in_utc, odoo_attendance_id"
            )
        )
    days = set(affected_days)
    if include_current_day:
        days.add(now_utc.astimezone(shift_config.SITE_TZ).date())
    by_id: dict[int, Mapping[str, object]] = {}
    exact_ids = sorted({_positive_int(value, "repair attendance id") for value in attendance_ids})
    if exact_ids:
        for row in db.query(
            "SELECT * FROM odoo_attendance_mirror WHERE deleted_at IS NULL "
            "AND (check_out_utc IS NULL OR check_out_utc > check_in_utc) "
            "AND odoo_attendance_id = ANY(%s) ORDER BY odoo_attendance_id",
            (exact_ids,),
        ):
            normalized = attendance_mirror._utc_database_row(row)  # noqa: SLF001
            by_id[int(normalized["odoo_attendance_id"])] = normalized
    for day in sorted(days):
        start, end = _day_bounds(day)
        for row in attendance_mirror.rows_overlapping(start, end):
            by_id[int(row["odoo_attendance_id"])] = row
    return tuple(by_id[key] for key in sorted(by_id))


def enqueue_after_successful_sync(
    *,
    affected_days: Sequence[date],
    attendance_ids: Sequence[int] = (),
    now_utc: datetime,
    include_current_day: bool,
    include_baseline: bool = False,
) -> int:
    """Project committed mirror truth and enqueue its safe repair candidates."""
    now = _now_utc(now_utc)
    raw_rows = _projection_rows(
        affected_days=affected_days,
        attendance_ids=attendance_ids,
        now_utc=now,
        include_current_day=include_current_day,
        include_baseline=include_baseline,
    )
    if not raw_rows:
        return 0
    rows = attendance_timeline._rows_with_employee_department_fallback(  # noqa: SLF001
        raw_rows
    )
    health = attendance_mirror.health_snapshot()
    verified = health.last_incremental_completed_at or health.baseline_completed_at or now
    department_cache: dict[str, int | None] = {}

    def expected_department_id(app_work_center_name: str) -> int | None:
        if app_work_center_name not in department_cache:
            odoo_work_center_id = work_centers_store.odoo_work_center_id_for(app_work_center_name)
            if odoo_work_center_id is None:
                raise RuntimeError("saved Odoo work-center mapping is unavailable")
            resolved = _facade.target_department_id_for_work_center(
                odoo_work_center_id,
                force=True,
            )
            if resolved is None:
                raise RuntimeError("Odoo target department is unavailable or ambiguous")
            department_cache[app_work_center_name] = _positive_int(
                resolved,
                "target_department_id",
            )
        return department_cache[app_work_center_name]

    spans = attendance_timeline.project_rows(
        rows,
        as_of_utc=now,
        verified_through_utc=verified,
        map_work_center=work_centers_store.app_work_center_name_for_odoo_id,
        requires_work_center=(
            attendance_timeline._department_requires_work_center_for_mirror  # noqa: SLF001
        ),
        expected_department_id=expected_department_id,
    )
    candidates = _candidates_from_spans(spans, projected_at_utc=now)
    if candidates is None:
        return 0
    raw_by_id = {int(row["odoo_attendance_id"]): row for row in raw_rows}
    proofs_by_id: dict[int, RepairProjectionProof] = {}
    for span in spans:
        if (
            span.status != "valid"
            or span.odoo_work_center_id is None
            or span.app_work_center_name is None
        ):
            continue
        target_department_id = expected_department_id(span.app_work_center_name)
        if target_department_id is None:
            raise RuntimeError("Odoo target department is unavailable or ambiguous")
        for attendance_id in span.attendance_ids:
            raw = raw_by_id.get(attendance_id)
            if raw is None or raw["odoo_work_center_id"] != span.odoo_work_center_id:
                continue
            proof = RepairProjectionProof(
                attendance_id=attendance_id,
                write_date=raw["odoo_write_date"],
                work_center_id=span.odoo_work_center_id,
                department_id=raw["odoo_department_id"],
                target_department_id=target_department_id,
                target_projected_at=now,
            )
            existing = proofs_by_id.setdefault(attendance_id, proof)
            if existing != proof:
                return 0
    candidate_ids = {candidate.attendance_id for candidate in candidates}
    if not candidate_ids <= proofs_by_id.keys():
        return 0
    for attendance_id, proof in tuple(proofs_by_id.items()):
        if attendance_id not in candidate_ids and proof.department_id != proof.target_department_id:
            del proofs_by_id[attendance_id]
    if not proofs_by_id:
        return 0
    return _backend.reconcile(
        candidates,
        tuple(proofs_by_id[key] for key in sorted(proofs_by_id)),
        now_utc=now,
    )


__all__ = [
    "RepairResult",
    "enqueue_from_spans",
    "process_next",
]
