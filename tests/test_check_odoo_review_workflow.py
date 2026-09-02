from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re

import pytest

from scripts import check_odoo_review_workflow as checker


TEST_UUID = "11111111-1111-4111-8111-111111111111"
PRODUCTION_UUID = "22222222-2222-4222-8222-222222222222"


class FakeOdooClient:
    def __init__(self, facts: checker.WorkflowFacts) -> None:
        self.facts = facts
        self.exercise_calls = 0

    def inspect(self) -> checker.WorkflowFacts:
        return self.facts

    def exercise(
        self,
        webhook_url: str,
        *,
        expected_database_uuid: str,
        production_database_uuid: str,
        expected_company: str,
    ) -> checker.ExerciseResult:
        assert webhook_url == "https://duplicate.invalid/secret-path"
        assert expected_database_uuid == TEST_UUID
        assert production_database_uuid == PRODUCTION_UUID
        assert expected_company == "Expected Duplicate Company"
        self.exercise_calls += 1
        return checker.ExerciseResult(rows=4, tasks=4, actions=5)


def good_automation(name: str) -> checker.AutomationFacts:
    if name == checker.CREATION_AUTOMATION_NAME:
        return checker.AutomationFacts(
            name=name,
            active=True,
            model=checker.IMPROVEMENT_MODEL,
            trigger=checker.CREATION_TRIGGER,
            domain=checker.CREATION_DOMAIN,
            watched_fields=checker.CREATION_WATCHED_FIELDS,
            record_getter="",
            log_webhook_calls=False,
            actions=(
                checker.ServerActionFacts(
                    "code", checker.CREATION_CODE_HASH, checker.IMPROVEMENT_MODEL
                ),
            ),
        )
    if name == checker.DIGITAL_AUTOMATION_NAME:
        return checker.AutomationFacts(
            name=name,
            active=True,
            model="project.task",
            trigger=checker.DIGITAL_TRIGGER,
            domain=checker.DIGITAL_DOMAIN,
            watched_fields=checker.DIGITAL_WATCHED_FIELDS,
            record_getter="",
            log_webhook_calls=False,
            actions=(checker.ServerActionFacts("code", checker.DIGITAL_CODE_HASH, "project.task"),),
        )
    return checker.AutomationFacts(
        name=name,
        active=True,
        model=checker.IMPROVEMENT_MODEL,
        trigger=checker.REVIEW_TRIGGER,
        domain=checker.REVIEW_DOMAIN,
        watched_fields=checker.REVIEW_WATCHED_FIELDS,
        record_getter=checker.REVIEW_RECORD_GETTER,
        log_webhook_calls=False,
        actions=(
            checker.ServerActionFacts("code", checker.REVIEW_CODE_HASH, checker.IMPROVEMENT_MODEL),
        ),
    )


def good_facts() -> checker.WorkflowFacts:
    return checker.WorkflowFacts(
        database_uuid=TEST_UUID,
        company_name="Expected Duplicate Company",
        webhook_binding_matches=True,
        type_values=checker.EXPECTED_TYPE_VALUES,
        projects=(checker.NamedRecord(id=10, name=checker.REVIEW_PROJECT),),
        stages={
            checker.INITIAL_STAGE: (checker.NamedRecord(id=20, name=checker.INITIAL_STAGE),),
            checker.MEETING_STAGE: (checker.NamedRecord(id=21, name=checker.MEETING_STAGE),),
        },
        dale_users=(
            checker.UserRecord(
                id=30,
                login=checker.DALE_LOGIN,
                active=True,
            ),
        ),
        automations={
            checker.CREATION_AUTOMATION_NAME: (good_automation(checker.CREATION_AUTOMATION_NAME),),
            checker.REVIEW_AUTOMATION_NAME: (good_automation(checker.REVIEW_AUTOMATION_NAME),),
            checker.DIGITAL_AUTOMATION_NAME: (good_automation(checker.DIGITAL_AUTOMATION_NAME),),
        },
        duplicate_source_identities=(),
    )


def config(**changes: object) -> checker.CheckConfig:
    base = checker.CheckConfig(
        webhook_configured=True,
        webhook_url="https://duplicate.invalid/secret-path",
        test_database_uuid=TEST_UUID,
        production_database_uuid=PRODUCTION_UUID,
        expected_company="Expected Duplicate Company",
        workflow_v2_enabled=False,
    )
    return replace(base, **changes)


