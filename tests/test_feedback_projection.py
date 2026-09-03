import base64
import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from zira_dashboard import feedback_store
from zira_dashboard.feedback_image import MAX_OUTPUT_BYTES, NormalizedImage
from zira_dashboard.feedback_projection import (
    BinaryEvidence,
    MAX_SIGNED_64,
    Projection,
    ReadbackMismatch,
    build_projection,
    build_projection_from_snapshot,
    readback_mismatched_fields,
    resolve_employee_id,
    source_id_for,
    verify_readback,
)
from zira_dashboard.odoo_improvements import ContractError, ImprovementContract


def feedback(**changes):
    row = {
        "id": 12345,
        "message": "Problem <script>alert(1)</script>",
        "task_type": "bug",
        "created_at": datetime(2026, 8, 20, 15, 30, tzinfo=UTC),
        "submitter": " Person@Example.com ",
        "status": "requested",
        "finished_at": None,
        "finished_by": None,
        "resolution_note": None,
        "projection_version": 1,
    }
    row.update(changes)
    return row


def normalized(raw: bytes) -> NormalizedImage:
    return NormalizedImage(
        jpeg_bytes=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw),
        width=8,
        height=8,
    )


def direct_projection_inputs(
    *,
    source_id="GPI-PM-FB-12345",
    fields_changes=None,
    binaries=None,
    manifest_transform=None,
):
    fields = {
        "x_name": "Safe feedback",
        "x_studio_source_id": "GPI-PM-FB-12345",
        "x_studio_source": "GPI Plant Manager",
    }
    if fields_changes:
        fields.update(fields_changes)
    selected_binaries = {} if binaries is None else dict(binaries)
    manifest = {
        "fields": dict(fields),
        "binary_evidence": {
            name: {
                "sha256": (evidence.sha256 if type(evidence.sha256) is str else "0" * 64),
                "byte_length": evidence.byte_length,
            }
            for name, evidence in selected_binaries.items()
        },
    }
    if manifest_transform is not None:
        manifest_transform(manifest)
    encoded = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return {
        "source_id": source_id,
        "fields": fields,
        "binaries": selected_binaries,
        "manifest": manifest,
        "manifest_digest": hashlib.sha256(encoded).hexdigest(),
    }


def projection_with_before(raw: bytes):
    return build_projection(
        feedback(),
        images={"before": normalized(raw)},
        employee_lookup=lambda _email: None,
        start_type="date",
        stop_type="date",
    )


def test_source_id_and_requested_bug_mapping_are_exact():
    projection = build_projection(
        feedback(),
        images={},
        employee_lookup=lambda _email: None,
        start_type="date",
        stop_type="date",
    )

    assert source_id_for(12345) == "GPI-PM-FB-12345"
    assert projection.fields == {
        "x_name": "Problem <script>alert(1)</script>",
        "x_studio_source_id": "GPI-PM-FB-12345",
        "x_studio_date_start": "2026-08-20",
        "x_studio_type": "Digital",
        "x_studio_status": "Requested",
        "x_studio_source": "GPI Plant Manager",
    }


def test_completed_feature_maps_terminal_fields_and_escapes_note():
    row = feedback(
        task_type="feature",
        status="completed",
        finished_at=datetime(2026, 8, 21, 2, 0, tzinfo=UTC),
        finished_by="admin@example.com",
        resolution_note="Fixed <b>safely</b> & checked",
        projection_version=3,
    )
    employees = {"person@example.com": 7, "admin@example.com": 8}

    projection = build_projection(
        row,
        images={},
        employee_lookup=employees.get,
        start_type="datetime",
        stop_type="datetime",
    )

    assert projection.fields["x_studio_type"] == "Digital - New Feature"
    assert projection.fields["x_studio_status"] == "Completed"
    assert projection.fields["x_studio_submitted_by"] == 7
    assert projection.fields["x_studio_completed_by"] == 8
    assert projection.fields["x_studio_date_stop"] == "2026-08-21 02:00:00"
    assert projection.fields["x_studio_notes"] == (
        "<p>Fixed &lt;b&gt;safely&lt;/b&gt; &amp; checked</p>"
    )


