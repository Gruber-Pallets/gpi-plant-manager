"""Build exact, immutable-version projections for the Odoo feedback mirror."""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from . import feedback_store
from .feedback_image import MAX_OUTPUT_BYTES, OUTPUT_LONG_SIDE, NormalizedImage
from .feedback_types import FEEDBACK_TYPES, feedback_type_or_legacy_bug
from .odoo_improvements import ContractError, ImprovementContract


MAX_SIGNED_64 = 9_223_372_036_854_775_807

STATUS_VALUES = {
    "requested": "Requested",
    "in_progress": "In-Progress",
    "completed": "Completed",
    "declined": "Declined",
}
TYPE_VALUES = {item.value: item.odoo_value for item in FEEDBACK_TYPES}

_BINARY_FIELDS = {
    "before": "x_studio_image",
    "after": "x_studio_after_image",
}
_BINARY_FIELD_NAMES = frozenset(_BINARY_FIELDS.values())
_NONBINARY_FIELD_NAMES = frozenset(
    {
        "x_name",
        "x_studio_source_id",
        "x_studio_date_start",
        "x_studio_type",
        "x_studio_status",
        "x_studio_source",
        "x_studio_submitted_by",
        "x_studio_date_stop",
        "x_studio_completed_by",
        "x_studio_notes",
    }
)
_PROJECTION_TYPE_VALUES = frozenset(TYPE_VALUES.values())
_EMPLOYEE_FIELDS = frozenset({"x_studio_submitted_by", "x_studio_completed_by"})
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+", re.ASCII)
_SOURCE_ID_RE = re.compile(r"GPI-PM-FB-([1-9][0-9]*)", re.ASCII)
_CHICAGO = ZoneInfo("America/Chicago")


class ReadbackMismatch(RuntimeError):
    """A remote read did not exactly match the dispatched projection."""


@dataclass(frozen=True)
class BinaryEvidence:
    jpeg_bytes: bytes
    sha256: str
    byte_length: int


def _feedback_id_from_source(source_id: object) -> int:
    if type(source_id) is not str or len(source_id) > len("GPI-PM-FB-") + 19:
        raise ValueError("projection source id is not canonical")
    match = _SOURCE_ID_RE.fullmatch(source_id)
    if match is None:
        raise ValueError("projection source id is not canonical")
    feedback_id = int(match.group(1))
    if feedback_id > MAX_SIGNED_64:
        raise ValueError("projection source id is not canonical")
    return feedback_id


def _validated_binary_evidence(field_name: object, evidence: object) -> tuple[str, BinaryEvidence]:
    if type(field_name) is not str or field_name not in _BINARY_FIELD_NAMES:
        raise ValueError("projection binary field is not allowlisted")
    if type(evidence) is not BinaryEvidence:
        raise ValueError("projection binary evidence is malformed")
    raw = evidence.jpeg_bytes
    if type(raw) is not bytes or not raw or len(raw) > MAX_OUTPUT_BYTES:
        raise ValueError("projection binary bytes are malformed")
    if (
        type(evidence.byte_length) is not int
        or evidence.byte_length <= 0
        or evidence.byte_length != len(raw)
    ):
        raise ValueError("projection binary length is malformed")
    digest = hashlib.sha256(raw).hexdigest()
    if type(evidence.sha256) is not str or evidence.sha256 != digest:
        raise ValueError("projection binary hash is malformed")
    return field_name, BinaryEvidence(
        jpeg_bytes=bytes(raw),
        sha256=digest,
        byte_length=len(raw),
    )


