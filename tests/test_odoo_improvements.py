import dataclasses
import traceback
import xmlrpc.client

import pytest

from zira_dashboard import odoo_improvements as improvements
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
        "x_studio_submitted_by": {
            "type": "many2one",
            "readonly": False,
            "relation": "hr.employee",
        },
        "x_studio_date_stop": {"type": "date", "readonly": False},
        "x_studio_completed_by": {
            "type": "many2one",
            "readonly": False,
            "relation": "hr.employee",
        },
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
                ["Physical - Issue", "Physical - Issue"],
                ["Physical - Suggestion", "Physical - Suggestion"],
            ],
        },
        "x_studio_linked_task": {
            "type": "many2one",
            "readonly": False,
            "relation": "project.task",
        },
        "x_studio_linked_wo": {
            "type": "many2one",
            "readonly": False,
            "relation": "maintenance.request",
        },
    }


V1_TYPE_VALUES = {
    "Digital",
    "Digital - New Feature",
    "Physical - Issue",
    "Physical - Suggestion",
}
V2_TYPE_VALUES = V1_TYPE_VALUES | {"2s Improvement"}


@pytest.mark.parametrize(
    ("selection_values", "expected_version"),
    [(V1_TYPE_VALUES, 1), (V2_TYPE_VALUES, 2)],
)
def test_contract_version_accepts_only_exact_known_type_sets(selection_values, expected_version):
    assert improvements.odoo_contract_version(selection_values) == expected_version


@pytest.mark.parametrize(
    "selection_values",
    [
        V1_TYPE_VALUES - {"Physical - Suggestion"},
        (V2_TYPE_VALUES - {"2s Improvement"}) | {"2S Improvement"},
        V2_TYPE_VALUES | {"Unknown"},
    ],
)
def test_contract_version_rejects_missing_renamed_or_extra_type_values(selection_values):
    with pytest.raises(ContractError, match="type selection values"):
        improvements.odoo_contract_version(selection_values)


def test_linked_fields_are_readable_but_not_app_writable():
    assert {"x_studio_linked_task", "x_studio_linked_wo"} <= improvements._READ_FIELDS
    assert {"x_studio_linked_task", "x_studio_linked_wo"}.isdisjoint(
        improvements.WRITABLE_TARGET_FIELDS
    )


