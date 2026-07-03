"""Server-to-server Odoo-like object API routes."""
from __future__ import annotations

import ipaddress
import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import api_keys, object_api, object_models

router = APIRouter(prefix="/api/v1/object")


def _registry():
    return object_models.build_registry()


def _auth_error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message, "details": {}}},
        status_code=status,
    )


def _bearer(request: Request) -> str | None:
    raw = request.headers.get("authorization") or ""
    if not raw.lower().startswith("bearer "):
        return None
    return raw.split(" ", 1)[1].strip()


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _https_ok(request: Request) -> bool:
    if os.environ.get("REQUIRE_API_HTTPS", "").strip().lower() not in ("1", "true", "yes"):
        return True
    return (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"


def _ip_allowed(row: dict, request: Request) -> bool:
    allowed = row.get("allowed_ips") or []
    if not allowed:
        return True
    raw = _client_ip(request)
    if not raw:
        return False
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return False
    for item in allowed:
        try:
            if "/" in item:
                if addr in ipaddress.ip_network(item, strict=False):
                    return True
            elif addr == ipaddress.ip_address(item):
                return True
        except ValueError:
            continue
    return False


def _key_row(request: Request) -> dict | JSONResponse:
    token = _bearer(request)
    if not token:
        return _auth_error("auth_required", "Bearer API key required", 401)
    row = api_keys.verify_key(token)
    if row is None:
        return _auth_error("invalid_api_key", "Invalid API key", 401)
    if not _https_ok(request):
        return _auth_error("https_required", "Object API requires HTTPS", 403)
    if not _ip_allowed(row, request):
        return _auth_error("ip_not_allowed", "Client IP is not allowed for this key", 403)
    return row


@router.post("/execute")
async def execute(request: Request):
    started = time.perf_counter()
    key = _key_row(request)
    if isinstance(key, JSONResponse):
        return key
    try:
        payload = await request.json()
    except Exception:
        payload = {}
        body = {
            "ok": False,
            "error": {"code": "invalid_request", "message": "Invalid JSON", "details": {}},
        }
        status = 400
    else:
        body, status = object_api.execute(
            _registry(),
            key,
            payload,
            {"client_ip": request.client.host if request.client else None},
        )
    object_api.audit_call(
        key_row=key,
        payload=payload,
        body=body,
        status_code=status,
        started_at=started,
        client_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return JSONResponse(body, status_code=status)


@router.get("/models")
async def models(request: Request):
    key = _key_row(request)
    if isinstance(key, JSONResponse):
        return key
    return JSONResponse({"ok": True, "models": _registry().list_models(key)})


@router.get("/models/{model_name}/fields")
async def model_fields(model_name: str, request: Request):
    key = _key_row(request)
    if isinstance(key, JSONResponse):
        return key
    body, status = object_api.execute(
        _registry(),
        key,
        {"model": model_name, "method": "fields_get"},
    )
    return JSONResponse(body, status_code=status)