@dataclass(frozen=True, init=False)
class Projection:
    source_id: str
    manifest_digest: str
    _fields_json: str = field(repr=False)
    _binary_items: tuple[tuple[str, BinaryEvidence], ...] = field(repr=False)
    _manifest_json: str = field(repr=False)

    def __init__(
        self,
        *,
        source_id: str,
        fields: Mapping[str, object],
        binaries: Mapping[str, BinaryEvidence],
        manifest: Mapping[str, object],
        manifest_digest: str,
    ):
        _feedback_id_from_source(source_id)
        if not isinstance(fields, Mapping) or any(
            type(field_name) is not str for field_name in fields
        ):
            raise ValueError("projection fields are malformed")
        copied_fields = dict(fields)
        field_source_id = copied_fields.get("x_studio_source_id")
        if type(field_source_id) is not str or field_source_id != source_id:
            raise ValueError("projection source id does not match fields")
        source_namespace = copied_fields.get("x_studio_source")
        if type(source_namespace) is not str or source_namespace != "GPI Plant Manager":
            raise ValueError("projection source namespace is not canonical")
        if _BINARY_FIELD_NAMES & copied_fields.keys():
            raise ValueError("binary values are forbidden from nonbinary fields")
        if any("token" in field_name.casefold() for field_name in copied_fields):
            raise ValueError("token fields are forbidden from projection")
        if copied_fields.keys() - _NONBINARY_FIELD_NAMES:
            raise ValueError("projection nonbinary field is not allowlisted")
        projection_type = copied_fields.get("x_studio_type")
        if projection_type is not None and (
            type(projection_type) is not str or projection_type not in _PROJECTION_TYPE_VALUES
        ):
            raise ValueError("projection type is not writable")
        try:
            fields_json = _canonical_json(copied_fields)
        except (TypeError, ValueError):
            raise ValueError("projection fields are not canonical JSON") from None

        if not isinstance(binaries, Mapping):
            raise ValueError("projection binaries are malformed")
        seen_binary_fields: set[str] = set()
        copied_binary_items: list[tuple[str, BinaryEvidence]] = []
        for field_name, evidence in binaries.items():
            copied_name, copied_evidence = _validated_binary_evidence(field_name, evidence)
            if copied_name in seen_binary_fields:
                raise ValueError("projection binary field is duplicated")
            seen_binary_fields.add(copied_name)
            copied_binary_items.append((copied_name, copied_evidence))
        binary_items = tuple(sorted(copied_binary_items))
        canonical_manifest = {
            "fields": json.loads(fields_json),
            "binary_evidence": {
                name: {
                    "sha256": evidence.sha256,
                    "byte_length": evidence.byte_length,
                }
                for name, evidence in binary_items
            },
        }
        manifest_json = _canonical_json(canonical_manifest)
        try:
            supplied_manifest_json = _canonical_json(manifest)
        except (TypeError, ValueError):
            raise ValueError("manifest is not canonical JSON") from None
        if supplied_manifest_json != manifest_json:
            raise ValueError("manifest does not match canonical projection state")
        calculated_digest = hashlib.sha256(manifest_json.encode()).hexdigest()
        if manifest_digest != calculated_digest:
            raise ValueError("projection manifest digest does not match canonical state")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "manifest_digest", calculated_digest)
        object.__setattr__(self, "_fields_json", fields_json)
        object.__setattr__(self, "_binary_items", binary_items)
        object.__setattr__(self, "_manifest_json", manifest_json)

    @property
    def fields(self) -> dict[str, object]:
        """Return a detached normal dictionary for the nonbinary fields."""
        return json.loads(self._fields_json)

    @property
    def binaries(self) -> dict[str, BinaryEvidence]:
        """Return a detached dictionary with copied immutable evidence."""
        return {
            name: BinaryEvidence(
                jpeg_bytes=bytes(evidence.jpeg_bytes),
                sha256=evidence.sha256,
                byte_length=evidence.byte_length,
            )
            for name, evidence in self._binary_items
        }

    @property
    def manifest(self) -> dict[str, object]:
        """Return a detached normal JSON dictionary ready for persistence."""
        return json.loads(self._manifest_json)

    def dispatch_fields(self) -> dict[str, object]:
        """Return a new RPC payload, adding binary values only to that copy."""
        values = self.fields
        for field_name, evidence in self._binary_items:
            values[field_name] = base64.b64encode(evidence.jpeg_bytes).decode("ascii")
        return values


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _positive_signed_64(value: object, label: str, error_type=ValueError) -> int:
    if type(value) is not int or not 0 < value <= MAX_SIGNED_64:
        raise error_type(f"{label} must be a positive signed-64-bit integer")
    return value