def _link_client(monkeypatch, *, linked_task=False, linked_wo=False, duplicate=False):
    calls = []
    reference = {
        "id": 71,
        "x_studio_source": SOURCE_VALUE,
        "x_studio_source_id": "GPI-PM-FB-17",
        "x_studio_linked_task": linked_task,
        "x_studio_linked_wo": linked_wo,
    }

    def executor(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        if (model, method) == (TARGET_MODEL, "search_read"):
            rows = [dict(reference)]
            if duplicate:
                rows.append({**reference, "id": 72})
            return rows
        if (model, method) == (TARGET_MODEL, "read"):
            return [dict(reference)]
        if (model, method) == (TARGET_MODEL, "write"):
            assert args == ([71], {"x_studio_linked_task": 902})
            reference["x_studio_linked_task"] = [902, "Review task"]
            return True
        if (model, method) == ("project.task", "read"):
            return [{"id": 902}]
        pytest.fail(f"unexpected RPC: {(model, method)}")

    client = client_with(monkeypatch, executor, uid=4)
    open_gates(monkeypatch)
    contract = ImprovementContract(start_type="date", stop_type="date", version=2)
    monkeypatch.setattr(client, "verify_target_identity", lambda: contract)
    return client, contract, calls


def test_link_task_once_rejects_preexisting_empty_link_without_writing(monkeypatch):
    client, contract, calls = _link_client(monkeypatch)

    with pytest.raises(ContractError, match="empty linked task cannot be filled safely"):
        client.link_task_once(71, 902, feedback_id=17, expected_contract=contract)

    writes = [call for call in calls if call[0:2] == (TARGET_MODEL, "write")]
    assert writes == []


def test_link_task_once_accepts_same_link_without_writing(monkeypatch):
    client, contract, calls = _link_client(
        monkeypatch, linked_task=[902, "Review task"]
    )

    client.link_task_once(71, 902, feedback_id=17, expected_contract=contract)

    assert not any(call[0:2] == (TARGET_MODEL, "write") for call in calls)


def test_link_task_once_rejects_conflicting_existing_link(monkeypatch):
    client, contract, calls = _link_client(
        monkeypatch, linked_task=[903, "Other task"]
    )

    with pytest.raises(ContractError, match="conflicting linked task"):
        client.link_task_once(71, 902, feedback_id=17, expected_contract=contract)

    assert not any(call[0:2] == (TARGET_MODEL, "write") for call in calls)


def test_link_task_once_rejects_duplicate_reference_without_writing(monkeypatch):
    client, contract, calls = _link_client(monkeypatch, duplicate=True)

    with pytest.raises(ContractError, match="duplicate reference"):
        client.link_task_once(71, 902, feedback_id=17, expected_contract=contract)

    assert not any(call[0:2] == (TARGET_MODEL, "write") for call in calls)


def client_with(monkeypatch, executor, *, uid=None):
    set_config(monkeypatch)
    return ImprovementsClient.from_env(executor=executor, uid=uid)


EXPECTED_CONTRACT = ImprovementContract(start_type="date", stop_type="date")


def create_fields(**changes):
    fields = {
        "x_name": "Safe",
        "x_studio_source": SOURCE_VALUE,
        "x_studio_source_id": "GPI-PM-FB-17",
    }
    fields.update(changes)
    return fields


def test_review_reference_create_includes_exact_task_link_in_one_rpc(monkeypatch):
    calls = []
    fields = create_fields(
        x_studio_type="Physical - Issue",
        x_studio_linked_task=902,
    )
    client = mutation_client(monkeypatch, 71, calls=calls)
    open_gates(monkeypatch)

    assert client.create_improvement(
        fields,
        feedback_id=17,
        expected_contract=EXPECTED_CONTRACT,
    ) == 71

    create_calls = [call for call in calls if call[0:2] == (TARGET_MODEL, "create")]
    assert len(create_calls) == 1
    assert create_calls[0][2] == (fields,)
    assert "x_studio_linked_wo" not in create_calls[0][2][0]


def mutation_client(monkeypatch, response, *, calls=None, fields=None):
    responses = iter(identity_responses(fields) + [response])

    def executor(model, method, *args, **kwargs):
        if calls is not None:
            calls.append((model, method, args, kwargs))
        return next(responses)

    return client_with(monkeypatch, executor, uid=4)


def authorization_client(monkeypatch):
    calls = []
    responses = iter(identity_responses())

    def executor(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        try:
            return next(responses)
        except StopIteration:
            pytest.fail("mutation executor called")

    client = client_with(monkeypatch, executor, uid=4)
    open_gates(monkeypatch)
    return client, calls


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


def test_client_does_not_expose_raw_executor_on_ordinary_attribute(monkeypatch):
    raw_executor = lambda *args, **kwargs: None
    client = client_with(monkeypatch, raw_executor, uid=4)

    assert "_executor" not in vars(client)
    with pytest.raises(AttributeError):
        _ = client._executor


def test_config_error_names_missing_settings_without_exposing_values(monkeypatch):
    set_config(monkeypatch)
    monkeypatch.setenv("ODOO_IMPROVEMENTS_LOGIN", "")
    monkeypatch.setenv("ODOO_IMPROVEMENTS_API_KEY", "do-not-expose")

    with pytest.raises(ImprovementsConfigError) as raised:
        ImprovementsConfig.from_env()

    message = str(raised.value)
    assert "ODOO_IMPROVEMENTS_LOGIN" in message
    assert "do-not-expose" not in message


def test_mixed_case_https_scheme_is_normalized_before_transport_selection(monkeypatch):
    set_config(monkeypatch)
    monkeypatch.setenv("ODOO_IMPROVEMENTS_URL", "HtTpS://odoo.invalid/")
    proxies = []

    def server_proxy(url, *, transport):
        proxies.append((url, transport))
        return object()

    monkeypatch.setattr(improvements.xmlrpc.client, "ServerProxy", server_proxy)

    client = ImprovementsClient.from_env()

    assert repr(client) == "ImprovementsClient(<redacted>)"
    assert [url for url, _transport in proxies] == [
        "https://odoo.invalid/xmlrpc/2/common",
        "https://odoo.invalid/xmlrpc/2/object",
    ]
    assert all(
        isinstance(transport, improvements._TimeoutSafeTransport) for _url, transport in proxies
    )


def test_default_executor_authenticates_and_calls_execute_kw_with_dedicated_values(monkeypatch):
    set_config(monkeypatch)
    calls = []

    class CommonProxy:
        def authenticate(self, *args):
            calls.append(("authenticate", args))
            return 4

    class ObjectProxy:
        def execute_kw(self, *args):
            calls.append(("execute_kw", args))
            return "uuid-expected"

    proxies = iter([CommonProxy(), ObjectProxy()])
    monkeypatch.setattr(improvements, "_server_proxy", lambda _url: next(proxies))
    client = ImprovementsClient.from_env()

    assert client.authenticate() == 4
    assert client._execute("ir.config_parameter", "get_param", "database.uuid") == ("uuid-expected")
    assert calls == [
        (
            "authenticate",
            ("database", "service@example.invalid", "secret-key", {}),
        ),
        (
            "execute_kw",
            (
                "database",
                4,
                "secret-key",
                "ir.config_parameter",
                "get_param",
                ["database.uuid"],
                {},
            ),
        ),
    ]


@pytest.mark.parametrize(
    ("transport_type", "base_type"),
    [
        (improvements._TimeoutTransport, xmlrpc.client.Transport),
        (improvements._TimeoutSafeTransport, xmlrpc.client.SafeTransport),
    ],
)
def test_dedicated_transports_set_socket_timeout(monkeypatch, transport_type, base_type):
    connection = type("Connection", (), {"timeout": None})()
    monkeypatch.setattr(base_type, "make_connection", lambda self, host: connection)

    assert transport_type().make_connection("odoo.invalid") is connection
    assert connection.timeout == 15


def assert_error_is_sanitized(error, forbidden):
    rendered = "".join(traceback.format_exception(error))
    for value in forbidden:
        assert value not in str(error)
        assert value not in repr(error)
        assert value not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


def test_default_authentication_error_drops_secrets_context_and_traceback(monkeypatch):
    set_config(monkeypatch)
    remote_text = "remote-auth-detail"

    class CommonProxy:
        def authenticate(self, *args):
            raise RuntimeError(
                f"{remote_text} {ENV['ODOO_IMPROVEMENTS_URL']} "
                f"{ENV['ODOO_IMPROVEMENTS_LOGIN']} {ENV['ODOO_IMPROVEMENTS_API_KEY']} "
                f"{ENV['ODOO_IMPROVEMENTS_DB']}"
            )

    proxies = iter([CommonProxy(), object()])
    monkeypatch.setattr(improvements, "_server_proxy", lambda _url: next(proxies))
    client = ImprovementsClient.from_env()

    with pytest.raises(OSError) as raised:
        client.authenticate()

    assert_error_is_sanitized(raised.value, [remote_text, *ENV.values()])


def test_default_proxy_construction_error_drops_url_and_context(monkeypatch):
    set_config(monkeypatch)
    remote_text = "proxy-construction-detail"

    def fail_proxy(_url):
        raise RuntimeError(f"{remote_text} {ENV['ODOO_IMPROVEMENTS_URL']}")

    monkeypatch.setattr(improvements, "_server_proxy", fail_proxy)

    with pytest.raises(OSError) as raised:
        ImprovementsClient.from_env()

    assert_error_is_sanitized(
        raised.value,
        [remote_text, ENV["ODOO_IMPROVEMENTS_URL"]],
    )


def test_default_execute_error_drops_remote_fault_context_and_traceback(monkeypatch):
    set_config(monkeypatch)
    remote_text = "remote-execute-detail"
    database_marker = "private-db-marker"
    monkeypatch.setenv("ODOO_IMPROVEMENTS_DB", database_marker)

    class CommonProxy:
        def authenticate(self, *args):
            return 4

    class ObjectProxy:
        def execute_kw(self, *args):
            raise xmlrpc.client.Fault(70017, f"{remote_text} {ENV['ODOO_IMPROVEMENTS_API_KEY']}")

    proxies = iter([CommonProxy(), ObjectProxy()])
    monkeypatch.setattr(improvements, "_server_proxy", lambda _url: next(proxies))
    client = ImprovementsClient.from_env()

    with pytest.raises(xmlrpc.client.Fault) as raised:
        client._execute("ir.config_parameter", "get_param", "database.uuid")

    assert raised.value.faultCode == 0
    assert_error_is_sanitized(
        raised.value,
        [
            remote_text,
            "70017",
            database_marker,
            ENV["ODOO_IMPROVEMENTS_URL"],
            ENV["ODOO_IMPROVEMENTS_LOGIN"],
            ENV["ODOO_IMPROVEMENTS_API_KEY"],
        ],
    )


def test_injected_executor_error_is_sanitized_without_chaining(monkeypatch):
    remote_text = "injected-remote-detail"
    database_marker = "private-db-marker"

    def executor(*args, **kwargs):
        raise ConnectionError(f"{remote_text} {ENV['ODOO_IMPROVEMENTS_API_KEY']}")

    set_config(monkeypatch)
    monkeypatch.setenv("ODOO_IMPROVEMENTS_DB", database_marker)
    client = ImprovementsClient.from_env(executor=executor)

    with pytest.raises(ConnectionError) as raised:
        client._execute("ir.config_parameter", "get_param", "database.uuid")

    assert_error_is_sanitized(
        raised.value,
        [
            remote_text,
            database_marker,
            ENV["ODOO_IMPROVEMENTS_URL"],
            ENV["ODOO_IMPROVEMENTS_LOGIN"],
            ENV["ODOO_IMPROVEMENTS_API_KEY"],
        ],
    )


def test_allowlist_error_does_not_echo_caller_controlled_identifiers(monkeypatch):
    client = client_with(monkeypatch, lambda *args, **kwargs: None)
    attacker_values = ["attacker-model-secret", "attacker-method-secret"]

    with pytest.raises(ContractError) as raised:
        client._execute(*attacker_values)

    assert_error_is_sanitized(raised.value, attacker_values)


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


def test_huge_canary_numeric_text_fails_closed_with_domain_error(monkeypatch):
    set_config(monkeypatch)
    open_gates(monkeypatch, canary="9" * 10_000)

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


def test_client_rejects_oversized_injected_user_id(monkeypatch):
    set_config(monkeypatch)

    with pytest.raises(ContractError):
        ImprovementsClient.from_env(
            executor=lambda *args, **kwargs: None,
            uid=9_223_372_036_854_775_808,
        )


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


@pytest.mark.parametrize("method", ["create", "write"])
def test_raw_execute_cannot_bypass_target_mutation_wrappers(monkeypatch, method):
    executor_calls = []
    client = client_with(
        monkeypatch,
        lambda *args, **kwargs: executor_calls.append((args, kwargs)) or True,
    )

    with pytest.raises(ContractError):
        client._execute(
            TARGET_MODEL,
            method,
            create_fields(),
            authorization=object(),
        )

    assert executor_calls == []


def test_raw_execute_rejects_legitimately_minted_mutation_authorization(monkeypatch):
    client, calls = authorization_client(monkeypatch)
    authorization = client._authorize_mutation(
        "write",
        feedback_id=17,
        remote_id=9,
        fields={"x_name": "Safe"},
        expected_contract=EXPECTED_CONTRACT,
    )

    with pytest.raises(ContractError):
        client._execute(
            TARGET_MODEL,
            "write",
            [9],
            {"x_name": "Safe"},
            authorization=authorization,
        )

    assert not any(method == "write" for _model, method, _args, _kwargs in calls)


@pytest.mark.parametrize(
    ("method", "feedback_id", "remote_id", "fields"),
    [
        ([], 17, 9, {"x_name": "Safe"}),
        ("create", 17, None, create_fields()),
        ("write", 18, 9, {"x_name": "Safe"}),
        ("write", 17, 10, {"x_name": "Safe"}),
        ("write", 17, 9, {"x_name": "Changed but valid"}),
        ("write", 17, 9, {"active": False}),
        ("write", 17, 9, {"x_studio_unapproved": "value"}),
        ("write", 17, 9, {"x_studio_type": "Physical"}),
        ("create", 17, None, {"x_name": "Missing identity"}),
    ],
)
def test_mutation_authorization_rejects_every_changed_operation_and_is_one_use(
    monkeypatch,
    method,
    feedback_id,
    remote_id,
    fields,
):
    client, calls = authorization_client(monkeypatch)
    authorization = client._authorize_mutation(
        "write",
        feedback_id=17,
        remote_id=9,
        fields={"x_name": "Safe"},
        expected_contract=EXPECTED_CONTRACT,
    )

    with pytest.raises(ContractError):
        client._consume_mutation_authorization(
            authorization,
            method,
            feedback_id=feedback_id,
            remote_id=remote_id,
            fields=fields,
        )
    with pytest.raises(ContractError):
        client._consume_mutation_authorization(
            authorization,
            "write",
            feedback_id=17,
            remote_id=9,
            fields={"x_name": "Safe"},
        )

    assert not any(
        called_method in {"create", "write"} for _model, called_method, _args, _kwargs in calls
    )


def test_mutation_authorization_defensively_copies_validated_payload(monkeypatch):
    client, calls = authorization_client(monkeypatch)
    fields = {"x_name": "Safe"}
    authorization = client._authorize_mutation(
        "write",
        feedback_id=17,
        remote_id=9,
        fields=fields,
        expected_contract=EXPECTED_CONTRACT,
    )
    fields["x_name"] = "Changed after minting"

    with pytest.raises(ContractError):
        client._consume_mutation_authorization(
            authorization,
            "write",
            feedback_id=17,
            remote_id=9,
            fields=fields,
        )

    assert not any(method == "write" for _model, method, _args, _kwargs in calls)


def test_authorization_rejects_non_string_value_that_claims_to_equal_source(monkeypatch):
    class PretendSource:
        def __ne__(self, other):
            return False

    executor_calls = []
    client = client_with(
        monkeypatch,
        lambda *args, **kwargs: executor_calls.append((args, kwargs)),
        uid=4,
    )
    open_gates(monkeypatch)

    with pytest.raises(ContractError):
        client._authorize_mutation(
            "write",
            feedback_id=17,
            remote_id=9,
            fields={"x_studio_source": PretendSource()},
            expected_contract=EXPECTED_CONTRACT,
        )

    assert executor_calls == []


@pytest.mark.parametrize(
    ("method", "feedback_id", "remote_id", "fields"),
    [
        ([], 17, 9, {"x_name": "Safe"}),
        ("unlink", 17, 9, {"x_name": "Safe"}),
        ("create", 17, 9, create_fields()),
        ("write", 17, None, {"x_name": "Safe"}),
        ("create", 17, None, {"x_name": "Missing identity"}),
        ("write", 17, 9, {"active": False}),
        ("write", 17, 9, {"x_studio_unapproved": "value"}),
        ("write", 17, 9, {"x_studio_type": "Physical"}),
    ],
)
def test_authorization_helper_cannot_mint_for_invalid_complete_operation(
    monkeypatch,
    method,
    feedback_id,
    remote_id,
    fields,
):
    executor_calls = []
    client = client_with(
        monkeypatch,
        lambda *args, **kwargs: executor_calls.append((args, kwargs)),
        uid=4,
    )
    open_gates(monkeypatch)

    with pytest.raises(ContractError):
        client._authorize_mutation(
            method,
            feedback_id=feedback_id,
            remote_id=remote_id,
            fields=fields,
            expected_contract=EXPECTED_CONTRACT,
        )

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
        {"x_studio_type": []},
        {"x_studio_status": {}},
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
        client.write_improvement(
            9,
            fields,
            feedback_id=17,
            expected_contract=EXPECTED_CONTRACT,
        )

    assert executor_calls == []


def test_target_field_error_does_not_echo_caller_controlled_name(monkeypatch):
    attacker_field = "attacker-field-secret"
    client = client_with(monkeypatch, lambda *args, **kwargs: None)

    with pytest.raises(ContractError) as raised:
        client.write_improvement(
            9,
            {attacker_field: "x"},
            feedback_id=17,
            expected_contract=EXPECTED_CONTRACT,
        )

    assert_error_is_sanitized(raised.value, [attacker_field])


@pytest.mark.parametrize(
    "remote_id",
    [True, 0, -1, 1.0, "9", None, 9_223_372_036_854_775_808],
)
def test_write_validates_remote_id_before_gate_or_rpc(monkeypatch, remote_id):
    client = client_with(
        monkeypatch,
        lambda *args, **kwargs: pytest.fail("executor called"),
    )

    with pytest.raises(ContractError, match="remote id"):
        client.write_improvement(
            remote_id,
            {"x_name": "Safe"},
            feedback_id=17,
            expected_contract=EXPECTED_CONTRACT,
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"x_name": "Safe"},
        {"x_name": "Safe", "x_studio_source": SOURCE_VALUE},
        {
            "x_name": "Safe",
            "x_studio_source_id": "GPI-PM-FB-17",
        },
        create_fields(x_studio_source="Another App"),
        create_fields(x_studio_source_id="GPI-PM-FB-18"),
    ],
)
def test_create_requires_exact_compound_source_identity_before_rpc(monkeypatch, fields):
    client = client_with(
        monkeypatch,
        lambda *args, **kwargs: pytest.fail("executor called"),
    )

    with pytest.raises(ContractError):
        client.create_improvement(
            fields,
            feedback_id=17,
            expected_contract=EXPECTED_CONTRACT,
        )