def test_resolution_note_escapes_html_text_but_keeps_quotes_literal():
    projection = build_projection(
        feedback(
            status="completed",
            resolution_note='They\'re "ready" & <safe>',
            projection_version=2,
        ),
        images={},
        employee_lookup=lambda _email: None,
        start_type="date",
        stop_type="date",
    )

    assert projection.fields["x_studio_notes"] == (
        '<p>They\'re "ready" &amp; &lt;safe&gt;</p>'
    )


def test_verify_readback_accepts_odoo_normalized_literal_quotes_in_note():
    projection = build_projection(
        feedback(
            status="completed",
            resolution_note='They\'re "ready" & <safe>',
            projection_version=2,
        ),
        images={},
        employee_lookup=lambda _email: None,
        start_type="date",
        stop_type="date",
    )
    remote = dict(projection.fields)
    remote["x_studio_notes"] = '<p>They\'re "ready" &amp; &lt;safe&gt;</p>'

    verify_readback(projection, remote)


@pytest.mark.parametrize(
    ("local_value", "odoo_value"),
    [
        ("bug", "Digital"),
        ("feature", "Digital - New Feature"),
        ("floor_issue", "Physical - Issue"),
        ("floor_suggestion", "Physical - Suggestion"),
        ("two_s_improvement", "2s Improvement"),
    ],
)
def test_projection_uses_authoritative_odoo_type(local_value, odoo_value):
    projected = build_projection(
        feedback(task_type=local_value),
        images={},
        employee_lookup=lambda _email: None,
        start_type="date",
        stop_type="date",
    )

    assert projected.fields["x_studio_type"] == odoo_value


def test_projection_contract_excludes_external_repair_type():
    from zira_dashboard import feedback_projection

    assert "repair" not in feedback_projection.TYPE_VALUES
    assert None not in feedback_projection.TYPE_VALUES.values()


def test_missing_optional_values_and_remote_sync_tokens_are_never_emitted():
    projection = build_projection(
        feedback(task_type=None),
        images={},
        employee_lookup=lambda _email: None,
        start_type="date",
        stop_type="date",
    )

    assert projection.fields["x_studio_type"] == "Digital"
    assert (
        not {
            "x_studio_submitted_by",
            "x_studio_image",
            "x_studio_after_image",
        }
        & projection.fields.keys()
    )
    assert all("token" not in key.lower() for key in projection.fields)
    assert "person@example.com" not in json.dumps(projection.manifest).casefold()
    assert all("token" not in key.lower() for key in projection.manifest)


