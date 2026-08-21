import dataclasses

import pytest

from zira_dashboard.odoo_improvements import (
    ContractError,
    GateClosed,
    ImprovementContract,
    ImprovementsClient,
    ImprovementsConfig,
    ImprovementsConfigError,
    MalformedMutationResponse,
    SOURCE_VALUE,
    TARGET_MODEL,
    TargetIdentityError,
)


ENV = {
    "ODOO_IMPROVEMENTS_URL": "https://odoo.invalid",
    "ODOO_IMPROVEMENTS_DB": "database",
    "ODOO_IMPROVEMENTS_LOGIN": "service@example.invalid",
    "ODOO_IMPROVEMENTS_API_KEY": "secret-key",
    "ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID": "uuid-expected",
    "ODOO_IMPROVEMENTS_EXPECTED_COMPANY": "Gruber Pallets, Inc.",
}

ALL_SERVICE_SETTINGS = (
    "ODOO_URL",
    "ODOO_DB",
    "ODOO_LOGIN",
    "ODOO_API_KEY",
    *ENV,
    "ODOO_SHARED_REPORTING_WRITE_ENABLED",
    "ODOO_IMPROVEMENTS_WRITE_ENABLED",
    "ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID",
)


@pytest.fixture(autouse=True)
def disable_real_services(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    for name in ALL_SERVICE_SETTINGS:
        monkeypatch.delenv(name, raising=False)


def set_config(monkeypatch):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)


def open_gates(monkeypatch, *, canary=""):
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "true")
    monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", "true")
    if canary:
        monkeypatch.setenv("ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID", canary)


def contract_fields(source_selection=None):
    if source_selection is None:
        source_selection = [[SOURCE_VALUE, SOURCE_VALUE]]
    return {
        "x_name": {"type": "char", "readonly": False},
        "x_studio_source_id": {"type": "char", "readonly": False},
        "x_studio_date_start": {"type": "date", "readonly": False},
        "x_studio_submitted_by": {"type": "many2one", "readonly": False},
        "x_studio_date_stop": {"type": "date", "readonly": False},
        "x_studio_completed_by": {"type": "many2one", "readonly": False},
        "x_studio_notes": {"type": "html", "readonly": False},
        "x_studio_image": {"type": "binary", "readonly": False},
        "x_studio_after_image": {"type": "binary", "readonly": False},
        "x_studio_source": {
            "type": "selection",
            "readonly": False,
            "selection": source_selection,
        },
        "x_studio_status": {
            "type": "selection",
            "readonly": False,
            "selection": [
                ["Requested", "Requested"],
                ["In-Progress", "In-Progress"],
                ["Completed", "Completed"],
                ["Declined", "Declined"],
            ],
        },
        "x_studio_type": {
            "type": "selection",
            "readonly": False,
            "selection": [
                ["Digital", "Digital - Bug"],
                ["Digital - New Feature", "Digital - New Feature"],
                ["Physical", "Physical"],
            ],
        },
    }


def client_with(monkeypatch, executor, *, uid=None):
    set_config(monkeypatch)
    return ImprovementsClient.from_env(executor=executor, uid=uid)


def test_config_uses_only_dedicated_settings_and_redacts_every_value(monkeypatch):
    monkeypatch.setenv("ODOO_URL", "https://generic.invalid")
    monkeypatch.setenv("ODOO_DB", "generic-database")
    monkeypatch.setenv("ODOO_LOGIN", "generic@example.invalid")
    monkeypatch.setenv("ODOO_API_KEY", "generic-secret")
    set_config(monkeypatch)

    config = ImprovementsConfig.from_env()

    assert config.url == ENV["ODOO_IMPROVEMENTS_URL"]
    assert config.database == ENV["ODOO_IMPROVEMENTS_DB"]
    assert repr(config) == "ImprovementsConfig(<redacted>)"
    assert dataclasses.is_dataclass(config)
    for secret in ENV.values():
        assert secret not in repr(config)


def test_config_error_names_missing_settings_without_exposing_values(monkeypatch):
    set_config(monkeypatch)
    monkeypatch.setenv("ODOO_IMPROVEMENTS_LOGIN", "")
    monkeypatch.setenv("ODOO_IMPROVEMENTS_API_KEY", "do-not-expose")

    with pytest.raises(ImprovementsConfigError) as raised:
        ImprovementsConfig.from_env()

    message = str(raised.value)
    assert "ODOO_IMPROVEMENTS_LOGIN" in message
    assert "do-not-expose" not in message


@pytest.mark.parametrize("value", [None, "", "TRUE", "True", " true", "true ", "1"])
def test_improvements_write_gate_requires_exact_lowercase_true(monkeypatch, value):
    set_config(monkeypatch)
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "true")
    if value is not None:
        monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", value)

    with pytest.raises(GateClosed):
        ImprovementsClient.from_env().assert_mutation_allowed(17)