def test_create_and_write_check_both_gates_and_canary_immediately_before_rpc(monkeypatch):
    calls = []
    responses = iter(identity_responses() + [23] + identity_responses() + [True])
    client = client_with(
        monkeypatch,
        lambda model, method, *args, **kwargs: (
            calls.append((model, method, args, kwargs)) or next(responses)
        ),
        uid=4,
    )
    open_gates(monkeypatch, canary="17")

    assert (
        client.create_improvement(
            create_fields(),
            feedback_id=17,
            expected_contract=EXPECTED_CONTRACT,
        )
        == 23
    )
    client.write_improvement(
        23,
        {"x_studio_status": "Completed"},
        feedback_id=17,
        expected_contract=EXPECTED_CONTRACT,
    )

    mutation_calls = [
        call for call in calls if call[0] == TARGET_MODEL and call[1] in {"create", "write"}
    ]
    assert [(call[0], call[1]) for call in mutation_calls] == [
        (TARGET_MODEL, "create"),
        (TARGET_MODEL, "write"),
    ]
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "false")
    with pytest.raises(GateClosed):
        client.write_improvement(
            23,
            {"x_name": "Still safe"},
            feedback_id=17,
            expected_contract=EXPECTED_CONTRACT,
        )
    assert len([call for call in calls if call[1] in {"create", "write"}]) == 2