def source_id_for(feedback_id: int) -> str:
    """Return this app's exact compound source identifier."""
    safe_feedback_id = _positive_signed_64(feedback_id, "feedback id")
    return f"GPI-PM-FB-{safe_feedback_id}"


def normalize_email(value: str | None) -> str | None:
    """Normalize an optional local work email without accepting another type."""
    if value is None:
        return None
    if type(value) is not str:
        return None
    cleaned = value.strip().casefold()
    return cleaned or None


def _valid_lookup_email(value: str) -> bool:
    return (
        value.isascii()
        and len(value) <= 320
        and not any(wildcard in value for wildcard in ("%", "_", "\\"))
        and _EMAIL_RE.fullmatch(value) is not None
    )


def resolve_employee_id(
    client,
    email: str | None,
    *,
    feedback_id: int,
    projection_version: int,
    warn: Callable[[int, int, str], None],
) -> int | None:
    """Resolve one normalized employee or emit a privacy-safe local warning."""
    safe_feedback_id = _positive_signed_64(feedback_id, "feedback id")
    safe_version = _positive_signed_64(projection_version, "projection version")
    normalized = normalize_email(email)
    if normalized is None:
        if email is not None and email != "":
            warn(safe_feedback_id, safe_version, "employee_missing")
        return None
    if not _valid_lookup_email(normalized):
        warn(safe_feedback_id, safe_version, "employee_missing")
        return None

    rows = client.find_employees_by_email(normalized, limit=3)
    if type(rows) is not list or len(rows) > 3:
        raise ContractError("employee lookup response was malformed")
    for row in rows:
        if not isinstance(row, Mapping) or type(row.get("work_email")) is not str:
            raise ContractError("employee lookup response was malformed")
        _positive_signed_64(row.get("id"), "employee id", ContractError)
    exact = [row for row in rows if normalize_email(row.get("work_email")) == normalized]
    if len(exact) != 1:
        warning_class = "employee_missing" if not exact else "employee_ambiguous"
        warn(safe_feedback_id, safe_version, warning_class)
        return None

    return _positive_signed_64(exact[0].get("id"), "employee id", ContractError)


def _odoo_time(value: object, field_type: str) -> str:
    if field_type not in {"date", "datetime"}:
        raise ValueError("Odoo date field must be date or datetime")
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError("feedback datetime must be timezone-aware")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError):
        offset = None
    if offset is None:
        raise ValueError("feedback datetime must be timezone-aware")
    aware = value.astimezone(UTC)
    if field_type == "datetime":
        return aware.strftime("%Y-%m-%d %H:%M:%S")
    return value.astimezone(_CHICAGO).date().isoformat()


def _binary_evidence(image: object) -> BinaryEvidence:
    if type(image) is not NormalizedImage:
        raise ValueError("projection images must be normalized images")
    raw = image.jpeg_bytes
    if type(raw) is not bytes or not raw or len(raw) > MAX_OUTPUT_BYTES:
        raise ValueError("normalized image bytes are malformed")
    if (
        type(image.byte_length) is not int
        or image.byte_length != len(raw)
        or not 0 < image.byte_length <= MAX_OUTPUT_BYTES
    ):
        raise ValueError("normalized image length is malformed")
    digest = hashlib.sha256(raw).hexdigest()
    if type(image.sha256) is not str or image.sha256 != digest:
        raise ValueError("normalized image hash is malformed")
    if (
        type(image.width) is not int
        or type(image.height) is not int
        or not 0 < image.width <= OUTPUT_LONG_SIDE
        or not 0 < image.height <= OUTPUT_LONG_SIDE
    ):
        raise ValueError("normalized image dimensions are malformed")
    return BinaryEvidence(bytes(raw), digest, len(raw))