@pytest.mark.parametrize("value", [None, "", "TRUE", "True", " true", "true ", "1"])
def test_shared_write_gate_requires_exact_lowercase_true(monkeypatch, value):
    set_config(monkeypatch)
    monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", "true")
    if value is not None:
        monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", value)

    with pytest.raises(GateClosed):
        ImprovementsClient.from_env().assert_mutation_allowed(17)


@pytest.mark.parametrize("value", ["0", "-1", "+1", " 17", "17 ", "1.0", "١٧"])
def test_invalid_canary_fails_closed(monkeypatch, value):
    set_config(monkeypatch)
    open_gates(monkeypatch, canary=value)

    with pytest.raises(GateClosed, match="invalid"):
        ImprovementsClient.from_env().assert_mutation_allowed(17)


def test_canary_fence_allows_only_exact_positive_feedback_id(monkeypatch):
    set_config(monkeypatch)
    open_gates(monkeypatch, canary="17")
    client = ImprovementsClient.from_env()

    client.assert_mutation_allowed(17)
    with pytest.raises(GateClosed, match="canary"):
        client.assert_mutation_allowed(18)


@pytest.mark.parametrize("feedback_id", [True, False, 0, -1, 1.0, "17", None])
def test_gate_rejects_non_positive_integer_feedback_ids(monkeypatch, feedback_id):
    set_config(monkeypatch)
    open_gates(monkeypatch)

    with pytest.raises(GateClosed, match="feedback id"):
        ImprovementsClient.from_env().assert_mutation_allowed(feedback_id)


@pytest.mark.parametrize(
    ("model", "method"),
    [
        (TARGET_MODEL, "unlink"),
        (TARGET_MODEL, "action_archive"),
        ("ir.model.fields", "write"),
        ("project.task", "write"),
        ("hr.employee", "create"),
        ("res.users", "search_read"),
    ],
)
def test_allowlist_rejects_destructive_and_unrelated_calls(monkeypatch, model, method):
    executor_calls = []
    client = client_with(
        monkeypatch,
        lambda *args, **kwargs: executor_calls.append((args, kwargs)),
    )

    with pytest.raises(ContractError, match="not allowlisted"):
        client._execute(model, method, [])

    assert executor_calls == []


@pytest.mark.parametrize(
    "fields",
    [
        {},
        [],
        {"active": False},
        {"sync_token": "x"},
        {"x_TOKEN_hint": "x"},
        {"x_unknown": "x"},
        {1: "x"},
        {"x_name": None},
        {"x_studio_submitted_by": True},
        {"x_studio_type": "Physical"},
        {"x_studio_source": "Another App"},
        {"x_studio_source_id": "17"},
        {"x_studio_image": "not base64"},
    ],
)
def test_target_write_rejects_forbidden_fields_and_payload_types_before_rpc(monkeypatch, fields):
    executor_calls = []
    client = client_with(
        monkeypatch,
        lambda *args, **kwargs: executor_calls.append((args, kwargs)) or True,
    )

    with pytest.raises(ContractError):
        client.write_improvement(9, fields, feedback_id=17)

    assert executor_calls == []


@pytest.mark.parametrize("remote_id", [True, 0, -1, 1.0, "9", None])
def test_write_validates_remote_id_before_gate_or_rpc(monkeypatch, remote_id):
    client = client_with(
        monkeypatch,
        lambda *args, **kwargs: pytest.fail("executor called"),
    )

    with pytest.raises(ContractError, match="remote id"):
        client.write_improvement(remote_id, {"x_name": "Safe"}, feedback_id=17)


def test_create_and_write_check_both_gates_and_canary_immediately_before_rpc(monkeypatch):
    calls = []
    client = client_with(
        monkeypatch,
        lambda model, method, *args, **kwargs: (
            calls.append((model, method, args, kwargs)) or (23 if method == "create" else True)
        ),
    )
    open_gates(monkeypatch, canary="17")

    assert client.create_improvement({"x_name": "Safe"}, feedback_id=17) == 23
    client.write_improvement(23, {"x_studio_status": "Completed"}, feedback_id=17)

    assert [(call[0], call[1]) for call in calls] == [
        (TARGET_MODEL, "create"),
        (TARGET_MODEL, "write"),
    ]
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "false")
    with pytest.raises(GateClosed):
        client.write_improvement(23, {"x_name": "Still safe"}, feedback_id=17)
    assert len(calls) == 2