def check(facts: checker.WorkflowFacts | None = None, **kwargs: object):
    client = FakeOdooClient(facts or good_facts())
    result = checker.check_workflow(client, config(), **kwargs)
    return client, result


def test_audited_hashes_match_the_versioned_runbook_code() -> None:
    runbook = Path("docs/odoo/2s-review-workflow-setup.md").read_text()
    blocks = re.findall(r"```python\n(.*?)```", runbook, re.DOTALL)

    assert checker._code_hash(blocks[1]) == checker.CREATION_CODE_HASH
    assert checker._normalize_code(blocks[2]) == checker.REVIEW_RECORD_GETTER
    assert checker._code_hash(blocks[3]) == checker.REVIEW_CODE_HASH
    assert checker._code_hash(blocks[4]) == checker.DIGITAL_CODE_HASH


@pytest.mark.parametrize("raw", [False, None, "", "[]"])
def test_empty_odoo_domain_normalizes_to_the_empty_contract(raw: object) -> None:
    assert checker._normalize_domain(raw) == ()


def test_exact_success_reports_only_fixed_safe_facts() -> None:
    client, result = check()

    assert result.ok is True
    assert result.issues == ()
    assert result.safe_lines == (
        "OK contract=V2",
        "OK project=one",
        "OK stages=General,L10",
        "OK dale=one-active",
        "OK automations=creation,review,digital-enabled",
        "OK automation-actions=audited",
        "OK company=matched",
        "OK webhook=duplicate-bound",
        "OK source-identities=unique",
    )
    assert client.exercise_calls == 0


@pytest.mark.parametrize(
    ("values", "issue"),
    [
        (
            checker.EXPECTED_TYPE_VALUES[:-1],
            checker.SafeIssue.TYPE_SELECTION,
        ),
        (
            checker.EXPECTED_TYPE_VALUES[:-1] + ("2S Improvement",),
            checker.SafeIssue.TYPE_SELECTION,
        ),
    ],
)
def test_missing_or_misspelled_fifth_selection_fails_safely(
    values: tuple[str, ...], issue: checker.SafeIssue
) -> None:
    _, result = check(replace(good_facts(), type_values=values))

    assert result.ok is False
    assert result.issues == (issue,)


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_duplicate_review_project_fails_safely(count: int) -> None:
    projects = tuple(
        checker.NamedRecord(id=10 + index, name=checker.REVIEW_PROJECT) for index in range(count)
    )

    _, result = check(replace(good_facts(), projects=projects))

    assert result.issues == (checker.SafeIssue.PROJECT_CARDINALITY,)


def test_archived_review_project_counts_as_missing() -> None:
    projects = (checker.NamedRecord(id=10, name=checker.REVIEW_PROJECT, active=False),)

    _, result = check(replace(good_facts(), projects=projects))

    assert result.issues == (checker.SafeIssue.PROJECT_CARDINALITY,)


@pytest.mark.parametrize("stage_name", [checker.INITIAL_STAGE, checker.MEETING_STAGE])
@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_duplicate_project_stage_fails_safely(stage_name: str, count: int) -> None:
    facts = good_facts()
    stages = dict(facts.stages)
    stages[stage_name] = tuple(
        checker.NamedRecord(id=40 + index, name=stage_name) for index in range(count)
    )

    _, result = check(replace(facts, stages=stages))

    expected = (
        checker.SafeIssue.INITIAL_STAGE_CARDINALITY
        if stage_name == checker.INITIAL_STAGE
        else checker.SafeIssue.MEETING_STAGE_CARDINALITY
    )
    assert result.issues == (expected,)


@pytest.mark.parametrize("stage_name", [checker.INITIAL_STAGE, checker.MEETING_STAGE])
def test_archived_project_stage_counts_as_missing(stage_name: str) -> None:
    facts = good_facts()
    stages = dict(facts.stages)
    stages[stage_name] = (checker.NamedRecord(id=40, name=stage_name, active=False),)

    _, result = check(replace(facts, stages=stages))

    expected = (
        checker.SafeIssue.INITIAL_STAGE_CARDINALITY
        if stage_name == checker.INITIAL_STAGE
        else checker.SafeIssue.MEETING_STAGE_CARDINALITY
    )
    assert result.issues == (expected,)


