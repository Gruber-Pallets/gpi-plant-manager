"""Odoo-like object API core: safe model dispatch, domains, fields, envelopes."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from . import api_keys, db


class ObjectAPIError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


@dataclass(frozen=True)
class FieldSpec:
    type: str
    string: str
    readonly: bool = False
    required: bool = False
    selection: list[str] | None = None

    def as_dict(self) -> dict:
        out = {
            "type": self.type,
            "string": self.string,
            "readonly": self.readonly,
            "required": self.required,
        }
        if self.selection is not None:
            out["selection"] = list(self.selection)
        return out


_OPS = {"=", "!=", "in", "not in", "ilike", "not ilike", ">", ">=", "<", "<="}


def _cmp(value: Any, op: str, expected: Any) -> bool:
    if op == "=":
        return value == expected
    if op == "!=":
        return value != expected
    if op == "in":
        return value in (expected or [])
    if op == "not in":
        return value not in (expected or [])
    if op == "ilike":
        return str(expected).lower() in str(value or "").lower()
    if op == "not ilike":
        return str(expected).lower() not in str(value or "").lower()
    if op in (">", ">=", "<", "<="):
        if value is None:
            return False
        if op == ">":
            return value > expected
        if op == ">=":
            return value >= expected
        if op == "<":
            return value < expected
        return value <= expected
    raise ObjectAPIError("invalid_domain", f"Unsupported operator: {op}", 400)


def apply_domain(
    records: list[dict],
    domain: list | None,
    fields: dict[str, FieldSpec],
) -> list[dict]:
    if domain in (None, []):
        return list(records)
    if not isinstance(domain, list):
        raise ObjectAPIError("invalid_domain", "Domain must be a list", 400)
    if len(domain) > 50:
        raise ObjectAPIError("invalid_domain", "Domain has too many clauses", 400)
    clauses: list[tuple[str, str, Any]] = []
    for clause in domain:
        if not isinstance(clause, list) or len(clause) != 3:
            raise ObjectAPIError(
                "invalid_domain",
                "Each domain clause must be [field, operator, value]",
                400,
            )
        field, op, expected = clause
        if field not in fields:
            raise ObjectAPIError("invalid_field", f"Unknown field: {field}", 400)
        if op not in _OPS:
            raise ObjectAPIError("invalid_domain", f"Unsupported operator: {op}", 400)
        clauses.append((field, op, expected))
    return [
        row
        for row in records
        if all(_cmp(row.get(field), op, expected) for field, op, expected in clauses)
    ]


def apply_order(records: list[dict], order: str | None) -> list[dict]:
    if not order:
        return list(records)
    parts = order.split()
    field = parts[0]
    desc = len(parts) > 1 and parts[1].lower() == "desc"
    return sorted(records, key=lambda row: (row.get(field) is None, row.get(field)), reverse=desc)


def select_fields(
    records: list[dict],
    wanted: list[str] | None,
    fields: dict[str, FieldSpec],
) -> list[dict]:
    names = wanted or list(fields.keys())
    for name in names:
        if name not in fields:
            raise ObjectAPIError("invalid_field", f"Unknown field: {name}", 400)
    return [{name: row.get(name) for name in names} for row in records]


class ObjectModel:
    name: str
    display_name: str
    fields: dict[str, FieldSpec]
    writable_fields: set[str] = set()
    allow_unlink: bool = False
    default_order: str = "id asc"

    def fields_get(self) -> dict:
        return {name: spec.as_dict() for name, spec in self.fields.items()}

    def all_records(self, context: dict) -> list[dict]:
        raise ObjectAPIError("method_not_allowed", "search/read not implemented", 400)

    def create_record(self, values: dict, context: dict):
        raise ObjectAPIError("method_not_allowed", "create not implemented", 400)

    def write_records(self, ids: list, values: dict, context: dict) -> bool:
        raise ObjectAPIError("method_not_allowed", "write not implemented", 400)

    def unlink_records(self, ids: list, context: dict) -> bool:
        raise ObjectAPIError("method_not_allowed", "unlink not implemented", 400)


class Registry:
    def __init__(self):
        self._models: dict[str, ObjectModel] = {}

    def register(self, model: ObjectModel) -> None:
        self._models[model.name] = model

    def get(self, name: str) -> ObjectModel:
        model = self._models.get(name)
        if model is None:
            raise ObjectAPIError("model_not_found", f"Unknown model: {name}", 404)
        return model

    def list_models(self, key_row: dict | None = None) -> list[dict]:
        out = []
        for model in self._models.values():
            out.append(
                {
                    "model": model.name,
                    "name": model.display_name,
                    "read": True,
                    "write": bool(model.writable_fields),
                    "unlink": bool(model.allow_unlink),
                }
            )
        return sorted(out, key=lambda row: row["model"])


def _ok(result: Any) -> tuple[dict, int]:
    return {"ok": True, "result": result}, 200


def _err(exc: ObjectAPIError) -> tuple[dict, int]:
    return {
        "ok": False,
        "error": {"code": exc.code, "message": exc.message, "details": exc.details},
    }, exc.status


def _ids_arg(args: list) -> list:
    if not args or not isinstance(args[0], list):
        raise ObjectAPIError("invalid_request", "Expected ids list as first arg", 400)
    return args[0]


def _values_arg(args: list, index: int) -> dict:
    if len(args) <= index or not isinstance(args[index], dict):
        raise ObjectAPIError("invalid_request", "Expected values object", 400)
    return args[index]


def _check_write_fields(model: ObjectModel, values: dict) -> None:
    for name in values.keys():
        if name not in model.fields:
            raise ObjectAPIError("invalid_field", f"Unknown field: {name}", 400)
        if name not in model.writable_fields:
            raise ObjectAPIError("invalid_field", f"Field is read-only: {name}", 400)


def _read_scope(method: str) -> str:
    return (
        "object:read"
        if method in {"fields_get", "search", "search_count", "read", "search_read"}
        else "object:write"
    )


def execute(
    registry: Registry,
    key_row: dict,
    payload: dict,
    client: dict | None = None,
) -> tuple[dict, int]:
    try:
        if not isinstance(payload, dict):
            raise ObjectAPIError("invalid_request", "JSON body must be an object", 400)
        model_name = payload.get("model")
        method = payload.get("method")
        args = payload.get("args") or []
        kwargs = payload.get("kwargs") or {}
        context = payload.get("context") or {}
        if not isinstance(model_name, str) or not isinstance(method, str):
            raise ObjectAPIError("invalid_request", "model and method are required", 400)
        if not isinstance(args, list) or not isinstance(kwargs, dict) or not isinstance(context, dict):
            raise ObjectAPIError(
                "invalid_request",
                "args must be list; kwargs/context must be objects",
                400,
            )
        model = registry.get(model_name)
        scope = _read_scope(method)
        if method == "unlink":
            scope = "object:unlink"
        if not api_keys.has_scope(key_row, scope, model_name):
            raise ObjectAPIError("access_denied", f"API key does not allow {scope}", 403)
        if method == "fields_get":
            return _ok(model.fields_get())
        if method in {"search", "search_count", "search_read"}:
            domain = args[0] if args else []
            records = apply_domain(model.all_records(context), domain, model.fields)
            records = apply_order(records, kwargs.get("order") or model.default_order)
            if method == "search_count":
                return _ok(len(records))
            offset = max(0, int(kwargs.get("offset") or 0))
            limit = min(1000, max(0, int(kwargs.get("limit") or 100)))
            page = records[offset:offset + limit]
            if method == "search":
                return _ok([row.get("id") for row in page])
            return _ok(select_fields(page, kwargs.get("fields"), model.fields))
        if method == "read":
            ids = set(_ids_arg(args))
            records = [row for row in model.all_records(context) if row.get("id") in ids]
            return _ok(select_fields(records, kwargs.get("fields"), model.fields))
        if method == "create":
            values = _values_arg(args, 0)
            _check_write_fields(model, values)
            return _ok(model.create_record(values, context))
        if method == "write":
            ids = _ids_arg(args)
            values = _values_arg(args, 1)
            _check_write_fields(model, values)
            return _ok(model.write_records(ids, values, context))
        if method == "unlink":
            if not model.allow_unlink:
                raise ObjectAPIError("method_not_allowed", "unlink disabled for this model", 400)
            return _ok(model.unlink_records(_ids_arg(args), context))
        raise ObjectAPIError("method_not_allowed", f"Unknown method: {method}", 400)
    except ObjectAPIError as exc:
        return _err(exc)
    except Exception:
        return _err(ObjectAPIError("server_error", "Unexpected server error", 500))


def audit_call(
    *,
    key_row: dict | None,
    payload: dict,
    body: dict,
    status_code: int,
    started_at: float,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    try:
        err = None if body.get("ok") else (body.get("error") or {}).get("code")
        ctx = payload.get("context") if isinstance(payload, dict) else {}
        actor = ctx.get("actor") if isinstance(ctx, dict) else None
        kwargs = payload.get("kwargs") if isinstance(payload, dict) else {}
        args = payload.get("args") if isinstance(payload, dict) else []
        summary = {
            "fields": kwargs.get("fields") if isinstance(kwargs, dict) else None,
            "limit": kwargs.get("limit") if isinstance(kwargs, dict) else None,
            "offset": kwargs.get("offset") if isinstance(kwargs, dict) else None,
            "args_count": len(args) if isinstance(args, list) else None,
        }
        db.execute(
            "INSERT INTO api_audit_log "
            "(api_key_id, app_name, actor, model, method, request_summary, status, "
            "error_code, duration_ms, client_ip, user_agent) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)",
            (
                key_row.get("id") if key_row else None,
                key_row.get("name") if key_row else "unknown",
                actor,
                payload.get("model") if isinstance(payload, dict) else None,
                payload.get("method") if isinstance(payload, dict) else None,
                json.dumps(summary),
                "ok" if status_code < 400 else "error",
                err,
                int((time.perf_counter() - started_at) * 1000),
                client_ip,
                user_agent,
            ),
        )
    except Exception:
        pass