def _manifest(
    fields: Mapping[str, object], binaries: Mapping[str, BinaryEvidence]
) -> tuple[dict[str, object], str]:
    value: dict[str, object] = {
        "fields": dict(fields),
        "binary_evidence": {
            name: {"sha256": item.sha256, "byte_length": item.byte_length}
            for name, item in sorted(binaries.items())
        },
    }
    encoded = _canonical_json(value).encode()
    return value, hashlib.sha256(encoded).hexdigest()


def _resolve_with_lookup(
    employee_lookup: Callable[[str], object],
    email: object,
    *,
    feedback_id: int,
    projection_version: int,
    warn: Callable[[int, int, str], None],
) -> int | None:
    normalized = normalize_email(email)
    if normalized is None:
        if email is not None and email != "":
            warn(feedback_id, projection_version, "employee_missing")
        return None
    if not _valid_lookup_email(normalized):
        warn(feedback_id, projection_version, "employee_missing")
        return None
    employee_id = employee_lookup(normalized)
    if employee_id is None:
        warn(feedback_id, projection_version, "employee_missing")
        return None
    return _positive_signed_64(employee_id, "employee id", ContractError)


def build_projection(
    feedback: Mapping[str, object],
    *,
    images: Mapping[str, NormalizedImage],
    employee_lookup: Callable[[str], object],
    start_type: str,
    stop_type: str,
    warn: Callable[[int, int, str], None] | None = None,
    employee_resolver: Callable[[object], int | None] | None = None,
) -> Projection:
    """Map one exact local feedback version to nonbinary Odoo fields."""
    if not isinstance(feedback, Mapping):
        raise ValueError("feedback must be a mapping")
    if not isinstance(images, Mapping) or len(images) > 2:
        raise ValueError("projection images must be a bounded mapping")
    if not callable(employee_lookup):
        raise ValueError("employee lookup must be callable")
    if start_type not in {"date", "datetime"} or stop_type not in {
        "date",
        "datetime",
    }:
        raise ValueError("Odoo date field must be date or datetime")

    feedback_id = _positive_signed_64(feedback.get("id"), "feedback id")
    projection_version = _positive_signed_64(
        feedback.get("projection_version"), "projection version"
    )
    message = feedback.get("message")
    if type(message) is not str or not message.strip():
        raise ValueError("feedback message must be a nonempty string")
    status = feedback.get("status")
    if status not in STATUS_VALUES or type(status) is not str:
        raise ValueError("unsupported feedback status")
    task_type = feedback.get("task_type")
    canonical_type = feedback_type_or_legacy_bug(task_type)

    source_id = source_id_for(feedback_id)
    fields: dict[str, object] = {
        "x_name": message,
        "x_studio_source_id": source_id,
        "x_studio_date_start": _odoo_time(feedback.get("created_at"), start_type),
        "x_studio_type": canonical_type.odoo_value,
        "x_studio_status": STATUS_VALUES[status],
        "x_studio_source": "GPI Plant Manager",
    }
    warning_callback = warn if warn is not None else lambda *_args: None

    def resolve(email: object) -> int | None:
        if employee_resolver is not None:
            return employee_resolver(email)
        return _resolve_with_lookup(
            employee_lookup,
            email,
            feedback_id=feedback_id,
            projection_version=projection_version,
            warn=warning_callback,
        )

    submitted_by = resolve(feedback.get("submitter"))
    if submitted_by is not None:
        fields["x_studio_submitted_by"] = _positive_signed_64(
            submitted_by, "employee id", ContractError
        )

    finished_at = feedback.get("finished_at")
    if finished_at is not None:
        fields["x_studio_date_stop"] = _odoo_time(finished_at, stop_type)

    completed_by = resolve(feedback.get("finished_by"))
    if completed_by is not None:
        fields["x_studio_completed_by"] = _positive_signed_64(
            completed_by, "employee id", ContractError
        )

    note = feedback.get("resolution_note")
    if note is not None:
        if type(note) is not str:
            raise ValueError("resolution note must be a string")
        fields["x_studio_notes"] = f"<p>{html.escape(note, quote=False)}</p>"

    binaries: dict[str, BinaryEvidence] = {}
    for role, image in images.items():
        if type(role) is not str or role not in _BINARY_FIELDS:
            raise ValueError("unsupported feedback image role")
        binaries[_BINARY_FIELDS[role]] = _binary_evidence(image)

    manifest, manifest_digest = _manifest(fields, binaries)
    return Projection(
        source_id=source_id,
        fields=fields,
        binaries=binaries,
        manifest=manifest,
        manifest_digest=manifest_digest,
    )