def test_mutation_contract_must_match_fresh_identity_contract(monkeypatch):
    calls = []
    client = mutation_client(monkeypatch, 23, calls=calls)
    open_gates(monkeypatch)

    with pytest.raises(TargetIdentityError):
        client.create_improvement(
            create_fields(),
            feedback_id=17,
            expected_contract=ImprovementContract("datetime", "date"),
        )

    assert not any(method == "create" for _model, method, _args, _kwargs in calls)


@pytest.mark.parametrize("expected_contract", [None, ("date", "date"), object()])
def test_mutation_rejects_non_contract_expected_value_before_rpc(monkeypatch, expected_contract):
    client = client_with(
        monkeypatch,
        lambda *args, **kwargs: pytest.fail("executor called"),
    )
    open_gates(monkeypatch)

    with pytest.raises(ContractError):
        client.create_improvement(
            create_fields(),
            feedback_id=17,
            expected_contract=expected_contract,
        )


def test_gates_are_rechecked_after_fresh_identity_and_before_executor(monkeypatch):
    calls = []
    client = mutation_client(monkeypatch, 23, calls=calls)
    open_gates(monkeypatch)
    real_check = client.assert_mutation_allowed
    gate_checks = 0

    def close_on_final_check(feedback_id):
        nonlocal gate_checks
        gate_checks += 1
        if gate_checks == 3:
            monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", "false")
        real_check(feedback_id)

    monkeypatch.setattr(client, "assert_mutation_allowed", close_on_final_check)

    with pytest.raises(GateClosed):
        client.create_improvement(
            create_fields(),
            feedback_id=17,
            expected_contract=EXPECTED_CONTRACT,
        )

    assert gate_checks == 3
    assert not any(method == "create" for _model, method, _args, _kwargs in calls)