def test_projection_manifest_is_canonical_and_contains_only_binary_evidence():
    projection = projection_with_before(b"safe-jpeg")
    canonical = json.dumps(
        projection.manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()

    assert projection.manifest_digest == hashlib.sha256(canonical).hexdigest()
    assert projection.manifest["fields"] == projection.fields
    assert projection.manifest["binary_evidence"] == {
        "x_studio_image": {
            "sha256": hashlib.sha256(b"safe-jpeg").hexdigest(),
            "byte_length": 9,
        }
    }
    serialized = json.dumps(projection.manifest)
    assert "safe-jpeg" not in serialized
    assert base64.b64encode(b"safe-jpeg").decode() not in serialized


def test_projection_copies_mutable_inputs_and_dispatch_returns_fresh_dictionaries():
    row = feedback()
    images = {"before": normalized(b"safe-jpeg")}
    projection = build_projection(
        row,
        images=images,
        employee_lookup=lambda _email: None,
        start_type="date",
        stop_type="date",
    )
    row["message"] = "changed"
    images.clear()

    first = projection.dispatch_fields()
    first["x_name"] = "changed again"
    first["x_studio_image"] = "changed binary"
    second = projection.dispatch_fields()

    assert projection.fields["x_name"] == "Problem <script>alert(1)</script>"
    assert projection.manifest["fields"]["x_name"] == "Problem <script>alert(1)</script>"
    assert second["x_name"] == "Problem <script>alert(1)</script>"
    assert second["x_studio_image"] == base64.b64encode(b"safe-jpeg").decode("ascii")


def test_projection_exposes_detached_dicts_that_cannot_change_canonical_state():
    projection = projection_with_before(b"safe-jpeg")
    digest = projection.manifest_digest

    exposed_fields = projection.fields
    exposed_binaries = projection.binaries
    exposed_manifest = projection.manifest

    dict.__setitem__(exposed_fields, "x_name", "changed through base dict")
    object.__setattr__(exposed_binaries["x_studio_image"], "sha256", "0" * 64)
    dict.__delitem__(exposed_binaries, "x_studio_image")
    dict.__setitem__(exposed_manifest["fields"], "x_name", "changed manifest")
    dict.__init__(
        exposed_manifest["binary_evidence"]["x_studio_image"],
        {"sha256": "0" * 64},
    )
    assert type(exposed_fields) is dict
    assert type(exposed_binaries) is dict
    assert type(exposed_manifest) is dict

    fresh_fields = projection.fields
    fresh_binaries = projection.binaries
    fresh_manifest = projection.manifest
    canonical = json.dumps(
        fresh_manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    assert fresh_fields is not projection.fields
    assert fresh_manifest is not projection.manifest
    assert fresh_fields["x_name"] == "Problem <script>alert(1)</script>"
    assert fresh_binaries["x_studio_image"].sha256 == hashlib.sha256(b"safe-jpeg").hexdigest()
    assert fresh_manifest["fields"]["x_name"] == "Problem <script>alert(1)</script>"
    assert fresh_manifest["binary_evidence"]["x_studio_image"] == {
        "sha256": hashlib.sha256(b"safe-jpeg").hexdigest(),
        "byte_length": 9,
    }
    assert projection.manifest_digest == digest == hashlib.sha256(canonical).hexdigest()
    dispatch = projection.dispatch_fields()
    assert dispatch["x_name"] == "Problem <script>alert(1)</script>"
    assert dispatch["x_studio_image"] == base64.b64encode(b"safe-jpeg").decode("ascii")


def test_projection_constructor_rejects_manifest_that_diverges_from_dispatch_fields():
    def diverge(manifest):
        manifest["fields"]["x_name"] = "manifest"

    with pytest.raises(ValueError, match="canonical projection state"):
        Projection(**direct_projection_inputs(manifest_transform=diverge))


@pytest.mark.parametrize("case", ["missing", "extra", "hash", "length"])
def test_projection_constructor_rejects_manifest_binary_evidence_divergence(case):
    evidence = binary_evidence_for_case("valid")

    def diverge(manifest):
        if case == "missing":
            manifest["binary_evidence"].clear()
        elif case == "extra":
            manifest["binary_evidence"]["x_studio_after_image"] = {
                "sha256": evidence.sha256,
                "byte_length": evidence.byte_length,
            }
        elif case == "hash":
            manifest["binary_evidence"]["x_studio_image"]["sha256"] = "0" * 64
        else:
            manifest["binary_evidence"]["x_studio_image"]["byte_length"] = 1

    with pytest.raises(ValueError, match="canonical projection state"):
        Projection(
            **direct_projection_inputs(
                binaries={"x_studio_image": evidence},
                manifest_transform=diverge,
            )
        )


@pytest.mark.parametrize(
    ("source_id", "fields_changes", "message"),
    [
        (True, {"x_studio_source_id": True}, "source id"),
        ("GPI-PM-FB-0", {"x_studio_source_id": "GPI-PM-FB-0"}, "source id"),
        ("GPI-PM-FB--1", {"x_studio_source_id": "GPI-PM-FB--1"}, "source id"),
        ("GPI-PM-FB-1.0", {"x_studio_source_id": "GPI-PM-FB-1.0"}, "source id"),
        (
            f"GPI-PM-FB-{MAX_SIGNED_64 + 1}",
            {"x_studio_source_id": f"GPI-PM-FB-{MAX_SIGNED_64 + 1}"},
            "source id",
        ),
        (
            "GPI-PM-FB-12345",
            {"x_studio_source_id": "GPI-PM-FB-12346"},
            "source id",
        ),
        (
            "GPI-PM-FB-12345",
            {"x_studio_source_id": 12345},
            "source id",
        ),
        (
            "GPI-PM-FB-12345",
            {"x_studio_source": "Another App"},
            "source namespace",
        ),
        (
            "GPI-PM-FB-12345",
            {"x_studio_image": "base64-does-not-belong-here"},
            "nonbinary fields",
        ),
        (
            "GPI-PM-FB-12345",
            {"x_studio_after_image": "base64-does-not-belong-here"},
            "nonbinary fields",
        ),
    ],
)
def test_projection_constructor_rejects_noncanonical_lookup_and_field_identity(
    source_id, fields_changes, message
):
    with pytest.raises(ValueError, match=message):
        Projection(
            **direct_projection_inputs(
                source_id=source_id,
                fields_changes=fields_changes,
            )
        )


@pytest.mark.parametrize(
    ("fields_changes", "message"),
    [
        ({"active": False}, "nonbinary field"),
        ({"claim_token": "local-only-value"}, "token"),
        ({"x_studio_unknown": "not allowlisted"}, "nonbinary field"),
        ({"x_studio_type": "Physical"}, "projection type"),
    ],
)
def test_projection_constructor_rejects_forbidden_nonbinary_payload_fields(fields_changes, message):
    with pytest.raises(ValueError, match=message):
        Projection(**direct_projection_inputs(fields_changes=fields_changes))


def binary_evidence_for_case(case):
    raw = b"safe-jpeg"
    digest = hashlib.sha256(raw).hexdigest()
    values = {
        "jpeg_bytes": raw,
        "sha256": digest,
        "byte_length": len(raw),
    }
    if case == "bytearray":
        values["jpeg_bytes"] = bytearray(raw)
    elif case == "memoryview":
        values["jpeg_bytes"] = memoryview(raw)
    elif case == "empty":
        values.update(jpeg_bytes=b"", sha256=hashlib.sha256(b"").hexdigest(), byte_length=0)
    elif case == "oversized":
        oversized = b"x" * (MAX_OUTPUT_BYTES + 1)
        values.update(
            jpeg_bytes=oversized,
            sha256=hashlib.sha256(oversized).hexdigest(),
            byte_length=len(oversized),
        )
    elif case == "bool_length":
        values["byte_length"] = True
    elif case == "float_length":
        values["byte_length"] = 9.0
    elif case == "zero_length":
        values["byte_length"] = 0
    elif case == "negative_length":
        values["byte_length"] = -1
    elif case == "wrong_length":
        values["byte_length"] = len(raw) - 1
    elif case == "uppercase_hash":
        values["sha256"] = digest.upper()
    elif case == "wrong_hash":
        values["sha256"] = "0" * 64
    elif case == "nonstring_hash":
        values["sha256"] = b"0" * 64
    return BinaryEvidence(**values)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("bytearray", "binary bytes"),
        ("memoryview", "binary bytes"),
        ("empty", "binary bytes"),
        ("oversized", "binary bytes"),
        ("bool_length", "binary length"),
        ("float_length", "binary length"),
        ("zero_length", "binary length"),
        ("negative_length", "binary length"),
        ("wrong_length", "binary length"),
        ("uppercase_hash", "binary hash"),
        ("wrong_hash", "binary hash"),
        ("nonstring_hash", "binary hash"),
    ],
)
def test_projection_constructor_rejects_malformed_binary_evidence(case, message):
    evidence = binary_evidence_for_case(case)

    with pytest.raises(ValueError, match=message):
        Projection(
            **direct_projection_inputs(
                binaries={"x_studio_image": evidence},
            )
        )