@pytest.mark.parametrize("response", [None, False, True, 0, -1, 2.0, "2"])
def test_create_requires_a_positive_integer_response(monkeypatch, response):
    client = client_with(monkeypatch, lambda *args, **kwargs: response)
    open_gates(monkeypatch)

    with pytest.raises(MalformedMutationResponse):
        client.create_improvement({"x_name": "Safe"}, feedback_id=17)


@pytest.mark.parametrize("response", [None, False, 1, "true", []])
def test_write_requires_exact_true_response(monkeypatch, response):
    client = client_with(monkeypatch, lambda *args, **kwargs: response)
    open_gates(monkeypatch)

    with pytest.raises(MalformedMutationResponse):
        client.write_improvement(9, {"x_name": "Safe"}, feedback_id=17)


def test_find_exact_fixes_compound_domain_fields_and_limit(monkeypatch):
    calls = []
    expected = [
        {
            "id": 8,
            "x_studio_source": SOURCE_VALUE,
            "x_studio_source_id": "GPI-PM-FB-17",
        }
    ]
    client = client_with(
        monkeypatch,
        lambda model, method, *args, **kwargs: (
            calls.append((model, method, args, kwargs)) or expected
        ),
    )

    assert client.find_exact("GPI-PM-FB-17") == expected
    assert calls == [
        (
            TARGET_MODEL,
            "search_read",
            (
                [
                    ("x_studio_source", "=", SOURCE_VALUE),
                    ("x_studio_source_id", "=", "GPI-PM-FB-17"),
                ],
            ),
            {
                "fields": ["id", "x_studio_source", "x_studio_source_id"],
                "limit": 3,
            },
        )
    ]


@pytest.mark.parametrize("source_id", [None, 17, "", "GPI-PM-FB-0", "GPI-PM-FB-01", "FB-17"])
def test_find_exact_rejects_noncanonical_source_ids(monkeypatch, source_id):
    client = client_with(
        monkeypatch,
        lambda *args, **kwargs: pytest.fail("executor called"),
    )

    with pytest.raises(ContractError, match="source id"):
        client.find_exact(source_id)


def test_read_improvement_can_request_full_binary_values(monkeypatch):
    calls = []
    client = client_with(
        monkeypatch,
        lambda model, method, *args, **kwargs: (
            calls.append((model, method, args, kwargs)) or [{"id": 9, "x_studio_image": "c2FmZQ=="}]
        ),
    )

    row = client.read_improvement(9, ["id", "x_studio_image"], full_binary=True)

    assert row == {"id": 9, "x_studio_image": "c2FmZQ=="}
    assert calls == [
        (
            TARGET_MODEL,
            "read",
            ([9],),
            {
                "fields": ["id", "x_studio_image"],
                "context": {"bin_size": False},
            },
        )
    ]


@pytest.mark.parametrize(
    ("remote_id", "fields", "full_binary"),
    [
        (0, ["id"], False),
        (True, ["id"], False),
        (9, [], False),
        (9, ["active"], False),
        (9, ["id", "id"], False),
        (9, "id", False),
        (9, ["id"], 1),
    ],
)
def test_read_improvement_validates_ids_fields_and_flags_before_rpc(
    monkeypatch, remote_id, fields, full_binary
):
    client = client_with(
        monkeypatch,
        lambda *args, **kwargs: pytest.fail("executor called"),
    )

    with pytest.raises(ContractError):
        client.read_improvement(remote_id, fields, full_binary=full_binary)


def test_employee_lookup_is_exact_bounded_and_includes_inactive_people(monkeypatch):
    calls = []
    rows = [{"id": 7, "work_email": "person@example.invalid"}]
    client = client_with(
        monkeypatch,
        lambda model, method, *args, **kwargs: calls.append((model, method, args, kwargs)) or rows,
    )

    assert client.find_employees_by_email("person@example.invalid", limit=3) == rows
    assert calls == [
        (
            "hr.employee",
            "search_read",
            ([("work_email", "=ilike", "person@example.invalid")],),
            {
                "fields": ["id", "work_email"],
                "limit": 3,
                "context": {"active_test": False},
            },
        )
    ]


@pytest.mark.parametrize(
    ("email", "limit"),
    [
        (None, 3),
        ("", 3),
        (" Person@example.invalid", 3),
        ("PERSON@example.invalid", 3),
        ("person.example.invalid", 3),
        ("person@example.invalid", True),
        ("person@example.invalid", 2),
        ("person@example.invalid", 4),
    ],
)
def test_employee_lookup_rejects_untrusted_email_and_limit_before_rpc(monkeypatch, email, limit):
    client = client_with(
        monkeypatch,
        lambda *args, **kwargs: pytest.fail("executor called"),
    )

    with pytest.raises(ContractError):
        client.find_employees_by_email(email, limit=limit)


