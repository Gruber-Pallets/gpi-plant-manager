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
    "discarded",
    "retrying",
    "failed",
]

MAX_ATTEMPTS = 3
_CLAIM_TIMEOUT = timedelta(minutes=5)
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


def _error_text(error: object) -> str:
    return (str(error) or type(error).__name__)[:_ERROR_LIMIT]


@dataclass(frozen=True)
class RepairCandidate:
    attendance_id: int
    expected_write_date: datetime
    target_department_id: int
    expected_work_center_id: int

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

    def set_attendance_department_id(
        self,
        attendance_id: int,
        department_id: int,
    ) -> None:
        odoo_client.set_attendance_department_id(attendance_id, department_id)

    def target_department_id_for_app_work_center(
        self,
        app_work_center_name: str,
    ) -> int | None:
        # This is a read-only Odoo lookup cached by the shared facade. Writes
        # remain isolated to ``set_attendance_department_id`` above.
        return odoo_client._department_id_for_wc(app_work_center_name)  # noqa: SLF001


def _upsert_verified_cur(cur, row: Mapping[str, object], *, now_utc: datetime) -> None:
    normalized = attendance_mirror._normalized_rows(  # noqa: SLF001
        (
            {
                **row,
                "employee_name": row.get("employee_name"),
                "odoo_work_center_name": row.get("odoo_work_center_name"),
                "odoo_department_name": row.get("odoo_department_name"),
            },
        )
    )
    state = attendance_mirror._locked_sync_state(cur)  # noqa: SLF001
    attendance_mirror._upsert_rows_cur(  # noqa: SLF001
        cur,
        normalized,
        sync_completed_at=now_utc,
        observed_at=now_utc,
        baseline_completed=state["baseline_completed_at"] is not None,
    )