@pytest.mark.parametrize("field_name", ["image", "x_image", "x_studio_other_image"])
def test_projection_constructor_rejects_nonallowlisted_binary_field_names(field_name):
    evidence = binary_evidence_for_case("valid")

    with pytest.raises(ValueError, match="binary field"):
        Projection(**direct_projection_inputs(binaries={field_name: evidence}))


@pytest.mark.parametrize("field_name", ["x_studio_image", "x_studio_after_image"])
def test_projection_constructor_accepts_only_exact_binary_fields(field_name):
    evidence = binary_evidence_for_case("valid")

    projection = Projection(**direct_projection_inputs(binaries={field_name: evidence}))

    assert projection.source_id == projection.fields["x_studio_source_id"]
    assert projection.fields["x_studio_source"] == "GPI Plant Manager"
    assert projection.manifest["fields"] == projection.fields
    assert projection.binaries == {field_name: evidence}
    assert set(projection.dispatch_fields()) >= {field_name, "x_studio_source_id"}
    verify_readback(projection, projection.dispatch_fields())


@pytest.mark.parametrize("value", [True, 1.0, 0, -1, MAX_SIGNED_64 + 1])
def test_source_id_rejects_non_exact_positive_signed_64_bit_ids(value):
    with pytest.raises(ValueError, match="positive signed-64-bit integer"):
        source_id_for(value)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"created_at": datetime(2026, 8, 20, 15, 30)}, "timezone-aware"),
        ({"created_at": "2026-08-20T15:30:00Z"}, "timezone-aware"),
        (
            {
                "status": "completed",
                "finished_at": datetime(2026, 8, 21, 2, 0),
            },
            "timezone-aware",
        ),
        ({"status": "unknown"}, "unsupported feedback status"),
        ({"task_type": "physical"}, "unsupported feedback type"),
    ],
)
def test_projection_rejects_naive_malformed_datetimes_and_unknown_values(changes, message):
    with pytest.raises(ValueError, match=message):
        build_projection(
            feedback(**changes),
            images={},
            employee_lookup=lambda _email: None,
            start_type="date",
            stop_type="date",
        )


