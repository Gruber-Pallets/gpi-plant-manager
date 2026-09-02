"""Audit the Odoo-owned 2s review workflow and optionally exercise a duplicate.

Normal operation is read-only.  The disposable exercise is reachable only when
both command-line flags are present and the live database UUID is the exact,
canonical duplicate UUID configured by the operator.  Production is an
independent hard stop even if the duplicate UUID setting is wrong.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
import xmlrpc.client
from collections import Counter
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import requests
from dotenv import load_dotenv


IMPROVEMENT_MODEL = "x_2s_improvements"
REVIEW_PROJECT = "GPI OS Manager - TASKS"
INITIAL_STAGE = "General"
MEETING_STAGE = "L10"
DALE_LOGIN = "dale@gruberpallets.com"
CREATION_AUTOMATION_NAME = "GPI 2s: Create and Link Task"
REVIEW_AUTOMATION_NAME = "GPI 2s: Review Action Webhook"
CREATION_TRIGGER = "on_create_or_write"
REVIEW_TRIGGER = "on_webhook"

EXPECTED_TYPE_VALUES = (
    "Digital",
    "Digital - New Feature",
    "Physical - Issue",
    "Physical - Suggestion",
    "2s Improvement",
)
REVIEW_TYPES = (
    "Physical - Issue",
    "Physical - Suggestion",
    "2s Improvement",
)
SUPPORTED_SOURCES = ("GPI Plant Manager", "GPI Sales Manager")
CREATION_DOMAIN = (
    ("x_studio_source", "in", SUPPORTED_SOURCES),
    ("x_studio_status", "=", "Requested"),
    ("x_studio_linked_task", "=", False),
    ("x_studio_linked_wo", "=", False),
)
CREATION_WATCHED_FIELDS = frozenset(
    {
        "x_studio_source",
        "x_studio_source_id",
        "x_studio_type",
        "x_studio_status",
        "x_studio_linked_task",
        "x_studio_linked_wo",
    }
)
REVIEW_DOMAIN: tuple[object, ...] = ()
REVIEW_WATCHED_FIELDS: frozenset[str] = frozenset()

_MAX_ID = 9_223_372_036_854_775_807
_MAX_NOTE = 2_000
_RPC_TIMEOUT_SECONDS = 15
_ENV_NAMES = {
    "url": "ODOO_IMPROVEMENTS_URL",
    "database": "ODOO_IMPROVEMENTS_DB",
    "login": "ODOO_IMPROVEMENTS_LOGIN",
    "api_key": "ODOO_IMPROVEMENTS_API_KEY",
}


class SafeRuntimeError(RuntimeError):
    """An external failure whose message is fixed and safe to display."""


class UnknownWebhookOutcome(RuntimeError):
    """The native webhook outcome is unknown and requires identity readback."""


class SafeIssue(str, Enum):
    TYPE_SELECTION = "type selection is not exact V2"
    PROJECT_CARDINALITY = "review project must resolve exactly once"
    INITIAL_STAGE_CARDINALITY = "General stage must resolve exactly once"
    MEETING_STAGE_CARDINALITY = "L10 stage must resolve exactly once"
    DALE_CARDINALITY = "Dale user must resolve to one active login"
    CREATION_AUTOMATION = "creation automation must exist once and be enabled"
    REVIEW_AUTOMATION = "review automation must exist once and be enabled"
    CREATION_AUTOMATION_CONTRACT = "creation automation contract does not match"
    REVIEW_AUTOMATION_CONTRACT = "review automation contract does not match"
    WEBHOOK_SECRET = "ODOO_REVIEW_ACTION_WEBHOOK_URL is not configured"
    DUPLICATE_SOURCE_IDENTITY = "duplicate source identity exists"
    EXERCISE_FLAGS = "exercise requires both explicit duplicate-database flags"
    TEST_UUID = "ODOO_REVIEW_TEST_DB_UUID must be one canonical UUID"
    PRODUCTION_UUID = (
        "ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID must be one canonical UUID for exercise"
    )
    TEST_UUID_MISMATCH = "live database is not the approved duplicate UUID"
    PRODUCTION_EXERCISE = "exercise is forbidden against the production UUID"


@dataclass(frozen=True)
class NamedRecord:
    id: int
    name: str
    active: bool = True


@dataclass(frozen=True)
class UserRecord:
    id: int
    login: str
    active: bool


@dataclass(frozen=True)
class AutomationFacts:
    name: str
    active: bool
    model: str
    trigger: str
    domain: tuple[object, ...]
    watched_fields: frozenset[str]


@dataclass(frozen=True)
class WorkflowFacts:
    database_uuid: str
    type_values: tuple[str, ...]
    projects: tuple[NamedRecord, ...]
    stages: Mapping[str, tuple[NamedRecord, ...]]
    dale_users: tuple[UserRecord, ...]
    automations: Mapping[str, tuple[AutomationFacts, ...]]
    duplicate_source_identities: tuple[tuple[str, str], ...]


@dataclass(frozen=True, repr=False)
class CheckConfig:
    webhook_configured: bool
    webhook_url: str
    test_database_uuid: str
    production_database_uuid: str
    workflow_v2_enabled: bool

    def __repr__(self) -> str:
        return "CheckConfig(<redacted>)"

    @classmethod
    def from_env(cls) -> CheckConfig:
        webhook_url = os.environ.get("ODOO_REVIEW_ACTION_WEBHOOK_URL", "").strip()
        gate = os.environ.get("ODOO_FEEDBACK_WORKFLOW_V2_ENABLED", "false").strip().lower()
        if gate not in {"true", "false"}:
            raise SafeRuntimeError("ODOO_FEEDBACK_WORKFLOW_V2_ENABLED must be true or false")
        return cls(
            webhook_configured=bool(webhook_url),
            webhook_url=webhook_url,
            test_database_uuid=os.environ.get("ODOO_REVIEW_TEST_DB_UUID", "").strip(),
            production_database_uuid=os.environ.get(
                "ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID", ""
            ).strip(),
            workflow_v2_enabled=gate == "true",
        )


@dataclass(frozen=True)
class ExerciseResult:
    rows: int
    tasks: int
    actions: int


@dataclass(frozen=True)
class ActionExpectation:
    task_state: str
    improvement_status: str
    stage_id: int
    assignee_user_ids: tuple[int, ...]
    date_stop_required: bool


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    issues: tuple[SafeIssue, ...]
    safe_lines: tuple[str, ...]
    exercise: ExerciseResult | None = None


def _matches_action_expectation(result: dict, expected: ActionExpectation) -> bool:
    task = result.get("task")
    improvement = result.get("improvement")
    if type(task) is not dict or type(improvement) is not dict:
        return False
    date_stop = improvement.get("dateStop")
    return (
        task.get("state") == expected.task_state
        and improvement.get("status") == expected.improvement_status
        and task.get("stageId") == expected.stage_id
        and task.get("assigneeUserIds") == list(expected.assignee_user_ids)
        and ((bool(date_stop)) if expected.date_stop_required else date_stop is None)
    )


def _validated_cleanup_task_id(task: dict, expected_name: str) -> int:
    if task.get("name") != expected_name:
        raise SafeRuntimeError("duplicate exercise cleanup ownership did not match")
    return _positive_id(task.get("id"))


class ReviewClient(Protocol):
    def inspect(self) -> WorkflowFacts: ...

    def exercise(self, webhook_url: str) -> ExerciseResult: ...


@dataclass(frozen=True, repr=False)
class _RpcConfig:
    url: str
    database: str
    login: str
    api_key: str

    def __repr__(self) -> str:
        return "_RpcConfig(<redacted>)"

    @classmethod
    def from_env(cls) -> _RpcConfig:
        values = {key: os.environ.get(name, "").strip() for key, name in _ENV_NAMES.items()}
        missing = [name for key, name in _ENV_NAMES.items() if not values[key]]
        if missing:
            raise SafeRuntimeError("missing dedicated Odoo settings: " + ", ".join(missing))
        try:
            parsed = urlsplit(values["url"])
        except ValueError:
            parsed = None
        if (
            parsed is None
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise SafeRuntimeError("ODOO_IMPROVEMENTS_URL is not a plain HTTP(S) base URL")
        values["url"] = parsed.geturl().rstrip("/")
        return cls(**values)


class _TimeoutTransport(xmlrpc.client.Transport):
    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = _RPC_TIMEOUT_SECONDS
        return connection


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = _RPC_TIMEOUT_SECONDS
        return connection


def _positive_id(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_ID:
        raise SafeRuntimeError("Odoo returned an invalid record identifier")
    return value


def _record_id(value: object) -> int:
    if type(value) is list and len(value) == 2:
        return _positive_id(value[0])
    raise SafeRuntimeError("Odoo returned an invalid relation")


def _record_name(value: object) -> str:
    if type(value) is list and len(value) == 2 and type(value[1]) is str:
        return value[1]
    raise SafeRuntimeError("Odoo returned an invalid relation")


def _normalize_domain(value: object) -> tuple[object, ...]:
    if type(value) is str:
        try:
            value = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return ("invalid-domain",)

    def freeze(item: object) -> object:
        if type(item) is list:
            return tuple(freeze(child) for child in item)
        if type(item) is tuple:
            return tuple(freeze(child) for child in item)
        return item

    frozen = freeze(value)
    return frozen if type(frozen) is tuple else ("invalid-domain",)


class XmlRpcReviewClient:
    """Narrow Odoo XML-RPC client used by this operator-only audit."""

    def __init__(self, config: _RpcConfig) -> None:
        self._config = config
        transport = (
            _TimeoutSafeTransport() if config.url.startswith("https://") else _TimeoutTransport()
        )
        try:
            common = xmlrpc.client.ServerProxy(
                f"{config.url}/xmlrpc/2/common", transport=transport, allow_none=True
            )
            uid = common.authenticate(
                config.database,
                config.login,
                config.api_key,
                {},
            )
        except Exception:
            raise SafeRuntimeError("dedicated Odoo authentication failed safely") from None
        self._uid = _positive_id(uid)
        object_transport = (
            _TimeoutSafeTransport() if config.url.startswith("https://") else _TimeoutTransport()
        )
        self._models = xmlrpc.client.ServerProxy(
            f"{config.url}/xmlrpc/2/object",
            transport=object_transport,
            allow_none=True,
        )

    @classmethod
    def from_env(cls) -> XmlRpcReviewClient:
        return cls(_RpcConfig.from_env())

    def _execute(self, model: str, method: str, args: list, kwargs: dict | None = None):
        try:
            return self._models.execute_kw(
                self._config.database,
                self._uid,
                self._config.api_key,
                model,
                method,
                args,
                kwargs or {},
            )
        except Exception:
            raise SafeRuntimeError("Odoo workflow inspection failed safely") from None

    def _search_read(
        self,
        model: str,
        domain: list,
        fields: list[str],
        *,
        context: dict | None = None,
    ) -> list[dict]:
        result = self._execute(
            model,
            "search_read",
            [domain],
            {"fields": fields, "context": context or {}},
        )
        if type(result) is not list or any(type(item) is not dict for item in result):
            raise SafeRuntimeError("Odoo workflow inspection returned invalid records")
        return result

    def inspect(self) -> WorkflowFacts:
        database_uuid = self._execute("ir.config_parameter", "get_param", ["database.uuid"])
        if type(database_uuid) is not str:
            raise SafeRuntimeError("Odoo database UUID is unavailable")

        field_meta = self._execute(
            IMPROVEMENT_MODEL,
            "fields_get",
            [],
            {"attributes": ["type", "selection"]},
        )
        try:
            selection = field_meta["x_studio_type"]["selection"]
            type_values = tuple(item[0] for item in selection)
        except (KeyError, TypeError, ValueError):
            raise SafeRuntimeError("x_studio_type metadata is invalid") from None
        if any(type(value) is not str for value in type_values):
            raise SafeRuntimeError("x_studio_type metadata is invalid")

        projects_raw = self._search_read(
            "project.project",
            [("name", "=", REVIEW_PROJECT)],
            ["id", "name", "active"],
            context={"active_test": False},
        )
        projects = tuple(
            NamedRecord(
                id=_positive_id(item.get("id")),
                name=str(item.get("name", "")),
                active=item.get("active") is True,
            )
            for item in projects_raw
        )
        project_ids = [item.id for item in projects]
        stages: dict[str, tuple[NamedRecord, ...]] = {}
        for stage_name in (INITIAL_STAGE, MEETING_STAGE):
            if len(project_ids) == 1:
                stage_raw = self._search_read(
                    "project.task.type",
                    [("name", "=", stage_name), ("project_ids", "in", project_ids)],
                    ["id", "name", "active"],
                    context={"active_test": False},
                )
            else:
                stage_raw = []
            stages[stage_name] = tuple(
                NamedRecord(
                    id=_positive_id(item.get("id")),
                    name=str(item.get("name", "")),
                    active=item.get("active") is True,
                )
                for item in stage_raw
            )

        user_raw = self._search_read(
            "res.users",
            [("login", "=ilike", DALE_LOGIN)],
            ["id", "login", "active"],
            context={"active_test": False},
        )
        dale_users = tuple(
            UserRecord(
                id=_positive_id(item.get("id")),
                login=str(item.get("login", "")),
                active=item.get("active") is True,
            )
            for item in user_raw
        )

        automation_raw = self._search_read(
            "base.automation",
            [
                (
                    "name",
                    "in",
                    [CREATION_AUTOMATION_NAME, REVIEW_AUTOMATION_NAME],
                )
            ],
            [
                "id",
                "name",
                "active",
                "model_id",
                "trigger",
                "filter_domain",
                "trigger_field_ids",
            ],
            context={"active_test": False},
        )
        model_ids = sorted({_record_id(item.get("model_id")) for item in automation_raw})
        model_raw = (
            self._search_read("ir.model", [("id", "in", model_ids)], ["id", "model"])
            if model_ids
            else []
        )
        models = {_positive_id(item.get("id")): str(item.get("model", "")) for item in model_raw}
        field_ids: set[int] = set()
        for item in automation_raw:
            raw_ids = item.get("trigger_field_ids")
            if type(raw_ids) is not list:
                raise SafeRuntimeError("automation watched-field metadata is invalid")
            field_ids.update(_positive_id(value) for value in raw_ids)
        watched_raw = (
            self._search_read("ir.model.fields", [("id", "in", sorted(field_ids))], ["id", "name"])
            if field_ids
            else []
        )
        watched = {_positive_id(item.get("id")): str(item.get("name", "")) for item in watched_raw}
        automations: dict[str, list[AutomationFacts]] = {
            CREATION_AUTOMATION_NAME: [],
            REVIEW_AUTOMATION_NAME: [],
        }
        for item in automation_raw:
            name = str(item.get("name", ""))
            if name not in automations:
                continue
            raw_ids = item["trigger_field_ids"]
            automations[name].append(
                AutomationFacts(
                    name=name,
                    active=item.get("active") is True,
                    model=models.get(_record_id(item.get("model_id")), ""),
                    trigger=str(item.get("trigger", "")),
                    domain=_normalize_domain(item.get("filter_domain", "")),
                    watched_fields=frozenset(watched.get(value, "") for value in raw_ids),
                )
            )

        identity_raw = self._search_read(
            IMPROVEMENT_MODEL,
            [("x_studio_source", "in", list(SUPPORTED_SOURCES))],
            ["x_studio_source", "x_studio_source_id"],
            context={"active_test": False},
        )
        identities = [
            (str(item.get("x_studio_source", "")), str(item.get("x_studio_source_id", "")))
            for item in identity_raw
        ]
        counts = Counter(identities)
        duplicate_source_identities = tuple(
            identity for identity, count in counts.items() if count > 1
        )

        return WorkflowFacts(
            database_uuid=database_uuid,
            type_values=type_values,
            projects=projects,
            stages=stages,
            dale_users=dale_users,
            automations={name: tuple(items) for name, items in automations.items()},
            duplicate_source_identities=duplicate_source_identities,
        )

    def _read_one(self, model: str, record_id: int, fields: list[str]) -> dict:
        records = self._execute(model, "read", [[record_id]], {"fields": fields})
        if type(records) is not list or len(records) != 1 or type(records[0]) is not dict:
            raise SafeRuntimeError("duplicate exercise readback failed safely")
        return records[0]

    def _post_acknowledgement(self, webhook_url: str, payload: dict) -> None:
        try:
            response = requests.post(webhook_url, json=payload, timeout=_RPC_TIMEOUT_SECONDS)
        except (requests.Timeout, requests.ConnectionError):
            raise UnknownWebhookOutcome from None
        except requests.RequestException:
            raise SafeRuntimeError("duplicate review webhook failed safely") from None
        try:
            response.raise_for_status()
        except requests.RequestException:
            raise SafeRuntimeError("duplicate review webhook failed safely") from None
        if response.status_code != 200:
            raise SafeRuntimeError("native webhook acknowledgement is invalid")
        try:
            acknowledgement = response.json()
        except ValueError:
            raise SafeRuntimeError("native webhook acknowledgement is invalid") from None
        if type(acknowledgement) is not dict or acknowledgement != {"status": "ok"}:
            raise SafeRuntimeError("native webhook acknowledgement is invalid")

    def _read_action_result(
        self,
        *,
        source: str,
        source_id: str,
        task_id: int,
    ) -> dict:
        references = self._search_read(
            IMPROVEMENT_MODEL,
            [
                ("x_studio_source", "=", source),
                ("x_studio_source_id", "=", source_id),
            ],
            ["id", "x_studio_status", "x_studio_linked_task", "x_studio_date_stop"],
            context={"active_test": False},
        )
        if len(references) != 1:
            raise SafeRuntimeError("review action identity readback failed safely")
        reference = references[0]
        linked_task_id = _record_id(reference.get("x_studio_linked_task"))
        if linked_task_id != task_id:
            raise SafeRuntimeError("review action relationship readback failed safely")

        tasks = self._search_read(
            "project.task",
            [("id", "=", task_id)],
            ["id", "state", "stage_id", "user_ids"],
            context={"active_test": False},
        )
        if len(tasks) != 1:
            raise SafeRuntimeError("review action task readback failed safely")
        task = tasks[0]
        task_result = {
            "id": _positive_id(task.get("id")),
            "state": task.get("state"),
            "stageId": _record_id(task.get("stage_id")),
            "assigneeUserIds": task.get("user_ids"),
        }
        improvement_result = {
            "id": _positive_id(reference.get("id")),
            "status": reference.get("x_studio_status"),
            "linkedTaskId": linked_task_id,
            "dateStop": reference.get("x_studio_date_stop") or None,
        }
        result = {"ok": True, "task": task_result, "improvement": improvement_result}
        assignees = task_result["assigneeUserIds"]
        if type(assignees) is not list:
            raise SafeRuntimeError("review action readback returned invalid assignees")
        for assignee in assignees:
            _positive_id(assignee)
        if type(task_result["state"]) is not str or type(improvement_result["status"]) is not str:
            raise SafeRuntimeError("review action readback returned invalid state")
        if (
            improvement_result["dateStop"] is not None
            and type(improvement_result["dateStop"]) is not str
        ):
            raise SafeRuntimeError("review action readback returned invalid completion date")
        return result

    def _call_action_and_readback(
        self,
        webhook_url: str,
        payload: dict,
        *,
        source: str,
        source_id: str,
        task_id: int,
        landed: Callable[[dict], bool],
    ) -> dict:
        unknown_outcome = False
        try:
            self._post_acknowledgement(webhook_url, payload)
        except UnknownWebhookOutcome:
            unknown_outcome = True
        result = self._read_action_result(
            source=source,
            source_id=source_id,
            task_id=task_id,
        )
        if not landed(result):
            if unknown_outcome:
                raise SafeRuntimeError("review webhook has an unknown outcome after readback")
            raise SafeRuntimeError("review action readback did not match the requested transition")
        return result

    def exercise(self, webhook_url: str) -> ExerciseResult:
        """Create, verify, and archive four disposable rows in an approved duplicate."""
        improvement_fields = self._execute(
            IMPROVEMENT_MODEL, "fields_get", [], {"attributes": ["type"]}
        )
        task_fields = self._execute("project.task", "fields_get", [], {"attributes": ["type"]})
        if "active" not in improvement_fields or "active" not in task_fields:
            raise SafeRuntimeError("duplicate exercise requires archival fields")
        employee_raw = self._search_read(
            "hr.employee",
            [("user_id", "=", self._uid), ("active", "=", True)],
            ["id"],
            context={"active_test": False},
        )
        if len(employee_raw) != 1:
            raise SafeRuntimeError("duplicate exercise actor must have one active employee")
        employee_id = _positive_id(employee_raw[0].get("id"))
        alternate_raw = self._execute(
            "hr.employee",
            "search_read",
            [
                [
                    ("active", "=", True),
                    ("user_id", "!=", False),
                    ("user_id", "!=", self._uid),
                ]
            ],
            {
                "fields": ["id", "user_id"],
                "limit": 1,
                "order": "id",
                "context": {"active_test": False},
            },
        )
        if type(alternate_raw) is not list or len(alternate_raw) != 1:
            raise SafeRuntimeError("duplicate exercise needs one alternate active assignee")
        alternate_user_id = _record_id(alternate_raw[0].get("user_id"))
        alternate_users = self._search_read(
            "res.users",
            [("id", "=", alternate_user_id), ("active", "=", True)],
            ["id"],
            context={"active_test": False},
        )
        if len(alternate_users) != 1:
            raise SafeRuntimeError("duplicate exercise alternate assignee is inactive")
        created_rows: list[int] = []
        created_tasks: list[int] = []
        token = uuid4().hex
        notes = f"<p>Disposable duplicate workflow check {token}</p>"
        source_number = (uuid4().int % (_MAX_ID - 4)) + 1
        source_ids = [f"GPI-PM-FB-{source_number + index}" for index in range(4)]
        task_names = [f"Disposable duplicate review check {token}-{index}" for index in range(4)]
        expected_name_by_row: dict[int, str] = {}
        existing_test_tasks = self._search_read(
            "project.task",
            [("name", "in", task_names)],
            ["id"],
            context={"active_test": False},
        )
        if existing_test_tasks:
            raise SafeRuntimeError("duplicate exercise task identity already exists")
        try:
            for index, source_id in enumerate(source_ids):
                row_id = self._execute(
                    IMPROVEMENT_MODEL,
                    "create",
                    [
                        {
                            "x_name": task_names[index],
                            "x_studio_source": "GPI Plant Manager",
                            "x_studio_source_id": source_id,
                            "x_studio_type": "2s Improvement",
                            "x_studio_status": "Requested",
                            "x_studio_submitted_by": employee_id,
                            "x_studio_date_start": date.today().isoformat(),
                            "x_studio_notes": notes,
                        }
                    ],
                )
                created_rows.append(_positive_id(row_id))
                expected_name_by_row[created_rows[-1]] = task_names[index]
                row = self._read_one(
                    IMPROVEMENT_MODEL,
                    created_rows[-1],
                    ["x_studio_linked_task", "x_studio_linked_wo", "x_studio_notes"],
                )
                task_id = _record_id(row.get("x_studio_linked_task"))
                linked_work_order = row.get("x_studio_linked_wo")
                if linked_work_order is not False and linked_work_order is not None:
                    raise SafeRuntimeError("duplicate review unexpectedly linked a work order")
                if row.get("x_studio_notes") != notes or task_id in created_tasks:
                    raise SafeRuntimeError("duplicate review creation readback did not match")
                task = self._read_one("project.task", task_id, ["id", "name"])
                created_tasks.append(_validated_cleanup_task_id(task, task_names[index]))
                acknowledged = self._execute(
                    IMPROVEMENT_MODEL,
                    "write",
                    [[created_rows[-1]], {"x_studio_status": "Requested"}],
                )
                if acknowledged is not True:
                    raise SafeRuntimeError("duplicate retry write was not acknowledged")
                retry = self._read_one(
                    IMPROVEMENT_MODEL,
                    created_rows[-1],
                    ["x_studio_linked_task"],
                )
                if _record_id(retry.get("x_studio_linked_task")) != task_id:
                    raise SafeRuntimeError("duplicate retry changed the linked task")

            common = {
                "source": "GPI Plant Manager",
                "actorUserId": self._uid,
            }

            facts = self.inspect()
            general = facts.stages.get(INITIAL_STAGE, ())
            l10 = facts.stages.get(MEETING_STAGE, ())
            dale = facts.dale_users
            if len(general) != 1 or len(l10) != 1 or len(dale) != 1:
                raise SafeRuntimeError("duplicate workflow identity readback did not match")

            requested = ActionExpectation(
                task_state="01_in_progress",
                improvement_status="Requested",
                stage_id=general[0].id,
                assignee_user_ids=(dale[0].id,),
                date_stop_required=False,
            )
            for index in range(4):
                initial = self._read_action_result(
                    source="GPI Plant Manager",
                    source_id=source_ids[index],
                    task_id=created_tasks[index],
                )
                if not _matches_action_expectation(initial, requested):
                    raise SafeRuntimeError("duplicate initial task state did not match")

            def action(
                index: int,
                action_name: str,
                landed: Callable[[dict], bool],
                **extra: object,
            ) -> dict:
                payload = {
                    **common,
                    "taskId": created_tasks[index],
                    "sourceId": source_ids[index],
                    "action": action_name,
                    **extra,
                }
                return self._call_action_and_readback(
                    webhook_url,
                    payload,
                    source="GPI Plant Manager",
                    source_id=source_ids[index],
                    task_id=created_tasks[index],
                    landed=landed,
                )

            accepted = action(
                0,
                "accept",
                lambda value: _matches_action_expectation(
                    value,
                    ActionExpectation(
                        "03_approved", "In-Progress", general[0].id, (dale[0].id,), False
                    ),
                ),
            )
            if not _matches_action_expectation(
                accepted,
                ActionExpectation(
                    "03_approved", "In-Progress", general[0].id, (dale[0].id,), False
                ),
            ):
                raise SafeRuntimeError("duplicate Accept transition did not match")
            completed = action(
                0,
                "complete",
                lambda value: _matches_action_expectation(
                    value,
                    ActionExpectation("1_done", "Completed", general[0].id, (dale[0].id,), True),
                ),
                note="Disposable completion result",
            )
            if not _matches_action_expectation(
                completed,
                ActionExpectation("1_done", "Completed", general[0].id, (dale[0].id,), True),
            ):
                raise SafeRuntimeError("duplicate Complete transition did not match")

            declined = action(
                1,
                "decline",
                lambda value: _matches_action_expectation(
                    value,
                    ActionExpectation("1_canceled", "Declined", general[0].id, (dale[0].id,), True),
                ),
                note="Disposable decline reason",
            )
            if not _matches_action_expectation(
                declined,
                ActionExpectation("1_canceled", "Declined", general[0].id, (dale[0].id,), True),
            ):
                raise SafeRuntimeError("duplicate Decline transition did not match")

            action(
                2,
                "accept",
                lambda value: _matches_action_expectation(
                    value,
                    ActionExpectation(
                        "03_approved", "In-Progress", general[0].id, (dale[0].id,), False
                    ),
                ),
            )
            assigned = action(
                2,
                "assign",
                lambda value: _matches_action_expectation(
                    value,
                    ActionExpectation(
                        "03_approved", "In-Progress", general[0].id, (alternate_user_id,), False
                    ),
                ),
                assigneeUserId=alternate_user_id,
            )
            if not _matches_action_expectation(
                assigned,
                ActionExpectation(
                    "03_approved", "In-Progress", general[0].id, (alternate_user_id,), False
                ),
            ):
                raise SafeRuntimeError("duplicate Assign transition did not match")

            action(
                3,
                "accept",
                lambda value: _matches_action_expectation(
                    value,
                    ActionExpectation(
                        "03_approved", "In-Progress", general[0].id, (dale[0].id,), False
                    ),
                ),
            )
            moved = action(
                3,
                "move_l10",
                lambda value: _matches_action_expectation(
                    value,
                    ActionExpectation(
                        "03_approved", "In-Progress", l10[0].id, (dale[0].id,), False
                    ),
                ),
            )
            if not _matches_action_expectation(
                moved,
                ActionExpectation("03_approved", "In-Progress", l10[0].id, (dale[0].id,), False),
            ):
                raise SafeRuntimeError("duplicate L10 transition did not match")

            for row_id in created_rows:
                row = self._read_one(
                    IMPROVEMENT_MODEL,
                    row_id,
                    ["x_studio_notes", "x_studio_linked_wo"],
                )
                linked_work_order = row.get("x_studio_linked_wo")
                if row.get("x_studio_notes") != notes or (
                    linked_work_order is not False and linked_work_order is not None
                ):
                    raise SafeRuntimeError("duplicate exercise changed submission content")
            return ExerciseResult(rows=4, tasks=4, actions=5)
        finally:
            cleanup_failed = False
            cleanup_task_ids = set(created_tasks)
            if created_rows:
                try:
                    cleanup_rows = self._search_read(
                        IMPROVEMENT_MODEL,
                        [("id", "in", created_rows)],
                        ["id", "x_studio_linked_task"],
                        context={"active_test": False},
                    )
                    for cleanup_row in cleanup_rows:
                        linked = cleanup_row.get("x_studio_linked_task")
                        if linked is not False and linked is not None:
                            linked_task_id = _record_id(linked)
                            cleanup_row_id = _positive_id(cleanup_row.get("id"))
                            expected_name = expected_name_by_row.get(cleanup_row_id)
                            if expected_name is None:
                                raise SafeRuntimeError(
                                    "duplicate exercise cleanup ownership did not match"
                                )
                            task = self._read_one("project.task", linked_task_id, ["id", "name"])
                            cleanup_task_ids.add(_validated_cleanup_task_id(task, expected_name))
                except SafeRuntimeError:
                    cleanup_failed = True
            if cleanup_task_ids:
                try:
                    archived = self._execute(
                        "project.task",
                        "write",
                        [sorted(cleanup_task_ids), {"active": False}],
                    )
                    cleanup_failed = cleanup_failed or archived is not True
                except SafeRuntimeError:
                    cleanup_failed = True
            if created_rows:
                try:
                    archived = self._execute(
                        IMPROVEMENT_MODEL, "write", [created_rows, {"active": False}]
                    )
                    cleanup_failed = cleanup_failed or archived is not True
                except SafeRuntimeError:
                    cleanup_failed = True
            if cleanup_failed:
                raise SafeRuntimeError("duplicate exercise cleanup failed safely")


def _automation_issue(
    facts: WorkflowFacts,
    *,
    name: str,
    expected_trigger: str,
    expected_domain: tuple[object, ...],
    expected_fields: frozenset[str],
    cardinality_issue: SafeIssue,
    contract_issue: SafeIssue,
) -> SafeIssue | None:
    records = facts.automations.get(name, ())
    if len(records) != 1 or records[0].active is not True:
        return cardinality_issue
    record = records[0]
    if (
        record.model != IMPROVEMENT_MODEL
        or record.trigger != expected_trigger
        or record.domain != expected_domain
        or record.watched_fields != expected_fields
    ):
        return contract_issue
    return None


def _canonical_uuid(value: str) -> bool:
    if type(value) is not str or len(value) != 36:
        return False
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError):
        return False
    return str(parsed) == value


def check_workflow(
    client: ReviewClient,
    config: CheckConfig,
    *,
    exercise: bool = False,
    allow_duplicate_db: bool = False,
) -> CheckResult:
    """Check fixed workflow facts and enter mutation code only behind every fence."""
    if exercise is not allow_duplicate_db:
        issue = SafeIssue.EXERCISE_FLAGS
        return CheckResult(False, (issue,), (f"ERROR {issue.value}",))

    facts = client.inspect()
    if exercise:
        if not _canonical_uuid(config.production_database_uuid):
            issue = SafeIssue.PRODUCTION_UUID
            return CheckResult(False, (issue,), (f"ERROR {issue.value}",))
        if (
            facts.database_uuid == config.production_database_uuid
            and config.production_database_uuid
        ):
            issue = SafeIssue.PRODUCTION_EXERCISE
            return CheckResult(False, (issue,), (f"ERROR {issue.value}",))
        if not _canonical_uuid(config.test_database_uuid):
            issue = SafeIssue.TEST_UUID
            return CheckResult(False, (issue,), (f"ERROR {issue.value}",))
        if facts.database_uuid != config.test_database_uuid:
            issue = SafeIssue.TEST_UUID_MISMATCH
            return CheckResult(False, (issue,), (f"ERROR {issue.value}",))

    issues: list[SafeIssue] = []
    safe_lines: list[str] = []
    if len(facts.type_values) != len(EXPECTED_TYPE_VALUES) or frozenset(
        facts.type_values
    ) != frozenset(EXPECTED_TYPE_VALUES):
        issues.append(SafeIssue.TYPE_SELECTION)
    else:
        safe_lines.append("OK contract=V2")

    if len(facts.projects) != 1 or facts.projects[0].active is not True:
        issues.append(SafeIssue.PROJECT_CARDINALITY)
    else:
        safe_lines.append("OK project=one")

    general = facts.stages.get(INITIAL_STAGE, ())
    l10 = facts.stages.get(MEETING_STAGE, ())
    if len(general) != 1 or general[0].active is not True:
        issues.append(SafeIssue.INITIAL_STAGE_CARDINALITY)
    if len(l10) != 1 or l10[0].active is not True:
        issues.append(SafeIssue.MEETING_STAGE_CARDINALITY)
    if len(general) == len(l10) == 1 and general[0].active is True and l10[0].active is True:
        safe_lines.append("OK stages=General,L10")

    dale = facts.dale_users
    if (
        len(dale) != 1
        or dale[0].active is not True
        or dale[0].login.casefold() != DALE_LOGIN.casefold()
    ):
        issues.append(SafeIssue.DALE_CARDINALITY)
    else:
        safe_lines.append("OK dale=one-active")

    creation_issue = _automation_issue(
        facts,
        name=CREATION_AUTOMATION_NAME,
        expected_trigger=CREATION_TRIGGER,
        expected_domain=CREATION_DOMAIN,
        expected_fields=CREATION_WATCHED_FIELDS,
        cardinality_issue=SafeIssue.CREATION_AUTOMATION,
        contract_issue=SafeIssue.CREATION_AUTOMATION_CONTRACT,
    )
    review_issue = _automation_issue(
        facts,
        name=REVIEW_AUTOMATION_NAME,
        expected_trigger=REVIEW_TRIGGER,
        expected_domain=REVIEW_DOMAIN,
        expected_fields=REVIEW_WATCHED_FIELDS,
        cardinality_issue=SafeIssue.REVIEW_AUTOMATION,
        contract_issue=SafeIssue.REVIEW_AUTOMATION_CONTRACT,
    )
    if creation_issue:
        issues.append(creation_issue)
    if review_issue:
        issues.append(review_issue)
    if creation_issue is None and review_issue is None:
        safe_lines.append("OK automations=creation,review-enabled")

    if not config.webhook_configured:
        issues.append(SafeIssue.WEBHOOK_SECRET)

    if facts.duplicate_source_identities:
        issues.append(SafeIssue.DUPLICATE_SOURCE_IDENTITY)
    else:
        safe_lines.append("OK source-identities=unique")

    if issues:
        return CheckResult(
            ok=False,
            issues=tuple(issues),
            safe_lines=tuple(f"ERROR {issue.value}" for issue in issues),
        )
    if exercise:
        exercise_result = client.exercise(config.webhook_url)
        return CheckResult(True, (), tuple(safe_lines), exercise_result)
    return CheckResult(True, (), tuple(safe_lines))


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        self.exit(2, f"{self.prog}: invalid arguments\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="check-odoo-review-workflow",
        description="Read-only Odoo review audit with a guarded duplicate exercise.",
        allow_abbrev=False,
    )
    parser.add_argument("--exercise", action="store_true")
    parser.add_argument("--allow-duplicate-db", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()
    try:
        config = CheckConfig.from_env()
        client = XmlRpcReviewClient.from_env()
        result = check_workflow(
            client,
            config,
            exercise=args.exercise,
            allow_duplicate_db=args.allow_duplicate_db,
        )
    except SafeRuntimeError as error:
        print(f"ERROR {error}", file=sys.stderr)
        return 2
    stream = sys.stdout if result.ok else sys.stderr
    for line in result.safe_lines:
        print(line, file=stream)
    if result.exercise is not None:
        print(
            "OK exercise="
            f"rows:{result.exercise.rows},tasks:{result.exercise.tasks},actions:{result.exercise.actions}",
            file=stream,
        )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
