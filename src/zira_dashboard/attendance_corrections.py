"""Pure, deterministic planning for exact Odoo attendance corrections.

This module deliberately performs no I/O.  It turns one employee's complete,
fresh attendance snapshot into an immutable plan that a durable executor can
apply and verify later.
"""

from __future__ import annotations

import base64
import binascii
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
import math
import re
import secrets
from types import MappingProxyType
import logging
from typing import Any, Literal, TypeAlias


OperationKind: TypeAlias = Literal["create", "update", "delete"]
JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]

_SCHEMA_VERSION = 2
_KEY_PREFIX = "attendance-correction-v2:"
_KEY_PATTERN = re.compile(
    r"^attendance-correction-v2:(0|[1-9][0-9]*):"
    r"([A-Za-z0-9_-]+):([0-9a-f]{64})$"
)
_INTEGRITY_PREFIX = "attendance-correction-plan-v1:"
_INTEGRITY_PATTERN = re.compile(r"^attendance-correction-plan-v1:[0-9a-f]{64}$")
_UTC_TEXT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z$")
_RESERVATION_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_KINDS = frozenset(("create", "update", "delete"))
_MUTABLE_FIELDS = (
    "employee_odoo_id",
    "check_in_utc",
    "check_out_utc",
    "odoo_work_center_id",
    "odoo_department_id",
)
_SOURCE_REQUIRED_FIELDS = (
    "employee_odoo_id",
    "check_in_utc",
    "check_out_utc",
    "odoo_work_center_id",
    "odoo_department_id",
)
_EXPECTED_REQUIRED_FIELDS = ("odoo_attendance_id", *_MUTABLE_FIELDS)

_JOB_STATUSES = frozenset(
    ("planned", "applying", "verifying", "recalculating", "complete", "failed")
)
_ACTIVE_JOB_STATUSES = frozenset(("planned", "applying", "verifying", "recalculating"))
_CLAIM_LEASE = timedelta(minutes=15)
_ERROR_LIMIT = 500
_TEXT_LIMIT = 200
_ITEM_KEY_LIMIT = 500
_ACTOR_LIMIT = 320
_MAX_EMPLOYEES = 100
_MAX_OPERATIONS = 1000
_MAX_EVENT_IDS = 100
_MAX_RECALC_HORIZON_DAYS = 500
_EVENT_DETAIL_FIELDS = frozenset(
    (
        "job_id",
        "operation_key",
        "operation_kind",
        "attendance_id",
        "employee_odoo_id",
        "work_center_id",
        "recalc_ids",
        "attendance_ids",
        "employee_ids",
        "operation_keys",
        "reason_code",
        "attempt_count",
        "verification_failure_count",
    )
)
_EVENT_OUTCOMES = frozenset(
    (
        ("planning", "created"),
        ("planning", "horizon_frozen"),
        ("planning", "horizon_failed"),
        ("planning", "invalid_plan"),
        ("claim", "claimed"),
        ("applying", "reserved"),
        ("applying", "source_changed"),
        ("applying", "odoo_failure"),
        ("applying", "confirmed"),
        ("applying", "adopted"),
        ("applying", "adopted_timeout"),
        ("applying", "operations_complete"),
        ("verifying", "mismatch"),
        ("verifying", "verified"),
        ("verifying", "odoo_failure"),
        ("mirror", "failed"),
        ("mirror", "complete"),
        ("recalculation", "enqueued"),
        ("recalculation", "failed"),
        ("recalculation", "complete"),
        ("cache", "failed"),
        ("cache", "complete"),
        ("audit", "failed"),
        ("audit", "complete"),
        ("completion", "complete"),
    )
)
_SOURCE_SNAPSHOT_SCHEMA = 1
_PLANS_WRAPPER_SCHEMA = 2
_SOURCE_INTEGRITY_PREFIX = "attendance-correction-source-v1:"
_MIRROR_DISPLAY_FIELDS = (
    "employee_name",
    "odoo_work_center_name",
    "odoo_department_name",
)
_log = logging.getLogger(__name__)