def test_invalid_field_type_is_rejected_even_without_a_terminal_timestamp():
    with pytest.raises(ValueError, match="date or datetime"):
        build_projection(
            feedback(),
            images={},
            employee_lookup=lambda _email: None,
            start_type="char",
            stop_type="date",
        )


class EmployeeClient:
    def __init__(self, rows=None, error=None):
        self.rows = [] if rows is None else rows
        self.error = error
        self.calls = []

    def find_employees_by_email(self, email, *, limit):
        self.calls.append((email, limit))
        if self.error is not None:
            raise self.error
        return self.rows


def test_resolve_employee_normalizes_email_and_accepts_one_exact_match():
    client = EmployeeClient([{"id": 17, "work_email": "person@example.com"}])
    warnings = []

    result = resolve_employee_id(
        client,
        " Person@Example.com ",
        feedback_id=12345,
        projection_version=7,
        warn=lambda *args: warnings.append(args),
    )

    assert result == 17
    assert client.calls == [("person@example.com", 3)]
    assert warnings == []


def test_resolve_employee_rejects_invalid_local_email_without_client_call():
    client = EmployeeClient([{"id": 17, "work_email": "not-an-email"}])
    warnings = []

    result = resolve_employee_id(
        client,
        "not-an-email",
        feedback_id=12345,
        projection_version=7,
        warn=lambda *args: warnings.append(args),
    )

    assert result is None
    assert client.calls == []
    assert warnings == [(12345, 7, "employee_missing")]
    assert "not-an-email" not in repr(warnings)


def test_resolve_employee_safely_rejects_malformed_nonstring_local_email():
    client = EmployeeClient()
    warnings = []

    result = resolve_employee_id(
        client,
        [],
        feedback_id=12345,
        projection_version=7,
        warn=lambda *args: warnings.append(args),
    )

    assert result is None
    assert client.calls == []
    assert warnings == [(12345, 7, "employee_missing")]


@pytest.mark.parametrize(
    ("rows", "warning_class"),
    [
        ([], "employee_missing"),
        (
            [
                {"id": 17, "work_email": "person@example.com"},
                {"id": 18, "work_email": "person@example.com"},
            ],
            "employee_ambiguous",
        ),
    ],
)
def test_resolve_employee_uses_only_safe_versioned_warnings(rows, warning_class):
    warnings = []

    result = resolve_employee_id(
        EmployeeClient(rows),
        "person@example.com",
        feedback_id=12345,
        projection_version=8,
        warn=lambda *args: warnings.append(args),
    )

    assert result is None
    assert warnings == [(12345, 8, warning_class)]
    assert "person@example.com" not in repr(warnings)