@pytest.mark.parametrize(
    "users",
    [
        (),
        (checker.UserRecord(id=30, login=checker.DALE_LOGIN, active=False),),
        (
            checker.UserRecord(id=30, login=checker.DALE_LOGIN, active=True),
            checker.UserRecord(id=31, login=checker.DALE_LOGIN, active=True),
        ),
    ],
)
def test_dale_missing_inactive_or_ambiguous_fails_safely(
    users: tuple[checker.UserRecord, ...],
) -> None:
    _, result = check(replace(good_facts(), dale_users=users))

    assert result.issues == (checker.SafeIssue.DALE_CARDINALITY,)


@pytest.mark.parametrize(
    ("automation_name", "issue"),
    [
        (
            checker.CREATION_AUTOMATION_NAME,
            checker.SafeIssue.CREATION_AUTOMATION,
        ),
        (
            checker.REVIEW_AUTOMATION_NAME,
            checker.SafeIssue.REVIEW_AUTOMATION,
        ),
    ],
)
@pytest.mark.parametrize("condition", ["missing", "disabled"])
def test_missing_or_disabled_automation_fails_safely(
    automation_name: str, issue: checker.SafeIssue, condition: str
) -> None:
    facts = good_facts()
    automations = dict(facts.automations)
    if condition == "missing":
        automations[automation_name] = ()
    else:
        automations[automation_name] = (replace(good_automation(automation_name), active=False),)

    _, result = check(replace(facts, automations=automations))

    assert result.issues == (issue,)


@pytest.mark.parametrize("wrong_part", ["domain", "watched_fields"])
def test_wrong_creation_domain_or_watched_fields_fails_safely(wrong_part: str) -> None:
    facts = good_facts()
    automation = good_automation(checker.CREATION_AUTOMATION_NAME)
    if wrong_part == "domain":
        automation = replace(automation, domain=())
    else:
        automation = replace(automation, watched_fields=frozenset())
    automations = dict(facts.automations)
    automations[checker.CREATION_AUTOMATION_NAME] = (automation,)

    _, result = check(replace(facts, automations=automations))

    assert result.issues == (checker.SafeIssue.CREATION_AUTOMATION_CONTRACT,)


@pytest.mark.parametrize("condition", ["missing", "disabled"])
def test_missing_or_disabled_digital_lifecycle_fails_safely(condition: str) -> None:
    facts = good_facts()
    automations = dict(facts.automations)
    if condition == "missing":
        automations[checker.DIGITAL_AUTOMATION_NAME] = ()
    else:
        automations[checker.DIGITAL_AUTOMATION_NAME] = (
            replace(good_automation(checker.DIGITAL_AUTOMATION_NAME), active=False),
        )

    _, result = check(replace(facts, automations=automations))

    assert result.issues == (checker.SafeIssue.DIGITAL_AUTOMATION,)


@pytest.mark.parametrize(
    ("name", "change", "issue"),
    [
        (
            checker.CREATION_AUTOMATION_NAME,
            {"actions": (checker.ServerActionFacts("code", "wrong", checker.IMPROVEMENT_MODEL),)},
            checker.SafeIssue.CREATION_AUTOMATION_CONTRACT,
        ),
        (
            checker.REVIEW_AUTOMATION_NAME,
            {"record_getter": "model.browse()"},
            checker.SafeIssue.REVIEW_AUTOMATION_CONTRACT,
        ),
        (
            checker.REVIEW_AUTOMATION_NAME,
            {"log_webhook_calls": True},
            checker.SafeIssue.REVIEW_AUTOMATION_CONTRACT,
        ),
        (
            checker.REVIEW_AUTOMATION_NAME,
            {
                "actions": (
                    checker.ServerActionFacts(
                        "code", checker.REVIEW_CODE_HASH, checker.IMPROVEMENT_MODEL
                    ),
                    checker.ServerActionFacts(
                        "code", checker.REVIEW_CODE_HASH, checker.IMPROVEMENT_MODEL
                    ),
                )
            },
            checker.SafeIssue.REVIEW_AUTOMATION_CONTRACT,
        ),
        (
            checker.DIGITAL_AUTOMATION_NAME,
            {
                "actions": (
                    checker.ServerActionFacts("email", checker.DIGITAL_CODE_HASH, "project.task"),
                )
            },
            checker.SafeIssue.DIGITAL_AUTOMATION_CONTRACT,
        ),
    ],
)
def test_server_action_getter_logging_type_cardinality_and_hash_are_audited(
    name: str, change: dict, issue: checker.SafeIssue
) -> None:
    facts = good_facts()
    automations = dict(facts.automations)
    automations[name] = (replace(good_automation(name), **change),)

    _, result = check(replace(facts, automations=automations))

    assert result.issues == (issue,)