def test_legacy_task_stage_read_fixes_ids_and_fields(monkeypatch):
    calls = []
    rows = [{"id": 90, "stage_id": [4, "Done"]}]
    client = client_with(
        monkeypatch,
        lambda model, method, *args, **kwargs: calls.append((model, method, args, kwargs)) or rows,
    )

    assert client.read_legacy_task_stages([90, 91]) == rows
    assert calls == [("project.task", "read", ([90, 91],), {"fields": ["id", "stage_id"]})]


@pytest.mark.parametrize(
    "task_ids",
    [[], (), [0], [-1], [True], [1.0], ["1"], [1, 1], list(range(1, 102))],
)
def test_legacy_task_stage_read_rejects_untrusted_ids_before_rpc(monkeypatch, task_ids):
    client = client_with(
        monkeypatch,
        lambda *args, **kwargs: pytest.fail("executor called"),
    )

    with pytest.raises(ContractError):
        client.read_legacy_task_stages(task_ids)


def identity_responses(fields=None):
    return [
        "uuid-expected",
        [{"id": 4, "company_id": [8, "Gruber Pallets, Inc."]}],
        [{"id": 8, "name": "Gruber Pallets, Inc."}],
        contract_fields() if fields is None else fields,
    ]


def test_verify_target_identity_checks_uuid_company_and_contract_every_time(monkeypatch):
    calls = []
    responses = iter(identity_responses() * 2)
    client = client_with(
        monkeypatch,
        lambda model, method, *args, **kwargs: (
            calls.append((model, method, args, kwargs)) or next(responses)
        ),
        uid=4,
    )

    first = client.verify_target_identity()
    second = client.verify_target_identity()

    assert first == second == ImprovementContract(start_type="date", stop_type="date")
    assert calls.count(("ir.config_parameter", "get_param", ("database.uuid",), {})) == 2
    assert sum(call[0:2] == (TARGET_MODEL, "fields_get") for call in calls) == 2


def test_read_contract_is_uncached_and_does_not_read_identity(monkeypatch):
    calls = []
    fields = contract_fields()
    fields["x_studio_date_start"]["type"] = "datetime"
    client = client_with(
        monkeypatch,
        lambda model, method, *args, **kwargs: (
            calls.append((model, method, args, kwargs)) or fields
        ),
        uid=4,
    )

    assert client.read_contract() == ImprovementContract(start_type="datetime", stop_type="date")
    assert client.read_contract() == ImprovementContract(start_type="datetime", stop_type="date")
    assert [call[0:2] for call in calls] == [
        (TARGET_MODEL, "fields_get"),
        (TARGET_MODEL, "fields_get"),
    ]


def test_contract_normalizes_tuple_and_reordered_selection_shapes(monkeypatch):
    fields = contract_fields(source_selection=(("Other App", "Other"), (SOURCE_VALUE, "Plant")))
    fields["x_studio_status"]["selection"] = tuple(
        reversed([tuple(item) for item in fields["x_studio_status"]["selection"]])
    )
    fields["x_studio_type"]["selection"] = tuple(
        reversed([tuple(item) for item in fields["x_studio_type"]["selection"]])
    )
    client = client_with(monkeypatch, lambda *args, **kwargs: fields, uid=4)

    assert client.read_contract() == ImprovementContract("date", "date")


@pytest.mark.parametrize("defect", ["missing", "readonly", "wrong_type", "selection"])
def test_contract_fails_closed_for_missing_readonly_wrong_type_or_selection(monkeypatch, defect):
    fields = contract_fields()
    if defect == "missing":
        fields.pop("x_studio_notes")
    elif defect == "readonly":
        fields["x_name"]["readonly"] = True
    elif defect == "wrong_type":
        fields["x_studio_date_start"]["type"] = "char"
    else:
        fields["x_studio_source"]["selection"] = [["Other App", "Other App"]]
    client = client_with(monkeypatch, lambda *args, **kwargs: fields, uid=4)

    with pytest.raises(ContractError):
        client.read_contract()


@pytest.mark.parametrize(
    "responses",
    [
        ["uuid-other"],
        ["uuid-expected", []],
        [
            "uuid-expected",
            [{"id": 4, "company_id": [8, "Gruber Pallets, Inc."]}],
            [{"id": 8, "name": "Other Company"}],
        ],
    ],
)
def test_target_identity_mismatches_fail_closed_without_secret_values(monkeypatch, responses):
    iterator = iter(responses)
    client = client_with(
        monkeypatch,
        lambda *args, **kwargs: next(iterator),
        uid=4,
    )

    with pytest.raises(TargetIdentityError) as raised:
        client.verify_target_identity()

    message = str(raised.value)
    assert "uuid-other" not in message
    assert "Other Company" not in message
    assert ENV["ODOO_IMPROVEMENTS_EXPECTED_COMPANY"] not in message