@pytest.mark.parametrize("employee_id", [True, 1.0, 0, -1, MAX_SIGNED_64 + 1])
def test_resolve_employee_rejects_malformed_exact_match_ids(employee_id):
    client = EmployeeClient([{"id": employee_id, "work_email": "person@example.com"}])

    with pytest.raises(ContractError, match="employee id"):
        resolve_employee_id(
            client,
            "person@example.com",
            feedback_id=12345,
            projection_version=1,
            warn=lambda *_args: None,
        )


@pytest.mark.parametrize("employee_id", [True, 1.0, 0, -1, MAX_SIGNED_64 + 1])
def test_build_projection_revalidates_employee_resolver_ids(employee_id):
    with pytest.raises(ContractError, match="employee id"):
        build_projection(
            feedback(),
            images={},
            employee_lookup=lambda _email: None,
            employee_resolver=lambda _email: employee_id,
            start_type="date",
            stop_type="date",
        )


def test_resolve_employee_propagates_client_transport_and_contract_failures():
    for error in (ConnectionError("offline"), ContractError("bad response")):
        with pytest.raises(type(error), match=str(error)):
            resolve_employee_id(
                EmployeeClient(error=error),
                "person@example.com",
                feedback_id=12345,
                projection_version=1,
                warn=lambda *_args: None,
            )


def test_resolve_employee_treats_a_malformed_client_result_as_a_contract_failure():
    client = EmployeeClient()
    client.rows = None
    with pytest.raises(ContractError, match="employee lookup response"):
        resolve_employee_id(
            client,
            "person@example.com",
            feedback_id=12345,
            projection_version=1,
            warn=lambda *_args: None,
        )


class SnapshotCursor:
    def __init__(self, feedback_row, image_rows):
        self.feedback_row = feedback_row
        self.image_rows = image_rows
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.feedback_row

    def fetchall(self):
        return self.image_rows


def install_snapshot_cursor(monkeypatch, feedback_row, image_rows):
    cursor = SnapshotCursor(feedback_row, image_rows)
    transactions = []

    @contextmanager
    def fake_cursor():
        transactions.append(cursor)
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)
    return cursor, transactions


def image_row(**changes):
    raw = b"safe-jpeg"
    row = {
        "feedback_id": 12345,
        "role": "before",
        "jpeg_bytes": raw,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_length": len(raw),
        "width": 8,
        "height": 8,
    }
    row.update(changes)
    return row


@pytest.mark.parametrize("origin", ["local", "legacy_project_task"])
def test_projection_snapshot_reads_exact_supported_version_and_images_once(monkeypatch, origin):
    cursor, transactions = install_snapshot_cursor(
        monkeypatch, feedback(lifecycle_origin=origin), [image_row()]
    )

    snapshot = feedback_store.projection_snapshot(12345, 1)

    assert transactions == [cursor]
    assert len(cursor.calls) == 2
    feedback_sql, feedback_params = cursor.calls[0]
    image_sql, image_params = cursor.calls[1]
    assert "id = %s" in feedback_sql
    assert "projection_version = %s" in feedback_sql
    assert "lifecycle_origin IN ('local', 'legacy_project_task')" in feedback_sql
    assert "status IS NOT NULL" in feedback_sql
    assert "FOR SHARE" in feedback_sql
    assert feedback_params == (12345, 1)
    assert "FROM feedback_images" in image_sql
    assert "LIMIT 3" in image_sql
    assert image_params == (12345,)
    assert snapshot.feedback["projection_version"] == 1
    assert snapshot.images["before"].jpeg_bytes == b"safe-jpeg"
    with pytest.raises(TypeError):
        snapshot.feedback["message"] = "changed"
    with pytest.raises(TypeError):
        snapshot.images["before"] = normalized(b"changed")


@pytest.mark.parametrize(
    "stored_row",
    [
        None,
        feedback(projection_version=2, lifecycle_origin="local"),
        feedback(lifecycle_origin=None),
        feedback(lifecycle_origin="foreign"),
        feedback(status=None, lifecycle_origin="local"),
        feedback(status=None, lifecycle_origin="legacy_project_task"),
        feedback(id=12346, lifecycle_origin="local"),
    ],
)
def test_projection_snapshot_rejects_missing_stale_wrong_origin_or_wrong_id(
    monkeypatch, stored_row
):
    install_snapshot_cursor(monkeypatch, stored_row, [])

    with pytest.raises(feedback_store.ProjectionSnapshotUnavailable):
        feedback_store.projection_snapshot(12345, 1)