class _FrozenMapping(Mapping[str, object]):
    """Small immutable mapping used by the public frozen values."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, object]) -> None:
        if not isinstance(values, Mapping):
            raise TypeError("value must be a mapping")
        normalized: dict[str, object] = {}
        keys = tuple(values)
        if not all(isinstance(key, str) for key in keys):
            raise TypeError("mapping keys must be text")
        for key in sorted(keys):
            if not isinstance(key, str):
                raise TypeError("mapping keys must be text")
            normalized[key] = _freeze_value(values[key])
        object.__setattr__(self, "_values", MappingProxyType(normalized))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("frozen mapping values cannot be reassigned")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("frozen mapping values cannot be deleted")

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return repr(self._values)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Mapping):
            return False
        return dict(self.items()) == dict(other.items())


def _freeze_value(value: object) -> object:
    if isinstance(value, datetime):
        return _aware_utc(value, "datetime value")
    if isinstance(value, Mapping):
        return _FrozenMapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite numbers are not valid JSON values")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"unsupported immutable plan value: {type(value).__name__}")


def _freeze_mapping(value: Mapping[str, object] | None) -> _FrozenMapping | None:
    if value is None:
        return None
    return _FrozenMapping(value)


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _optional_positive_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field_name)


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


@dataclass(frozen=True)
class SourceVersion:
    attendance_id: int
    write_date: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "attendance_id",
            _positive_int(self.attendance_id, "attendance_id"),
        )
        object.__setattr__(
            self,
            "write_date",
            _aware_utc(self.write_date, "write_date"),
        )


@dataclass(frozen=True)
class CorrectionOperation:
    key: str
    kind: OperationKind
    attendance_id: int | None
    employee_odoo_id: int
    before: Mapping[str, object] | None
    after: Mapping[str, object] | None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not _KEY_PATTERN.fullmatch(self.key):
            raise ValueError("operation key is not a canonical correction key")
        if self.kind not in _KINDS:
            raise ValueError(f"invalid correction operation kind: {self.kind!r}")
        attendance_id = _optional_positive_int(self.attendance_id, "attendance_id")
        employee_id = _positive_int(self.employee_odoo_id, "employee_odoo_id")
        before = _freeze_mapping(self.before)
        after = _freeze_mapping(self.after)
        if self.kind == "create":
            if attendance_id is not None or before is not None or after is None:
                raise ValueError("create operations require only an after value")
        elif self.kind == "update":
            if attendance_id is None or before is None or after is None:
                raise ValueError("update operations require an id, before, and after")
            if not before or not after or set(before) != set(after):
                raise ValueError("update before and after fields must match")
        elif attendance_id is None or before is None or after is not None:
            raise ValueError("delete operations require only an id and before value")
        object.__setattr__(self, "attendance_id", attendance_id)
        object.__setattr__(self, "employee_odoo_id", employee_id)
        object.__setattr__(self, "before", before)
        object.__setattr__(self, "after", after)
        _validate_operation_mapping(self)


@dataclass(frozen=True)
class CorrectionPlan:
    source_versions: tuple[SourceVersion, ...]
    operations: tuple[CorrectionOperation, ...]
    expected_intervals: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        if isinstance(self.source_versions, (str, bytes)) or not isinstance(
            self.source_versions, Sequence
        ):
            raise TypeError("source_versions must be a sequence")
        if isinstance(self.operations, (str, bytes)) or not isinstance(self.operations, Sequence):
            raise TypeError("operations must be a sequence")
        if isinstance(self.expected_intervals, (str, bytes)) or not isinstance(
            self.expected_intervals, Sequence
        ):
            raise TypeError("expected_intervals must be a sequence")
        versions = tuple(self.source_versions)
        operations = tuple(self.operations)
        if not all(isinstance(item, SourceVersion) for item in versions):
            raise TypeError("source_versions must contain SourceVersion values")
        if not all(isinstance(item, CorrectionOperation) for item in operations):
            raise TypeError("operations must contain CorrectionOperation values")
        intervals = tuple(_FrozenMapping(item) for item in self.expected_intervals)
        object.__setattr__(self, "source_versions", versions)
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "expected_intervals", intervals)
        _validate_plan(self)


@dataclass(frozen=True)
class _SourceRow:
    attendance_id: int
    employee_id: int
    start: datetime
    end: datetime | None
    work_center_id: int | None
    department_id: int | None
    write_date: datetime
    values: Mapping[str, object]


@dataclass(frozen=True)
class _Piece:
    start: datetime
    end: datetime | None
    work_center_id: int | None
    department_id: int | None
    attendance_id: int | None
    source: _SourceRow | None
    target: bool


@dataclass(frozen=True)
class _PendingOperation:
    kind: OperationKind
    attendance_id: int | None
    employee_id: int
    before: Mapping[str, object] | None
    after: Mapping[str, object] | None
    source: _SourceRow | None
    effective_start: datetime


def _aliased_required(row: Mapping[str, object], canonical: str, alias: str) -> object:
    has_canonical = canonical in row
    has_alias = alias in row
    if not has_canonical and not has_alias:
        raise ValueError(f"attendance row omitted required {canonical}")
    canonical_value = row.get(canonical)
    alias_value = row.get(alias)
    if has_canonical and has_alias and canonical_value != alias_value:
        raise ValueError(f"attendance row {canonical}/{alias} mismatch")
    return canonical_value if has_canonical else alias_value


def _normalize_source_row(raw: Mapping[str, object], employee_id: int) -> _SourceRow:
    if not isinstance(raw, Mapping):
        raise TypeError("attendance row must be a mapping")
    missing = [field for field in _SOURCE_REQUIRED_FIELDS if field not in raw]
    if missing:
        raise ValueError("attendance row omitted required field(s): " + ", ".join(missing))
    attendance_id = _positive_int(
        _aliased_required(raw, "odoo_attendance_id", "id"),
        "odoo_attendance_id",
    )
    if "odoo_attendance_id" in raw and "id" in raw:
        alias_attendance_id = _positive_int(raw["id"], "id")
        if alias_attendance_id != attendance_id:
            raise ValueError("attendance row odoo_attendance_id/id mismatch")
    write_date = _aware_utc(
        _aliased_required(raw, "odoo_write_date", "write_date"),
        "odoo_write_date",
    )
    if "odoo_write_date" in raw and "write_date" in raw:
        alias_write_date = _aware_utc(raw["write_date"], "write_date")
        if alias_write_date != write_date:
            raise ValueError("attendance row odoo_write_date/write_date mismatch")
    row_employee_id = _positive_int(raw["employee_odoo_id"], "employee_odoo_id")
    if row_employee_id != employee_id:
        raise ValueError("attendance row employee does not match selected employee")
    if "employee_id" in raw:
        alias = raw["employee_id"]
        if isinstance(alias, (list, tuple)):
            if not alias:
                raise ValueError("attendance row employee_id alias is empty")
            alias = alias[0]
        if _positive_int(alias, "employee_id") != row_employee_id:
            raise ValueError("attendance row employee identity aliases mismatch")
    start = _aware_utc(raw["check_in_utc"], "check_in_utc")
    end = _optional_aware_utc(raw["check_out_utc"], "check_out_utc")
    if end is not None and end <= start:
        raise ValueError("attendance source interval must have positive duration")
    work_center_id = _optional_positive_int(raw["odoo_work_center_id"], "odoo_work_center_id")
    department_id = _optional_positive_int(raw["odoo_department_id"], "odoo_department_id")
    if raw.get("deleted_at") is not None:
        raise ValueError("deleted attendance rows cannot be corrected")
    values = dict(raw)
    values.pop("id", None)
    values.pop("write_date", None)
    values["odoo_attendance_id"] = attendance_id
    values["odoo_write_date"] = write_date
    values["employee_odoo_id"] = row_employee_id
    values["check_in_utc"] = start
    values["check_out_utc"] = end
    values["odoo_work_center_id"] = work_center_id
    values["odoo_department_id"] = department_id
    return _SourceRow(
        attendance_id=attendance_id,
        employee_id=row_employee_id,
        start=start,
        end=end,
        work_center_id=work_center_id,
        department_id=department_id,
        write_date=write_date,
        values=_FrozenMapping(values),
    )


def _normalize_source_rows(
    rows: Sequence[Mapping[str, object]], employee_id: int
) -> tuple[_SourceRow, ...]:
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise TypeError("rows must be a sequence")
    normalized = sorted(
        (_normalize_source_row(row, employee_id) for row in rows),
        key=lambda item: (item.start, item.attendance_id),
    )
    seen_ids: set[int] = set()
    previous: _SourceRow | None = None
    for item in normalized:
        if item.attendance_id in seen_ids:
            raise ValueError("duplicate attendance source id")
        seen_ids.add(item.attendance_id)
        if previous is not None:
            if previous.end is None or previous.end > item.start:
                raise ValueError("attendance source intervals overlap")
        previous = item
    return tuple(normalized)


def _piece_from_source(
    source: _SourceRow,
    *,
    start: datetime,
    end: datetime | None,
    work_center_id: int | None,
    department_id: int | None,
    attendance_id: int | None,
    target: bool,
) -> _Piece:
    return _Piece(
        start=start,
        end=end,
        work_center_id=work_center_id,
        department_id=department_id,
        attendance_id=attendance_id,
        source=source,
        target=target,
    )


def _untouched_piece(source: _SourceRow) -> _Piece:
    return _piece_from_source(
        source,
        start=source.start,
        end=source.end,
        work_center_id=source.work_center_id,
        department_id=source.department_id,
        attendance_id=source.attendance_id,
        target=False,
    )


def _target_piece(
    source: _SourceRow | None,
    start: datetime,
    end: datetime | None,
    work_center_id: int,
    department_id: int | None,
    *,
    reuse_source_id: bool,
) -> _Piece:
    return _Piece(
        start=start,
        end=end,
        work_center_id=work_center_id,
        department_id=department_id,
        attendance_id=(source.attendance_id if source is not None and reuse_source_id else None),
        source=source,
        target=True,
    )


def _closed_pieces_and_groups(
    sources: tuple[_SourceRow, ...],
    start: datetime,
    end: datetime,
    work_center_id: int,
    department_id: int | None,
) -> tuple[list[_Piece], frozenset[int]]:
    overlaps = [
        item for item in sources if item.start < end and (item.end is None or item.end > start)
    ]
    if not overlaps:
        return [
            *map(_untouched_piece, sources),
            _target_piece(
                None,
                start,
                end,
                work_center_id,
                department_id,
                reuse_source_id=False,
            ),
        ], frozenset()

    grouped: list[list[_SourceRow]] = []
    for item in overlaps:
        if grouped and grouped[-1][-1].end == item.start:
            grouped[-1].append(item)
        else:
            grouped.append([item])
    affected_ids = {item.attendance_id for item in overlaps}
    pieces = [_untouched_piece(item) for item in sources if item.attendance_id not in affected_ids]
    for group in grouped:
        first = group[0]
        last = group[-1]
        target_start = max(start, first.start)
        last_end = last.end if last.end is not None else end
        target_end = min(end, last_end)
        fully_covered = [
            item
            for item in group
            if item.start >= target_start and item.end is not None and item.end <= target_end
        ]
        reuse = fully_covered[0] if fully_covered else None
        shoulders: list[_Piece] = []
        if first.start < target_start:
            shoulders.append(
                _piece_from_source(
                    first,
                    start=first.start,
                    end=target_start,
                    work_center_id=first.work_center_id,
                    department_id=first.department_id,
                    attendance_id=first.attendance_id,
                    target=False,
                )
            )
        if last.end is None or last.end > target_end:
            left_already_reused = (
                first.attendance_id == last.attendance_id and first.start < target_start
            )
            shoulders.append(
                _piece_from_source(
                    last,
                    start=target_end,
                    end=last.end,
                    work_center_id=last.work_center_id,
                    department_id=last.department_id,
                    attendance_id=(None if left_already_reused else last.attendance_id),
                    target=False,
                )
            )
        pieces.extend(shoulders)
        pieces.append(
            _target_piece(
                reuse if reuse is not None else first,
                target_start,
                target_end,
                work_center_id,
                department_id,
                reuse_source_id=reuse is not None,
            )
        )
    return pieces, frozenset(affected_ids)


def _open_pieces_and_groups(
    sources: tuple[_SourceRow, ...],
    start: datetime,
    work_center_id: int,
    department_id: int | None,
) -> tuple[list[_Piece], frozenset[int]]:
    affected = [item for item in sources if item.end is None or item.end > start]
    unaffected = [item for item in sources if item.end is not None and item.end <= start]
    if not affected:
        return [
            *map(_untouched_piece, sources),
            _target_piece(
                None,
                start,
                None,
                work_center_id,
                department_id,
                reuse_source_id=False,
            ),
        ], frozenset()
    boundary = affected[0] if affected[0].start < start else None
    fully_covered = [item for item in affected if item.start >= start]
    reuse = fully_covered[0] if fully_covered else None
    shoulders: list[_Piece] = []
    if boundary is not None:
        shoulders.append(
            _piece_from_source(
                boundary,
                start=boundary.start,
                end=start,
                work_center_id=boundary.work_center_id,
                department_id=boundary.department_id,
                attendance_id=boundary.attendance_id,
                target=False,
            )
        )
    pieces = [*map(_untouched_piece, unaffected), *shoulders]
    pieces.append(
        _target_piece(
            reuse if reuse is not None else boundary,
            start,
            None,
            work_center_id,
            department_id,
            reuse_source_id=reuse is not None,
        )
    )
    return pieces, frozenset(item.attendance_id for item in affected)


def _mutable_values(
    *,
    employee_id: int,
    start: datetime,
    end: datetime | None,
    work_center_id: int | None,
    department_id: int | None,
) -> dict[str, object]:
    return {
        "employee_odoo_id": employee_id,
        "check_in_utc": start,
        "check_out_utc": end,
        "odoo_work_center_id": work_center_id,
        "odoo_department_id": department_id,
    }


def _source_mutable_values(source: _SourceRow) -> dict[str, object]:
    return _mutable_values(
        employee_id=source.employee_id,
        start=source.start,
        end=source.end,
        work_center_id=source.work_center_id,
        department_id=source.department_id,
    )


def _piece_mutable_values(piece: _Piece, employee_id: int) -> dict[str, object]:
    return _mutable_values(
        employee_id=employee_id,
        start=piece.start,
        end=piece.end,
        work_center_id=piece.work_center_id,
        department_id=piece.department_id,
    )


def _expected_mapping(piece: _Piece, employee_id: int) -> dict[str, object]:
    if piece.source is None or (piece.target and piece.attendance_id is None):
        values: dict[str, object] = {}
    else:
        values = {
            key: value
            for key, value in piece.source.values.items()
            if key not in {"odoo_write_date", "write_date", "id"}
        }
    values.update(_piece_mutable_values(piece, employee_id))
    values["odoo_attendance_id"] = piece.attendance_id
    if piece.source is not None:
        if piece.source.work_center_id != piece.work_center_id:
            values.pop("odoo_work_center_name", None)
        if piece.source.department_id != piece.department_id:
            values.pop("odoo_department_name", None)
    return values


def _operation_key_value(value: object) -> object:
    if isinstance(value, datetime):
        return _utc_text(_aware_utc(value, "operation key datetime"))
    if isinstance(value, Mapping):
        return {key: _operation_key_value(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_operation_key_value(item) for item in value]
    return value


def _request_identity(
    *,
    start: datetime,
    end: datetime | None,
    work_center_id: int,
    department_id: int | None,
) -> dict[str, object]:
    return {
        "start_utc": start,
        "end_utc": end,
        "odoo_work_center_id": work_center_id,
        "odoo_department_id": department_id,
    }


def _request_token(request: Mapping[str, object]) -> str:
    encoded = json.dumps(
        _operation_key_value(request),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_request_token(token: str) -> Mapping[str, object]:
    def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate correction request key: {key}")
            result[key] = value
        return result

    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + padding).decode("utf-8")
        decoded = json.loads(raw, object_pairs_hook=_unique_object)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("operation key request identity is malformed") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError("operation key request identity must be an object")
    _exact_keys(
        decoded,
        frozenset(
            (
                "start_utc",
                "end_utc",
                "odoo_work_center_id",
                "odoo_department_id",
            )
        ),
        "operation key request identity",
    )
    start = _parse_utc_text(decoded["start_utc"], "request start_utc")
    end_value = decoded["end_utc"]
    end = None if end_value is None else _parse_utc_text(end_value, "request end_utc")
    if end is not None and end <= start:
        raise ValueError("operation key request interval is invalid")
    request = _request_identity(
        start=start,
        end=end,
        work_center_id=_positive_int(decoded["odoo_work_center_id"], "odoo_work_center_id"),
        department_id=_optional_positive_int(decoded["odoo_department_id"], "odoo_department_id"),
    )
    if _request_token(request) != token:
        raise ValueError("operation key request identity is not canonical")
    return _FrozenMapping(request)


def _operation_key_from_values(
    *,
    kind: OperationKind,
    attendance_id: int | None,
    employee_id: int,
    before: Mapping[str, object] | None,
    after: Mapping[str, object] | None,
    source_id: int,
    source_write_date: datetime | None,
    request: Mapping[str, object],
) -> str:
    request_token = _request_token(request)
    identity = {
        "contract": "attendance-correction-operation-v2",
        "employee_odoo_id": employee_id,
        "source_attendance_id": source_id,
        "source_write_date": source_write_date,
        "kind": kind,
        "attendance_id": attendance_id,
        "before": before,
        "after": after,
        "request": request,
    }
    encoded = json.dumps(
        _operation_key_value(identity),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"{_KEY_PREFIX}{source_id}:{request_token}:{hashlib.sha256(encoded).hexdigest()}"


def _operation_key(
    pending: _PendingOperation,
    *,
    request_start: datetime,
    request_end: datetime | None,
    desired_work_center_id: int,
    desired_department_id: int | None,
) -> str:
    source = pending.source
    return _operation_key_from_values(
        kind=pending.kind,
        attendance_id=pending.attendance_id,
        employee_id=pending.employee_id,
        before=pending.before,
        after=pending.after,
        source_id=source.attendance_id if source is not None else 0,
        source_write_date=source.write_date if source is not None else None,
        request=_request_identity(
            start=request_start,
            end=request_end,
            work_center_id=desired_work_center_id,
            department_id=desired_department_id,
        ),
    )


def _validate_operation_key(
    operation: CorrectionOperation,
    versions_by_id: Mapping[int, SourceVersion],
) -> Mapping[str, object]:
    match = _KEY_PATTERN.fullmatch(operation.key)
    if match is None:
        raise ValueError("operation key is not a canonical correction key")
    source_id = int(match.group(1))
    request = _decode_request_token(match.group(2))
    if operation.kind in {"update", "delete"}:
        if source_id != operation.attendance_id:
            raise ValueError("operation key source does not match attendance id")
    source_version = versions_by_id.get(source_id) if source_id else None
    if source_id and source_version is None:
        raise ValueError("operation key references an unknown source version")
    expected = _operation_key_from_values(
        kind=operation.kind,
        attendance_id=operation.attendance_id,
        employee_id=operation.employee_odoo_id,
        before=operation.before,
        after=operation.after,
        source_id=source_id,
        source_write_date=(source_version.write_date if source_version is not None else None),
        request=request,
    )
    if operation.key != expected:
        raise ValueError("operation key does not match canonical identity material")
    return request


def _operations_for_pieces(
    *,
    sources: tuple[_SourceRow, ...],
    pieces: list[_Piece],
    affected_ids: frozenset[int],
    employee_id: int,
) -> list[_PendingOperation]:
    pending: list[_PendingOperation] = []
    pieces_by_id = {
        piece.attendance_id: piece for piece in pieces if piece.attendance_id is not None
    }
    for source in sources:
        if source.attendance_id not in affected_ids:
            continue
        retained = pieces_by_id.get(source.attendance_id)
        if retained is None:
            pending.append(
                _PendingOperation(
                    kind="delete",
                    attendance_id=source.attendance_id,
                    employee_id=employee_id,
                    before=_source_mutable_values(source),
                    after=None,
                    source=source,
                    effective_start=source.start,
                )
            )
            continue
        before_values = _source_mutable_values(source)
        retained_values = _piece_mutable_values(retained, employee_id)
        changed = tuple(
            field for field in _MUTABLE_FIELDS if before_values[field] != retained_values[field]
        )
        if changed:
            pending.append(
                _PendingOperation(
                    kind="update",
                    attendance_id=source.attendance_id,
                    employee_id=employee_id,
                    before={field: before_values[field] for field in changed},
                    after={field: retained_values[field] for field in changed},
                    source=source,
                    effective_start=retained.start,
                )
            )
    for piece in pieces:
        if piece.attendance_id is None:
            pending.append(
                _PendingOperation(
                    kind="create",
                    attendance_id=None,
                    employee_id=employee_id,
                    before=None,
                    after=_piece_mutable_values(piece, employee_id),
                    source=piece.source,
                    effective_start=piece.start,
                )
            )
    return pending


def _operation_sort_key(
    operation: CorrectionOperation,
    expected_by_id: Mapping[int, Mapping[str, object]] | None = None,
) -> tuple[object, ...]:
    values = operation.after if operation.after is not None else operation.before
    assert values is not None
    start = values.get("check_in_utc")
    if (
        not isinstance(start, datetime)
        and operation.attendance_id is not None
        and expected_by_id is not None
        and operation.attendance_id in expected_by_id
    ):
        start = expected_by_id[operation.attendance_id].get("check_in_utc")
    if not isinstance(start, datetime):
        start = datetime.max.replace(tzinfo=UTC)
    source_id = operation.attendance_id if operation.attendance_id is not None else 2**63
    return (start, source_id, operation.kind, operation.key)


def plan_correction(
    *,
    rows: Sequence[Mapping[str, object]],
    employee_odoo_id: int,
    start_utc: datetime,
    end_utc: datetime | None,
    odoo_work_center_id: int,
    odoo_department_id: int | None,
) -> CorrectionPlan:
    """Plan exact interval surgery for one selected Odoo employee.

    Positive explicit Odoo IDs are the pure planner's caller boundary.  Active
    roster membership and app-to-Odoo mapping-name resolution belong to the
    I/O-owning caller, which must supply a fresh complete row snapshot here.
    """

    employee_id = _positive_int(employee_odoo_id, "employee_odoo_id")
    work_center_id = _positive_int(odoo_work_center_id, "odoo_work_center_id")
    department_id = _optional_positive_int(odoo_department_id, "odoo_department_id")
    start = _aware_utc(start_utc, "start_utc")
    end = _optional_aware_utc(end_utc, "end_utc")
    if end is not None and end <= start:
        raise ValueError("end_utc must be later than start_utc")
    sources = _normalize_source_rows(rows, employee_id)

    if end is None:
        pieces, affected_ids = _open_pieces_and_groups(
            sources, start, work_center_id, department_id
        )
    else:
        pieces, affected_ids = _closed_pieces_and_groups(
            sources, start, end, work_center_id, department_id
        )
    pending = _operations_for_pieces(
        sources=sources,
        pieces=pieces,
        affected_ids=affected_ids,
        employee_id=employee_id,
    )
    operations = [
        CorrectionOperation(
            key=_operation_key(
                item,
                request_start=start,
                request_end=end,
                desired_work_center_id=work_center_id,
                desired_department_id=department_id,
            ),
            kind=item.kind,
            attendance_id=item.attendance_id,
            employee_odoo_id=item.employee_id,
            before=item.before,
            after=item.after,
        )
        for item in pending
    ]
    expected = [
        _expected_mapping(piece, employee_id)
        for piece in sorted(
            pieces,
            key=lambda item: (
                item.start,
                item.attendance_id if item.attendance_id is not None else 2**63,
            ),
        )
    ]
    expected_by_id = {
        item["odoo_attendance_id"]: item
        for item in expected
        if item["odoo_attendance_id"] is not None
    }
    operations.sort(key=lambda item: _operation_sort_key(item, expected_by_id))
    versions = tuple(
        sorted(
            (SourceVersion(item.attendance_id, item.write_date) for item in sources),
            key=lambda item: (item.attendance_id, item.write_date),
        )
    )
    return CorrectionPlan(versions, tuple(operations), tuple(expected))


def _validate_mutable_mapping(
    value: Mapping[str, object],
    *,
    require_all: bool,
    context: str,
) -> None:
    unknown = sorted(set(value) - set(_MUTABLE_FIELDS))
    if unknown:
        raise ValueError(f"{context} contains unknown field {unknown[0]}")
    if require_all and set(value) != set(_MUTABLE_FIELDS):
        raise ValueError(f"{context} must contain every mutable attendance field")
    if "employee_odoo_id" in value:
        _positive_int(value["employee_odoo_id"], "employee_odoo_id")
    if "check_in_utc" in value:
        _aware_utc(value["check_in_utc"], "check_in_utc")
    if "check_out_utc" in value:
        _optional_aware_utc(value["check_out_utc"], "check_out_utc")
    if "odoo_work_center_id" in value:
        _optional_positive_int(value["odoo_work_center_id"], "odoo_work_center_id")
    if "odoo_department_id" in value:
        _optional_positive_int(value["odoo_department_id"], "odoo_department_id")
    if "check_in_utc" in value and "check_out_utc" in value:
        start = value["check_in_utc"]
        end = value["check_out_utc"]
        assert isinstance(start, datetime)
        if end is not None and end <= start:
            raise ValueError(f"{context} interval must have positive duration")


def _validate_operation_mapping(operation: CorrectionOperation) -> None:
    if operation.kind == "create":
        assert operation.after is not None
        _validate_mutable_mapping(operation.after, require_all=True, context="create after")
        if operation.after["employee_odoo_id"] != operation.employee_odoo_id:
            raise ValueError("create employee does not match operation employee")
    elif operation.kind == "delete":
        assert operation.before is not None
        _validate_mutable_mapping(operation.before, require_all=True, context="delete before")
        if operation.before["employee_odoo_id"] != operation.employee_odoo_id:
            raise ValueError("delete employee does not match operation employee")
    else:
        assert operation.before is not None and operation.after is not None
        _validate_mutable_mapping(operation.before, require_all=False, context="update before")
        _validate_mutable_mapping(operation.after, require_all=False, context="update after")
        if operation.before == operation.after:
            raise ValueError("update operation must change at least one value")
        for values in (operation.before, operation.after):
            if (
                "employee_odoo_id" in values
                and values["employee_odoo_id"] != operation.employee_odoo_id
            ):
                raise ValueError("update employee does not match operation employee")


def _validate_expected_interval(
    value: Mapping[str, object],
) -> tuple[datetime, datetime | None, int | None, int]:
    missing = [field for field in _EXPECTED_REQUIRED_FIELDS if field not in value]
    if missing:
        raise ValueError("expected interval omitted required field(s): " + ", ".join(missing))
    attendance_id = _optional_positive_int(value["odoo_attendance_id"], "odoo_attendance_id")
    employee_id = _positive_int(value["employee_odoo_id"], "employee_odoo_id")
    start = _aware_utc(value["check_in_utc"], "check_in_utc")
    end = _optional_aware_utc(value["check_out_utc"], "check_out_utc")
    if end is not None and end <= start:
        raise ValueError("expected interval must have positive duration")
    _optional_positive_int(value["odoo_work_center_id"], "odoo_work_center_id")
    _optional_positive_int(value["odoo_department_id"], "odoo_department_id")
    return start, end, attendance_id, employee_id


def _validate_plan(plan: CorrectionPlan) -> None:
    version_order = tuple(
        sorted(
            plan.source_versions,
            key=lambda item: (item.attendance_id, item.write_date),
        )
    )
    if plan.source_versions != version_order:
        raise ValueError("source versions are not in canonical order")
    version_ids = [item.attendance_id for item in plan.source_versions]
    if len(version_ids) != len(set(version_ids)):
        raise ValueError("duplicate source attendance id")
    versions_by_id = {item.attendance_id: item for item in plan.source_versions}

    interval_state = [_validate_expected_interval(item) for item in plan.expected_intervals]
    canonical_intervals = tuple(
        sorted(
            plan.expected_intervals,
            key=lambda item: (
                item["check_in_utc"],
                item["odoo_attendance_id"] if item["odoo_attendance_id"] is not None else 2**63,
            ),
        )
    )
    if plan.expected_intervals != canonical_intervals:
        raise ValueError("expected intervals are not in canonical order")
    seen_interval_ids: set[int] = set()
    employee_ids = {item[3] for item in interval_state}
    previous_end: datetime | None = None
    for index, (start, end, attendance_id, _employee_id) in enumerate(interval_state):
        if index and (previous_end is None or previous_end > start):
            raise ValueError("expected intervals overlap")
        previous_end = end
        if attendance_id is not None:
            if attendance_id in seen_interval_ids:
                raise ValueError("duplicate expected attendance id")
            if attendance_id not in versions_by_id:
                raise ValueError("expected interval references unknown source id")
            seen_interval_ids.add(attendance_id)

    operation_keys = [item.key for item in plan.operations]
    if len(operation_keys) != len(set(operation_keys)):
        raise ValueError("duplicate correction operation key")
    expected_by_id = {
        item["odoo_attendance_id"]: item
        for item in plan.expected_intervals
        if item["odoo_attendance_id"] is not None
    }
    if plan.operations != tuple(
        sorted(
            plan.operations,
            key=lambda item: _operation_sort_key(item, expected_by_id),
        )
    ):
        raise ValueError("operations are not in canonical order")
    mutations_by_id: dict[int, CorrectionOperation] = {}
    request_identity: Mapping[str, object] | None = None
    for operation in plan.operations:
        operation_request = _validate_operation_key(operation, versions_by_id)
        if request_identity is None:
            request_identity = operation_request
        elif request_identity != operation_request:
            raise ValueError("operations contain mixed correction requests")
        employee_ids.add(operation.employee_odoo_id)
        if operation.attendance_id is not None:
            if operation.attendance_id not in versions_by_id:
                raise ValueError("operation references an unknown source attendance id")
            if operation.attendance_id in mutations_by_id:
                raise ValueError("duplicate source mutation operation")
            mutations_by_id[operation.attendance_id] = operation
    if len(employee_ids) > 1:
        raise ValueError("correction plan contains mixed employees")

    for source_id in versions_by_id:
        operation = mutations_by_id.get(source_id)
        represented = source_id in expected_by_id
        if operation is not None and operation.kind == "delete":
            if represented:
                raise ValueError("deleted source remains in expected intervals")
            continue
        if not represented:
            raise ValueError("unoperated source is missing from expected intervals")
        if operation is not None:
            assert operation.kind == "update" and operation.after is not None
            expected = expected_by_id[source_id]
            for field, value in operation.after.items():
                if expected[field] != value:
                    raise ValueError("update operation does not match expected interval")

    expected_creates = Counter(
        tuple(item[field] for field in _MUTABLE_FIELDS)
        for item in plan.expected_intervals
        if item["odoo_attendance_id"] is None
    )
    operation_creates = Counter(
        tuple(operation.after[field] for field in _MUTABLE_FIELDS)
        for operation in plan.operations
        if operation.kind == "create" and operation.after is not None
    )
    if expected_creates != operation_creates:
        raise ValueError("create operations do not match created expected intervals")
    if request_identity is not None:
        request_start = request_identity["start_utc"]
        request_end = request_identity["end_utc"]
        assert isinstance(request_start, datetime)
        matching_target = any(
            item["odoo_work_center_id"] == request_identity["odoo_work_center_id"]
            and item["odoo_department_id"] == request_identity["odoo_department_id"]
            and item["check_in_utc"] < (request_end or datetime.max.replace(tzinfo=UTC))
            and (item["check_out_utc"] is None or item["check_out_utc"] > request_start)
            for item in plan.expected_intervals
        )
        if not matching_target:
            raise ValueError("correction request has no matching expected target interval")


def _utc_text(value: datetime) -> str:
    value = _aware_utc(value, "datetime")
    return value.isoformat().replace("+00:00", "Z")


def _parse_utc_text(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not _UTC_TEXT_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a canonical UTC datetime")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if _utc_text(parsed) != value:
        raise ValueError(f"{field_name} must be a canonical UTC datetime")
    return parsed.astimezone(UTC)


def _encode_data(value: object) -> JSONValue:
    if isinstance(value, datetime):
        return {"type": "datetime", "value": _utc_text(value)}
    if isinstance(value, Mapping):
        items: list[JSONValue] = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError("mapping keys must be text")
            items.append([key, _encode_data(value[key])])
        return {"type": "mapping", "items": items}
    if isinstance(value, (tuple, list)):
        return {
            "type": "tuple",
            "items": [_encode_data(item) for item in value],
        }
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite numbers are not valid JSON values")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"unsupported JSON plan value: {type(value).__name__}")


def _exact_keys(value: Mapping[str, object], required: frozenset[str], context: str) -> None:
    actual = set(value)
    if actual != required:
        unknown = sorted(actual - required)
        missing = sorted(required - actual)
        if unknown:
            raise ValueError(f"{context} contains unknown key {unknown[0]}")
        raise ValueError(f"{context} omitted required key {missing[0]}")


def _decode_data(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite numbers are not valid JSON values")
        return value
    if not isinstance(value, Mapping):
        raise TypeError("encoded plan data must be a JSON scalar or object")
    tag = value.get("type")
    if tag == "datetime":
        _exact_keys(value, frozenset(("type", "value")), "encoded datetime")
        return _parse_utc_text(value["value"], "encoded datetime")
    if tag == "tuple":
        _exact_keys(value, frozenset(("type", "items")), "encoded tuple")
        items = value["items"]
        if not isinstance(items, list):
            raise TypeError("encoded tuple items must be a list")
        return tuple(_decode_data(item) for item in items)
    if tag == "mapping":
        _exact_keys(value, frozenset(("type", "items")), "encoded mapping")
        items = value["items"]
        if not isinstance(items, list):
            raise TypeError("encoded mapping items must be a list")
        decoded: dict[str, object] = {}
        for item in items:
            if not isinstance(item, list) or len(item) != 2:
                raise TypeError("encoded mapping entries must be two-item lists")
            key = item[0]
            if not isinstance(key, str):
                raise TypeError("encoded mapping keys must be text")
            if key in decoded:
                raise ValueError(f"duplicate encoded mapping key: {key}")
            decoded[key] = _decode_data(item[1])
        return _FrozenMapping(decoded)
    raise ValueError("encoded plan data contains an unknown type")


def _plan_json_payload(plan: CorrectionPlan) -> dict[str, JSONValue]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "source_versions": [
            {
                "attendance_id": item.attendance_id,
                "write_date": _utc_text(item.write_date),
            }
            for item in plan.source_versions
        ],
        "operations": [
            {
                "key": item.key,
                "kind": item.kind,
                "attendance_id": item.attendance_id,
                "employee_odoo_id": item.employee_odoo_id,
                "before": _encode_data(item.before) if item.before is not None else None,
                "after": _encode_data(item.after) if item.after is not None else None,
            }
            for item in plan.operations
        ],
        "expected_intervals": [_encode_data(item) for item in plan.expected_intervals],
    }


def _plan_integrity(payload: Mapping[str, JSONValue]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return _INTEGRITY_PREFIX + hashlib.sha256(encoded).hexdigest()


def plan_to_json(plan: CorrectionPlan) -> JSONValue:
    """Return the canonical, lossless JSONB value for an immutable plan."""

    if not isinstance(plan, CorrectionPlan):
        raise TypeError("plan must be a CorrectionPlan")
    _validate_plan(plan)
    payload = _plan_json_payload(plan)
    return {**payload, "integrity": _plan_integrity(payload)}


def _require_json_list(value: object, field_name: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a JSON list")
    return value


def plan_from_json(value: object) -> CorrectionPlan:
    """Decode and revalidate a canonical JSONB correction plan."""

    if not isinstance(value, Mapping):
        raise TypeError("correction plan JSON must be an object")
    _exact_keys(
        value,
        frozenset(
            (
                "schema_version",
                "source_versions",
                "operations",
                "expected_intervals",
                "integrity",
            )
        ),
        "correction plan JSON",
    )
    version = value["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != _SCHEMA_VERSION:
        raise ValueError("unsupported correction plan schema version")
    integrity = value["integrity"]
    if not isinstance(integrity, str) or not _INTEGRITY_PATTERN.fullmatch(integrity):
        raise ValueError("correction plan integrity is not canonical")

    source_versions: list[SourceVersion] = []
    for item in _require_json_list(value["source_versions"], "source_versions"):
        if not isinstance(item, Mapping):
            raise TypeError("source version must be an object")
        _exact_keys(
            item,
            frozenset(("attendance_id", "write_date")),
            "source version",
        )
        source_versions.append(
            SourceVersion(
                _positive_int(item["attendance_id"], "attendance_id"),
                _parse_utc_text(item["write_date"], "write_date"),
            )
        )

    operations: list[CorrectionOperation] = []
    operation_fields = frozenset(
        (
            "key",
            "kind",
            "attendance_id",
            "employee_odoo_id",
            "before",
            "after",
        )
    )
    for item in _require_json_list(value["operations"], "operations"):
        if not isinstance(item, Mapping):
            raise TypeError("correction operation must be an object")
        _exact_keys(item, operation_fields, "correction operation")
        kind = item["kind"]
        if not isinstance(kind, str) or kind not in _KINDS:
            raise ValueError("invalid correction operation kind")
        before = None if item["before"] is None else _decode_data(item["before"])
        after = None if item["after"] is None else _decode_data(item["after"])
        if before is not None and not isinstance(before, Mapping):
            raise TypeError("operation before must decode to a mapping")
        if after is not None and not isinstance(after, Mapping):
            raise TypeError("operation after must decode to a mapping")
        operations.append(
            CorrectionOperation(
                key=item["key"],  # type: ignore[arg-type]
                kind=kind,  # type: ignore[arg-type]
                attendance_id=item["attendance_id"],  # type: ignore[arg-type]
                employee_odoo_id=item["employee_odoo_id"],  # type: ignore[arg-type]
                before=before,
                after=after,
            )
        )

    expected: list[Mapping[str, object]] = []
    for item in _require_json_list(value["expected_intervals"], "expected_intervals"):
        decoded = _decode_data(item)
        if not isinstance(decoded, Mapping):
            raise TypeError("expected interval must decode to a mapping")
        expected.append(decoded)
    plan = CorrectionPlan(tuple(source_versions), tuple(operations), tuple(expected))
    if integrity != _plan_integrity(_plan_json_payload(plan)):
        raise ValueError("correction plan integrity does not match its contents")
    return plan


# ---------------------------------------------------------------------------
# Durable correction orchestration


@dataclass(frozen=True)
class CorrectionPreview:
    item_key: str
    employee_odoo_ids: tuple[int, ...]
    target_work_center_name: str
    target_odoo_work_center_id: int
    target_odoo_department_id: int | None
    start_utc: datetime
    end_utc: datetime | None
    plans: tuple[CorrectionPlan, ...]

    def __post_init__(self) -> None:
        item_key = _bounded_text(self.item_key, "item_key", _ITEM_KEY_LIMIT)
        work_center_name = _bounded_text(
            self.target_work_center_name,
            "target_work_center_name",
            _TEXT_LIMIT,
        )
        employees = _employee_ids(self.employee_odoo_ids)
        work_center_id = _positive_int(
            self.target_odoo_work_center_id, "target_odoo_work_center_id"
        )
        department_id = _optional_positive_int(
            self.target_odoo_department_id, "target_odoo_department_id"
        )
        start = _aware_utc(self.start_utc, "start_utc")
        end = _optional_aware_utc(self.end_utc, "end_utc")
        if end is not None and end <= start:
            raise ValueError("end_utc must be later than start_utc")
        plans = tuple(self.plans)
        if len(plans) not in (0, len(employees)):
            raise ValueError("preview must contain one plan per employee")
        if not all(isinstance(plan, CorrectionPlan) for plan in plans):
            raise TypeError("plans must contain CorrectionPlan values")
        object.__setattr__(self, "item_key", item_key)
        object.__setattr__(self, "employee_odoo_ids", employees)
        object.__setattr__(self, "target_work_center_name", work_center_name)
        object.__setattr__(self, "target_odoo_work_center_id", work_center_id)
        object.__setattr__(self, "target_odoo_department_id", department_id)
        object.__setattr__(self, "start_utc", start)
        object.__setattr__(self, "end_utc", end)
        object.__setattr__(self, "plans", plans)


@dataclass(frozen=True)
class CorrectionJobResult:
    job_id: int
    status: str
    attempt_count: int
    error: str | None = None
    retry_at: datetime | None = None
    completed_operation_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", _positive_int(self.job_id, "job_id"))
        if self.status not in _JOB_STATUSES and self.status not in (
            "recoverable",
            "superseded",
        ):
            raise ValueError("invalid correction job result status")
        if (
            isinstance(self.attempt_count, bool)
            or not isinstance(self.attempt_count, int)
            or self.attempt_count < 0
        ):
            raise ValueError("attempt_count must be a non-negative integer")
        if (
            isinstance(self.completed_operation_count, bool)
            or not isinstance(self.completed_operation_count, int)
            or self.completed_operation_count < 0
        ):
            raise ValueError("completed_operation_count must be non-negative")
        if self.error is not None:
            object.__setattr__(self, "error", str(self.error)[:_ERROR_LIMIT])
        if self.retry_at is not None:
            object.__setattr__(self, "retry_at", _aware_utc(self.retry_at, "retry_at"))


@dataclass(frozen=True)
class _JobClaim:
    job_id: int
    attempt_count: int
    lease_until: datetime
    row: Mapping[str, Any]


@dataclass(frozen=True)
class _OperationReservation:
    job_id: int
    attempt_count: int
    operation_key: str
    token: str
    reserved_until: datetime


class _SourceChanged(RuntimeError):
    pass


class _RecoverableWrite(RuntimeError):
    pass


class _StaleClaim(RuntimeError):
    pass


def _bounded_text(value: object, field_name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field_name} cannot be empty")
    if len(clean) > limit:
        raise ValueError(f"{field_name} is too long")
    return clean


def _optional_bounded_text(value: object, field_name: str, limit: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field_name, limit)


def _employee_ids(values: Sequence[int]) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("employee_odoo_ids must be a sequence")
    normalized = tuple(sorted({_positive_int(value, "employee_odoo_id") for value in values}))
    if not normalized:
        raise ValueError("at least one employee is required")
    if len(normalized) > _MAX_EMPLOYEES:
        raise ValueError("too many employees selected")
    return normalized


def _validated_request(
    *,
    item_key: str,
    employee_odoo_ids: Sequence[int],
    target_work_center_name: str,
    start_utc: datetime,
    end_utc: datetime | None,
) -> tuple[str, tuple[int, ...], str, datetime, datetime | None]:
    key = _bounded_text(item_key, "item_key", _ITEM_KEY_LIMIT)
    employees = _employee_ids(employee_odoo_ids)
    name = _bounded_text(target_work_center_name, "target_work_center_name", _TEXT_LIMIT)
    start = _aware_utc(start_utc, "start_utc")
    end = _optional_aware_utc(end_utc, "end_utc")
    if end is not None and end <= start:
        raise ValueError("end_utc must be later than start_utc")
    return key, employees, name, start, end


def _default_facade():
    """Import Odoo only after validated correction work is available."""
    from . import odoo_client

    return odoo_client


def _before_remote(callback: Callable[[], None] | None) -> None:
    if callback is not None:
        callback()


def _resolve_mapping(
    target_name: str,
    facade,
    *,
    before_remote_call: Callable[[], None] | None = None,
) -> tuple[int, int | None]:
    """Resolve an explicit saved mapping and confirm the Odoo row is active.

    The facade owns the exact work-center-to-department rule. A correction
    cannot safely create or update an interval when that identity is missing or
    ambiguous, because ``None`` would omit or clear a known department.
    """
    from . import db

    rows = db.query(
        "SELECT odoo_work_center_id, odoo_work_center_name FROM work_centers WHERE name = %s",
        (target_name,),
    )
    if len(rows) != 1 or rows[0].get("odoo_work_center_id") is None:
        raise ValueError("target work center has no saved Odoo mapping")
    work_center_id = _positive_int(
        int(rows[0]["odoo_work_center_id"]), "target_odoo_work_center_id"
    )
    _before_remote(before_remote_call)
    catalog = facade.fetch_manufacturing_work_centers(force=True)
    active = [
        row for row in catalog if isinstance(row, Mapping) and row.get("id") == work_center_id
    ]
    if len(active) != 1:
        raise ValueError("target Odoo work center is unknown or inactive")
    saved_odoo_name = str(rows[0].get("odoo_work_center_name") or "").strip()
    active_name = str(active[0].get("name") or "").strip()
    if saved_odoo_name and active_name != saved_odoo_name:
        raise ValueError("saved Odoo work-center mapping is stale")
    resolver = getattr(facade, "target_department_id_for_work_center", None)
    if resolver is None:
        raise ValueError("target Odoo department resolver is unavailable")
    _before_remote(before_remote_call)
    department_id = _optional_positive_int(
        resolver(work_center_id, force=True), "target_odoo_department_id"
    )
    if department_id is None:
        raise ValueError("target Odoo department is missing or ambiguous")
    return work_center_id, department_id


def _canonical_source_row(row: Mapping[str, object], employee_id: int) -> dict[str, object]:
    source = _normalize_source_row(row, employee_id)
    return {
        "odoo_attendance_id": source.attendance_id,
        "employee_odoo_id": source.employee_id,
        "check_in_utc": source.start,
        "check_out_utc": source.end,
        "odoo_work_center_id": source.work_center_id,
        "odoo_department_id": source.department_id,
        "odoo_write_date": source.write_date,
    }


def _normalized_verification_row(row: Mapping[str, object], employee_id: int) -> dict[str, object]:
    """Keep validated Odoo labels for mirror storage, separate from comparison."""
    source = _normalize_source_row(row, employee_id)
    normalized = _canonical_source_row(source.values, employee_id)
    for field in _MIRROR_DISPLAY_FIELDS:
        value = source.values.get(field)
        if value is None:
            normalized[field] = None
            continue
        if not isinstance(value, str):
            raise TypeError(f"{field} must be text or None")
        clean = value.strip()
        if len(clean) > _TEXT_LIMIT:
            raise ValueError(f"{field} is too long")
        normalized[field] = clean or None
    return normalized


def _build_preview(
    *,
    item_key: str,
    employee_odoo_ids: tuple[int, ...],
    target_work_center_name: str,
    start_utc: datetime,
    end_utc: datetime | None,
) -> CorrectionPreview:
    facade = _default_facade()
    roster = facade.fetch_employee_statuses()
    active_employee_ids = {
        int(row["id"])
        for row in roster
        if isinstance(row, Mapping)
        and not isinstance(row.get("id"), bool)
        and isinstance(row.get("id"), int)
        and row.get("active") is True
    }
    missing = sorted(set(employee_odoo_ids) - active_employee_ids)
    if missing:
        raise ValueError("selected employee is unknown or inactive in Odoo")
    work_center_id, department_id = _resolve_mapping(target_work_center_name, facade)
    plans: list[CorrectionPlan] = []
    for employee_id in employee_odoo_ids:
        rows = facade.fetch_employee_attendance_rows(employee_id, start_utc, end_utc)
        plans.append(
            plan_correction(
                rows=rows,
                employee_odoo_id=employee_id,
                start_utc=start_utc,
                end_utc=end_utc,
                odoo_work_center_id=work_center_id,
                odoo_department_id=department_id,
            )
        )
    return CorrectionPreview(
        item_key=item_key,
        employee_odoo_ids=employee_odoo_ids,
        target_work_center_name=target_work_center_name,
        target_odoo_work_center_id=work_center_id,
        target_odoo_department_id=department_id,
        start_utc=start_utc,
        end_utc=end_utc,
        plans=tuple(plans),
    )


def correction_preview(
    *,
    item_key: str,
    employee_odoo_ids: Sequence[int],
    target_work_center_name: str,
    start_utc: datetime,
    end_utc: datetime | None,
) -> CorrectionPreview:
    """Re-read Odoo and return an immutable, read-only correction preview."""
    key, employees, target, start, end = _validated_request(
        item_key=item_key,
        employee_odoo_ids=employee_odoo_ids,
        target_work_center_name=target_work_center_name,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    return _build_preview(
        item_key=key,
        employee_odoo_ids=employees,
        target_work_center_name=target,
        start_utc=start,
        end_utc=end,
    )


def _snapshot_payload(preview: CorrectionPreview) -> dict[str, object]:
    employees: list[dict[str, object]] = []
    for employee_id, plan in zip(preview.employee_odoo_ids, preview.plans, strict=True):
        versions = [
            {
                "attendance_id": version.attendance_id,
                "write_date": _utc_text(version.write_date),
            }
            for version in plan.source_versions
        ]
        source_by_id: dict[int, dict[str, object]] = {}
        for expected in plan.expected_intervals:
            attendance_id = expected["odoo_attendance_id"]
            if attendance_id is not None:
                source_by_id.setdefault(
                    int(attendance_id),
                    {
                        "odoo_attendance_id": int(attendance_id),
                        "employee_odoo_id": employee_id,
                        **{
                            field: expected[field] for field in _MUTABLE_FIELDS if field in expected
                        },
                    },
                )
        for operation in plan.operations:
            if operation.attendance_id is None or operation.before is None:
                continue
            source = source_by_id.setdefault(
                operation.attendance_id,
                {
                    "odoo_attendance_id": operation.attendance_id,
                    "employee_odoo_id": employee_id,
                },
            )
            source.update(operation.before)
        version_by_id = {item["attendance_id"]: item["write_date"] for item in versions}
        rows = []
        for attendance_id in sorted(source_by_id):
            row = source_by_id[attendance_id]
            row["odoo_write_date"] = _parse_utc_text(version_by_id[attendance_id], "write_date")
            # An update operation stores only changed fields. The expected
            # interval supplies the rest; a deleted row's delete-before is full.
            missing = [field for field in _SOURCE_REQUIRED_FIELDS if field not in row]
            if missing:
                raise ValueError("plan cannot reconstruct canonical source snapshot")
            rows.append(_encode_data(row))
        employees.append({"employee_odoo_id": employee_id, "rows": rows})
    payload: dict[str, object] = {
        "schema_version": _SOURCE_SNAPSHOT_SCHEMA,
        "employees": employees,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload["integrity"] = (
        _SOURCE_INTEGRITY_PREFIX + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    )
    return payload


def _plans_payload(preview: CorrectionPreview) -> dict[str, object]:
    return {
        "schema_version": _PLANS_WRAPPER_SCHEMA,
        "plans": [
            {
                "employee_odoo_id": employee_id,
                "plan": plan_to_json(plan),
            }
            for employee_id, plan in zip(preview.employee_odoo_ids, preview.plans, strict=True)
        ],
    }


def _decode_json_column(value: object, field_name: str) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(f"{field_name} is corrupt JSON") from error
    return value


def _source_rows_from_json(
    value: object, employee_ids: tuple[int, ...]
) -> dict[int, tuple[dict[str, object], ...]]:
    raw = _decode_json_column(value, "source_snapshot")
    if not isinstance(raw, Mapping):
        raise TypeError("source_snapshot must be an object")
    _exact_keys(
        raw,
        frozenset(("schema_version", "employees", "integrity")),
        "source_snapshot",
    )
    if (
        isinstance(raw["schema_version"], bool)
        or not isinstance(raw["schema_version"], int)
        or raw["schema_version"] != _SOURCE_SNAPSHOT_SCHEMA
    ):
        raise ValueError("unsupported source snapshot schema version")
    payload = {
        "schema_version": raw["schema_version"],
        "employees": raw["employees"],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    expected_integrity = (
        _SOURCE_INTEGRITY_PREFIX + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    )
    if raw["integrity"] != expected_integrity:
        raise ValueError("source snapshot integrity does not match")
    items = _require_json_list(raw["employees"], "source employees")
    result: dict[int, tuple[dict[str, object], ...]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise TypeError("source employee must be an object")
        _exact_keys(item, frozenset(("employee_odoo_id", "rows")), "source employee")
        employee_id = _positive_int(item["employee_odoo_id"], "employee_odoo_id")
        if employee_id in result:
            raise ValueError("duplicate source employee")
        rows: list[dict[str, object]] = []
        for encoded_row in _require_json_list(item["rows"], "source rows"):
            decoded = _decode_data(encoded_row)
            if not isinstance(decoded, Mapping):
                raise TypeError("source row must decode to a mapping")
            rows.append(_canonical_source_row(decoded, employee_id))
        if [int(row["odoo_attendance_id"]) for row in rows] != sorted(
            int(row["odoo_attendance_id"]) for row in rows
        ):
            raise ValueError("source rows are not in canonical ID order")
        _normalize_source_rows(rows, employee_id)
        result[employee_id] = tuple(rows)
    if tuple(sorted(result)) != employee_ids:
        raise ValueError("source snapshot employees do not match the job")
    return result


def _plans_from_json(value: object, employee_ids: tuple[int, ...]) -> dict[int, CorrectionPlan]:
    raw = _decode_json_column(value, "operations")
    if not isinstance(raw, Mapping):
        raise TypeError("operations must be an object")
    _exact_keys(raw, frozenset(("schema_version", "plans")), "operations wrapper")
    if (
        isinstance(raw["schema_version"], bool)
        or not isinstance(raw["schema_version"], int)
        or raw["schema_version"] != _PLANS_WRAPPER_SCHEMA
    ):
        raise ValueError("unsupported operations schema version")
    result: dict[int, CorrectionPlan] = {}
    ordered_employee_ids: list[int] = []
    for item in _require_json_list(raw["plans"], "plans"):
        if not isinstance(item, Mapping):
            raise TypeError("plan wrapper must be an object")
        _exact_keys(item, frozenset(("employee_odoo_id", "plan")), "plan wrapper")
        employee_id = _positive_int(item["employee_odoo_id"], "employee_odoo_id")
        if employee_id in result:
            raise ValueError("duplicate employee correction plan")
        ordered_employee_ids.append(employee_id)
        result[employee_id] = plan_from_json(item["plan"])
    if tuple(sorted(result)) != employee_ids:
        raise ValueError("plan employees do not match the job")
    if tuple(ordered_employee_ids) != employee_ids:
        raise ValueError("plans are not in canonical employee order")
    if sum(len(plan.operations) for plan in result.values()) > _MAX_OPERATIONS:
        raise ValueError("correction job contains too many operations")
    return result


def _json_list(value: object, field_name: str) -> list[dict[str, object]]:
    raw = _decode_json_column(value, field_name)
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise ValueError(f"{field_name} must be a JSON list of objects")
    return [dict(item) for item in raw]


def _validated_completed_records(
    value: object, plans: Mapping[int, CorrectionPlan]
) -> list[dict[str, object]]:
    records = _json_list(value, "completed_operations")
    if len(records) > _MAX_OPERATIONS + 10:
        raise ValueError("completed_operations is not bounded")
    operations = {
        operation.key: operation for plan in plans.values() for operation in plan.operations
    }
    allowed_stages = frozenset(
        (
            "recalc_horizon",
            "mirror_complete",
            "recalc_enqueued",
            "recalc_complete",
            "cache_refreshed",
        )
    )
    stage_order = (
        "mirror_complete",
        "recalc_enqueued",
        "recalc_complete",
        "cache_refreshed",
    )
    seen: set[str] = set()
    seen_operation_keys: set[str] = set()
    seen_stages: list[str] = []
    horizon_ids: list[str] | None = None
    enqueued_ids: list[str] | None = None
    reservation_key: str | None = None
    for index, record in enumerate(records):
        operation_key = record.get("operation_key")
        stage = record.get("stage")
        if "reservation_token" in record:
            if set(record) != {
                "operation_key",
                "reservation_token",
                "reservation_attempt_count",
                "reservation_until",
            }:
                raise ValueError("operation reservation has unknown fields")
            if not isinstance(operation_key, str) or operation_key not in operations:
                raise ValueError("operation reservation does not match the saved plan")
            token = record["reservation_token"]
            if not isinstance(token, str) or not _RESERVATION_TOKEN_PATTERN.fullmatch(token):
                raise ValueError("operation reservation token is invalid")
            _positive_int(record["reservation_attempt_count"], "reservation_attempt_count")
            _parse_utc_text(record["reservation_until"], "reservation_until")
            if reservation_key is not None or index != len(records) - 1:
                raise ValueError("operation reservation must be the final unique record")
            reservation_key = operation_key
            identity = "reservation"
        elif isinstance(operation_key, str):
            if set(record) != {"operation_key", "kind", "attendance_id"}:
                raise ValueError("completed operation has unknown fields")
            operation = operations.get(operation_key)
            if operation is None or record.get("kind") != operation.kind:
                raise ValueError("completed operation does not match the saved plan")
            attendance_id = _positive_int(record.get("attendance_id"), "attendance_id")
            if operation.kind != "create" and attendance_id != operation.attendance_id:
                raise ValueError("completed operation attendance id changed")
            identity = "operation:" + operation_key
            seen_operation_keys.add(operation_key)
        elif isinstance(stage, str):
            has_recalc_ids = stage in ("recalc_horizon", "recalc_enqueued")
            allowed = {"stage", "recalc_ids"} if has_recalc_ids else {"stage"}
            if set(record) != allowed or stage not in allowed_stages:
                raise ValueError("completed stage is invalid")
            if has_recalc_ids:
                recalc_ids = record["recalc_ids"]
                if (
                    isinstance(recalc_ids, (str, bytes))
                    or not isinstance(recalc_ids, list)
                    or len(recalc_ids) > _MAX_RECALC_HORIZON_DAYS
                    or not all(
                        isinstance(item, str) and 1 <= len(item) <= 20 for item in recalc_ids
                    )
                ):
                    raise ValueError("completed recalculation IDs are invalid")
                try:
                    parsed_days = [date.fromisoformat(item) for item in recalc_ids]
                except ValueError as error:
                    raise ValueError("completed recalculation IDs are invalid") from error
                if [day.isoformat() for day in parsed_days] != recalc_ids or parsed_days != sorted(
                    set(parsed_days)
                ):
                    raise ValueError("completed recalculation IDs are not canonical")
                if stage == "recalc_horizon":
                    if index != 0:
                        raise ValueError("recalculation horizon must be the first record")
                    horizon_ids = recalc_ids
                else:
                    enqueued_ids = recalc_ids
            identity = "stage:" + stage
            if stage != "recalc_horizon":
                seen_stages.append(stage)
        else:
            raise ValueError("completed record omitted its identity")
        if identity in seen:
            raise ValueError("duplicate completed operation or stage")
        seen.add(identity)
    if seen_stages:
        if seen_operation_keys != set(operations):
            raise ValueError("downstream stages began before every operation completed")
        if tuple(seen_stages) != stage_order[: len(seen_stages)]:
            raise ValueError("completed stages are out of order")
    if reservation_key is not None:
        if reservation_key in seen_operation_keys or seen_stages:
            raise ValueError("operation reservation conflicts with durable progress")
    if horizon_ids is not None and enqueued_ids is not None and horizon_ids != enqueued_ids:
        raise ValueError("enqueued recalculation days changed from the frozen horizon")
    return records


def _event_detail(**values: object) -> dict[str, object]:
    unknown = set(values) - _EVENT_DETAIL_FIELDS
    if unknown:
        raise ValueError("event detail keys must be allowlisted")
    detail: dict[str, object] = {}
    for key, value in values.items():
        if value is None:
            continue
        if key.endswith("_ids") or key == "operation_keys":
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise TypeError(f"{key} must be a sequence")
            if len(value) > _MAX_EVENT_IDS:
                raise ValueError("event detail is not bounded")
            cleaned: list[int | str] = []
            for item in value:
                if isinstance(item, str):
                    if key == "operation_keys" and _KEY_PATTERN.fullmatch(item):
                        item = item.rsplit(":", 1)[-1]
                    if not item or len(item) > _TEXT_LIMIT:
                        raise ValueError("event detail is not bounded")
                    cleaned.append(item)
                else:
                    cleaned.append(_positive_int(item, key))
            detail[key] = cleaned
        elif isinstance(value, str):
            if key == "operation_key" and _KEY_PATTERN.fullmatch(value):
                value = value.rsplit(":", 1)[-1]
            if not value or len(value) > _TEXT_LIMIT:
                raise ValueError("event detail is not bounded")
            detail[key] = value
        else:
            detail[key] = _positive_int(value, key)
    encoded = json.dumps(detail, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 10_000:
        raise ValueError("event detail is not bounded")
    return detail


def _append_event_cur(
    cur,
    job_id: int,
    phase: str,
    result: str,
    detail: Mapping[str, object] | None = None,
) -> None:
    phase = _bounded_text(phase, "phase", 50)
    result = _bounded_text(result, "result", 50)
    if (phase, result) not in _EVENT_OUTCOMES:
        raise ValueError("correction event phase/result is not allowlisted")
    safe_detail = _event_detail(**dict(detail or {}))
    cur.execute(
        "INSERT INTO attendance_correction_job_events "
        "(correction_job_id, phase, result, detail) VALUES (%s, %s, %s, %s::jsonb)",
        (job_id, phase, result, json.dumps(safe_detail, separators=(",", ":"))),
    )


def _active_job_id(item_key: str) -> int | None:
    from . import db

    rows = db.query(
        "SELECT id FROM attendance_correction_jobs WHERE item_key = %s "
        "AND status IN ('planned','applying','verifying','recalculating') "
        "ORDER BY id DESC LIMIT 1",
        (item_key,),
    )
    return int(rows[0]["id"]) if rows else None


def create_job(
    *,
    item_key: str,
    employee_odoo_ids: Sequence[int],
    target_work_center_name: str,
    start_utc: datetime,
    end_utc: datetime | None,
    actor_email: str | None,
    actor_name: str | None,
) -> int:
    """Persist a fresh schema-v2 plan, deduplicated by active inbox item."""
    key, employees, target, start, end = _validated_request(
        item_key=item_key,
        employee_odoo_ids=employee_odoo_ids,
        target_work_center_name=target_work_center_name,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    email = _optional_bounded_text(actor_email, "actor_email", _ACTOR_LIMIT)
    name = _optional_bounded_text(actor_name, "actor_name", _ACTOR_LIMIT)
    existing = _active_job_id(key)
    if existing is not None:
        return existing
    preview = _build_preview(
        item_key=key,
        employee_odoo_ids=employees,
        target_work_center_name=target,
        start_utc=start,
        end_utc=end,
    )
    source_snapshot = _snapshot_payload(preview)
    plans = _plans_payload(preview)
    from . import db

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO attendance_correction_jobs "
            "(item_key, status, target_work_center_name, "
            "target_odoo_work_center_id, start_utc, end_utc, employee_odoo_ids, "
            "source_snapshot, operations, completed_operations, actor_email, actor_name) "
            "VALUES (%s, 'planned', %s, %s, %s, %s, %s::jsonb, %s::jsonb, "
            "%s::jsonb, '[]'::jsonb, %s, %s) "
            "ON CONFLICT (item_key) WHERE status IN "
            "('planned','applying','verifying','recalculating') DO NOTHING "
            "RETURNING id",
            (
                key,
                target,
                preview.target_odoo_work_center_id,
                start,
                end,
                json.dumps(list(employees)),
                json.dumps(source_snapshot, separators=(",", ":")),
                json.dumps(plans, separators=(",", ":")),
                email,
                name,
            ),
        )
        row = cur.fetchone()
        if row is not None:
            job_id = int(row["id"])
            _append_event_cur(
                cur,
                job_id,
                "planning",
                "created",
                _event_detail(
                    job_id=job_id,
                    employee_ids=employees,
                    work_center_id=preview.target_odoo_work_center_id,
                ),
            )
            return job_id
        cur.execute(
            "SELECT id FROM attendance_correction_jobs WHERE item_key = %s "
            "AND status IN ('planned','applying','verifying','recalculating') "
            "ORDER BY id DESC LIMIT 1 FOR UPDATE",
            (key,),
        )
        duplicate = cur.fetchone()
        if duplicate is None:
            raise RuntimeError("active correction dedupe winner disappeared")
        return int(duplicate["id"])


def _claim_job(*, job_id: int | None, now_utc: datetime) -> _JobClaim | None:
    from . import db

    now = _aware_utc(now_utc, "now_utc")
    lease_until = now + _CLAIM_LEASE
    with db.cursor() as cur:
        params: list[object] = [now]
        predicate = ""
        if job_id is not None:
            predicate = "AND id = %s "
            params.append(_positive_int(job_id, "job_id"))
        cur.execute(
            "SELECT * FROM attendance_correction_jobs "
            "WHERE status IN ('planned','applying','verifying','recalculating') "
            "AND (status = 'planned' OR updated_at <= %s) "
            + predicate
            + "ORDER BY created_at ASC, id ASC LIMIT 1 FOR UPDATE SKIP LOCKED",
            tuple(params),
        )
        row = cur.fetchone()
        if row is None:
            return None
        next_attempt = int(row.get("attempt_count") or 0) + 1
        next_status = "applying" if row["status"] == "planned" else row["status"]
        cur.execute(
            "UPDATE attendance_correction_jobs SET status = %s, "
            "attempt_count = %s, updated_at = %s, last_error = NULL "
            "WHERE id = %s AND attempt_count = %s RETURNING *",
            (
                next_status,
                next_attempt,
                lease_until,
                row["id"],
                int(row.get("attempt_count") or 0),
            ),
        )
        claimed = cur.fetchone()
        if claimed is None:
            return None
        claimed = dict(claimed)
        _append_event_cur(
            cur,
            int(claimed["id"]),
            "claim",
            "claimed",
            _event_detail(job_id=int(claimed["id"]), attempt_count=next_attempt),
        )
        return _JobClaim(int(claimed["id"]), next_attempt, lease_until, claimed)


def _transition(
    claim: _JobClaim,
    *,
    status: str | None = None,
    phase: str,
    result: str,
    detail: Mapping[str, object] | None = None,
    last_error: str | None = None,
    retry_at: datetime | None = None,
    verification_increment: bool = False,
) -> bool:
    from . import db

    if status is not None and status not in _JOB_STATUSES:
        raise ValueError("invalid correction job status")
    updated_at = claim.lease_until if retry_at is None else _aware_utc(retry_at, "retry_at")
    bounded_error = None if last_error is None else str(last_error)[:_ERROR_LIMIT]
    with db.cursor() as cur:
        cur.execute(
            "UPDATE attendance_correction_jobs SET "
            "status = COALESCE(%s, status), updated_at = %s, last_error = %s, "
            "verification_failure_count = verification_failure_count + %s "
            "WHERE id = %s AND attempt_count = %s "
            "AND status IN ('applying','verifying','recalculating') RETURNING id",
            (
                status,
                updated_at,
                bounded_error,
                1 if verification_increment else 0,
                claim.job_id,
                claim.attempt_count,
            ),
        )
        if cur.fetchone() is None:
            return False
        _append_event_cur(cur, claim.job_id, phase, result, detail)
        return True


def _complete_record(
    claim: _JobClaim,
    record: Mapping[str, object],
    *,
    phase: str,
    result: str,
    detail: Mapping[str, object],
) -> bool:
    from . import db

    completed = _json_list(claim.row.get("completed_operations", []), "completed_operations")
    key = record.get("operation_key") or record.get("stage")
    if any((item.get("operation_key") or item.get("stage")) == key for item in completed):
        return True
    if record.get("stage") == "recalc_horizon":
        completed.insert(0, dict(record))
    else:
        completed.append(dict(record))
    with db.cursor() as cur:
        cur.execute(
            "UPDATE attendance_correction_jobs SET completed_operations = %s::jsonb, "
            "updated_at = %s WHERE id = %s AND attempt_count = %s "
            "AND status IN ('applying','verifying','recalculating') RETURNING id",
            (
                json.dumps(completed, separators=(",", ":")),
                claim.lease_until,
                claim.job_id,
                claim.attempt_count,
            ),
        )
        if cur.fetchone() is None:
            return False
        _append_event_cur(cur, claim.job_id, phase, result, detail)
    claim.row["completed_operations"] = completed  # type: ignore[index]
    return True


def _claim_is_current(claim: _JobClaim) -> bool:
    """Read the durable fence before starting another non-transactional step."""
    from . import db

    rows = db.query(
        "SELECT 1 FROM attendance_correction_jobs WHERE id = %s "
        "AND attempt_count = %s AND status IN "
        "('applying','verifying','recalculating')",
        (claim.job_id, claim.attempt_count),
    )
    return bool(rows)


def _reservation_from_record(record: Mapping[str, object]) -> _OperationReservation:
    if set(record) != {
        "operation_key",
        "reservation_token",
        "reservation_attempt_count",
        "reservation_until",
    }:
        raise ValueError("operation reservation has unknown fields")
    operation_key = record["operation_key"]
    token = record["reservation_token"]
    if not isinstance(operation_key, str) or not _KEY_PATTERN.fullmatch(operation_key):
        raise ValueError("operation reservation key is invalid")
    if not isinstance(token, str) or not _RESERVATION_TOKEN_PATTERN.fullmatch(token):
        raise ValueError("operation reservation token is invalid")
    return _OperationReservation(
        job_id=0,
        attempt_count=_positive_int(
            record["reservation_attempt_count"], "reservation_attempt_count"
        ),
        operation_key=operation_key,
        token=token,
        reserved_until=_parse_utc_text(record["reservation_until"], "reservation_until"),
    )


def _reservation_record(reservation: _OperationReservation) -> dict[str, object]:
    return {
        "operation_key": reservation.operation_key,
        "reservation_token": reservation.token,
        "reservation_attempt_count": reservation.attempt_count,
        "reservation_until": _utc_text(reservation.reserved_until),
    }


def _heartbeat_claim(claim: _JobClaim, *, now_utc: datetime | None = None) -> datetime:
    """Renew the durable job lease immediately before one Odoo call."""
    from . import db

    now = _aware_utc(now_utc or datetime.now(UTC), "now_utc")
    lease_until = now + _CLAIM_LEASE
    with db.cursor() as cur:
        cur.execute(
            "UPDATE attendance_correction_jobs SET updated_at = %s "
            "WHERE id = %s AND attempt_count = %s "
            "AND status IN ('applying','verifying','recalculating') RETURNING id",
            (lease_until, claim.job_id, claim.attempt_count),
        )
        if cur.fetchone() is None:
            raise _StaleClaim("correction claim was superseded")
    return lease_until


def _reserve_operation(
    claim: _JobClaim,
    operation: CorrectionOperation,
    *,
    now_utc: datetime | None = None,
) -> _OperationReservation:
    """Persist exclusive ownership of one unconfirmed Odoo mutation."""
    from . import db

    now = _aware_utc(now_utc or datetime.now(UTC), "now_utc")
    reserved_until = now + _CLAIM_LEASE
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, attempt_count, completed_operations FROM "
            "attendance_correction_jobs WHERE id = %s FOR UPDATE",
            (claim.job_id,),
        )
        locked = cur.fetchone()
        if (
            locked is None
            or locked["status"] != "applying"
            or int(locked["attempt_count"]) != claim.attempt_count
        ):
            raise _StaleClaim("correction claim was superseded")
        records = _json_list(locked.get("completed_operations", []), "completed_operations")
        if any(item.get("operation_key") == operation.key and "kind" in item for item in records):
            raise _StaleClaim("correction operation is already complete")
        existing_records = [item for item in records if "reservation_token" in item]
        if len(existing_records) > 1:
            raise ValueError("multiple operation reservations are invalid")
        if existing_records:
            existing = _reservation_from_record(existing_records[0])
            if existing.reserved_until > now:
                raise _StaleClaim("correction operation is still reserved")
            records = [item for item in records if "reservation_token" not in item]
        reservation = _OperationReservation(
            job_id=claim.job_id,
            attempt_count=claim.attempt_count,
            operation_key=operation.key,
            token=secrets.token_hex(16),
            reserved_until=reserved_until,
        )
        records.append(_reservation_record(reservation))
        cur.execute(
            "UPDATE attendance_correction_jobs SET completed_operations = %s::jsonb, "
            "updated_at = %s WHERE id = %s AND attempt_count = %s "
            "AND status = 'applying' RETURNING id",
            (
                json.dumps(records, separators=(",", ":")),
                reserved_until,
                claim.job_id,
                claim.attempt_count,
            ),
        )
        if cur.fetchone() is None:
            raise _StaleClaim("correction claim was superseded")
        _append_event_cur(
            cur,
            claim.job_id,
            "applying",
            "reserved",
            _event_detail(
                job_id=claim.job_id,
                operation_key=operation.key,
                operation_kind=operation.kind,
                employee_odoo_id=operation.employee_odoo_id,
            ),
        )
    claim.row["completed_operations"] = records  # type: ignore[index]
    return reservation


def _renew_operation_reservation(
    claim: _JobClaim,
    reservation: _OperationReservation,
    *,
    now_utc: datetime | None = None,
) -> _OperationReservation:
    """Fence and extend the reservation immediately before remote I/O."""
    from . import db

    now = _aware_utc(now_utc or datetime.now(UTC), "now_utc")
    renewed = _OperationReservation(
        job_id=reservation.job_id,
        attempt_count=reservation.attempt_count,
        operation_key=reservation.operation_key,
        token=reservation.token,
        reserved_until=now + _CLAIM_LEASE,
    )
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, attempt_count, completed_operations FROM "
            "attendance_correction_jobs WHERE id = %s FOR UPDATE",
            (claim.job_id,),
        )
        locked = cur.fetchone()
        if (
            locked is None
            or locked["status"] != "applying"
            or int(locked["attempt_count"]) != claim.attempt_count
        ):
            raise _StaleClaim("correction claim was superseded")
        records = _json_list(locked.get("completed_operations", []), "completed_operations")
        matched = False
        next_records: list[dict[str, object]] = []
        for item in records:
            if item.get("reservation_token") == reservation.token:
                parsed = _reservation_from_record(item)
                if (
                    parsed.operation_key != reservation.operation_key
                    or parsed.attempt_count != reservation.attempt_count
                ):
                    raise _StaleClaim("correction reservation changed")
                next_records.append(_reservation_record(renewed))
                matched = True
            else:
                next_records.append(item)
        if not matched:
            raise _StaleClaim("correction reservation changed")
        cur.execute(
            "UPDATE attendance_correction_jobs SET completed_operations = %s::jsonb, "
            "updated_at = %s WHERE id = %s AND attempt_count = %s "
            "AND status = 'applying' RETURNING id",
            (
                json.dumps(next_records, separators=(",", ":")),
                renewed.reserved_until,
                claim.job_id,
                claim.attempt_count,
            ),
        )
        if cur.fetchone() is None:
            raise _StaleClaim("correction claim was superseded")
    claim.row["completed_operations"] = next_records  # type: ignore[index]
    return renewed


def _complete_reserved_operation(
    claim: _JobClaim,
    reservation: _OperationReservation,
    record: Mapping[str, object],
    *,
    result: str,
    detail: Mapping[str, object],
) -> bool:
    """Atomically replace the owned reservation with confirmed progress."""
    from . import db

    if record.get("operation_key") != reservation.operation_key:
        raise ValueError("completed operation does not match its reservation")
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, attempt_count, completed_operations FROM "
            "attendance_correction_jobs WHERE id = %s FOR UPDATE",
            (claim.job_id,),
        )
        locked = cur.fetchone()
        if (
            locked is None
            or locked["status"] != "applying"
            or int(locked["attempt_count"]) != claim.attempt_count
        ):
            return False
        records = _json_list(locked.get("completed_operations", []), "completed_operations")
        found = False
        next_records: list[dict[str, object]] = []
        for item in records:
            if item.get("reservation_token") == reservation.token:
                parsed = _reservation_from_record(item)
                if (
                    parsed.operation_key != reservation.operation_key
                    or parsed.attempt_count != reservation.attempt_count
                ):
                    return False
                found = True
                continue
            next_records.append(item)
        if not found:
            return False
        next_records.append(dict(record))
        cur.execute(
            "UPDATE attendance_correction_jobs SET completed_operations = %s::jsonb, "
            "updated_at = %s WHERE id = %s AND attempt_count = %s "
            "AND status = 'applying' RETURNING id",
            (
                json.dumps(next_records, separators=(",", ":")),
                max(claim.lease_until, reservation.reserved_until),
                claim.job_id,
                claim.attempt_count,
            ),
        )
        if cur.fetchone() is None:
            return False
        _append_event_cur(cur, claim.job_id, "applying", result, detail)
    claim.row["completed_operations"] = next_records  # type: ignore[index]
    return True


def _operation_source_state(
    operation: CorrectionOperation,
    *,
    source_row: Mapping[str, object],
    current_row: Mapping[str, object] | None,
) -> Literal["before", "after", "source_changed"]:
    if current_row is None:
        return "after" if operation.kind == "delete" else "source_changed"
    employee_id = operation.employee_odoo_id
    source = _canonical_source_row(source_row, employee_id)
    current = _canonical_source_row(current_row, employee_id)
    if operation.kind != "delete":
        desired = dict(source)
        assert operation.after is not None
        desired.update(operation.after)
        if all(current[field] == desired[field] for field in _MUTABLE_FIELDS):
            return "after"
    exact_before = all(
        current[field] == source[field]
        for field in (
            "odoo_attendance_id",
            *_MUTABLE_FIELDS,
            "odoo_write_date",
        )
    )
    return "before" if exact_before else "source_changed"


def _operation_effective_row(
    operation: CorrectionOperation, source: Mapping[str, object]
) -> dict[str, object] | None:
    if operation.kind == "delete":
        return None
    result = dict(source)
    assert operation.after is not None
    result.update(operation.after)
    return result


def _source_row_by_id(
    source_rows: Sequence[Mapping[str, object]], attendance_id: int
) -> Mapping[str, object]:
    matches = [row for row in source_rows if row["odoo_attendance_id"] == attendance_id]
    if len(matches) != 1:
        raise ValueError("operation source row is missing or duplicated")
    return matches[0]


def _is_open_producing(
    operation: CorrectionOperation, source_rows: Sequence[Mapping[str, object]]
) -> bool:
    if operation.after is None:
        return False
    if "check_out_utc" in operation.after:
        return operation.after["check_out_utc"] is None
    if operation.attendance_id is None:
        return False
    source = _source_row_by_id(source_rows, operation.attendance_id)
    return source["check_out_utc"] is None


def _ordered_operations(
    operations: Sequence[CorrectionOperation],
    *,
    source_rows: Sequence[Mapping[str, object]],
) -> tuple[CorrectionOperation, ...]:
    def phase(operation: CorrectionOperation) -> int:
        if _is_open_producing(operation, source_rows):
            return 4
        if operation.kind == "update":
            source = _source_row_by_id(source_rows, int(operation.attendance_id))
            if source["check_out_utc"] is None:
                return 0
            return 1
        if operation.kind == "create":
            return 2
        return 3

    return tuple(
        sorted(
            operations,
            key=lambda operation: (
                phase(operation),
                operation.employee_odoo_id,
                operation.key,
            ),
        )
    )


def _validate_operation_progress(
    completed: Sequence[Mapping[str, object]],
    ordered_operations: Sequence[CorrectionOperation],
    *,
    current_attempt_count: int,
) -> None:
    """Require durable write progress to be one exact global safe-order prefix."""
    ordered_keys = [operation.key for operation in ordered_operations]
    completed_keys = [
        str(item["operation_key"]) for item in completed if item.get("kind") in _KINDS
    ]
    if completed_keys != ordered_keys[: len(completed_keys)]:
        raise ValueError("completed operations are not an Odoo-safe ordered prefix")
    reservations = [item for item in completed if "reservation_token" in item]
    if not reservations:
        return
    if len(reservations) != 1 or len(completed_keys) >= len(ordered_keys):
        raise ValueError("operation reservation has no valid next operation")
    reservation = _reservation_from_record(reservations[0])
    if reservation.operation_key != ordered_keys[len(completed_keys)]:
        raise ValueError("operation reservation is not the next safe operation")
    if reservation.attempt_count >= current_attempt_count:
        raise ValueError("operation reservation attempt is not recoverable")


def _preflight_operations(
    facade,
    operations: Sequence[CorrectionOperation],
    source_rows: Sequence[Mapping[str, object]],
    *,
    before_remote_call: Callable[[], None] | None = None,
) -> None:
    """Prove the whole remaining preview is safe before its first new write.

    Per-operation guards still run immediately before each mutation. This
    additional pass prevents a row that was already stale farther down the plan
    from being discovered only after an earlier row was changed.
    """
    operation_by_source = {
        int(operation.attendance_id): operation
        for operation in operations
        if operation.attendance_id is not None
    }
    source_ids = {int(row["odoo_attendance_id"]) for row in source_rows}
    for source in source_rows:
        attendance_id = int(source["odoo_attendance_id"])
        operation = operation_by_source.get(attendance_id)
        current = _read_one(facade, attendance_id, before_remote_call=before_remote_call)
        if operation is None:
            if current is None:
                raise _SourceChanged("unoperated source row disappeared")
            normalized = _canonical_source_row(current, int(source["employee_odoo_id"]))
            expected = _canonical_source_row(source, int(source["employee_odoo_id"]))
            if any(
                normalized[field] != expected[field]
                for field in (
                    "odoo_attendance_id",
                    *_MUTABLE_FIELDS,
                    "odoo_write_date",
                )
            ):
                raise _SourceChanged("unoperated source row changed")
            continue
        if (
            _operation_source_state(operation, source_row=source, current_row=current)
            == "source_changed"
        ):
            raise _SourceChanged("source row changed after preview")

    for operation in operations:
        if operation.kind != "create":
            continue
        assert operation.after is not None
        candidates = _create_candidates(facade, operation, before_remote_call=before_remote_call)
        outsiders = [row for row in candidates if int(row["odoo_attendance_id"]) not in source_ids]
        exact_outsiders = [row for row in outsiders if _exact_mutable(row, operation.after)]
        if len(exact_outsiders) > 1 or any(row not in exact_outsiders for row in outsiders):
            raise _SourceChanged("create interval has new conflicting Odoo state")
        if exact_outsiders and len(candidates) != 1:
            raise _SourceChanged("create interval has ambiguous Odoo state")


def _read_one(
    facade,
    attendance_id: int,
    *,
    before_remote_call: Callable[[], None] | None = None,
) -> Mapping[str, object] | None:
    _before_remote(before_remote_call)
    rows = facade.fetch_attendance_rows_by_ids([attendance_id])
    if len(rows) > 1:
        raise _SourceChanged("duplicate source attendance identity")
    return rows[0] if rows else None


def _interval_overlaps(row: Mapping[str, object], start: datetime, end: datetime | None) -> bool:
    row_start = _aware_utc(row["check_in_utc"], "check_in_utc")
    row_end = _optional_aware_utc(row["check_out_utc"], "check_out_utc")
    infinity = datetime.max.replace(tzinfo=UTC)
    return row_start < (end or infinity) and (row_end or infinity) > start


def _exact_mutable(row: Mapping[str, object], values: Mapping[str, object]) -> bool:
    return all(row.get(field) == values[field] for field in _MUTABLE_FIELDS)


def _create_candidates(
    facade,
    operation: CorrectionOperation,
    *,
    before_remote_call: Callable[[], None] | None = None,
) -> tuple[Mapping[str, object], ...]:
    assert operation.after is not None
    start = _aware_utc(operation.after["check_in_utc"], "check_in_utc")
    end = _optional_aware_utc(operation.after["check_out_utc"], "check_out_utc")
    _before_remote(before_remote_call)
    rows = facade.fetch_employee_attendance_rows(operation.employee_odoo_id, start, end)
    return tuple(row for row in rows if _interval_overlaps(row, start, end))


def _perform_operation(
    facade,
    operation: CorrectionOperation,
    source_rows: Sequence[Mapping[str, object]],
    *,
    before_remote_call: Callable[[], None] | None = None,
) -> tuple[dict[str, object], str]:
    """Apply or safely adopt one operation, returning its durable record."""
    if operation.kind == "create":
        assert operation.after is not None
        candidates = _create_candidates(facade, operation, before_remote_call=before_remote_call)
        exact = [row for row in candidates if _exact_mutable(row, operation.after)]
        if len(exact) == 1 and len(candidates) == 1:
            attendance_id = _positive_int(exact[0]["odoo_attendance_id"], "odoo_attendance_id")
            return {
                "operation_key": operation.key,
                "kind": operation.kind,
                "attendance_id": attendance_id,
            }, "adopted"
        if exact or candidates:
            raise _SourceChanged("create interval overlaps changed Odoo state")
        try:
            _before_remote(before_remote_call)
            attendance_id = facade.create_attendance_interval(
                employee_odoo_id=operation.employee_odoo_id,
                check_in_utc=operation.after["check_in_utc"],
                check_out_utc=operation.after["check_out_utc"],
                odoo_work_center_id=operation.after["odoo_work_center_id"],
                odoo_department_id=operation.after["odoo_department_id"],
            )
        except _StaleClaim:
            raise
        except Exception as error:  # noqa: BLE001 - ambiguous timeout needs reread
            candidates = _create_candidates(
                facade, operation, before_remote_call=before_remote_call
            )
            exact = [row for row in candidates if _exact_mutable(row, operation.after)]
            if len(exact) == 1 and len(candidates) == 1:
                return {
                    "operation_key": operation.key,
                    "kind": operation.kind,
                    "attendance_id": _positive_int(
                        exact[0]["odoo_attendance_id"], "odoo_attendance_id"
                    ),
                }, "adopted_timeout"
            if exact or candidates:
                raise _SourceChanged("create outcome is ambiguous") from error
            raise _RecoverableWrite(str(error) or type(error).__name__) from error
        confirmed = _read_one(
            facade,
            _positive_int(attendance_id, "attendance_id"),
            before_remote_call=before_remote_call,
        )
        if confirmed is None or not _exact_mutable(confirmed, operation.after):
            raise _RecoverableWrite("created attendance is not yet exactly visible")
        return {
            "operation_key": operation.key,
            "kind": operation.kind,
            "attendance_id": int(attendance_id),
        }, "confirmed"

    assert operation.attendance_id is not None
    source = _source_row_by_id(source_rows, operation.attendance_id)
    current = _read_one(facade, operation.attendance_id, before_remote_call=before_remote_call)
    state = _operation_source_state(operation, source_row=source, current_row=current)
    if state == "after":
        return {
            "operation_key": operation.key,
            "kind": operation.kind,
            "attendance_id": operation.attendance_id,
        }, "adopted"
    if state != "before":
        raise _SourceChanged("source row changed after preview")
    try:
        if operation.kind == "update":
            assert operation.after is not None
            _before_remote(before_remote_call)
            facade.update_attendance_interval(operation.attendance_id, values=dict(operation.after))
        else:
            _before_remote(before_remote_call)
            facade.delete_attendance_interval(operation.attendance_id)
    except _StaleClaim:
        raise
    except Exception as error:  # noqa: BLE001 - ambiguous timeout needs reread
        current = _read_one(facade, operation.attendance_id, before_remote_call=before_remote_call)
        state = _operation_source_state(operation, source_row=source, current_row=current)
        if state == "after":
            return {
                "operation_key": operation.key,
                "kind": operation.kind,
                "attendance_id": operation.attendance_id,
            }, "adopted_timeout"
        if state == "source_changed":
            raise _SourceChanged("Odoo write outcome conflicts with the plan") from error
        raise _RecoverableWrite(str(error) or type(error).__name__) from error
    current = _read_one(facade, operation.attendance_id, before_remote_call=before_remote_call)
    if _operation_source_state(operation, source_row=source, current_row=current) != "after":
        raise _RecoverableWrite("Odoo write was not exactly visible")
    return {
        "operation_key": operation.key,
        "kind": operation.kind,
        "attendance_id": operation.attendance_id,
    }, "confirmed"


def _expected_with_created_ids(
    plan: CorrectionPlan,
    completed: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    by_key = {
        item.get("operation_key"): item
        for item in completed
        if item.get("operation_key") is not None
    }
    create_by_values: dict[tuple[object, ...], CorrectionOperation] = {}
    for operation in plan.operations:
        if operation.kind == "create":
            assert operation.after is not None
            create_by_values[tuple(operation.after[field] for field in _MUTABLE_FIELDS)] = operation
    result: list[dict[str, object]] = []
    for expected in plan.expected_intervals:
        row = dict(expected)
        if row["odoo_attendance_id"] is None:
            values = tuple(row[field] for field in _MUTABLE_FIELDS)
            operation = create_by_values.get(values)
            record = by_key.get(operation.key if operation is not None else None)
            if record is None or record.get("attendance_id") is None:
                raise ValueError("created attendance id was not confirmed")
            row["odoo_attendance_id"] = _positive_int(record["attendance_id"], "attendance_id")
        result.append(row)
    return tuple(result)


def _verification_rows(
    facade,
    plans: Mapping[int, CorrectionPlan],
    completed: Sequence[Mapping[str, object]],
    start: datetime,
    end: datetime | None,
    *,
    before_remote_call: Callable[[], None] | None = None,
) -> tuple[dict[str, object], ...]:
    verified: list[dict[str, object]] = []
    for employee_id in sorted(plans):
        expected = _expected_with_created_ids(plans[employee_id], completed)
        verification_start = min((row["check_in_utc"] for row in expected), default=start)
        expected_ends = [row["check_out_utc"] for row in expected]
        verification_end = (
            None
            if any(value is None for value in expected_ends)
            else max(expected_ends, default=end)
        )
        _before_remote(before_remote_call)
        actual = facade.fetch_employee_attendance_rows(
            employee_id, verification_start, verification_end
        )
        normalized_actual = tuple(
            sorted(
                (_normalized_verification_row(row, employee_id) for row in actual),
                key=lambda row: (row["check_in_utc"], row["odoo_attendance_id"]),
            )
        )
        comparable_actual = tuple(
            {
                "odoo_attendance_id": row["odoo_attendance_id"],
                **{field: row[field] for field in _MUTABLE_FIELDS},
            }
            for row in normalized_actual
        )
        comparable_expected = tuple(
            sorted(
                (
                    {
                        "odoo_attendance_id": row["odoo_attendance_id"],
                        **{field: row[field] for field in _MUTABLE_FIELDS},
                    }
                    for row in expected
                ),
                key=lambda row: (row["check_in_utc"], row["odoo_attendance_id"]),
            )
        )
        if comparable_actual != comparable_expected:
            raise _SourceChanged("verified Odoo intervals do not match the plan")
        verified.extend(normalized_actual)
    return tuple(verified)


def _touched_days(
    source_rows: Mapping[int, Sequence[Mapping[str, object]]],
    plans: Mapping[int, CorrectionPlan],
    *,
    open_end: datetime,
) -> tuple[date, ...]:
    from . import attendance_mirror

    effective_open_end = _aware_utc(open_end, "open_end")
    days: set[date] = set()
    for rows in source_rows.values():
        for row in rows:
            end = row["check_out_utc"] or effective_open_end
            days.update(attendance_mirror.local_days_touched(row["check_in_utc"], end))
    for plan in plans.values():
        for row in plan.expected_intervals:
            end = row["check_out_utc"] or effective_open_end
            days.update(attendance_mirror.local_days_touched(row["check_in_utc"], end))
    return tuple(sorted(days))


def _durable_recalc_days(
    completed: Sequence[Mapping[str, object]],
) -> tuple[date, ...] | None:
    for wanted_stage in ("recalc_horizon", "recalc_enqueued"):
        for item in completed:
            if item.get("stage") != wanted_stage:
                continue
            recalc_ids = item.get("recalc_ids")
            if not isinstance(recalc_ids, list):
                raise ValueError("completed recalculation IDs are invalid")
            return tuple(date.fromisoformat(str(value)) for value in recalc_ids)
    return None


def _freeze_recalc_horizon(
    claim: _JobClaim,
    days: Sequence[date],
) -> bool:
    """Persist one bounded downstream day set before any remote mutation."""
    recalc_ids = [day.isoformat() for day in days]
    marker: dict[str, object] = {
        "stage": "recalc_horizon",
        "recalc_ids": recalc_ids,
    }
    return _complete_record(
        claim,
        marker,
        phase="planning",
        result="horizon_frozen",
        detail=_event_detail(
            job_id=claim.job_id,
            recalc_ids=recalc_ids[:_MAX_EVENT_IDS],
        ),
    )


def _mirror_verified_rows(
    claim: _JobClaim,
    rows: Sequence[Mapping[str, object]],
    source_rows: Mapping[int, Sequence[Mapping[str, object]]],
    completed: Sequence[Mapping[str, object]],
    *,
    completed_at: datetime,
) -> bool:
    from . import attendance_mirror, db

    # The facade normally includes display names. The mirror contract accepts
    # nullable display values, so identifiers and exact interval fields remain
    # authoritative when a test/minimal facade omits them.
    mirror_rows = [
        {
            **row,
            "employee_name": row.get("employee_name"),
            "odoo_work_center_name": row.get("odoo_work_center_name"),
            "odoo_department_name": row.get("odoo_department_name"),
        }
        for row in rows
    ]
    verified_ids = {int(row["odoo_attendance_id"]) for row in rows}
    source_ids = {
        int(row["odoo_attendance_id"])
        for employee_rows in source_rows.values()
        for row in employee_rows
    }
    deleted_ids = sorted(source_ids - verified_ids)
    normalized = attendance_mirror._normalized_rows(mirror_rows)
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, attempt_count FROM attendance_correction_jobs WHERE id = %s FOR UPDATE",
            (claim.job_id,),
        )
        owned = cur.fetchone()
        if (
            owned is None
            or owned["status"] != "recalculating"
            or int(owned["attempt_count"]) != claim.attempt_count
        ):
            return False
        state = attendance_mirror._locked_sync_state(cur)
        attendance_mirror._upsert_rows_cur(
            cur,
            normalized,
            sync_completed_at=completed_at,
            observed_at=completed_at,
            baseline_completed=state["baseline_completed_at"] is not None,
        )
        if deleted_ids:
            cur.execute(
                "UPDATE odoo_attendance_mirror SET deleted_at = %s, last_seen_at = %s "
                "WHERE odoo_attendance_id = ANY(%s) AND deleted_at IS NULL",
                (completed_at, completed_at, deleted_ids),
            )
        next_completed = [dict(item) for item in completed]
        next_completed.append({"stage": "mirror_complete"})
        cur.execute(
            "UPDATE attendance_correction_jobs SET completed_operations = %s::jsonb, "
            "updated_at = %s WHERE id = %s AND attempt_count = %s "
            "AND status = 'recalculating' RETURNING id",
            (
                json.dumps(next_completed, separators=(",", ":")),
                claim.lease_until,
                claim.job_id,
                claim.attempt_count,
            ),
        )
        if cur.fetchone() is None:
            raise RuntimeError("mirror completion fence changed")
        _append_event_cur(
            cur,
            claim.job_id,
            "mirror",
            "complete",
            _event_detail(
                job_id=claim.job_id,
                attendance_ids=sorted(int(item["odoo_attendance_id"]) for item in rows)[
                    :_MAX_EVENT_IDS
                ],
            ),
        )
    claim.row["completed_operations"] = next_completed  # type: ignore[index]
    return True


def _enqueue_recalculation(
    claim: _JobClaim,
    days: Sequence[date],
    completed: Sequence[Mapping[str, object]],
    *,
    requested_at: datetime,
) -> bool:
    """Fence, enqueue strict days, and record the stage in one transaction."""
    from . import attendance_mirror, db

    with db.cursor() as cur:
        cur.execute(
            "SELECT status, attempt_count FROM attendance_correction_jobs WHERE id = %s FOR UPDATE",
            (claim.job_id,),
        )
        owned = cur.fetchone()
        if (
            owned is None
            or owned["status"] != "recalculating"
            or int(owned["attempt_count"]) != claim.attempt_count
        ):
            return False
        attendance_mirror._enqueue_recalc_cur(
            cur,
            days,
            "attendance_correction_verified",
            mark_strict=True,
            requested_at=requested_at,
        )
        marker = {
            "stage": "recalc_enqueued",
            "recalc_ids": [day.isoformat() for day in days],
        }
        next_completed = [dict(item) for item in completed]
        next_completed.append(marker)
        cur.execute(
            "UPDATE attendance_correction_jobs SET completed_operations = %s::jsonb, "
            "updated_at = %s WHERE id = %s AND attempt_count = %s "
            "AND status = 'recalculating' RETURNING id",
            (
                json.dumps(next_completed, separators=(",", ":")),
                claim.lease_until,
                claim.job_id,
                claim.attempt_count,
            ),
        )
        if cur.fetchone() is None:
            raise RuntimeError("recalculation enqueue fence changed")
        _append_event_cur(
            cur,
            claim.job_id,
            "recalculation",
            "enqueued",
            _event_detail(
                job_id=claim.job_id,
                recalc_ids=[day.isoformat() for day in days][:_MAX_EVENT_IDS],
            ),
        )
    claim.row["completed_operations"] = next_completed  # type: ignore[index]
    return True


def _recalc_complete(days: Sequence[date]) -> bool:
    from . import db

    if not days:
        return True
    rows = db.query(
        "SELECT day, completed_at, cache_ready_at FROM attendance_recalc_queue WHERE day = ANY(%s)",
        (list(days),),
    )
    return len(rows) == len(days) and all(
        row["completed_at"] is not None and row["cache_ready_at"] is not None for row in rows
    )


def _run_recalculation(days: Sequence[date]) -> bool:
    from . import attendance_recalc

    for _ in range(max(1, len(days))):
        if _recalc_complete(days):
            return True
        result = attendance_recalc.process_next()
        if result is None or result.status == "failed":
            break
    return _recalc_complete(days)


def _complete_with_audit(
    claim: _JobClaim,
    *,
    source_rows: Mapping[int, Sequence[Mapping[str, object]]],
    plans: Mapping[int, CorrectionPlan],
    completed: Sequence[Mapping[str, object]],
    completed_at: datetime,
) -> bool:
    from . import db, inbox_log

    operation_keys = [
        str(item["operation_key"]).rsplit(":", 1)[-1]
        for item in completed
        if isinstance(item.get("operation_key"), str)
    ]
    before_ids = sorted(
        int(row["odoo_attendance_id"]) for rows in source_rows.values() for row in rows
    )
    expected_ids = sorted(
        int(row["odoo_attendance_id"])
        for plan in plans.values()
        for row in _expected_with_created_ids(plan, completed)
    )
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, attempt_count, completed_operations FROM "
            "attendance_correction_jobs WHERE id = %s FOR UPDATE",
            (claim.job_id,),
        )
        locked = cur.fetchone()
        if (
            locked is None
            or locked["status"] != "recalculating"
            or int(locked["attempt_count"]) != claim.attempt_count
        ):
            return False
        stages = _json_list(locked["completed_operations"], "completed_operations")
        if any(item.get("stage") == "audit_complete" for item in stages):
            return False
        inbox_log.record_event_with_cursor(
            cur,
            item_kind="attendance_correction",
            item_key=str(claim.row["item_key"]),
            person_name=None,
            category_label="Odoo attendance correction",
            action="corrected_odoo_attendance",
            outcome="Verified and recalculated",
            actor_upn=claim.row.get("actor_email"),
            actor_name=claim.row.get("actor_name"),
            source="inbox",
            reversible=False,
            detail={
                "job_id": claim.job_id,
                "employee_ids": sorted(source_rows),
                "before_attendance_ids": before_ids[:_MAX_EVENT_IDS],
                "after_attendance_ids": expected_ids[:_MAX_EVENT_IDS],
                "operation_keys": operation_keys[:_MAX_EVENT_IDS],
            },
            resolved_at=completed_at,
        )
        _append_event_cur(
            cur,
            claim.job_id,
            "audit",
            "complete",
            _event_detail(
                job_id=claim.job_id,
                operation_keys=operation_keys[:_MAX_EVENT_IDS],
            ),
        )
        stages.append({"stage": "audit_complete"})
        cur.execute(
            "UPDATE attendance_correction_jobs SET status = 'complete', "
            "completed_operations = %s::jsonb, completed_at = %s, updated_at = %s, "
            "last_error = NULL WHERE id = %s AND attempt_count = %s "
            "AND status = 'recalculating' RETURNING id",
            (
                json.dumps(stages, separators=(",", ":")),
                completed_at,
                completed_at,
                claim.job_id,
                claim.attempt_count,
            ),
        )
        if cur.fetchone() is None:
            raise RuntimeError("correction completion fence changed during audit")
        _append_event_cur(
            cur,
            claim.job_id,
            "completion",
            "complete",
            _event_detail(
                job_id=claim.job_id,
                operation_keys=operation_keys[:_MAX_EVENT_IDS],
                attendance_ids=expected_ids[:_MAX_EVENT_IDS],
            ),
        )
    return True


def _retry_at(attempt_count: int, now: datetime) -> datetime:
    seconds = min(15 * (2 ** min(max(attempt_count - 1, 0), 10)), 900)
    return now + timedelta(seconds=seconds)


def _saved_target_department_id(
    *,
    plans: Mapping[int, CorrectionPlan],
    source_rows: Mapping[int, Sequence[Mapping[str, object]]],
    target_work_center_id: int,
    start_utc: datetime,
    end_utc: datetime | None,
) -> int:
    """Derive one exact positive target department from the saved plans."""
    operation_departments: set[int] = set()
    expected_departments: set[int] = set()
    for employee_id, plan in plans.items():
        for operation in plan.operations:
            if operation.kind == "delete":
                continue
            if operation.kind == "create":
                source: Mapping[str, object] = {}
            else:
                assert operation.attendance_id is not None
                source = _source_row_by_id(source_rows[employee_id], operation.attendance_id)
            effective = _operation_effective_row(operation, source)
            assert effective is not None
            if effective["odoo_work_center_id"] == target_work_center_id and _interval_overlaps(
                effective, start_utc, end_utc
            ):
                operation_departments.add(
                    _positive_int(
                        effective["odoo_department_id"],
                        "saved_target_odoo_department_id",
                    )
                )
        for expected in plan.expected_intervals:
            if expected["odoo_work_center_id"] == target_work_center_id and _interval_overlaps(
                expected, start_utc, end_utc
            ):
                expected_departments.add(
                    _positive_int(
                        expected["odoo_department_id"],
                        "saved_target_odoo_department_id",
                    )
                )
    if len(expected_departments) != 1:
        raise _SourceChanged("saved plan target department is absent or inconsistent")
    if operation_departments and operation_departments != expected_departments:
        raise _SourceChanged("saved operation target departments are inconsistent")
    return next(iter(expected_departments))


def _validate_applying_targets(
    row: Mapping[str, object],
    employee_ids: tuple[int, ...],
    plans: Mapping[int, CorrectionPlan],
    source_rows: Mapping[int, Sequence[Mapping[str, object]]],
    facade,
    *,
    before_remote_call: Callable[[], None] | None = None,
) -> None:
    target_work_center_id = _positive_int(
        row["target_odoo_work_center_id"], "target_odoo_work_center_id"
    )
    saved_department_id = _saved_target_department_id(
        plans=plans,
        source_rows=source_rows,
        target_work_center_id=target_work_center_id,
        start_utc=_aware_utc(row["start_utc"], "start_utc"),
        end_utc=_optional_aware_utc(row.get("end_utc"), "end_utc"),
    )
    try:
        mapped_id, department_id = _resolve_mapping(
            _bounded_text(
                row["target_work_center_name"],
                "target_work_center_name",
                _TEXT_LIMIT,
            ),
            facade,
            before_remote_call=before_remote_call,
        )
    except ValueError as error:
        raise _SourceChanged(str(error)) from error
    if mapped_id != target_work_center_id:
        raise _SourceChanged("saved work-center mapping changed after preview")
    if department_id != saved_department_id:
        raise _SourceChanged("saved target department changed after preview")
    _before_remote(before_remote_call)
    roster = facade.fetch_employee_statuses()
    active_ids = {
        int(item["id"])
        for item in roster
        if isinstance(item, Mapping)
        and isinstance(item.get("id"), int)
        and not isinstance(item.get("id"), bool)
        and item.get("active") is True
    }
    if set(employee_ids) - active_ids:
        raise _SourceChanged("selected employee became inactive after preview")


def _result(
    claim: _JobClaim,
    status: str,
    *,
    error: str | None = None,
    retry_at: datetime | None = None,
) -> CorrectionJobResult:
    try:
        completed = _json_list(claim.row.get("completed_operations", []), "completed_operations")
    except (TypeError, ValueError):
        completed = []
    return CorrectionJobResult(
        job_id=claim.job_id,
        status=status,
        attempt_count=claim.attempt_count,
        error=error,
        retry_at=retry_at,
        completed_operation_count=sum(
            1
            for item in completed
            if item.get("operation_key") is not None and item.get("kind") in _KINDS
        ),
    )


def _process_claim(claim: _JobClaim, *, now_utc: datetime) -> CorrectionJobResult:
    now = _aware_utc(now_utc, "now_utc")
    row = claim.row
    employee_ids = _employee_ids(_decode_json_column(row["employee_odoo_ids"], "employee_odoo_ids"))
    try:
        source_rows = _source_rows_from_json(row["source_snapshot"], employee_ids)
        plans = _plans_from_json(row["operations"], employee_ids)
        completed = _validated_completed_records(row.get("completed_operations", []), plans)
        for employee_id in employee_ids:
            plan_versions = {
                (item.attendance_id, item.write_date) for item in plans[employee_id].source_versions
            }
            source_versions = {
                (int(item["odoo_attendance_id"]), item["odoo_write_date"])
                for item in source_rows[employee_id]
            }
            if plan_versions != source_versions:
                raise ValueError("source snapshot does not match schema-v2 plan")
        all_operations = list(
            _ordered_operations(
                tuple(
                    operation
                    for employee_id in employee_ids
                    for operation in plans[employee_id].operations
                ),
                source_rows=tuple(
                    source for employee_id in employee_ids for source in source_rows[employee_id]
                ),
            )
        )
        _validate_operation_progress(
            completed,
            all_operations,
            current_attempt_count=claim.attempt_count,
        )
        durable_days = _durable_recalc_days(completed)
        days = (
            durable_days
            if durable_days is not None
            else _touched_days(source_rows, plans, open_end=now)
        )
        if len(days) > _MAX_RECALC_HORIZON_DAYS:
            raise ValueError("correction recalculation horizon is too large")
    except Exception as error:  # noqa: BLE001 - corrupt durable plans fail closed
        _transition(
            claim,
            status="failed",
            phase="planning",
            result="invalid_plan",
            detail=_event_detail(job_id=claim.job_id, reason_code="invalid_plan"),
            last_error="fresh preview required: invalid saved plan",
        )
        return _result(claim, "failed", error=str(error))

    if not any(item.get("stage") == "recalc_horizon" for item in completed):
        try:
            frozen = _freeze_recalc_horizon(claim, days)
        except Exception as error:  # noqa: BLE001 - no Odoo work has started
            retry_at = _retry_at(claim.attempt_count, now)
            _transition(
                claim,
                phase="planning",
                result="horizon_failed",
                detail=_event_detail(
                    job_id=claim.job_id,
                    reason_code="horizon_persistence_failed",
                ),
                last_error=str(error),
                retry_at=retry_at,
            )
            return _result(claim, "recoverable", error=str(error), retry_at=retry_at)
        if not frozen:
            return _result(claim, "superseded")
        horizon_marker = {
            "stage": "recalc_horizon",
            "recalc_ids": [day.isoformat() for day in days],
        }
        completed.insert(0, horizon_marker)

    facade = _default_facade()
    completed_keys = {
        item["operation_key"]
        for item in completed
        if isinstance(item.get("operation_key"), str) and item.get("kind") in _KINDS
    }

    if row["status"] == "applying":
        heartbeat = lambda: _heartbeat_claim(claim)
        try:
            _validate_applying_targets(
                row,
                employee_ids,
                plans,
                source_rows,
                facade,
                before_remote_call=heartbeat,
            )
        except _StaleClaim:
            return _result(claim, "superseded")
        except _SourceChanged as error:
            _transition(
                claim,
                status="failed",
                phase="applying",
                result="source_changed",
                detail=_event_detail(
                    job_id=claim.job_id,
                    reason_code="mapping_or_employee_changed",
                ),
                last_error="source_changed: fresh preview required",
            )
            return _result(claim, "failed", error=str(error))
        except Exception as error:  # noqa: BLE001 - target read is retryable
            retry_at = _retry_at(claim.attempt_count, now)
            _transition(
                claim,
                phase="applying",
                result="odoo_failure",
                detail=_event_detail(
                    job_id=claim.job_id,
                    reason_code="target_validation_unavailable",
                ),
                last_error=str(error),
                retry_at=retry_at,
            )
            return _result(claim, "recoverable", error=str(error), retry_at=retry_at)
        try:
            _preflight_operations(
                facade,
                all_operations,
                tuple(
                    source for employee_id in employee_ids for source in source_rows[employee_id]
                ),
                before_remote_call=heartbeat,
            )
        except _StaleClaim:
            return _result(claim, "superseded")
        except _SourceChanged as error:
            _transition(
                claim,
                status="failed",
                phase="applying",
                result="source_changed",
                detail=_event_detail(
                    job_id=claim.job_id,
                    reason_code="preflight_source_changed",
                ),
                last_error="source_changed: fresh preview required",
            )
            return _result(claim, "failed", error=str(error))
        except Exception as error:  # noqa: BLE001 - preflight read is retryable
            retry_at = _retry_at(claim.attempt_count, now)
            _transition(
                claim,
                phase="applying",
                result="odoo_failure",
                detail=_event_detail(
                    job_id=claim.job_id,
                    reason_code="preflight_read_unavailable",
                ),
                last_error=str(error),
                retry_at=retry_at,
            )
            return _result(claim, "recoverable", error=str(error), retry_at=retry_at)
        for operation in all_operations:
            if operation.key in completed_keys:
                continue
            if not _claim_is_current(claim):
                return _result(claim, "superseded")
            try:
                reservation = _reserve_operation(claim, operation)
            except _StaleClaim:
                return _result(claim, "superseded")
            reservation_box = [reservation]

            def renew_operation() -> None:
                reservation_box[0] = _renew_operation_reservation(claim, reservation_box[0])

            try:
                record, confirmation = _perform_operation(
                    facade,
                    operation,
                    source_rows[operation.employee_odoo_id],
                    before_remote_call=renew_operation,
                )
            except _StaleClaim:
                return _result(claim, "superseded")
            except _SourceChanged as error:
                _transition(
                    claim,
                    status="failed",
                    phase="applying",
                    result="source_changed",
                    detail=_event_detail(
                        job_id=claim.job_id,
                        operation_key=operation.key,
                        operation_kind=operation.kind,
                        employee_odoo_id=operation.employee_odoo_id,
                        reason_code="fresh_preview_required",
                    ),
                    last_error="source_changed: fresh preview required",
                )
                return _result(claim, "failed", error=str(error))
            except _RecoverableWrite as error:
                retry_at = max(
                    _retry_at(claim.attempt_count, now),
                    reservation_box[0].reserved_until,
                )
                _transition(
                    claim,
                    phase="applying",
                    result="odoo_failure",
                    detail=_event_detail(
                        job_id=claim.job_id,
                        operation_key=operation.key,
                        operation_kind=operation.kind,
                        employee_odoo_id=operation.employee_odoo_id,
                        reason_code="recoverable_odoo_failure",
                    ),
                    last_error=str(error),
                    retry_at=retry_at,
                )
                return _result(claim, "recoverable", error=str(error), retry_at=retry_at)
            except Exception as error:  # noqa: BLE001 - source reads can fail too
                retry_at = max(
                    _retry_at(claim.attempt_count, now),
                    reservation_box[0].reserved_until,
                )
                _transition(
                    claim,
                    phase="applying",
                    result="odoo_failure",
                    detail=_event_detail(
                        job_id=claim.job_id,
                        operation_key=operation.key,
                        operation_kind=operation.kind,
                        employee_odoo_id=operation.employee_odoo_id,
                        reason_code="odoo_read_unavailable",
                    ),
                    last_error=str(error),
                    retry_at=retry_at,
                )
                return _result(claim, "recoverable", error=str(error), retry_at=retry_at)
            if not _complete_reserved_operation(
                claim,
                reservation_box[0],
                record,
                result=confirmation,
                detail=_event_detail(
                    job_id=claim.job_id,
                    operation_key=operation.key,
                    operation_kind=operation.kind,
                    attendance_id=record["attendance_id"],
                    employee_odoo_id=operation.employee_odoo_id,
                ),
            ):
                return _result(claim, "superseded")
            completed.append(record)
            completed_keys.add(operation.key)
        if not _transition(
            claim,
            status="verifying",
            phase="applying",
            result="operations_complete",
            detail=_event_detail(
                job_id=claim.job_id,
                operation_keys=sorted(completed_keys)[:_MAX_EVENT_IDS],
            ),
        ):
            return _result(claim, "superseded")
        row["status"] = "verifying"

    if row["status"] == "verifying":
        try:
            verified = _verification_rows(
                facade,
                plans,
                completed,
                _aware_utc(row["start_utc"], "start_utc"),
                _optional_aware_utc(row.get("end_utc"), "end_utc"),
                before_remote_call=lambda: _heartbeat_claim(claim),
            )
        except _StaleClaim:
            return _result(claim, "superseded")
        except _SourceChanged as error:  # exact mismatch is terminal
            _transition(
                claim,
                status="failed",
                phase="verifying",
                result="mismatch",
                detail=_event_detail(
                    job_id=claim.job_id,
                    reason_code="verified_intervals_mismatch",
                ),
                last_error="verification failed: fresh preview required",
                verification_increment=True,
            )
            return _result(claim, "failed", error=str(error))
        except Exception as error:  # noqa: BLE001 - verification read is retryable
            retry_at = _retry_at(claim.attempt_count, now)
            _transition(
                claim,
                phase="verifying",
                result="odoo_failure",
                detail=_event_detail(
                    job_id=claim.job_id,
                    reason_code="verification_read_unavailable",
                ),
                last_error=str(error),
                retry_at=retry_at,
            )
            return _result(claim, "recoverable", error=str(error), retry_at=retry_at)
        if not _transition(
            claim,
            status="recalculating",
            phase="verifying",
            result="verified",
            detail=_event_detail(
                job_id=claim.job_id,
                attendance_ids=sorted(int(item["odoo_attendance_id"]) for item in verified)[
                    :_MAX_EVENT_IDS
                ],
            ),
        ):
            return _result(claim, "superseded")
        row["status"] = "recalculating"
    else:
        # A durable recalculation resume re-reads exact Odoo state but never
        # replays writes. This also supplies the rows needed for mirror repair.
        try:
            verified = _verification_rows(
                facade,
                plans,
                completed,
                _aware_utc(row["start_utc"], "start_utc"),
                _optional_aware_utc(row.get("end_utc"), "end_utc"),
                before_remote_call=lambda: _heartbeat_claim(claim),
            )
        except _StaleClaim:
            return _result(claim, "superseded")
        except _SourceChanged as error:
            _transition(
                claim,
                status="failed",
                phase="verifying",
                result="mismatch",
                detail=_event_detail(
                    job_id=claim.job_id,
                    reason_code="verified_intervals_changed",
                ),
                last_error="verification changed: fresh preview required",
                verification_increment=True,
            )
            return _result(claim, "failed", error=str(error))
        except Exception as error:  # noqa: BLE001 - verification read is retryable
            retry_at = _retry_at(claim.attempt_count, now)
            _transition(
                claim,
                phase="verifying",
                result="odoo_failure",
                detail=_event_detail(
                    job_id=claim.job_id,
                    reason_code="verification_read_unavailable",
                ),
                last_error=str(error),
                retry_at=retry_at,
            )
            return _result(claim, "recoverable", error=str(error), retry_at=retry_at)

    stages = {str(item["stage"]) for item in completed if isinstance(item.get("stage"), str)}
    # ``days`` came from the pre-I/O durable horizon above. Later claims wait
    # on this same bounded set rather than expanding an open interval past days
    # that were never enqueued.
    if "mirror_complete" not in stages:
        try:
            mirror_complete = _mirror_verified_rows(
                claim,
                verified,
                source_rows,
                completed,
                completed_at=now,
            )
        except Exception as error:  # noqa: BLE001 - retry downstream only
            retry_at = _retry_at(claim.attempt_count, now)
            _transition(
                claim,
                phase="mirror",
                result="failed",
                detail=_event_detail(job_id=claim.job_id, reason_code="mirror_refresh_failed"),
                last_error=str(error),
                retry_at=retry_at,
            )
            return _result(claim, "recoverable", error=str(error), retry_at=retry_at)
        if not mirror_complete:
            return _result(claim, "superseded")
        marker = {"stage": "mirror_complete"}
        completed.append(marker)
        stages.add("mirror_complete")

    if "recalc_enqueued" not in stages:
        try:
            enqueued = _enqueue_recalculation(
                claim,
                days,
                completed,
                requested_at=now,
            )
        except Exception as error:  # noqa: BLE001 - durable enqueue is retryable
            retry_at = _retry_at(claim.attempt_count, now)
            _transition(
                claim,
                phase="recalculation",
                result="failed",
                detail=_event_detail(
                    job_id=claim.job_id,
                    reason_code="recalculation_enqueue_failed",
                ),
                last_error=str(error),
                retry_at=retry_at,
            )
            return _result(claim, "recoverable", error=str(error), retry_at=retry_at)
        if not enqueued:
            return _result(claim, "superseded")
        marker = {
            "stage": "recalc_enqueued",
            "recalc_ids": [day.isoformat() for day in days],
        }
        completed.append(marker)
        stages.add("recalc_enqueued")

    try:
        recalculated = _run_recalculation(days)
    except Exception as error:  # noqa: BLE001 - production source retry
        recalculated = False
        recalc_error = str(error)
    else:
        recalc_error = "targeted recalculation is still pending"
    if not recalculated:
        retry_at = _retry_at(claim.attempt_count, now)
        _transition(
            claim,
            phase="recalculation",
            result="failed",
            detail=_event_detail(
                job_id=claim.job_id,
                reason_code="recalculation_pending",
            ),
            last_error=recalc_error,
            retry_at=retry_at,
        )
        return _result(claim, "recoverable", error=recalc_error, retry_at=retry_at)
    if "recalc_complete" not in stages:
        marker = {"stage": "recalc_complete"}
        if not _complete_record(
            claim,
            marker,
            phase="recalculation",
            result="complete",
            detail=_event_detail(
                job_id=claim.job_id,
                recalc_ids=[day.isoformat() for day in days][:_MAX_EVENT_IDS],
            ),
        ):
            return _result(claim, "superseded")
        completed.append(marker)
        stages.add("recalc_complete")

    if "cache_refreshed" not in stages:
        if not _claim_is_current(claim):
            return _result(claim, "superseded")
        # Whichever durable recalculation worker completes each queue day owns
        # its one post-commit cache refresh. This job only records that every
        # target day reached that boundary; refreshing here would do it twice
        # and would race a normal recalc warmer that completed the same day.
        marker = {"stage": "cache_refreshed"}
        if not _complete_record(
            claim,
            marker,
            phase="cache",
            result="complete",
            detail=_event_detail(job_id=claim.job_id),
        ):
            return _result(claim, "superseded")
        completed.append(marker)

    try:
        complete = _complete_with_audit(
            claim,
            source_rows=source_rows,
            plans=plans,
            completed=completed,
            completed_at=now,
        )
    except Exception as error:  # audit failure keeps durable job incomplete
        retry_at = _retry_at(claim.attempt_count, now)
        _transition(
            claim,
            phase="audit",
            result="failed",
            detail=_event_detail(job_id=claim.job_id, reason_code="audit_write_failed"),
            last_error=str(error),
            retry_at=retry_at,
        )
        return _result(claim, "recoverable", error=str(error), retry_at=retry_at)
    if not complete:
        return _result(claim, "superseded")
    return _result(claim, "complete")


def process_job(job_id: int) -> CorrectionJobResult:
    """Claim and advance one exact job without holding DB locks over I/O."""
    now = datetime.now(UTC)
    claim = _claim_job(job_id=_positive_int(job_id, "job_id"), now_utc=now)
    if claim is None:
        from . import db

        rows = db.query(
            "SELECT id, status, attempt_count, last_error, completed_operations "
            "FROM attendance_correction_jobs WHERE id = %s",
            (job_id,),
        )
        if not rows:
            raise ValueError("correction job does not exist")
        row = rows[0]
        return CorrectionJobResult(
            job_id=int(row["id"]),
            status=(row["status"] if row["status"] in ("complete", "failed") else "superseded"),
            attempt_count=int(row.get("attempt_count") or 0),
            error=row.get("last_error"),
            completed_operation_count=sum(
                1
                for item in _json_list(row.get("completed_operations", []), "completed_operations")
                if item.get("operation_key") is not None and item.get("kind") in _KINDS
            ),
        )
    return _process_claim(claim, now_utc=now)


def process_next() -> CorrectionJobResult | None:
    """Claim the oldest eligible correction and advance at most one job."""
    now = datetime.now(UTC)
    claim = _claim_job(job_id=None, now_utc=now)
    if claim is None:
        return None
    return _process_claim(claim, now_utc=now)


__all__ = [
    "CorrectionJobResult",
    "CorrectionOperation",
    "CorrectionPlan",
    "CorrectionPreview",
    "SourceVersion",
    "correction_preview",
    "create_job",
    "plan_correction",
    "plan_from_json",
    "plan_to_json",
    "process_job",
    "process_next",
]
