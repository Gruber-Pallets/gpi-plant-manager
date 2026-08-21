"""Dedicated, allowlisted Odoo client for the feedback improvements mirror.

This module intentionally owns its configuration, authentication, transports,
and RPC allowlist.  It must stay independent from the application's generic
``odoo_client`` facade so feedback rollout cannot widen another integration.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import xmlrpc.client
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlsplit


TARGET_MODEL = "x_2s_improvements"
SOURCE_VALUE = "GPI Plant Manager"

TARGET_FIELDS = frozenset(
    {
        "x_name",
        "x_studio_source_id",
        "x_studio_date_start",
        "x_studio_submitted_by",
        "x_studio_date_stop",
        "x_studio_completed_by",
        "x_studio_notes",
        "x_studio_type",
        "x_studio_image",
        "x_studio_after_image",
        "x_studio_status",
        "x_studio_source",
    }
)

ALLOWED = frozenset(
    {
        ("ir.config_parameter", "get_param"),
        ("res.users", "read"),
        ("res.company", "read"),
        (TARGET_MODEL, "fields_get"),
        (TARGET_MODEL, "search_read"),
        (TARGET_MODEL, "read"),
        (TARGET_MODEL, "create"),
        (TARGET_MODEL, "write"),
        ("hr.employee", "search_read"),
        ("project.task", "read"),
    }
)

_CONFIG_NAMES = {
    "url": "ODOO_IMPROVEMENTS_URL",
    "database": "ODOO_IMPROVEMENTS_DB",
    "login": "ODOO_IMPROVEMENTS_LOGIN",
    "api_key": "ODOO_IMPROVEMENTS_API_KEY",
    "expected_database_uuid": "ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID",
    "expected_company": "ODOO_IMPROVEMENTS_EXPECTED_COMPANY",
}
_SOURCE_ID_RE = re.compile(r"GPI-PM-FB-([1-9][0-9]*)", re.ASCII)
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+", re.ASCII)
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_DATETIME_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}",
    re.ASCII,
)
_XMLRPC_TIMEOUT_SECONDS = 15
_CONTRACT_ATTRIBUTES = ["type", "readonly", "selection"]
_READ_FIELDS = frozenset({"id"}) | TARGET_FIELDS
_EXPECTED_TYPES = {
    "x_name": "char",
    "x_studio_source_id": "char",
    "x_studio_submitted_by": "many2one",
    "x_studio_completed_by": "many2one",
    "x_studio_notes": "html",
    "x_studio_image": "binary",
    "x_studio_after_image": "binary",
    "x_studio_source": "selection",
    "x_studio_status": "selection",
    "x_studio_type": "selection",
}
_EXPECTED_STATUS_VALUES = frozenset({"Requested", "In-Progress", "Completed", "Declined"})
_EXPECTED_TYPE_VALUES = frozenset({"Digital", "Digital - New Feature", "Physical"})
_WRITABLE_TYPE_VALUES = frozenset({"Digital", "Digital - New Feature"})
_WRITABLE_STATUS_VALUES = _EXPECTED_STATUS_VALUES


class ImprovementsConfigError(RuntimeError):
    """Dedicated improvements configuration is absent or malformed."""


class ImprovementsAuthenticationError(RuntimeError):
    """Dedicated Odoo credentials were rejected."""


class GateClosed(RuntimeError):
    """A mutation was denied by a write gate or canary fence."""


class TargetIdentityError(RuntimeError):
    """The live Odoo database or current company is not the approved target."""


class ContractError(RuntimeError):
    """An RPC or target-field request is outside the permanent contract."""


class MalformedMutationResponse(RuntimeError):
    """Odoo did not return the exact mutation acknowledgement required."""


@dataclass(frozen=True, repr=False)
class ImprovementsConfig:
    url: str
    database: str
    login: str
    api_key: str
    expected_database_uuid: str
    expected_company: str

    def __repr__(self) -> str:
        return "ImprovementsConfig(<redacted>)"

    @classmethod
    def from_env(cls) -> ImprovementsConfig:
        values = {field: os.environ.get(env_name, "") for field, env_name in _CONFIG_NAMES.items()}
        missing = [
            _CONFIG_NAMES[field]
            for field, value in values.items()
            if not value or not value.strip()
        ]
        if missing:
            raise ImprovementsConfigError("missing dedicated Odoo settings: " + ", ".join(missing))

        url = values["url"].rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ImprovementsConfigError(
                "ODOO_IMPROVEMENTS_URL must be a plain HTTP or HTTPS base URL"
            )
        values["url"] = url
        return cls(**values)


@dataclass(frozen=True)
class ImprovementContract:
    start_type: str
    stop_type: str


class _TimeoutTransport(xmlrpc.client.Transport):
    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = _XMLRPC_TIMEOUT_SECONDS
        return connection


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = _XMLRPC_TIMEOUT_SECONDS
        return connection


def _server_proxy(url: str) -> xmlrpc.client.ServerProxy:
    transport = _TimeoutSafeTransport() if url.startswith("https://") else _TimeoutTransport()
    return xmlrpc.client.ServerProxy(url, transport=transport)


def _raise_safe_transport_error(error: Exception) -> None:
    """Preserve retry classification without echoing a URL or remote payload."""
    if isinstance(error, xmlrpc.client.Fault):
        raise xmlrpc.client.Fault(error.faultCode, "dedicated Odoo request failed") from None
    if isinstance(error, TimeoutError):
        raise TimeoutError("dedicated Odoo request timed out") from None
    if isinstance(error, ConnectionError):
        raise ConnectionError("dedicated Odoo connection failed") from None
    raise OSError("dedicated Odoo request failed") from None


class _XmlRpcExecutor:
    def __init__(self, config: ImprovementsConfig):
        self._config = config
        self._uid: int | None = None
        self._common = _server_proxy(f"{config.url}/xmlrpc/2/common")
        self._object = _server_proxy(f"{config.url}/xmlrpc/2/object")

    def __repr__(self) -> str:
        return "_XmlRpcExecutor(<redacted>)"

    def authenticate(self) -> int:
        if self._uid is not None:
            return self._uid
        try:
            uid = self._common.authenticate(
                self._config.database,
                self._config.login,
                self._config.api_key,
                {},
            )
        except Exception as error:
            _raise_safe_transport_error(error)
        if type(uid) is not int or uid <= 0:
            raise ImprovementsAuthenticationError("dedicated Odoo credentials were rejected")
        self._uid = uid
        return uid

    def __call__(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        uid = self.authenticate()
        try:
            return self._object.execute_kw(
                self._config.database,
                uid,
                self._config.api_key,
                model,
                method,
                list(args),
                kwargs,
            )
        except Exception as error:
            _raise_safe_transport_error(error)


def _build_default_executor(config: ImprovementsConfig) -> _XmlRpcExecutor:
    return _XmlRpcExecutor(config)


def _positive_integer(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ContractError(f"{label} must be a positive integer")
    return value


def _canonical_source_id(value: object) -> tuple[str, int]:
    if not isinstance(value, str):
        raise ContractError("source id must use the GPI-PM-FB format")
    match = _SOURCE_ID_RE.fullmatch(value)
    if match is None:
        raise ContractError("source id must use the GPI-PM-FB format")
    return value, int(match.group(1))


def _row_list(value: object, *, label: str, maximum: int) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) > maximum:
        raise ContractError(f"{label} response was malformed")
    if any(type(row) is not dict for row in value):
        raise ContractError(f"{label} response was malformed")
    return value


def _valid_odoo_time(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if _DATE_RE.fullmatch(value):
        format_string = "%Y-%m-%d"
    elif _DATETIME_RE.fullmatch(value):
        format_string = "%Y-%m-%d %H:%M:%S"
    else:
        return False
    try:
        datetime.strptime(value, format_string)
    except ValueError:
        return False
    return True


def _validate_field_value(field_name: str, value: object, feedback_id: int) -> None:
    if field_name == "x_name":
        if not isinstance(value, str) or not value.strip():
            raise ContractError("x_name must be a nonempty string")
    elif field_name == "x_studio_source_id":
        _source_id, source_feedback_id = _canonical_source_id(value)
        if source_feedback_id != feedback_id:
            raise ContractError("source id does not match feedback id")
    elif field_name in {"x_studio_date_start", "x_studio_date_stop"}:
        if not _valid_odoo_time(value):
            raise ContractError(f"{field_name} must be an Odoo date or datetime")
    elif field_name in {"x_studio_submitted_by", "x_studio_completed_by"}:
        _positive_integer(value, field_name)
    elif field_name == "x_studio_notes":
        if not isinstance(value, str):
            raise ContractError("x_studio_notes must be a string")
    elif field_name in {"x_studio_image", "x_studio_after_image"}:
        if not isinstance(value, str) or not value:
            raise ContractError(f"{field_name} must be nonempty base64 text")
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise ContractError(f"{field_name} must be valid base64 text") from None
        if not decoded:
            raise ContractError(f"{field_name} must contain binary data")
    elif field_name == "x_studio_type":
        if value not in _WRITABLE_TYPE_VALUES:
            raise ContractError("x_studio_type is outside the writable selection")
    elif field_name == "x_studio_status":
        if value not in _WRITABLE_STATUS_VALUES:
            raise ContractError("x_studio_status is outside the writable selection")
    elif field_name == "x_studio_source":
        if value != SOURCE_VALUE:
            raise ContractError("x_studio_source is outside this app's namespace")


def _normalize_selection(metadata: dict[str, Any], field_name: str) -> frozenset[str]:
    selection = metadata.get("selection")
    if not isinstance(selection, (list, tuple)):
        raise ContractError(f"{field_name} selection metadata is malformed")
    values: list[str] = []
    for item in selection:
        if (
            not isinstance(item, (list, tuple))
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            raise ContractError(f"{field_name} selection metadata is malformed")
        values.append(item[0])
    if len(values) != len(set(values)):
        raise ContractError(f"{field_name} selection metadata has duplicate values")
    return frozenset(sorted(values))


class ImprovementsClient:
    """Narrow RPC facade for the feedback mirror's permanent contract."""

    default_executor = staticmethod(_build_default_executor)

    def __init__(
        self,
        config: ImprovementsConfig,
        executor: Callable[..., Any],
        *,
        uid: int | None = None,
    ):
        if not callable(executor):
            raise ImprovementsConfigError("dedicated Odoo executor must be callable")
        if uid is not None:
            _positive_integer(uid, "user id")
        self._config = config
        self._executor = executor
        self._uid = uid

    def __repr__(self) -> str:
        return "ImprovementsClient(<redacted>)"

    @classmethod
    def from_env(
        cls,
        executor: Callable[..., Any] | None = None,
        uid: int | None = None,
    ) -> ImprovementsClient:
        config = ImprovementsConfig.from_env()
        selected_executor = cls.default_executor(config) if executor is None else executor
        return cls(config, selected_executor, uid=uid)

    def authenticate(self) -> int:
        if self._uid is not None:
            return self._uid
        authenticate = getattr(self._executor, "authenticate", None)
        if not callable(authenticate):
            raise ContractError("injected executor requires an explicit user id")
        uid = authenticate()
        self._uid = _positive_integer(uid, "authenticated user id")
        return self._uid

    def _execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        if (model, method) not in ALLOWED:
            raise ContractError(f"{model}.{method} is not allowlisted")
        return self._executor(model, method, *args, **kwargs)

    def assert_mutation_allowed(self, feedback_id: int) -> None:
        if type(feedback_id) is not int or feedback_id <= 0:
            raise GateClosed("feedback id must be a positive integer")
        if os.environ.get("ODOO_SHARED_REPORTING_WRITE_ENABLED") != "true":
            raise GateClosed("shared reporting write gate is closed")
        if os.environ.get("ODOO_IMPROVEMENTS_WRITE_ENABLED") != "true":
            raise GateClosed("improvements write gate is closed")
        raw_canary = os.environ.get("ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID")
        if raw_canary not in {None, ""}:
            if not raw_canary.isascii() or not raw_canary.isdigit() or int(raw_canary) <= 0:
                raise GateClosed("invalid improvements canary feedback id")
            if int(raw_canary) != feedback_id:
                raise GateClosed("feedback is outside the canary fence")

    def _validate_target_fields(self, fields: object, *, feedback_id: int) -> dict[str, Any]:
        _positive_integer(feedback_id, "feedback id")
        if type(fields) is not dict or not fields:
            raise ContractError("target fields must be a nonempty dictionary")
        for field_name, value in fields.items():
            if not isinstance(field_name, str):
                raise ContractError("target field names must be strings")
            if "token" in field_name.casefold():
                raise ContractError("token fields are forbidden")
            if field_name == "active":
                raise ContractError("active is not writable")
            if field_name not in TARGET_FIELDS:
                raise ContractError(f"{field_name} is not an allowed target field")
            _validate_field_value(field_name, value, feedback_id)
        return fields

    def find_exact(self, source_id: str) -> list[dict[str, Any]]:
        canonical, _feedback_id = _canonical_source_id(source_id)
        result = self._execute(
            TARGET_MODEL,
            "search_read",
            [
                ("x_studio_source", "=", SOURCE_VALUE),
                ("x_studio_source_id", "=", canonical),
            ],
            fields=["id", "x_studio_source", "x_studio_source_id"],
            limit=3,
        )
        rows = _row_list(result, label="exact lookup", maximum=3)
        for row in rows:
            _positive_integer(row.get("id"), "exact lookup id")
            if (
                row.get("x_studio_source") != SOURCE_VALUE
                or row.get("x_studio_source_id") != canonical
            ):
                raise ContractError("exact lookup returned a record outside its domain")
        return rows

    def create_improvement(self, fields: dict, *, feedback_id: int) -> int:
        safe_fields = self._validate_target_fields(fields, feedback_id=feedback_id)
        self.assert_mutation_allowed(feedback_id)
        result = self._execute(TARGET_MODEL, "create", safe_fields)
        if type(result) is not int or result <= 0:
            raise MalformedMutationResponse("create response was not a positive integer")
        return result

    def write_improvement(self, remote_id: int, fields: dict, *, feedback_id: int) -> None:
        safe_remote_id = _positive_integer(remote_id, "remote id")
        safe_fields = self._validate_target_fields(fields, feedback_id=feedback_id)
        self.assert_mutation_allowed(feedback_id)
        result = self._execute(TARGET_MODEL, "write", [safe_remote_id], safe_fields)
        if result is not True:
            raise MalformedMutationResponse("write response was not exactly true")

    def read_improvement(
        self,
        remote_id: int,
        fields: list[str],
        *,
        full_binary: bool,
    ) -> dict[str, Any]:
        safe_remote_id = _positive_integer(remote_id, "remote id")
        if (
            type(fields) is not list
            or not fields
            or any(type(field_name) is not str for field_name in fields)
            or len(fields) != len(set(fields))
            or not set(fields) <= _READ_FIELDS
        ):
            raise ContractError("read fields must be a unique allowed nonempty list")
        if type(full_binary) is not bool:
            raise ContractError("full_binary must be a boolean")
        kwargs: dict[str, Any] = {"fields": list(fields)}
        if full_binary:
            kwargs["context"] = {"bin_size": False}
        result = self._execute(TARGET_MODEL, "read", [safe_remote_id], **kwargs)
        rows = _row_list(result, label="improvement read", maximum=1)
        if len(rows) != 1 or rows[0].get("id") != safe_remote_id:
            raise ContractError("improvement read did not return the requested record")
        if any(field_name not in rows[0] for field_name in fields):
            raise ContractError("improvement read omitted a requested field")
        return rows[0]

    def find_employees_by_email(self, email: str, *, limit: int) -> list[dict[str, Any]]:
        if (
            not isinstance(email, str)
            or email != email.strip().casefold()
            or not email.isascii()
            or _EMAIL_RE.fullmatch(email) is None
        ):
            raise ContractError("employee email must be normalized")
        if type(limit) is not int or limit != 3:
            raise ContractError("employee lookup limit must be exactly 3")
        result = self._execute(
            "hr.employee",
            "search_read",
            [("work_email", "=ilike", email)],
            fields=["id", "work_email"],
            limit=3,
            context={"active_test": False},
        )
        rows = _row_list(result, label="employee lookup", maximum=3)
        for row in rows:
            _positive_integer(row.get("id"), "employee id")
            if not isinstance(row.get("work_email"), str):
                raise ContractError("employee lookup response was malformed")
        return rows

    def read_legacy_task_stages(self, task_ids: list[int]) -> list[dict[str, Any]]:
        if (
            type(task_ids) is not list
            or not task_ids
            or len(task_ids) > 100
            or len(task_ids) != len(set(task_ids))
            or any(type(task_id) is not int or task_id <= 0 for task_id in task_ids)
        ):
            raise ContractError("legacy task ids must be 1 to 100 unique positive integers")
        result = self._execute("project.task", "read", list(task_ids), fields=["id", "stage_id"])
        rows = _row_list(result, label="legacy task stage read", maximum=len(task_ids))
        seen: set[int] = set()
        for row in rows:
            task_id = _positive_integer(row.get("id"), "legacy task id")
            if task_id not in task_ids or task_id in seen or "stage_id" not in row:
                raise ContractError("legacy task stage response was malformed")
            stage = row["stage_id"]
            if (
                stage is not False
                and stage is not None
                and not (
                    isinstance(stage, (list, tuple))
                    and len(stage) == 2
                    and type(stage[0]) is int
                    and stage[0] > 0
                    and isinstance(stage[1], str)
                )
            ):
                raise ContractError("legacy task stage response was malformed")
            seen.add(task_id)
        return rows

    def _read_contract(self) -> ImprovementContract:
        result = self._execute(
            TARGET_MODEL,
            "fields_get",
            sorted(TARGET_FIELDS),
            attributes=list(_CONTRACT_ATTRIBUTES),
        )
        if type(result) is not dict:
            raise ContractError("target fields metadata was malformed")
        missing = TARGET_FIELDS - set(result)
        if missing:
            raise ContractError("target contract is missing required fields")

        for field_name in TARGET_FIELDS:
            metadata = result.get(field_name)
            if type(metadata) is not dict or metadata.get("readonly") is not False:
                raise ContractError(f"{field_name} must be writable")

        for field_name, expected_type in _EXPECTED_TYPES.items():
            if result[field_name].get("type") != expected_type:
                raise ContractError(f"{field_name} has the wrong type")

        start_type = result["x_studio_date_start"].get("type")
        stop_type = result["x_studio_date_stop"].get("type")
        if start_type not in {"date", "datetime"}:
            raise ContractError("x_studio_date_start has the wrong type")
        if stop_type not in {"date", "datetime"}:
            raise ContractError("x_studio_date_stop has the wrong type")

        source_values = _normalize_selection(result["x_studio_source"], "x_studio_source")
        status_values = _normalize_selection(result["x_studio_status"], "x_studio_status")
        type_values = _normalize_selection(result["x_studio_type"], "x_studio_type")
        if SOURCE_VALUE not in source_values:
            raise ContractError("required source selection value is absent")
        if status_values != _EXPECTED_STATUS_VALUES:
            raise ContractError("status selection values do not match the contract")
        if type_values != _EXPECTED_TYPE_VALUES:
            raise ContractError("type selection values do not match the contract")
        return ImprovementContract(start_type=start_type, stop_type=stop_type)

    def read_contract(self) -> ImprovementContract:
        """Read and validate target metadata without caching it."""
        return self._read_contract()

    def verify_target_identity(self) -> ImprovementContract:
        """Freshly verify database, current company, and target metadata."""
        uid = self.authenticate()
        database_uuid = self._execute("ir.config_parameter", "get_param", "database.uuid")
        if (
            not isinstance(database_uuid, str)
            or database_uuid != self._config.expected_database_uuid
        ):
            raise TargetIdentityError("dedicated Odoo database identity mismatch")

        user_rows = _row_list(
            self._execute("res.users", "read", [uid], fields=["id", "company_id"]),
            label="current user",
            maximum=1,
        )
        if len(user_rows) != 1 or user_rows[0].get("id") != uid:
            raise TargetIdentityError("dedicated Odoo current user was malformed")
        company_value = user_rows[0].get("company_id")
        if (
            not isinstance(company_value, (list, tuple))
            or len(company_value) < 1
            or type(company_value[0]) is not int
            or company_value[0] <= 0
        ):
            raise TargetIdentityError("dedicated Odoo current company was malformed")
        company_id = company_value[0]

        company_rows = _row_list(
            self._execute("res.company", "read", [company_id], fields=["id", "name"]),
            label="current company",
            maximum=1,
        )
        if (
            len(company_rows) != 1
            or company_rows[0].get("id") != company_id
            or company_rows[0].get("name") != self._config.expected_company
        ):
            raise TargetIdentityError("dedicated Odoo current company mismatch")
        return self._read_contract()