@pytest.mark.parametrize(
    ("feedback_id", "projection_version"),
    [
        (True, 1),
        (1.0, 1),
        (0, 1),
        (-1, 1),
        (MAX_SIGNED_64 + 1, 1),
        (12345, True),
        (12345, 1.0),
        (12345, 0),
        (12345, -1),
        (12345, MAX_SIGNED_64 + 1),
    ],
)
def test_projection_snapshot_rejects_non_exact_signed_64_bit_inputs(
    monkeypatch, feedback_id, projection_version
):
    monkeypatch.setattr(
        feedback_store.db,
        "cursor",
        lambda: (_ for _ in ()).throw(AssertionError("transaction opened")),
    )

    with pytest.raises(ValueError, match="positive signed-64-bit integer"):
        feedback_store.projection_snapshot(feedback_id, projection_version)


def test_projection_snapshot_rejects_duplicate_image_roles(monkeypatch):
    install_snapshot_cursor(
        monkeypatch,
        feedback(lifecycle_origin="local"),
        [
            image_row(),
            image_row(
                jpeg_bytes=b"other", sha256=hashlib.sha256(b"other").hexdigest(), byte_length=5
            ),
        ],
    )

    with pytest.raises(feedback_store.ProjectionSnapshotUnavailable, match="duplicate image role"):
        feedback_store.projection_snapshot(12345, 1)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"jpeg_bytes": "not-bytes"}, "image bytes"),
        ({"sha256": "0" * 64}, "image hash"),
        ({"byte_length": 8}, "image length"),
        ({"byte_length": True}, "image length"),
        ({"width": 0}, "image dimensions"),
        ({"height": 2049}, "image dimensions"),
        ({"width": 1.5}, "image dimensions"),
    ],
)
def test_projection_snapshot_rejects_malformed_image_evidence(monkeypatch, changes, message):
    install_snapshot_cursor(
        monkeypatch,
        feedback(lifecycle_origin="local"),
        [image_row(**changes)],
    )

    with pytest.raises(feedback_store.ProjectionSnapshotUnavailable, match=message):
        feedback_store.projection_snapshot(12345, 1)


def test_record_sync_warning_persists_only_safe_versioned_values(monkeypatch):
    cursor, transactions = install_snapshot_cursor(monkeypatch, None, [])

    feedback_store.record_sync_warning(12345, 9, "employee_missing")

    assert transactions == [cursor]
    assert len(cursor.calls) == 1
    sql, params = cursor.calls[0]
    assert "INSERT INTO feedback_odoo_warnings" in sql
    assert "ON CONFLICT" in sql
    assert params == (12345, 9, "employee_missing")
    assert "email" not in repr(params).casefold()


def test_build_projection_from_snapshot_binds_warning_to_immutable_version(
    monkeypatch,
):
    install_snapshot_cursor(
        monkeypatch,
        feedback(submitter="bad-email", projection_version=9, lifecycle_origin="local"),
        [],
    )
    snapshot = feedback_store.projection_snapshot(12345, 9)
    client = EmployeeClient()
    warnings = []
    monkeypatch.setattr(
        feedback_store,
        "record_sync_warning",
        lambda *args: warnings.append(args),
    )

    projection = build_projection_from_snapshot(
        snapshot,
        client=client,
        contract=ImprovementContract(start_type="date", stop_type="datetime"),
    )

    assert client.calls == []
    assert warnings == [(12345, 9, "employee_missing")]
    serialized = json.dumps(projection.manifest).casefold()
    assert "bad-email" not in serialized
    assert "employee_missing" not in serialized


def test_two_s_projection_requires_the_active_v2_type_selection(monkeypatch):
    install_snapshot_cursor(
        monkeypatch,
        feedback(task_type="two_s_improvement", lifecycle_origin="local"),
        [],
    )
    snapshot = feedback_store.projection_snapshot(12345, 1)

    with pytest.raises(ContractError, match="2s Improvement.*selection"):
        build_projection_from_snapshot(
            snapshot,
            client=EmployeeClient(),
            contract=ImprovementContract(start_type="date", stop_type="date", version=1),
        )