def test_expected_company_is_required_and_must_match() -> None:
    client = FakeOdooClient(good_facts())
    missing = checker.check_workflow(client, config(expected_company=""))
    mismatch = checker.check_workflow(
        client,
        config(expected_company="Other Company"),
    )

    assert missing.issues == (checker.SafeIssue.EXPECTED_COMPANY,)
    assert mismatch.issues == (checker.SafeIssue.COMPANY_MISMATCH,)


def test_webhook_must_bind_to_the_inspected_duplicate_rule() -> None:
    facts = replace(good_facts(), webhook_binding_matches=False)

    _, result = check(facts)

    assert result.issues == (checker.SafeIssue.WEBHOOK_BINDING,)


def test_secret_absent_locally_fails_without_echoing_a_value() -> None:
    client = FakeOdooClient(good_facts())

    result = checker.check_workflow(
        client,
        config(webhook_configured=False, webhook_url=""),
    )

    assert result.issues == (checker.SafeIssue.WEBHOOK_SECRET,)
    assert all("http" not in line for line in result.safe_lines)


def test_duplicate_source_identity_fails_safely() -> None:
    facts = replace(
        good_facts(),
        duplicate_source_identities=(("GPI Plant Manager", "GPI-PM-FB-1"),),
    )

    _, result = check(facts)

    assert result.issues == (checker.SafeIssue.DUPLICATE_SOURCE_IDENTITY,)


def test_exercise_requires_both_explicit_flags() -> None:
    client = FakeOdooClient(good_facts())

    first = checker.check_workflow(client, config(), exercise=True)
    second = checker.check_workflow(
        client,
        config(),
        allow_duplicate_db=True,
    )

    assert first.issues == (checker.SafeIssue.EXERCISE_FLAGS,)
    assert second.issues == (checker.SafeIssue.EXERCISE_FLAGS,)
    assert client.exercise_calls == 0


def test_exercise_rejects_production_target_even_if_test_uuid_is_misconfigured() -> None:
    facts = replace(good_facts(), database_uuid=PRODUCTION_UUID)
    client = FakeOdooClient(facts)

    result = checker.check_workflow(
        client,
        config(test_database_uuid=PRODUCTION_UUID),
        exercise=True,
        allow_duplicate_db=True,
    )

    assert result.issues == (checker.SafeIssue.PRODUCTION_EXERCISE,)
    assert client.exercise_calls == 0


@pytest.mark.parametrize("production_uuid", ["", "not-a-uuid"])
def test_exercise_requires_a_canonical_production_uuid_fence(
    production_uuid: str,
) -> None:
    client = FakeOdooClient(good_facts())

    result = checker.check_workflow(
        client,
        config(production_database_uuid=production_uuid),
        exercise=True,
        allow_duplicate_db=True,
    )

    assert result.issues == (checker.SafeIssue.PRODUCTION_UUID,)
    assert client.exercise_calls == 0


@pytest.mark.parametrize(
    "test_uuid",
    ["", "not-a-uuid", "11111111-1111-4111-8111-11111111111A", PRODUCTION_UUID],
)
def test_exercise_requires_exact_canonical_duplicate_uuid(test_uuid: str) -> None:
    client = FakeOdooClient(good_facts())

    result = checker.check_workflow(
        client,
        config(test_database_uuid=test_uuid),
        exercise=True,
        allow_duplicate_db=True,
    )

    assert result.issues in {
        (checker.SafeIssue.TEST_UUID,),
        (checker.SafeIssue.TEST_UUID_MISMATCH,),
    }
    assert client.exercise_calls == 0


def test_guarded_exercise_calls_client_only_after_every_check_passes() -> None:
    client = FakeOdooClient(good_facts())

    result = checker.check_workflow(
        client,
        config(),
        exercise=True,
        allow_duplicate_db=True,
    )

    assert result.ok is True
    assert result.exercise == checker.ExerciseResult(rows=4, tasks=4, actions=5)
    assert client.exercise_calls == 1


