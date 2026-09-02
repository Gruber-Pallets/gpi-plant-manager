from __future__ import annotations

from dataclasses import replace

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

    def exercise(self, webhook_url: str) -> checker.ExerciseResult:
        assert webhook_url == "https://duplicate.invalid/secret-path"
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
        )
    return checker.AutomationFacts(
        name=name,
        active=True,
        model=checker.IMPROVEMENT_MODEL,
        trigger=checker.REVIEW_TRIGGER,
        domain=checker.REVIEW_DOMAIN,
        watched_fields=checker.REVIEW_WATCHED_FIELDS,
    )


def good_facts() -> checker.WorkflowFacts:
    return checker.WorkflowFacts(
        database_uuid=TEST_UUID,
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
        },
        duplicate_source_identities=(),
    )


def config(**changes: object) -> checker.CheckConfig:
    base = checker.CheckConfig(
        webhook_configured=True,
        webhook_url="https://duplicate.invalid/secret-path",
        test_database_uuid=TEST_UUID,
        production_database_uuid=PRODUCTION_UUID,
        workflow_v2_enabled=False,
    )
    return replace(base, **changes)


def check(facts: checker.WorkflowFacts | None = None, **kwargs: object):
    client = FakeOdooClient(facts or good_facts())
    result = checker.check_workflow(client, config(), **kwargs)
    return client, result


def test_exact_success_reports_only_fixed_safe_facts() -> None:
    client, result = check()

    assert result.ok is True
    assert result.issues == ()
    assert result.safe_lines == (
        "OK contract=V2",
        "OK project=one",
        "OK stages=General,L10",
        "OK dale=one-active",
        "OK automations=creation,review-enabled",
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


def bare_rpc_client() -> checker.XmlRpcReviewClient:
    return object.__new__(checker.XmlRpcReviewClient)


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


@pytest.mark.parametrize(
    "body",
    [
        {"status": "OK"},
        {"status": "ok", "task": {}},
        {"ok": True},
    ],
)
def test_native_webhook_requires_exact_acknowledgement(monkeypatch, body: dict) -> None:
    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return body

    monkeypatch.setattr(checker.requests, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(checker.SafeRuntimeError, match="acknowledgement"):
        bare_rpc_client()._post_acknowledgement("https://example.invalid/redacted", {})


def test_native_webhook_rejects_non_200_success_status(monkeypatch) -> None:
    class Response:
        status_code = 201

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"status": "ok"}

    monkeypatch.setattr(checker.requests, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(checker.SafeRuntimeError, match="acknowledgement"):
        bare_rpc_client()._post_acknowledgement("https://example.invalid/redacted", {})