def test_verify_readback_accepts_exact_scalars_many2one_and_binary():
    projection = build_projection(
        feedback(),
        images={"before": normalized(b"safe-jpeg")},
        employee_lookup=lambda _email: 17,
        start_type="date",
        stop_type="date",
    )
    remote = dict(projection.fields)
    remote["x_studio_submitted_by"] = [17, "Person"]
    remote["x_studio_image"] = base64.b64encode(b"safe-jpeg").decode("ascii")
    remote["id"] = 999
    remote["unrequested_extra"] = "ignored"

    verify_readback(projection, remote)


def test_readback_diagnostic_reports_every_mismatched_field_without_values():
    projection = projection_with_before(b"saved-private-image")
    remote = dict(projection.fields)
    remote["x_name"] = "different private note"
    remote["x_studio_image"] = base64.b64encode(b"different-private-image").decode(
        "ascii"
    )

    mismatches = readback_mismatched_fields(projection, remote)

    assert mismatches == ("x_name", "x_studio_image")
    serialized = repr(mismatches)
    assert "different private note" not in serialized
    assert "different-private-image" not in serialized


@pytest.mark.parametrize("remote", [None, [], object()])
def test_readback_diagnostic_rejects_malformed_remote_response_without_values(remote):
    projection = projection_with_before(b"saved-private-image")

    with pytest.raises(ReadbackMismatch, match="readback response was malformed") as caught:
        readback_mismatched_fields(projection, remote)

    assert "saved-private-image" not in repr(caught.value)


@pytest.mark.parametrize("value", [17, [17], [17, "Person", "extra"], (17, "Person"), [17, 3]])
def test_verify_readback_rejects_malformed_many2one_shapes(value):
    projection = build_projection(
        feedback(),
        images={},
        employee_lookup=lambda _email: 17,
        start_type="date",
        stop_type="date",
    )
    remote = dict(projection.fields)
    remote["x_studio_submitted_by"] = value

    with pytest.raises(ReadbackMismatch, match="x_studio_submitted_by"):
        verify_readback(projection, remote)


@pytest.mark.parametrize("employee_id", [18, True, 1.0, 0, -1, MAX_SIGNED_64 + 1])
def test_verify_readback_rejects_many2one_id_mismatches(employee_id):
    projection = build_projection(
        feedback(),
        images={},
        employee_lookup=lambda _email: 17,
        start_type="date",
        stop_type="date",
    )
    remote = dict(projection.fields)
    remote["x_studio_submitted_by"] = [employee_id, "Person"]

    with pytest.raises(ReadbackMismatch, match="x_studio_submitted_by"):
        verify_readback(projection, remote)


def test_verify_readback_compares_full_binary_hash_and_length():
    projection = projection_with_before(b"safe-jpeg")
    remote = dict(projection.fields)
    remote["x_studio_image"] = base64.b64encode(b"safe-jpeg").decode("ascii")
    verify_readback(projection, remote)

    remote["x_studio_image"] = base64.b64encode(b"other").decode("ascii")
    with pytest.raises(ReadbackMismatch, match="x_studio_image"):
        verify_readback(projection, remote)


@pytest.mark.parametrize("value", ["%%%", "c2FmZQ", "", False, None])
def test_verify_readback_rejects_malformed_base64(value):
    projection = projection_with_before(b"safe-jpeg")
    remote = dict(projection.fields)
    remote["x_studio_image"] = value

    with pytest.raises(ReadbackMismatch, match="x_studio_image"):
        verify_readback(projection, remote)


def test_verify_readback_rejects_missing_or_exact_scalar_mismatch():
    projection = build_projection(
        feedback(),
        images={},
        employee_lookup=lambda _email: None,
        start_type="date",
        stop_type="date",
    )
    missing = dict(projection.fields)
    del missing["x_studio_status"]
    with pytest.raises(ReadbackMismatch, match="x_studio_status"):
        verify_readback(projection, missing)

    mismatched = dict(projection.fields)
    mismatched["x_studio_status"] = "requested"
    with pytest.raises(ReadbackMismatch, match="x_studio_status"):
        verify_readback(projection, mismatched)