def action_result(*, status: str = "In-Progress", state: str = "03_approved") -> dict:
    return {
        "ok": True,
        "task": {
            "id": 40,
            "state": state,
            "stageId": 20,
            "assigneeUserIds": [30],
        },
        "improvement": {
            "id": 50,
            "status": status,
            "linkedTaskId": 40,
            "dateStop": None,
        },
    }


def test_transition_expectation_rejects_any_changed_invariant() -> None:
    expected = checker.ActionExpectation(
        task_state="03_approved",
        improvement_status="In-Progress",
        stage_id=20,
        assignee_user_ids=(30,),
        date_stop_required=False,
    )
    baseline = action_result()

    assert checker._matches_action_expectation(baseline, expected) is True
    mutations = (
        {"task": {**baseline["task"], "state": "01_in_progress"}},
        {"task": {**baseline["task"], "stageId": 21}},
        {"task": {**baseline["task"], "assigneeUserIds": [31]}},
        {"improvement": {**baseline["improvement"], "status": "Requested"}},
        {"improvement": {**baseline["improvement"], "dateStop": "2026-09-02"}},
    )
    for mutation in mutations:
        candidate = {**baseline, **mutation}
        assert checker._matches_action_expectation(candidate, expected) is False


def test_terminal_transition_expectation_requires_completion_date() -> None:
    expected = checker.ActionExpectation(
        task_state="1_done",
        improvement_status="Completed",
        stage_id=20,
        assignee_user_ids=(30,),
        date_stop_required=True,
    )

    assert (
        checker._matches_action_expectation(
            action_result(status="Completed", state="1_done"), expected
        )
        is False
    )
    terminal = action_result(status="Completed", state="1_done")
    terminal["improvement"]["dateStop"] = "2026-09-02"
    assert checker._matches_action_expectation(terminal, expected) is True


def test_cleanup_ownership_requires_exact_random_task_name() -> None:
    expected_name = "Disposable duplicate review check random-token-0"

    assert (
        checker._validated_cleanup_task_id({"id": 40, "name": expected_name}, expected_name) == 40
    )
    with pytest.raises(checker.SafeRuntimeError, match="cleanup ownership"):
        checker._validated_cleanup_task_id(
            {"id": 40, "name": "Existing production task"}, expected_name
        )


def test_exercise_actor_must_be_the_exact_audited_dale_user() -> None:
    dale = (checker.UserRecord(30, checker.DALE_LOGIN, True),)

    assert checker._require_dale_exercise_actor(30, dale) == 30
    with pytest.raises(checker.SafeRuntimeError, match="authenticated Dale user"):
        checker._require_dale_exercise_actor(31, dale)


def bare_rpc_client() -> checker.XmlRpcReviewClient:
    return object.__new__(checker.XmlRpcReviewClient)