@pytest.mark.parametrize("response", [None, False, True, 0, -1, 2.0, "2"])
def test_create_requires_a_positive_integer_response(monkeypatch, response):
    client = mutation_client(monkeypatch, response)
    open_gates(monkeypatch)

    with pytest.raises(MalformedMutationResponse):
        client.create_improvement(
            create_fields(),
            feedback_id=17,
            expected_contract=EXPECTED_CONTRACT,
        )


def test_create_rejects_oversized_positive_integer_response(monkeypatch):
    client = mutation_client(monkeypatch, 9_223_372_036_854_775_808)
    open_gates(monkeypatch)

    with pytest.raises(MalformedMutationResponse):
        client.create_improvement(
            create_fields(),
            feedback_id=17,
            expected_contract=EXPECTED_CONTRACT,
        )


@pytest.mark.parametrize("response", [None, False, 1, "true", []])
def test_write_requires_exact_true_response(monkeypatch, response):
    client = mutation_client(monkeypatch, response)
    open_gates(monkeypatch)

    with pytest.raises(MalformedMutationResponse):
        client.write_improvement(
            9,
            {"x_name": "Safe"},
            feedback_id=17,
            expected_contract=EXPECTED_CONTRACT,
        )


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


def test_find_exact_rejects_oversized_remote_id(monkeypatch):
    rows = [
        {
            "id": 9_223_372_036_854_775_808,
            "x_studio_source": SOURCE_VALUE,
            "x_studio_source_id": "GPI-PM-FB-17",
        }
    ]
    client = client_with(monkeypatch, lambda *args, **kwargs: rows)

    with pytest.raises(ContractError):
        client.find_exact("GPI-PM-FB-17")