def build_projection_from_snapshot(
    snapshot: feedback_store.ProjectionSnapshot,
    *,
    client,
    contract: ImprovementContract,
) -> Projection:
    """Build from one frozen snapshot and persist only safe versioned warnings."""
    if type(snapshot) is not feedback_store.ProjectionSnapshot:
        raise ValueError("projection requires an immutable feedback snapshot")
    if type(contract) is not ImprovementContract:
        raise ContractError("projection requires immutable contract metadata")

    feedback_id = _positive_signed_64(snapshot.feedback.get("id"), "feedback id")
    projection_version = _positive_signed_64(
        snapshot.feedback.get("projection_version"), "projection version"
    )

    def employee_resolver(email: object) -> int | None:
        return resolve_employee_id(
            client,
            email,
            feedback_id=feedback_id,
            projection_version=projection_version,
            warn=feedback_store.record_sync_warning,
        )

    return build_projection(
        snapshot.feedback,
        images=snapshot.images,
        employee_lookup=lambda _email: None,
        start_type=contract.start_type,
        stop_type=contract.stop_type,
        employee_resolver=employee_resolver,
    )


def _mismatch(field_name: str) -> ReadbackMismatch:
    return ReadbackMismatch(f"readback mismatch for {field_name}")


def readback_mismatched_fields(
    projection: Projection,
    remote: Mapping[str, object],
) -> tuple[str, ...]:
    """Return only allowlisted field names whose complete readback differs."""
    if type(projection) is not Projection or not isinstance(remote, Mapping):
        raise ReadbackMismatch("readback response was malformed")

    mismatches: set[str] = set()
    for field_name, expected in projection.fields.items():
        if field_name not in remote:
            mismatches.add(field_name)
            continue
        actual = remote[field_name]
        if field_name in _EMPLOYEE_FIELDS:
            if (
                type(actual) is not list
                or len(actual) != 2
                or type(actual[0]) is not int
                or not 0 < actual[0] <= MAX_SIGNED_64
                or type(actual[1]) is not str
                or type(expected) is not int
                or actual[0] != expected
            ):
                mismatches.add(field_name)
        elif type(actual) is not type(expected) or actual != expected:
            mismatches.add(field_name)

    for field_name, evidence in projection.binaries.items():
        if field_name not in remote or type(remote[field_name]) is not str:
            mismatches.add(field_name)
            continue
        try:
            decoded = base64.b64decode(remote[field_name], validate=True)
        except (binascii.Error, ValueError):
            mismatches.add(field_name)
            continue
        if (
            len(decoded) != evidence.byte_length
            or hashlib.sha256(decoded).hexdigest() != evidence.sha256
        ):
            mismatches.add(field_name)
    return tuple(sorted(mismatches))


def verify_readback(projection: Projection, remote: Mapping[str, object]) -> None:
    """Verify every dispatched scalar, relation, and complete binary exactly."""
    mismatches = readback_mismatched_fields(projection, remote)
    if mismatches:
        raise _mismatch(mismatches[0])
