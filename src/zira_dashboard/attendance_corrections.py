"""Pure, deterministic planning for exact Odoo attendance corrections.

This module deliberately performs no I/O.  It turns one employee's complete,
fresh attendance snapshot into an immutable plan that a durable executor can
apply and verify later.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Literal, TypeAlias


OperationKind: TypeAlias = Literal["create", "update", "delete"]
JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]

_SCHEMA_VERSION = 2
_KEY_PREFIX = "attendance-correction-v2:"
_KEY_PATTERN = re.compile(r"^attendance-correction-v2:[0-9a-f]{64}$")
_UTC_TEXT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z$")
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
_REQUEST_FIELDS = frozenset(
    (
        "employee_odoo_id",
        "start_utc",
        "end_utc",
        "odoo_work_center_id",
        "odoo_department_id",
    )
)


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
        del value
        raise AttributeError(f"{type(self).__name__} is immutable: {name}")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable: {name}")

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
    request: Mapping[str, object]
    source_intervals: tuple[Mapping[str, object], ...]

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
        if not isinstance(self.request, Mapping):
            raise TypeError("request must be a mapping")
        if isinstance(self.source_intervals, (str, bytes)) or not isinstance(
            self.source_intervals, Sequence
        ):
            raise TypeError("source_intervals must be a sequence")
        versions = tuple(self.source_versions)
        operations = tuple(self.operations)
        if not all(isinstance(item, SourceVersion) for item in versions):
            raise TypeError("source_versions must contain SourceVersion values")
        if not all(isinstance(item, CorrectionOperation) for item in operations):
            raise TypeError("operations must contain CorrectionOperation values")
        intervals = tuple(_FrozenMapping(item) for item in self.expected_intervals)
        request = _FrozenMapping(self.request)
        source_intervals = tuple(_FrozenMapping(item) for item in self.source_intervals)
        object.__setattr__(self, "source_versions", versions)
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "expected_intervals", intervals)
        object.__setattr__(self, "request", request)
        object.__setattr__(self, "source_intervals", source_intervals)
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
) -> _Piece:
    return _Piece(
        start=start,
        end=end,
        work_center_id=work_center_id,
        department_id=department_id,
        attendance_id=source.attendance_id if source is not None else None,
        source=source,
        target=True,
    )


def _closed_pieces(
    sources: tuple[_SourceRow, ...],
    start: datetime,
    end: datetime,
    work_center_id: int,
    department_id: int | None,
) -> list[_Piece]:
    overlaps = [
        item for item in sources if item.start < end and (item.end is None or item.end > start)
    ]
    if not overlaps:
        return [
            *map(_untouched_piece, sources),
            _target_piece(None, start, end, work_center_id, department_id),
        ]

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
        used_ids: set[int] = set()
        if first.start < target_start:
            pieces.append(
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
            used_ids.add(first.attendance_id)
        if last.end is None or last.end > target_end:
            right_id = last.attendance_id if last.attendance_id not in used_ids else None
            pieces.append(
                _piece_from_source(
                    last,
                    start=target_end,
                    end=last.end,
                    work_center_id=last.work_center_id,
                    department_id=last.department_id,
                    attendance_id=right_id,
                    target=False,
                )
            )
            if right_id is not None:
                used_ids.add(right_id)
        reusable = next(
            (
                source
                for source in group
                if source.attendance_id not in used_ids
                and source.start >= target_start
                and source.end is not None
                and source.end <= target_end
            ),
            None,
        )
        pieces.append(
            _target_piece(
                reusable,
                target_start,
                target_end,
                work_center_id,
                department_id,
            )
        )
    return pieces


def _open_pieces(
    sources: tuple[_SourceRow, ...],
    start: datetime,
    work_center_id: int,
    department_id: int | None,
) -> list[_Piece]:
    affected = [item for item in sources if item.end is None or item.end > start]
    unaffected = [item for item in sources if item.end is not None and item.end <= start]
    if not affected:
        return [
            *map(_untouched_piece, sources),
            _target_piece(None, start, None, work_center_id, department_id),
        ]
    used_ids: set[int] = set()
    pieces = [*map(_untouched_piece, unaffected)]
    first = affected[0]
    if first.start < start:
        pieces.append(
            _piece_from_source(
                first,
                start=first.start,
                end=start,
                work_center_id=first.work_center_id,
                department_id=first.department_id,
                attendance_id=first.attendance_id,
                target=False,
            )
        )
        used_ids.add(first.attendance_id)
    reusable = next(
        (
            source
            for source in affected
            if source.attendance_id not in used_ids and source.start >= start
        ),
        None,
    )
    pieces.append(_target_piece(reusable, start, None, work_center_id, department_id))
    return pieces


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
    if piece.attendance_id is None or piece.source is None:
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


def _operation_key(
    *,
    kind: OperationKind,
    attendance_id: int | None,
    employee_id: int,
    before: Mapping[str, object] | None,
    after: Mapping[str, object] | None,
    request: Mapping[str, object],
    source_intervals: Sequence[Mapping[str, object]],
) -> str:
    identity = {
        "contract": "attendance-correction-operation-v2",
        "request": request,
        "source_intervals": source_intervals,
        "kind": kind,
        "attendance_id": attendance_id,
        "employee_odoo_id": employee_id,
        "before": before,
        "after": after,
    }
    encoded = json.dumps(
        _operation_key_value(identity),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return _KEY_PREFIX + hashlib.sha256(encoded).hexdigest()


def _operations_for_pieces(
    *,
    sources: tuple[_SourceRow, ...],
    pieces: list[_Piece],
    employee_id: int,
) -> list[_PendingOperation]:
    pending: list[_PendingOperation] = []
    pieces_by_id = {
        piece.attendance_id: piece for piece in pieces if piece.attendance_id is not None
    }
    for source in sources:
        piece = pieces_by_id.get(source.attendance_id)
        if piece is None:
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
        after_values = _piece_mutable_values(piece, employee_id)
        changed = tuple(
            field for field in _MUTABLE_FIELDS if before_values[field] != after_values[field]
        )
        if changed:
            pending.append(
                _PendingOperation(
                    kind="update",
                    attendance_id=source.attendance_id,
                    employee_id=employee_id,
                    before={field: before_values[field] for field in changed},
                    after={field: after_values[field] for field in changed},
                    source=source,
                    effective_start=piece.start,
                )
            )
    for piece in pieces:
        if piece.attendance_id is not None:
            continue
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


def _correction_is_no_op(
    sources: tuple[_SourceRow, ...],
    *,
    start: datetime,
    end: datetime | None,
    work_center_id: int,
    department_id: int | None,
) -> bool:
    if end is None:
        affected = [item for item in sources if item.end is None or item.end > start]
        return (
            len(affected) == 1
            and affected[0].start <= start
            and affected[0].end is None
            and affected[0].work_center_id == work_center_id
            and affected[0].department_id == department_id
        )
    overlaps = [
        item for item in sources if item.start < end and (item.end is None or item.end > start)
    ]
    return bool(overlaps) and all(
        item.work_center_id == work_center_id and item.department_id == department_id
        for item in overlaps
    )


def _pieces_for_request(
    sources: tuple[_SourceRow, ...],
    *,
    start: datetime,
    end: datetime | None,
    work_center_id: int,
    department_id: int | None,
) -> list[_Piece]:
    if _correction_is_no_op(
        sources,
        start=start,
        end=end,
        work_center_id=work_center_id,
        department_id=department_id,
    ):
        return list(map(_untouched_piece, sources))
    if end is None:
        return _open_pieces(sources, start, work_center_id, department_id)
    return _closed_pieces(sources, start, end, work_center_id, department_id)


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
    request = _FrozenMapping(
        {
            "employee_odoo_id": employee_id,
            "start_utc": start,
            "end_utc": end,
            "odoo_work_center_id": work_center_id,
            "odoo_department_id": department_id,
        }
    )
    source_intervals = tuple(item.values for item in sources)

    pieces = _pieces_for_request(
        sources,
        start=start,
        end=end,
        work_center_id=work_center_id,
        department_id=department_id,
    )
    pending = _operations_for_pieces(
        sources=sources,
        pieces=pieces,
        employee_id=employee_id,
    )
    operations = [
        CorrectionOperation(
            key=_operation_key(
                kind=item.kind,
                attendance_id=item.attendance_id,
                employee_id=item.employee_id,
                before=item.before,
                after=item.after,
                request=request,
                source_intervals=source_intervals,
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
    return CorrectionPlan(
        versions,
        tuple(operations),
        tuple(expected),
        request,
        source_intervals,
    )


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
        if any(operation.before[field] == operation.after[field] for field in operation.before):
            raise ValueError("every update field must change")
        for values in (operation.before, operation.after):
            if (
                "employee_odoo_id" in values
                and values["employee_odoo_id"] != operation.employee_odoo_id
            ):
                raise ValueError("update employee does not match operation employee")


def _validate_request(
    value: Mapping[str, object],
) -> tuple[int, datetime, datetime | None, int, int | None]:
    _exact_keys(value, _REQUEST_FIELDS, "correction request")
    employee_id = _positive_int(value["employee_odoo_id"], "employee_odoo_id")
    start = _aware_utc(value["start_utc"], "start_utc")
    end = _optional_aware_utc(value["end_utc"], "end_utc")
    if end is not None and end <= start:
        raise ValueError("request interval must have positive duration")
    work_center_id = _positive_int(value["odoo_work_center_id"], "odoo_work_center_id")
    department_id = _optional_positive_int(value["odoo_department_id"], "odoo_department_id")
    return employee_id, start, end, work_center_id, department_id


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


def _source_expected_mapping(source: _SourceRow) -> dict[str, object]:
    values = {
        key: value
        for key, value in source.values.items()
        if key not in {"odoo_write_date", "write_date", "id"}
    }
    values.update(_source_mutable_values(source))
    values["odoo_attendance_id"] = source.attendance_id
    return values


def _simulate_operations(
    sources: tuple[_SourceRow, ...],
    operations: tuple[CorrectionOperation, ...],
) -> tuple[Mapping[str, object], ...]:
    state = {source.attendance_id: _source_expected_mapping(source) for source in sources}
    mutable_by_id = {source.attendance_id: _source_mutable_values(source) for source in sources}
    created: list[dict[str, object]] = []
    for operation in operations:
        if operation.kind == "create":
            assert operation.after is not None
            created.append({"odoo_attendance_id": None, **dict(operation.after)})
            continue
        assert operation.attendance_id is not None
        if operation.attendance_id not in state:
            raise ValueError("operation projection references a consumed source row")
        if operation.kind == "delete":
            assert operation.before is not None
            if dict(operation.before) != mutable_by_id[operation.attendance_id]:
                raise ValueError("delete before values do not match the source snapshot")
            del state[operation.attendance_id]
            continue
        assert operation.before is not None and operation.after is not None
        current = state[operation.attendance_id]
        for field in operation.before:
            if current.get(field) != operation.before[field]:
                raise ValueError("update before values do not match the source snapshot")
        if (
            "odoo_work_center_id" in operation.after
            and current.get("odoo_work_center_id") != operation.after["odoo_work_center_id"]
        ):
            current.pop("odoo_work_center_name", None)
        if (
            "odoo_department_id" in operation.after
            and current.get("odoo_department_id") != operation.after["odoo_department_id"]
        ):
            current.pop("odoo_department_name", None)
        current.update(operation.after)

    projected = [*state.values(), *created]
    projected.sort(
        key=lambda item: (
            item["check_in_utc"],
            item["odoo_attendance_id"] if item["odoo_attendance_id"] is not None else 2**63,
        )
    )
    return tuple(_FrozenMapping(item) for item in projected)


def _validate_plan(plan: CorrectionPlan) -> None:
    (
        request_employee_id,
        request_start,
        request_end,
        request_work_center_id,
        request_department_id,
    ) = _validate_request(plan.request)
    source_ids_in_payload = tuple(
        _positive_int(item.get("odoo_attendance_id"), "odoo_attendance_id")
        for item in plan.source_intervals
    )
    sources = _normalize_source_rows(plan.source_intervals, request_employee_id)
    if source_ids_in_payload != tuple(item.attendance_id for item in sources):
        raise ValueError("source intervals are not in canonical order")
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
    payload_versions = tuple(
        sorted(
            (SourceVersion(item.attendance_id, item.write_date) for item in sources),
            key=lambda item: (item.attendance_id, item.write_date),
        )
    )
    if plan.source_versions != payload_versions:
        raise ValueError("source versions do not match source intervals")
    operation_keys = [item.key for item in plan.operations]
    if len(operation_keys) != len(set(operation_keys)):
        raise ValueError("duplicate correction operation key")
    operation_ids = [
        item.attendance_id for item in plan.operations if item.attendance_id is not None
    ]
    if len(operation_ids) != len(set(operation_ids)):
        raise ValueError("duplicate operation attendance id")
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
    known_ids = set(version_ids)
    for operation in plan.operations:
        if operation.attendance_id is not None and operation.attendance_id not in known_ids:
            raise ValueError("operation references an unknown source attendance id")
        if operation.employee_odoo_id != request_employee_id:
            raise ValueError("operation employee does not match correction request")
        expected_key = _operation_key(
            kind=operation.kind,
            attendance_id=operation.attendance_id,
            employee_id=operation.employee_odoo_id,
            before=operation.before,
            after=operation.after,
            request=plan.request,
            source_intervals=plan.source_intervals,
        )
        if operation.key != expected_key:
            raise ValueError("operation key does not authenticate plan contents")

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
    employee_ids.update(item.employee_odoo_id for item in plan.operations)
    employee_ids.add(request_employee_id)
    if len(employee_ids) > 1:
        raise ValueError("correction plan contains mixed employees")
    previous_end: datetime | None = None
    for index, (start, end, attendance_id, _employee_id) in enumerate(interval_state):
        if index and (previous_end is None or previous_end > start):
            raise ValueError("expected intervals overlap")
        previous_end = end
        if attendance_id is not None:
            if attendance_id in seen_interval_ids:
                raise ValueError("duplicate expected attendance id")
            if attendance_id not in known_ids:
                raise ValueError("expected interval references unknown source id")
            seen_interval_ids.add(attendance_id)
    if plan.expected_intervals != _simulate_operations(sources, plan.operations):
        raise ValueError("operation projection does not match expected intervals")

    planned_pieces = _pieces_for_request(
        sources,
        start=request_start,
        end=request_end,
        work_center_id=request_work_center_id,
        department_id=request_department_id,
    )
    planned_expected = tuple(
        _FrozenMapping(_expected_mapping(piece, request_employee_id))
        for piece in sorted(
            planned_pieces,
            key=lambda item: (
                item.start,
                item.attendance_id if item.attendance_id is not None else 2**63,
            ),
        )
    )
    if plan.expected_intervals != planned_expected:
        raise ValueError("expected intervals do not implement the correction request")
    pending = _operations_for_pieces(
        sources=sources,
        pieces=planned_pieces,
        employee_id=request_employee_id,
    )
    planned_operations = [
        CorrectionOperation(
            key=_operation_key(
                kind=item.kind,
                attendance_id=item.attendance_id,
                employee_id=item.employee_id,
                before=item.before,
                after=item.after,
                request=plan.request,
                source_intervals=plan.source_intervals,
            ),
            kind=item.kind,
            attendance_id=item.attendance_id,
            employee_odoo_id=item.employee_id,
            before=item.before,
            after=item.after,
        )
        for item in pending
    ]
    planned_expected_by_id = {
        item["odoo_attendance_id"]: item
        for item in planned_expected
        if item["odoo_attendance_id"] is not None
    }
    planned_operations.sort(key=lambda item: _operation_sort_key(item, planned_expected_by_id))
    if plan.operations != tuple(planned_operations):
        raise ValueError("operations do not implement the correction request")


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


def plan_to_json(plan: CorrectionPlan) -> JSONValue:
    """Return the canonical, lossless JSONB value for an immutable plan."""

    if not isinstance(plan, CorrectionPlan):
        raise TypeError("plan must be a CorrectionPlan")
    _validate_plan(plan)
    return {
        "schema_version": _SCHEMA_VERSION,
        "request": _encode_data(plan.request),
        "source_intervals": [_encode_data(item) for item in plan.source_intervals],
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
                "request",
                "source_intervals",
                "source_versions",
                "operations",
                "expected_intervals",
            )
        ),
        "correction plan JSON",
    )
    version = value["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != _SCHEMA_VERSION:
        raise ValueError("unsupported correction plan schema version")

    request = _decode_data(value["request"])
    if not isinstance(request, Mapping):
        raise TypeError("request must decode to a mapping")
    source_intervals: list[Mapping[str, object]] = []
    for item in _require_json_list(value["source_intervals"], "source_intervals"):
        decoded = _decode_data(item)
        if not isinstance(decoded, Mapping):
            raise TypeError("source interval must decode to a mapping")
        source_intervals.append(decoded)

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
    return CorrectionPlan(
        tuple(source_versions),
        tuple(operations),
        tuple(expected),
        request,
        tuple(source_intervals),
    )
