"""Audit the Odoo-owned 2s review workflow and optionally exercise a duplicate.

Normal operation is read-only.  The disposable exercise is reachable only when
both command-line flags are present and the live database UUID is the exact,
canonical duplicate UUID configured by the operator.  Production is an
independent hard stop even if the duplicate UUID setting is wrong.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
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
DIGITAL_AUTOMATION_NAME = "GPI 2s: Sync Digital Lifecycle"
CREATION_TRIGGER = "on_create_or_write"
REVIEW_TRIGGER = "on_webhook"
DIGITAL_TRIGGER = "on_create_or_write"

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
DIGITAL_DOMAIN: tuple[object, ...] = ()
DIGITAL_WATCHED_FIELDS = frozenset({"state"})
REVIEW_RECORD_GETTER = """model.with_context(active_test=False).search([
    ('x_studio_source', '=', payload.get('source')),
    ('x_studio_source_id', '=', payload.get('sourceId')),
    ('x_studio_linked_task', '=', int(payload.get('taskId') or 0)),
], limit=1)"""
CREATION_CODE_HASH = "c31f9d2c3cb8bb7147cf4f1babf828abd25edb7a23d12ed4ed8e04c1bd1d1208"
REVIEW_CODE_HASH = "1c5b32f2f2a8bc269ceca77621896a6ccb473fda5c63e40f7228d3406a60d37f"
DIGITAL_CODE_HASH = "6a27576c27d0aaa5a9675d8e92aade46918d2916e0f575856441a3d03b6a3bee"

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


class UnknownMutationOutcome(SafeRuntimeError):
    """An XML-RPC mutation response was lost and must be reconciled by identity."""


class UnresolvedWebhookOutcome(SafeRuntimeError):
    """A webhook outcome remained unknown after its immediate identity readback."""


class NativeWebhookOutcome(str, Enum):
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"


class SafeIssue(str, Enum):
    TYPE_SELECTION = "type selection is not exact V2"
    PROJECT_CARDINALITY = "review project must resolve exactly once"
    INITIAL_STAGE_CARDINALITY = "General stage must resolve exactly once"
    MEETING_STAGE_CARDINALITY = "L10 stage must resolve exactly once"
    DALE_CARDINALITY = "Dale user must resolve to one active login"
    CREATION_AUTOMATION = "creation automation must exist once and be enabled"
    REVIEW_AUTOMATION = "review automation must exist once and be enabled"
    DIGITAL_AUTOMATION = "digital lifecycle automation must exist once and be enabled"
    CREATION_AUTOMATION_CONTRACT = "creation automation contract does not match"
    REVIEW_AUTOMATION_CONTRACT = "review automation contract does not match"
    DIGITAL_AUTOMATION_CONTRACT = "digital lifecycle automation contract does not match"
    WEBHOOK_SECRET = "ODOO_REVIEW_ACTION_WEBHOOK_URL is not configured"
    WEBHOOK_BINDING = "review webhook is not bound to the inspected duplicate"
    EXPECTED_COMPANY = "ODOO_IMPROVEMENTS_EXPECTED_COMPANY is not configured"
    COMPANY_MISMATCH = "dedicated Odoo company identity does not match"
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
    record_getter: str
    log_webhook_calls: bool
    actions: tuple[ServerActionFacts, ...]


@dataclass(frozen=True)
class ServerActionFacts:
    state: str
    code_hash: str
    model: str = ""


@dataclass(frozen=True)
class WorkflowFacts:
    database_uuid: str
    company_name: str
    webhook_binding_matches: bool
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
    expected_company: str
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
            expected_company=os.environ.get("ODOO_IMPROVEMENTS_EXPECTED_COMPANY", "").strip(),
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


def _require_dale_exercise_actor(
    authenticated_user_id: int,
    dale_users: tuple[UserRecord, ...],
) -> int:
    if (
        len(dale_users) != 1
        or dale_users[0].active is not True
        or dale_users[0].login.casefold() != DALE_LOGIN.casefold()
        or dale_users[0].id != authenticated_user_id
    ):
        raise SafeRuntimeError("duplicate exercise requires the authenticated Dale user")
    return dale_users[0].id


class ReviewClient(Protocol):
    def inspect(self) -> WorkflowFacts: ...

    def exercise(
        self,
        webhook_url: str,
        *,
        expected_database_uuid: str,
        production_database_uuid: str,
        expected_company: str,
    ) -> ExerciseResult: ...


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
    if value is False or value is None or value == "":
        return ()
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


def _normalize_code(value: object) -> str:
    if type(value) is not str:
        return ""
    return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").split("\n")).strip()


def _code_hash(value: object) -> str:
    if type(value) is not str:
        return "invalid-code"
    return hashlib.sha256(_normalize_code(value).encode()).hexdigest()


def _webhook_url_matches(base_url: str, webhook_url: str, webhook_uuid: object) -> bool:
    if type(webhook_uuid) is not str or not _canonical_uuid(webhook_uuid):
        return False
    try:
        base = urlsplit(base_url)
        webhook = urlsplit(webhook_url)
        base_port = base.port
        webhook_port = webhook.port
    except ValueError:
        return False
    if (
        base.scheme.casefold() not in {"http", "https"}
        or webhook.scheme.casefold() != base.scheme.casefold()
        or webhook.hostname is None
        or base.hostname is None
        or webhook.hostname.casefold() != base.hostname.casefold()
        or webhook_port != base_port
        or webhook.username is not None
        or webhook.password is not None
        or webhook.query
        or webhook.fragment
    ):
        return False
    expected_path = f"{base.path.rstrip('/')}/web/hook/{webhook_uuid}"
    return webhook.path == expected_path


class XmlRpcReviewClient:
    """Narrow Odoo XML-RPC client used by this operator-only audit."""

    def __init__(self, config: _RpcConfig, webhook_url: str = "") -> None:
        self._config = config
        self._webhook_url = webhook_url
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
    def from_env(cls, webhook_url: str = "") -> XmlRpcReviewClient:
        return cls(_RpcConfig.from_env(), webhook_url)

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

    def _execute_mutation(self, model: str, method: str, args: list, kwargs: dict | None = None):
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
            raise UnknownMutationOutcome(
                "duplicate XML-RPC mutation has an unknown outcome"
            ) from None

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

        current_users = self._search_read(
            "res.users",
            [("id", "=", self._uid)],
            ["id", "company_id"],
            context={"active_test": False},
        )
        if len(current_users) != 1:
            raise SafeRuntimeError("dedicated Odoo company identity is unavailable")
        company_relation = current_users[0].get("company_id")
        company_id = _record_id(company_relation)
        companies = self._search_read(
            "res.company",
            [("id", "=", company_id)],
            ["id", "name"],
            context={"active_test": False},
        )
        if len(companies) != 1 or type(companies[0].get("name")) is not str:
            raise SafeRuntimeError("dedicated Odoo company identity is unavailable")
        company_name = companies[0]["name"]
        if _record_name(company_relation) != company_name:
            raise SafeRuntimeError("dedicated Odoo company identity is inconsistent")

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
                    [
                        CREATION_AUTOMATION_NAME,
                        REVIEW_AUTOMATION_NAME,
                        DIGITAL_AUTOMATION_NAME,
                    ],
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
                "record_getter",
                "log_webhook_calls",
                "action_server_ids",
                "webhook_uuid",
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
        action_ids: set[int] = set()
        for item in automation_raw:
            raw_action_ids = item.get("action_server_ids")
            if type(raw_action_ids) is not list:
                raise SafeRuntimeError("automation action metadata is invalid")
            action_ids.update(_positive_id(value) for value in raw_action_ids)
        action_raw = (
            self._search_read(
                "ir.actions.server",
                [("id", "in", sorted(action_ids))],
                ["id", "state", "code", "model_id"],
                context={"active_test": False},
            )
            if action_ids
            else []
        )
        action_model_ids = sorted({_record_id(item.get("model_id")) for item in action_raw})
        missing_model_ids = [value for value in action_model_ids if value not in models]
        if missing_model_ids:
            extra_models = self._search_read(
                "ir.model", [("id", "in", missing_model_ids)], ["id", "model"]
            )
            models.update(
                {_positive_id(item.get("id")): str(item.get("model", "")) for item in extra_models}
            )
        actions_by_id = {
            _positive_id(item.get("id")): ServerActionFacts(
                state=str(item.get("state", "")),
                code_hash=_code_hash(item.get("code")),
                model=models.get(_record_id(item.get("model_id")), ""),
            )
            for item in action_raw
        }
        if set(actions_by_id) != action_ids:
            raise SafeRuntimeError("automation action metadata is incomplete")
        automations: dict[str, list[AutomationFacts]] = {
            CREATION_AUTOMATION_NAME: [],
            REVIEW_AUTOMATION_NAME: [],
            DIGITAL_AUTOMATION_NAME: [],
        }
        webhook_binding_matches = False
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
                    record_getter=_normalize_code(item.get("record_getter")),
                    log_webhook_calls=item.get("log_webhook_calls") is True,
                    actions=tuple(
                        actions_by_id[_positive_id(value)] for value in item["action_server_ids"]
                    ),
                )
            )
            if name == REVIEW_AUTOMATION_NAME:
                webhook_binding_matches = _webhook_url_matches(
                    self._config.url,
                    self._webhook_url,
                    item.get("webhook_uuid"),
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
            company_name=company_name,
            webhook_binding_matches=webhook_binding_matches,
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

    def _verify_mutation_target(
        self,
        *,
        expected_database_uuid: str,
        production_database_uuid: str,
        expected_company: str,
        webhook_url: str | None = None,
    ) -> None:
        database_uuid = self._execute("ir.config_parameter", "get_param", ["database.uuid"])
        if (
            type(database_uuid) is not str
            or database_uuid == production_database_uuid
            or database_uuid != expected_database_uuid
        ):
            raise SafeRuntimeError("duplicate mutation database identity did not match")
        users = self._search_read(
            "res.users",
            [("id", "=", self._uid)],
            ["id", "company_id"],
            context={"active_test": False},
        )
        if len(users) != 1:
            raise SafeRuntimeError("duplicate mutation company identity did not match")
        company_relation = users[0].get("company_id")
        company_id = _record_id(company_relation)
        if _record_name(company_relation) != expected_company:
            raise SafeRuntimeError("duplicate mutation company identity did not match")
        companies = self._search_read(
            "res.company",
            [("id", "=", company_id)],
            ["id", "name"],
            context={"active_test": False},
        )
        if len(companies) != 1 or companies[0].get("name") != expected_company:
            raise SafeRuntimeError("duplicate mutation company identity did not match")
        if webhook_url is not None:
            rules = self._search_read(
                "base.automation",
                [("name", "=", REVIEW_AUTOMATION_NAME)],
                ["id", "active", "trigger", "webhook_uuid"],
                context={"active_test": False},
            )
            if (
                len(rules) != 1
                or rules[0].get("active") is not True
                or rules[0].get("trigger") != REVIEW_TRIGGER
                or not _webhook_url_matches(
                    self._config.url,
                    webhook_url,
                    rules[0].get("webhook_uuid"),
                )
            ):
                raise SafeRuntimeError("duplicate review webhook binding did not match")

    def _post_webhook(self, webhook_url: str, payload: dict) -> NativeWebhookOutcome:
        try:
            response = requests.post(webhook_url, json=payload, timeout=_RPC_TIMEOUT_SECONDS)
        except requests.RequestException:
            raise UnknownWebhookOutcome from None
        if response.status_code not in (200, 500):
            raise UnknownWebhookOutcome
        try:
            body = response.json()
        except ValueError:
            raise UnknownWebhookOutcome from None
        if response.status_code == 200 and type(body) is dict and body == {"status": "ok"}:
            return NativeWebhookOutcome.ACKNOWLEDGED
        if response.status_code == 500 and type(body) is dict and body == {"status": "error"}:
            return NativeWebhookOutcome.REJECTED
        raise UnknownWebhookOutcome

    def _post_acknowledgement(self, webhook_url: str, payload: dict) -> None:
        if self._post_webhook(webhook_url, payload) is NativeWebhookOutcome.REJECTED:
            raise SafeRuntimeError("native webhook returned a known rejection")

    def _post_rejection(self, webhook_url: str, payload: dict) -> None:
        if self._post_webhook(webhook_url, payload) is NativeWebhookOutcome.ACKNOWLEDGED:
            raise SafeRuntimeError("native webhook returned a known acknowledgement")

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
            [
                "id",
                "x_studio_status",
                "x_studio_linked_task",
                "x_studio_date_stop",
                "active",
            ],
            context={"active_test": False},
        )
        if len(references) != 1 or references[0].get("active") is not True:
            raise SafeRuntimeError("review action identity readback failed safely")
        reference = references[0]
        linked_task_id = _record_id(reference.get("x_studio_linked_task"))
        if linked_task_id != task_id:
            raise SafeRuntimeError("review action relationship readback failed safely")

        tasks = self._search_read(
            "project.task",
            [("id", "=", task_id)],
            ["id", "state", "stage_id", "user_ids", "active"],
            context={"active_test": False},
        )
        if len(tasks) != 1 or tasks[0].get("active") is not True:
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
        try:
            result = self._read_action_result(
                source=source,
                source_id=source_id,
                task_id=task_id,
            )
        except SafeRuntimeError:
            if unknown_outcome:
                raise UnresolvedWebhookOutcome(
                    "review webhook has an unknown outcome after readback"
                ) from None
            raise
        if not landed(result):
            if unknown_outcome:
                raise UnresolvedWebhookOutcome(
                    "review webhook has an unknown outcome after readback"
                )
            raise SafeRuntimeError("review action readback did not match the requested transition")
        return result

    def _call_rejection_and_readback(
        self,
        webhook_url: str,
        payload: dict,
        *,
        source: str,
        source_id: str,
        task_id: int,
        before: dict,
    ) -> None:
        unknown_outcome = False
        try:
            self._post_rejection(webhook_url, payload)
        except UnknownWebhookOutcome:
            unknown_outcome = True
        try:
            after = self._read_action_result(
                source=source,
                source_id=source_id,
                task_id=task_id,
            )
        except SafeRuntimeError:
            if unknown_outcome:
                raise UnresolvedWebhookOutcome(
                    "negative review webhook has an unknown outcome after readback"
                ) from None
            raise
        if unknown_outcome:
            raise UnresolvedWebhookOutcome(
                "negative review webhook has an unknown outcome after readback"
            )
        if after != before:
            raise SafeRuntimeError("duplicate rejected action changed state")

    def exercise(
        self,
        webhook_url: str,
        *,
        expected_database_uuid: str,
        production_database_uuid: str,
        expected_company: str,
    ) -> ExerciseResult:
        """Create, verify, and archive four disposable rows in an approved duplicate."""
        improvement_fields = self._execute(
            IMPROVEMENT_MODEL, "fields_get", [], {"attributes": ["type"]}
        )
        task_fields = self._execute("project.task", "fields_get", [], {"attributes": ["type"]})
        if "active" not in improvement_fields or "active" not in task_fields:
            raise SafeRuntimeError("duplicate exercise requires archival fields")

        def verify_mutation(*, webhook: bool = False) -> None:
            self._verify_mutation_target(
                expected_database_uuid=expected_database_uuid,
                production_database_uuid=production_database_uuid,
                expected_company=expected_company,
                webhook_url=webhook_url if webhook else None,
            )

        xmlrpc_outcome_unknown = False
        webhook_outcome_unknown = False

        def mutate(model: str, method: str, args: list):
            nonlocal xmlrpc_outcome_unknown
            try:
                return self._execute_mutation(model, method, args)
            except UnknownMutationOutcome:
                xmlrpc_outcome_unknown = True
                raise

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
        expected_name_by_source = {source_ids[index]: task_names[index] for index in range(4)}
        expected_name_by_row: dict[int, str] = {}
        attempted_source_ids: list[str] = []
        existing_test_tasks = self._search_read(
            "project.task",
            [("name", "in", task_names)],
            ["id"],
            context={"active_test": False},
        )
        if existing_test_tasks:
            raise SafeRuntimeError("duplicate exercise task identity already exists")
        existing_test_rows = self._search_read(
            IMPROVEMENT_MODEL,
            [
                ("x_studio_source", "=", "GPI Plant Manager"),
                ("x_studio_source_id", "in", source_ids),
            ],
            ["id"],
            context={"active_test": False},
        )
        if existing_test_rows:
            raise SafeRuntimeError("duplicate exercise source identity already exists")
        facts = self.inspect()
        general = facts.stages.get(INITIAL_STAGE, ())
        l10 = facts.stages.get(MEETING_STAGE, ())
        dale = facts.dale_users
        if len(facts.projects) != 1 or len(general) != 1 or len(l10) != 1 or len(dale) != 1:
            raise SafeRuntimeError("duplicate workflow identity readback did not match")
        dale_user_id = _require_dale_exercise_actor(self._uid, dale)
        try:
            for index, source_id in enumerate(source_ids):
                verify_mutation()
                attempted_source_ids.append(source_id)
                row_id = mutate(
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
                task = self._read_one(
                    "project.task",
                    task_id,
                    ["id", "name", "project_id", "stage_id", "user_ids", "state", "active"],
                )
                created_tasks.append(_validated_cleanup_task_id(task, task_names[index]))
                if (
                    task.get("active") is not True
                    or _record_id(task.get("project_id")) != facts.projects[0].id
                    or _record_id(task.get("stage_id")) != general[0].id
                    or task.get("user_ids") != [dale[0].id]
                    or task.get("state") != "01_in_progress"
                ):
                    raise SafeRuntimeError("duplicate created task contract did not match")
                verify_mutation()
                acknowledged = mutate(
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
                "actorUserId": dale_user_id,
            }

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
                nonlocal webhook_outcome_unknown
                payload = {
                    **common,
                    "taskId": created_tasks[index],
                    "sourceId": source_ids[index],
                    "action": action_name,
                    **extra,
                }
                verify_mutation(webhook=True)
                try:
                    return self._call_action_and_readback(
                        webhook_url,
                        payload,
                        source="GPI Plant Manager",
                        source_id=source_ids[index],
                        task_id=created_tasks[index],
                        landed=landed,
                    )
                except UnresolvedWebhookOutcome:
                    webhook_outcome_unknown = True
                    raise

            def rejected(index: int, **overrides: object) -> None:
                nonlocal webhook_outcome_unknown
                before = self._read_action_result(
                    source="GPI Plant Manager",
                    source_id=source_ids[index],
                    task_id=created_tasks[index],
                )
                payload = {
                    **common,
                    "taskId": created_tasks[index],
                    "sourceId": source_ids[index],
                    "action": "accept",
                    **overrides,
                }
                verify_mutation(webhook=True)
                try:
                    self._call_rejection_and_readback(
                        webhook_url,
                        payload,
                        source="GPI Plant Manager",
                        source_id=source_ids[index],
                        task_id=created_tasks[index],
                        before=before,
                    )
                except UnresolvedWebhookOutcome:
                    webhook_outcome_unknown = True
                    raise

            rejected(0, unexpected=True)
            rejected(0, actorUserId=alternate_user_id)
            rejected(0, action="complete", note="Must not complete before Accept")
            rejected(0, taskId=created_tasks[1])

            coding_projects = self._search_read(
                "project.project",
                [("name", "=", "Plant Manager"), ("active", "=", True)],
                ["id"],
                context={"active_test": False},
            )
            if len(coding_projects) != 1 or len(facts.projects) != 1:
                raise SafeRuntimeError("duplicate wrong-project fixture is unavailable")
            verify_mutation()
            corrupted = mutate(
                "project.task",
                "write",
                [[created_tasks[3]], {"project_id": _positive_id(coding_projects[0].get("id"))}],
            )
            if corrupted is not True:
                raise SafeRuntimeError("duplicate wrong-project fixture was not acknowledged")
            rejected(3)
            corrupted_task = self._read_one("project.task", created_tasks[3], ["project_id"])
            if _record_id(corrupted_task.get("project_id")) != _positive_id(
                coding_projects[0].get("id")
            ):
                raise SafeRuntimeError(
                    "duplicate rejected action changed the wrong-project fixture"
                )
            verify_mutation()
            restored = mutate(
                "project.task",
                "write",
                [
                    [created_tasks[3]],
                    {"project_id": facts.projects[0].id, "stage_id": general[0].id},
                ],
            )
            if restored is not True:
                raise SafeRuntimeError("duplicate wrong-project fixture restore failed")
            restored_result = self._read_action_result(
                source="GPI Plant Manager",
                source_id=source_ids[3],
                task_id=created_tasks[3],
            )
            restored_task = self._read_one("project.task", created_tasks[3], ["project_id"])
            if (
                not _matches_action_expectation(restored_result, requested)
                or _record_id(restored_task.get("project_id")) != facts.projects[0].id
            ):
                raise SafeRuntimeError("duplicate wrong-project fixture did not restore")

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
            rejected(0, action="complete", note="Terminal replay must fail")

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
            rejected(1, action="decline", note="Terminal replay must fail")

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
            cleanup_row_ids = set(created_rows)
            cleanup_task_ids = set(created_tasks)
            attempted_task_names = {
                expected_name_by_source[source_id] for source_id in attempted_source_ids
            }
            if attempted_source_ids:
                try:
                    cleanup_rows = self._search_read(
                        IMPROVEMENT_MODEL,
                        [
                            ("x_studio_source", "=", "GPI Plant Manager"),
                            ("x_studio_source_id", "in", attempted_source_ids),
                        ],
                        [
                            "id",
                            "x_name",
                            "x_studio_source_id",
                            "x_studio_linked_task",
                        ],
                        context={"active_test": False},
                    )
                    seen_cleanup_sources: set[str] = set()
                    for cleanup_row in cleanup_rows:
                        cleanup_source_id = cleanup_row.get("x_studio_source_id")
                        if (
                            type(cleanup_source_id) is not str
                            or cleanup_source_id in seen_cleanup_sources
                            or cleanup_row.get("x_name")
                            != expected_name_by_source.get(cleanup_source_id)
                        ):
                            raise SafeRuntimeError(
                                "duplicate exercise cleanup ownership did not match"
                            )
                        seen_cleanup_sources.add(cleanup_source_id)
                        cleanup_row_id = _positive_id(cleanup_row.get("id"))
                        cleanup_row_ids.add(cleanup_row_id)
                        expected_name_by_row[cleanup_row_id] = expected_name_by_source[
                            cleanup_source_id
                        ]
                        linked = cleanup_row.get("x_studio_linked_task")
                        if linked is not False and linked is not None:
                            linked_task_id = _record_id(linked)
                            expected_name = expected_name_by_row.get(cleanup_row_id)
                            if expected_name is None:
                                raise SafeRuntimeError(
                                    "duplicate exercise cleanup ownership did not match"
                                )
                            task = self._read_one("project.task", linked_task_id, ["id", "name"])
                            cleanup_task_ids.add(_validated_cleanup_task_id(task, expected_name))
                except SafeRuntimeError:
                    cleanup_failed = True
            if attempted_task_names:
                try:
                    discovered_tasks = self._search_read(
                        "project.task",
                        [("name", "in", sorted(attempted_task_names))],
                        ["id", "name"],
                        context={"active_test": False},
                    )
                    seen_task_names: set[str] = set()
                    for task in discovered_tasks:
                        task_name = task.get("name")
                        if (
                            type(task_name) is not str
                            or task_name not in attempted_task_names
                            or task_name in seen_task_names
                        ):
                            raise SafeRuntimeError(
                                "duplicate exercise cleanup ownership did not match"
                            )
                        seen_task_names.add(task_name)
                        cleanup_task_ids.add(_validated_cleanup_task_id(task, task_name))
                except SafeRuntimeError:
                    cleanup_failed = True
            if xmlrpc_outcome_unknown or webhook_outcome_unknown:
                raise SafeRuntimeError(
                    "duplicate exercise cleanup deferred after an unknown remote outcome"
                )
            if cleanup_task_ids:
                try:
                    verify_mutation()
                    archived = mutate(
                        "project.task",
                        "write",
                        [sorted(cleanup_task_ids), {"active": False}],
                    )
                    cleanup_failed = cleanup_failed or archived is not True
                except SafeRuntimeError:
                    cleanup_failed = True
            if xmlrpc_outcome_unknown:
                raise SafeRuntimeError(
                    "duplicate exercise cleanup deferred after an unknown remote outcome"
                )
            if cleanup_row_ids:
                try:
                    verify_mutation()
                    archived = mutate(
                        IMPROVEMENT_MODEL,
                        "write",
                        [sorted(cleanup_row_ids), {"active": False}],
                    )
                    cleanup_failed = cleanup_failed or archived is not True
                except SafeRuntimeError:
                    cleanup_failed = True
            if xmlrpc_outcome_unknown:
                raise SafeRuntimeError(
                    "duplicate exercise cleanup deferred after an unknown remote outcome"
                )
            if cleanup_failed:
                raise SafeRuntimeError("duplicate exercise cleanup failed safely")


def _automation_issue(
    facts: WorkflowFacts,
    *,
    name: str,
    expected_model: str,
    expected_trigger: str,
    expected_domain: tuple[object, ...],
    expected_fields: frozenset[str],
    expected_record_getter: str | None,
    expected_log_webhook_calls: bool | None,
    expected_code_hash: str,
    cardinality_issue: SafeIssue,
    contract_issue: SafeIssue,
) -> SafeIssue | None:
    records = facts.automations.get(name, ())
    if len(records) != 1 or records[0].active is not True:
        return cardinality_issue
    record = records[0]
    if (
        record.model != expected_model
        or record.trigger != expected_trigger
        or record.domain != expected_domain
        or record.watched_fields != expected_fields
        or (expected_record_getter is not None and record.record_getter != expected_record_getter)
        or (
            expected_log_webhook_calls is not None
            and record.log_webhook_calls is not expected_log_webhook_calls
        )
        or record.actions != (ServerActionFacts("code", expected_code_hash, expected_model),)
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
        expected_model=IMPROVEMENT_MODEL,
        expected_trigger=CREATION_TRIGGER,
        expected_domain=CREATION_DOMAIN,
        expected_fields=CREATION_WATCHED_FIELDS,
        expected_record_getter=None,
        expected_log_webhook_calls=None,
        expected_code_hash=CREATION_CODE_HASH,
        cardinality_issue=SafeIssue.CREATION_AUTOMATION,
        contract_issue=SafeIssue.CREATION_AUTOMATION_CONTRACT,
    )
    review_issue = _automation_issue(
        facts,
        name=REVIEW_AUTOMATION_NAME,
        expected_model=IMPROVEMENT_MODEL,
        expected_trigger=REVIEW_TRIGGER,
        expected_domain=REVIEW_DOMAIN,
        expected_fields=REVIEW_WATCHED_FIELDS,
        expected_record_getter=REVIEW_RECORD_GETTER,
        expected_log_webhook_calls=False,
        expected_code_hash=REVIEW_CODE_HASH,
        cardinality_issue=SafeIssue.REVIEW_AUTOMATION,
        contract_issue=SafeIssue.REVIEW_AUTOMATION_CONTRACT,
    )
    digital_issue = _automation_issue(
        facts,
        name=DIGITAL_AUTOMATION_NAME,
        expected_model="project.task",
        expected_trigger=DIGITAL_TRIGGER,
        expected_domain=DIGITAL_DOMAIN,
        expected_fields=DIGITAL_WATCHED_FIELDS,
        expected_record_getter=None,
        expected_log_webhook_calls=None,
        expected_code_hash=DIGITAL_CODE_HASH,
        cardinality_issue=SafeIssue.DIGITAL_AUTOMATION,
        contract_issue=SafeIssue.DIGITAL_AUTOMATION_CONTRACT,
    )
    if creation_issue:
        issues.append(creation_issue)
    if review_issue:
        issues.append(review_issue)
    if digital_issue:
        issues.append(digital_issue)
    if creation_issue is None and review_issue is None and digital_issue is None:
        safe_lines.append("OK automations=creation,review,digital-enabled")
        safe_lines.append("OK automation-actions=audited")

    if not config.webhook_configured:
        issues.append(SafeIssue.WEBHOOK_SECRET)
    elif facts.webhook_binding_matches is not True:
        issues.append(SafeIssue.WEBHOOK_BINDING)

    if not config.expected_company:
        issues.append(SafeIssue.EXPECTED_COMPANY)
    elif facts.company_name != config.expected_company:
        issues.append(SafeIssue.COMPANY_MISMATCH)
    else:
        safe_lines.append("OK company=matched")
    if config.webhook_configured and facts.webhook_binding_matches is True:
        safe_lines.append("OK webhook=duplicate-bound")

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
        exercise_result = client.exercise(
            config.webhook_url,
            expected_database_uuid=config.test_database_uuid,
            production_database_uuid=config.production_database_uuid,
            expected_company=config.expected_company,
        )
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
        client = XmlRpcReviewClient.from_env(config.webhook_url)
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