@pytest.mark.parametrize("source_id", [None, 17, "", "GPI-PM-FB-0", "GPI-PM-FB-01", "FB-17"])
def test_find_exact_rejects_noncanonical_source_ids(monkeypatch, source_id):
    client = client_with(
        monkeypatch,
        lambda *args, **kwargs: pytest.fail("executor called"),
    )

    with pytest.raises(ContractError, match="source id"):
        client.find_exact(source_id)


def test_find_exact_rejects_huge_numeric_source_id_with_contract_error(monkeypatch):
    client = client_with(
        monkeypatch,
        lambda *args, **kwargs: pytest.fail("executor called"),
    )

    with pytest.raises(ContractError, match="source id"):
        client.find_exact("GPI-PM-FB-" + "9" * 10_000)


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


def test_read_improvement_rejects_boolean_response_id(monkeypatch):
    client = client_with(monkeypatch, lambda *args, **kwargs: [{"id": True}])

    with pytest.raises(ContractError):
        client.read_improvement(1, ["id"], full_binary=False)


@pytest.mark.parametrize(
    ("remote_id", "fields", "full_binary"),
    [
        (0, ["id"], False),
        (True, ["id"], False),
        (9_223_372_036_854_775_808, ["id"], False),
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
        ("person%40example@example.invalid", 3),
        ("person_name@example.invalid", 3),
        ("person\\name@example.invalid", 3),
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


@pytest.mark.parametrize(
    "rows",
    [
        [{"id": 7, "work_email": "other@example.invalid"}],
        [{"id": 7, "work_email": " Person@Example.invalid "}],
        [
            {"id": 7, "work_email": "person@example.invalid"},
            {"id": 7, "work_email": "person@example.invalid"},
        ],
        [{"id": True, "work_email": "person@example.invalid"}],
        [
            {
                "id": 9_223_372_036_854_775_808,
                "work_email": "person@example.invalid",
            }
        ],
    ],
)
def test_employee_lookup_rejects_mismatched_email_and_duplicate_or_invalid_ids(monkeypatch, rows):
    client = client_with(monkeypatch, lambda *args, **kwargs: rows)

    with pytest.raises(ContractError):
        client.find_employees_by_email("person@example.invalid", limit=3)


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
    [
        [],
        (),
        [0],
        [-1],
        [True],
        [1.0],
        ["1"],
        [9_223_372_036_854_775_808],
        [1, 1],
        list(range(1, 102)),
    ],
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
    assert all("relation" in call[3]["attributes"] for call in calls)


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


@pytest.mark.parametrize("defect", ["missing", "extra"])
def test_contract_rejects_missing_or_extra_type_selection_value(monkeypatch, defect):
    fields = contract_fields()
    if defect == "missing":
        fields["x_studio_type"]["selection"].pop()
    else:
        fields["x_studio_type"]["selection"].append(["Other", "Other"])
    client = client_with(monkeypatch, lambda *args, **kwargs: fields, uid=4)

    with pytest.raises(ContractError, match="type selection values"):
        client.read_contract()


@pytest.mark.parametrize("defect", ["missing", "readonly", "wrong_type", "selection", "relation"])
def test_contract_fails_closed_for_missing_readonly_wrong_type_or_selection(monkeypatch, defect):
    fields = contract_fields()
    if defect == "missing":
        fields.pop("x_studio_notes")
    elif defect == "readonly":
        fields["x_name"]["readonly"] = True
    elif defect == "wrong_type":
        fields["x_studio_date_start"]["type"] = "char"
    elif defect == "selection":
        fields["x_studio_source"]["selection"] = [["Other App", "Other App"]]
    else:
        fields["x_studio_completed_by"]["relation"] = "res.users"
    client = client_with(monkeypatch, lambda *args, **kwargs: fields, uid=4)

    with pytest.raises(ContractError):
        client.read_contract()


def test_contract_requires_create_only_review_task_link_to_be_writable(monkeypatch):
    fields = contract_fields()
    fields["x_studio_linked_task"]["readonly"] = True
    client = client_with(monkeypatch, lambda *args, **kwargs: fields, uid=4)

    with pytest.raises(ContractError, match="x_studio_linked_task must be writable"):
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


@pytest.mark.parametrize("malformed_at", ["user", "company"])
def test_target_identity_rejects_boolean_response_ids(monkeypatch, malformed_at):
    user_id = True if malformed_at == "user" else 1
    linked_company_id = 1 if malformed_at == "company" else 8
    company_id = True if malformed_at == "company" else 8
    responses = iter(
        [
            "uuid-expected",
            [
                {
                    "id": user_id,
                    "company_id": [linked_company_id, "Gruber Pallets, Inc."],
                }
            ],
            [{"id": company_id, "name": "Gruber Pallets, Inc."}],
            contract_fields(),
        ]
    )
    client = client_with(monkeypatch, lambda *args, **kwargs: next(responses), uid=1)

    with pytest.raises(TargetIdentityError):
        client.verify_target_identity()


@pytest.mark.parametrize(
    "company_value",
    [
        (8, "Gruber Pallets, Inc."),
        [8],
        [8, "Gruber Pallets, Inc.", "extra"],
        [True, "Gruber Pallets, Inc."],
        [8.0, "Gruber Pallets, Inc."],
        [0, "Gruber Pallets, Inc."],
        [9_223_372_036_854_775_808, "Gruber Pallets, Inc."],
        [8, 9],
        [8, "Other Company"],
    ],
)
def test_target_identity_requires_exact_company_many2one_shape(monkeypatch, company_value):
    responses = iter(
        [
            "uuid-expected",
            [{"id": 4, "company_id": company_value}],
            [{"id": 8, "name": "Gruber Pallets, Inc."}],
            contract_fields(),
        ]
    )
    client = client_with(monkeypatch, lambda *args, **kwargs: next(responses), uid=4)

    with pytest.raises(TargetIdentityError):
        client.verify_target_identity()