class _PostgresBackend:
    def enqueue(
        self,
        candidates: Sequence[RepairCandidate],
        *,
        now_utc: datetime,
    ) -> int:
        changed = 0
        with db.cursor() as cur:
            for candidate in candidates:
                cur.execute(
                    "INSERT INTO attendance_department_repairs "
                    "(odoo_attendance_id, expected_write_date, "
                    "target_odoo_department_id, status, attempt_count, "
                    "updated_at, last_error) "
                    "VALUES (%s, %s, %s, 'pending', 0, %s, NULL) "
                    "ON CONFLICT (odoo_attendance_id) DO UPDATE SET "
                    "expected_write_date = EXCLUDED.expected_write_date, "
                    "target_odoo_department_id = EXCLUDED.target_odoo_department_id, "
                    "status = 'pending', attempt_count = 0, "
                    "updated_at = EXCLUDED.updated_at, last_error = NULL "
                    "WHERE attendance_department_repairs.status <> 'applying' "
                    "AND (attendance_department_repairs.expected_write_date "
                    "IS DISTINCT FROM EXCLUDED.expected_write_date "
                    "OR attendance_department_repairs.target_odoo_department_id "
                    "IS DISTINCT FROM EXCLUDED.target_odoo_department_id) "
                    "RETURNING odoo_attendance_id",
                    (
                        candidate.attendance_id,
                        candidate.expected_write_date,
                        candidate.target_department_id,
                        now_utc,
                    ),
                )
                if cur.fetchone() is not None:
                    changed += 1
        return changed

    def claim_next(self, *, now_utc: datetime) -> RepairClaim | None:
        stale_before = now_utc - _CLAIM_TIMEOUT
        with db.cursor() as cur:
            cur.execute(
                "SELECT r.odoo_attendance_id, r.expected_write_date, "
                "r.target_odoo_department_id, r.attempt_count, "
                "CASE WHEN m.deleted_at IS NULL THEN m.odoo_work_center_id END "
                "AS expected_work_center_id, "
                "CASE WHEN m.deleted_at IS NULL THEN m.odoo_write_date END "
                "AS mirror_write_date "
                "FROM attendance_department_repairs r "
                "LEFT JOIN odoo_attendance_mirror m "
                "ON m.odoo_attendance_id = r.odoo_attendance_id "
                "WHERE r.status = 'pending' "
                "OR (r.status = 'applying' AND r.updated_at <= %s) "
                "ORDER BY r.updated_at, r.odoo_attendance_id "
                "FOR UPDATE OF r SKIP LOCKED LIMIT 1",
                (stale_before,),
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
                int(row["expected_work_center_id"])
                if row["expected_work_center_id"] is not None
                else None
            ),
            mirror_write_date=row["mirror_write_date"],
            attempt_count=attempt_count,
        )

    @staticmethod
    def _lock_owned(cur, claim: RepairClaim) -> bool:
        cur.execute(
            "SELECT status, attempt_count FROM attendance_department_repairs "
            "WHERE odoo_attendance_id = %s FOR UPDATE",
            (claim.attendance_id,),
        )
        row = cur.fetchone()
        return bool(
            row is not None
            and row["status"] == "applying"
            and int(row["attempt_count"]) == claim.attempt_count
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
            if not self._lock_owned(cur, claim):
                return False
            _upsert_verified_cur(cur, normalized, now_utc=now_utc)
            cur.execute(
                "UPDATE attendance_department_repairs SET "
                "expected_write_date = %s, status = 'pending', "
                "attempt_count = 0, updated_at = %s, last_error = NULL "
                "WHERE odoo_attendance_id = %s AND status = 'applying' "
                "AND attempt_count = %s",
                (
                    normalized["odoo_write_date"],
                    now_utc,
                    claim.attendance_id,
                    claim.attempt_count,
                ),
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
            if not self._lock_owned(cur, claim):
                return False
            cur.execute(
                "UPDATE attendance_department_repairs SET "
                "status = 'complete', updated_at = %s, last_error = NULL "
                "WHERE odoo_attendance_id = %s AND status = 'applying' "
                "AND attempt_count = %s",
                (now_utc, claim.attendance_id, claim.attempt_count),
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
            if not self._lock_owned(cur, claim):
                return False
            _upsert_verified_cur(cur, normalized, now_utc=now_utc)
            cur.execute(
                "UPDATE attendance_department_repairs SET "
                "expected_write_date = %s, status = 'complete', "
                "updated_at = %s, last_error = NULL "
                "WHERE odoo_attendance_id = %s AND status = 'applying' "
                "AND attempt_count = %s",
                (
                    normalized["odoo_write_date"],
                    now_utc,
                    claim.attendance_id,
                    claim.attempt_count,
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
    ) -> bool:
        normalized = (
            attendance_mirror._normalize_row(current_row)  # noqa: SLF001
            if current_row is not None
            else None
        )
        failed = claim.attempt_count >= MAX_ATTEMPTS
        with db.cursor() as cur:
            if not self._lock_owned(cur, claim):
                return failed
            if normalized is not None:
                if normalized["odoo_attendance_id"] != claim.attendance_id:
                    raise ValueError("verified row does not match the repair claim")
                _upsert_verified_cur(cur, normalized, now_utc=now_utc)
            cur.execute(
                "UPDATE attendance_department_repairs SET "
                "expected_write_date = COALESCE(%s, expected_write_date), "
                "status = %s, updated_at = %s, last_error = %s "
                "WHERE odoo_attendance_id = %s AND status = 'applying' "
                "AND attempt_count = %s",
                (
                    normalized["odoo_write_date"] if normalized is not None else None,
                    "failed" if failed else "pending",
                    now_utc,
                    _error_text(error),
                    claim.attendance_id,
                    claim.attempt_count,
                ),
            )
        return failed


_facade = _OdooFacade()
_backend = _PostgresBackend()
_live_enabled = attendance_location_policy.live_is_active


def _candidate_for_span(
    span: attendance_timeline.LocationSpan,
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
    )


def enqueue_from_spans(
    spans: Sequence[attendance_timeline.LocationSpan],
) -> int:
    """Queue each unambiguous, valid projected mismatch at most once."""
    if isinstance(spans, (str, bytes)) or not isinstance(spans, Sequence):
        raise TypeError("spans must be a sequence")
    by_id: dict[int, RepairCandidate] = {}
    ambiguous: set[int] = set()
    for span in spans:
        if not isinstance(span, attendance_timeline.LocationSpan):
            raise TypeError("spans must contain LocationSpan values")
        candidate = _candidate_for_span(span)
        if candidate is None:
            continue
        existing = by_id.setdefault(candidate.attendance_id, candidate)
        if existing != candidate:
            ambiguous.add(candidate.attendance_id)
    candidates = tuple(
        by_id[attendance_id] for attendance_id in sorted(by_id) if attendance_id not in ambiguous
    )
    if not candidates:
        return 0
    return _backend.enqueue(candidates, now_utc=_now_utc(None))


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
    return RepairResult(
        attendance_id=claim.attendance_id,
        outcome=outcome,
        attempt_count=claim.attempt_count,
        error=error,
    )


def _discard(
    claim: RepairClaim,
    *,
    now_utc: datetime,
    reason: str,
) -> RepairResult | None:
    if not _backend.discard(claim, now_utc=now_utc, reason=reason):
        return None
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
    return _result(claim, "failed" if failed else "retrying", message)


def _finish(
    claim: RepairClaim,
    row: Mapping[str, object],
    *,
    now_utc: datetime,
    outcome: Literal["repaired", "adopted_timeout", "already_correct"],
) -> RepairResult | None:
    if not _backend.finish_verified(claim, row, now_utc=now_utc):
        return None
    return _result(claim, outcome)


def process_next(*, now_utc: datetime | None = None) -> RepairResult | None:
    """Advance at most one live-mode repair through reread, write, and verify."""
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
    if current["odoo_department_id"] == claim.target_department_id:
        return _finish(
            claim,
            current,
            now_utc=now,
            outcome="already_correct",
        )
    if current["odoo_write_date"] != claim.expected_write_date:
        if not _backend.refresh_expected(claim, current, now_utc=now):
            return None
        return _result(claim, "version_refreshed")

    try:
        _facade.set_attendance_department_id(
            claim.attendance_id,
            claim.target_department_id,
        )
    except Exception as write_error:  # noqa: BLE001 - timeout outcome is ambiguous
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
                return _finish(
                    claim,
                    after_error,
                    now_utc=now,
                    outcome="adopted_timeout",
                )
        return _fail(
            claim,
            write_error,
            now_utc=now,
            current_row=after_error,
        )

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


def _spans_for_day(day: date, *, now_utc: datetime) -> tuple[attendance_timeline.LocationSpan, ...]:
    start, end = _day_bounds(day)
    as_of = min(max(now_utc, start), end)
    if as_of <= start:
        return ()
    rows = attendance_mirror.rows_overlapping(start, end)
    if not rows:
        return ()
    return attendance_timeline.project_rows(
        rows,
        as_of_utc=as_of,
        verified_through_utc=now_utc,
        map_work_center=work_centers_store.app_work_center_name_for_odoo_id,
        requires_work_center=(
            attendance_timeline._department_requires_work_center_for_mirror  # noqa: SLF001
        ),
        expected_department_id=_facade.target_department_id_for_app_work_center,
    )


def enqueue_after_successful_sync(
    *,
    affected_days: Sequence[date],
    now_utc: datetime,
    include_current_day: bool,
) -> int:
    """Project committed mirror days and enqueue only safe repair candidates."""
    now = _now_utc(now_utc)
    days = set(affected_days)
    if include_current_day:
        days.add(now.astimezone(shift_config.SITE_TZ).date())
    queued = 0
    for day in sorted(days):
        try:
            queued += enqueue_from_spans(_spans_for_day(day, now_utc=now))
        except Exception:  # noqa: BLE001 - a repair scan cannot invalidate a good sync
            _log.exception("could not project Odoo attendance department repairs for %s", day)
    return queued


__all__ = [
    "RepairResult",
    "enqueue_from_spans",
    "process_next",
]