def test_action_readback_rejects_an_archived_task(monkeypatch) -> None:
    client = bare_rpc_client()
    responses = iter(
        [
            [
                {
                    "id": 50,
                    "x_studio_status": "In-Progress",
                    "x_studio_linked_task": [40, "Task"],
                    "x_studio_date_stop": False,
                    "active": True,
                }
            ],
            [
                {
                    "id": 40,
                    "state": "03_approved",
                    "stage_id": [20, "General"],
                    "user_ids": [30],
                    "active": False,
                }
            ],
        ]
    )
    monkeypatch.setattr(client, "_search_read", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(checker.SafeRuntimeError, match="task readback"):
        client._read_action_result(
            source="GPI Plant Manager",
            source_id="GPI-PM-FB-1",
            task_id=40,
        )


def test_action_readback_rejects_an_archived_reference(monkeypatch) -> None:
    client = bare_rpc_client()
    monkeypatch.setattr(
        client,
        "_search_read",
        lambda *_args, **_kwargs: [
            {
                "id": 50,
                "x_studio_status": "In-Progress",
                "x_studio_linked_task": [40, "Task"],
                "x_studio_date_stop": False,
                "active": False,
            }
        ],
    )

    with pytest.raises(checker.SafeRuntimeError, match="identity readback"):
        client._read_action_result(
            source="GPI Plant Manager",
            source_id="GPI-PM-FB-1",
            task_id=40,
        )


@pytest.mark.parametrize(
    "webhook_url",
    [
        "https://production.invalid/web/hook/33333333-3333-4333-8333-333333333333",
        "https://duplicate.invalid/web/hook/44444444-4444-4444-8444-444444444444",
        "https://duplicate.invalid/web/hook/33333333-3333-4333-8333-333333333333?leak=1",
    ],
)
def test_webhook_binding_requires_exact_duplicate_base_and_rule_uuid(webhook_url: str) -> None:
    assert (
        checker._webhook_url_matches(
            "https://duplicate.invalid",
            webhook_url,
            "33333333-3333-4333-8333-333333333333",
        )
        is False
    )


def test_mutation_fence_rejects_production_before_company_or_webhook_reads(monkeypatch) -> None:
    client = bare_rpc_client()
    client._config = checker._RpcConfig("https://duplicate.invalid", "db", "login", "key")
    client._uid = 30
    monkeypatch.setattr(client, "_execute", lambda *_args, **_kwargs: PRODUCTION_UUID)
    monkeypatch.setattr(
        client,
        "_search_read",
        lambda *_args, **_kwargs: pytest.fail("identity fence read past production UUID"),
    )

    with pytest.raises(checker.SafeRuntimeError, match="database identity"):
        client._verify_mutation_target(
            expected_database_uuid=PRODUCTION_UUID,
            production_database_uuid=PRODUCTION_UUID,
            expected_company="Expected Duplicate Company",
        )


def test_mutation_fence_requires_fresh_exact_company_and_webhook_binding(monkeypatch) -> None:
    client = bare_rpc_client()
    client._config = checker._RpcConfig("https://duplicate.invalid", "db", "login", "key")
    client._uid = 30
    monkeypatch.setattr(client, "_execute", lambda *_args, **_kwargs: TEST_UUID)
    responses = iter(
        [
            [{"id": 30, "company_id": [60, "Expected Duplicate Company"]}],
            [{"id": 60, "name": "Expected Duplicate Company"}],
            [
                {
                    "id": 70,
                    "active": True,
                    "trigger": checker.REVIEW_TRIGGER,
                    "webhook_uuid": "33333333-3333-4333-8333-333333333333",
                }
            ],
        ]
    )
    monkeypatch.setattr(client, "_search_read", lambda *_args, **_kwargs: next(responses))

    client._verify_mutation_target(
        expected_database_uuid=TEST_UUID,
        production_database_uuid=PRODUCTION_UUID,
        expected_company="Expected Duplicate Company",
        webhook_url=("https://duplicate.invalid/web/hook/33333333-3333-4333-8333-333333333333"),
    )


def test_mutation_fence_rejects_a_fresh_company_mismatch(monkeypatch) -> None:
    client = bare_rpc_client()
    client._config = checker._RpcConfig("https://duplicate.invalid", "db", "login", "key")
    client._uid = 30
    monkeypatch.setattr(client, "_execute", lambda *_args, **_kwargs: TEST_UUID)
    responses = iter(
        [
            [{"id": 30, "company_id": [60, "Expected Duplicate Company"]}],
            [{"id": 60, "name": "Different Company"}],
        ]
    )
    monkeypatch.setattr(client, "_search_read", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(checker.SafeRuntimeError, match="company identity"):
        client._verify_mutation_target(
            expected_database_uuid=TEST_UUID,
            production_database_uuid=PRODUCTION_UUID,
            expected_company="Expected Duplicate Company",
        )


def test_xmlrpc_mutation_loss_is_an_explicit_unknown_outcome() -> None:
    class Models:
        def execute_kw(self, *_args, **_kwargs):
            raise TimeoutError("must not be exposed")

    client = bare_rpc_client()
    client._config = checker._RpcConfig("https://duplicate.invalid", "db", "login", "key")
    client._uid = 30
    client._models = Models()

    with pytest.raises(checker.UnknownMutationOutcome, match="unknown outcome") as error:
        client._execute_mutation(checker.IMPROVEMENT_MODEL, "create", [{}])
    assert "must not be exposed" not in str(error.value)


def test_native_acknowledgement_is_followed_by_exact_authenticated_readback(
    monkeypatch,
) -> None:
    client = bare_rpc_client()
    events: list[str] = []

    monkeypatch.setattr(client, "_post_acknowledgement", lambda *_args: events.append("ack"))
    monkeypatch.setattr(
        client,
        "_read_action_result",
        lambda **_kwargs: events.append("readback") or action_result(),
    )

    result = client._call_action_and_readback(
        "https://example.invalid/redacted",
        {"action": "accept"},
        source="GPI Plant Manager",
        source_id="GPI-PM-CHECK-redacted",
        task_id=40,
        landed=lambda value: value["improvement"]["status"] == "In-Progress",
    )

    assert result == action_result()
    assert events == ["ack", "readback"]


def test_timeout_reads_back_before_returning_a_landed_transition(monkeypatch) -> None:
    client = bare_rpc_client()
    events: list[str] = []

    def timeout(*_args) -> None:
        events.append("unknown")
        raise checker.UnknownWebhookOutcome

    monkeypatch.setattr(client, "_post_acknowledgement", timeout)
    monkeypatch.setattr(
        client,
        "_read_action_result",
        lambda **_kwargs: events.append("readback") or action_result(),
    )

    result = client._call_action_and_readback(
        "https://example.invalid/redacted",
        {"action": "accept"},
        source="GPI Plant Manager",
        source_id="GPI-PM-CHECK-redacted",
        task_id=40,
        landed=lambda value: value["task"]["state"] == "03_approved",
    )

    assert result == action_result()
    assert events == ["unknown", "readback"]


def test_timeout_with_unlanded_transition_fails_without_a_retry(monkeypatch) -> None:
    client = bare_rpc_client()
    events: list[str] = []

    def timeout(*_args) -> None:
        events.append("unknown")
        raise checker.UnknownWebhookOutcome

    monkeypatch.setattr(client, "_post_acknowledgement", timeout)
    monkeypatch.setattr(
        client,
        "_read_action_result",
        lambda **_kwargs: events.append("readback") or action_result(status="Requested"),
    )

    with pytest.raises(checker.SafeRuntimeError, match="unknown outcome"):
        client._call_action_and_readback(
            "https://example.invalid/redacted",
            {"action": "accept"},
            source="GPI Plant Manager",
            source_id="GPI-PM-CHECK-redacted",
            task_id=40,
            landed=lambda value: value["improvement"]["status"] == "In-Progress",
        )

    assert events == ["unknown", "readback"]


def test_timeout_with_failed_readback_remains_an_explicit_unknown(monkeypatch) -> None:
    client = bare_rpc_client()

    def timeout(*_args) -> None:
        raise checker.UnknownWebhookOutcome

    def failed_readback(**_kwargs):
        raise checker.SafeRuntimeError("fixed readback failure")

    monkeypatch.setattr(client, "_post_acknowledgement", timeout)
    monkeypatch.setattr(client, "_read_action_result", failed_readback)

    with pytest.raises(checker.UnresolvedWebhookOutcome, match="unknown outcome"):
        client._call_action_and_readback(
            "https://example.invalid/redacted",
            {"action": "accept"},
            source="GPI Plant Manager",
            source_id="GPI-PM-CHECK-redacted",
            task_id=40,
            landed=lambda _value: False,
        )


@pytest.mark.parametrize(
    "body",
    [
        {"status": "OK"},
        {"status": "ok", "task": {}},
        {"ok": True},
    ],
)
def test_non_native_200_body_is_an_unknown_outcome(monkeypatch, body: dict) -> None:
    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return body

    monkeypatch.setattr(checker.requests, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(checker.UnknownWebhookOutcome):
        bare_rpc_client()._post_acknowledgement("https://example.invalid/redacted", {})


def test_exact_native_200_is_a_known_acknowledgement(monkeypatch) -> None:
    class Response:
        status_code = 200

        def json(self) -> dict:
            return {"status": "ok"}

    monkeypatch.setattr(checker.requests, "post", lambda *_args, **_kwargs: Response())

    bare_rpc_client()._post_acknowledgement("https://example.invalid/redacted", {})


def test_exact_native_500_is_a_known_rejection_on_the_positive_path(monkeypatch) -> None:
    class Response:
        status_code = 500

        def json(self) -> dict:
            return {"status": "error"}

    monkeypatch.setattr(checker.requests, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(checker.SafeRuntimeError, match="known rejection"):
        bare_rpc_client()._post_acknowledgement("https://example.invalid/redacted", {})


def test_non_native_success_status_is_an_unknown_outcome(monkeypatch) -> None:
    class Response:
        status_code = 201

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"status": "ok"}

    monkeypatch.setattr(checker.requests, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(checker.UnknownWebhookOutcome):
        bare_rpc_client()._post_acknowledgement("https://example.invalid/redacted", {})


@pytest.mark.parametrize("status_code", [502, 504])
def test_gateway_response_triggers_positive_identity_readback(
    monkeypatch, status_code: int
) -> None:
    client = bare_rpc_client()
    events: list[str] = []

    class Response:
        def raise_for_status(self) -> None:
            raise checker.requests.HTTPError

        def json(self) -> dict:
            pytest.fail("a gateway body is not a native Odoo response")

    response = Response()
    response.status_code = status_code
    monkeypatch.setattr(
        checker.requests,
        "post",
        lambda *_args, **_kwargs: events.append("post") or response,
    )
    monkeypatch.setattr(
        client,
        "_read_action_result",
        lambda **_kwargs: events.append("readback") or action_result(status="Requested"),
    )

    with pytest.raises(checker.UnresolvedWebhookOutcome, match="unknown outcome"):
        client._call_action_and_readback(
            "https://example.invalid/redacted",
            {"action": "accept"},
            source="GPI Plant Manager",
            source_id="GPI-PM-CHECK-redacted",
            task_id=40,
            landed=lambda value: value["improvement"]["status"] == "In-Progress",
        )

    assert events == ["post", "readback"]


def test_malformed_positive_body_triggers_identity_readback(monkeypatch) -> None:
    client = bare_rpc_client()
    events: list[str] = []

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            raise ValueError

    monkeypatch.setattr(
        checker.requests,
        "post",
        lambda *_args, **_kwargs: events.append("post") or Response(),
    )
    monkeypatch.setattr(
        client,
        "_read_action_result",
        lambda **_kwargs: events.append("readback") or action_result(status="Requested"),
    )

    with pytest.raises(checker.UnresolvedWebhookOutcome, match="unknown outcome"):
        client._call_action_and_readback(
            "https://example.invalid/redacted",
            {"action": "accept"},
            source="GPI Plant Manager",
            source_id="GPI-PM-CHECK-redacted",
            task_id=40,
            landed=lambda value: value["improvement"]["status"] == "In-Progress",
        )

    assert events == ["post", "readback"]


def test_negative_exercise_requires_exact_native_error_response(monkeypatch) -> None:
    class Response:
        status_code = 400

        def json(self) -> dict:
            return {"status": "error"}

    monkeypatch.setattr(checker.requests, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(checker.UnknownWebhookOutcome):
        bare_rpc_client()._post_rejection("https://example.invalid/redacted", {})


def test_exact_native_500_is_a_known_rejection(monkeypatch) -> None:
    class Response:
        status_code = 500

        def json(self) -> dict:
            return {"status": "error"}

    monkeypatch.setattr(checker.requests, "post", lambda *_args, **_kwargs: Response())

    bare_rpc_client()._post_rejection("https://example.invalid/redacted", {})


def test_exact_native_200_is_a_known_acknowledgement_on_the_negative_path(
    monkeypatch,
) -> None:
    class Response:
        status_code = 200

        def json(self) -> dict:
            return {"status": "ok"}

    monkeypatch.setattr(checker.requests, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(checker.SafeRuntimeError, match="known acknowledgement"):
        bare_rpc_client()._post_rejection("https://example.invalid/redacted", {})


@pytest.mark.parametrize("status_code", [500, 502, 504])
def test_non_native_negative_response_reads_back_then_defers_cleanup(
    monkeypatch, status_code: int
) -> None:
    client = bare_rpc_client()
    events: list[str] = []

    class Response:
        def json(self) -> dict:
            raise ValueError

    response = Response()
    response.status_code = status_code
    monkeypatch.setattr(
        checker.requests,
        "post",
        lambda *_args, **_kwargs: events.append("post") or response,
    )
    monkeypatch.setattr(
        client,
        "_read_action_result",
        lambda **_kwargs: events.append("readback") or action_result(status="Requested"),
    )
    before = action_result(status="Requested")

    with pytest.raises(checker.UnresolvedWebhookOutcome, match="unknown outcome"):
        client._call_rejection_and_readback(
            "https://example.invalid/redacted",
            {"unexpected": True},
            source="GPI Plant Manager",
            source_id="GPI-PM-CHECK-redacted",
            task_id=40,
            before=before,
        )

    assert events == ["post", "readback"]
