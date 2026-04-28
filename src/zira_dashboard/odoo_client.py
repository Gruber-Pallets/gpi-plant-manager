"""Odoo XML-RPC client. Read-only access to hr.employee + hr_skills.

Configuration comes from environment variables:
- ODOO_URL  — base URL, e.g. https://gruber-pallets.odoo.com (no trailing /odoo)
- ODOO_DB   — database name
- ODOO_LOGIN — username (email)
- ODOO_API_KEY — Odoo API key (Settings → Users → Account Security)

Never log or echo these values.
"""

from __future__ import annotations

import os
import xmlrpc.client
from typing import Any


class OdooConfigError(RuntimeError):
    """Required env var is missing or malformed."""


class OdooAuthError(RuntimeError):
    """Odoo accepted the request but rejected our credentials."""


_uid_cache: int | None = None
_object_proxy: xmlrpc.client.ServerProxy | None = None


def _reset_cache_for_tests() -> None:
    """Clear cached uid + object proxy; tests call this between cases."""
    global _uid_cache, _object_proxy
    _uid_cache = None
    _object_proxy = None


def _config() -> tuple[str, str, str, str]:
    url = os.environ.get("ODOO_URL", "").rstrip("/")
    db = os.environ.get("ODOO_DB", "")
    login = os.environ.get("ODOO_LOGIN", "")
    key = os.environ.get("ODOO_API_KEY", "")
    missing = [k for k, v in (
        ("ODOO_URL", url), ("ODOO_DB", db),
        ("ODOO_LOGIN", login), ("ODOO_API_KEY", key),
    ) if not v]
    if missing:
        raise OdooConfigError(f"Missing env vars: {', '.join(missing)}")
    return url, db, login, key


def authenticate() -> int:
    global _uid_cache
    if _uid_cache is not None:
        return _uid_cache
    url, db, login, key = _config()
    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, login, key, {})
    if not uid:
        raise OdooAuthError("Odoo rejected credentials")
    _uid_cache = uid
    return uid


def execute(model: str, method: str, *args: Any, **kwargs: Any) -> Any:
    """Run an XML-RPC call against `model.method(*args, **kwargs)`. Caches
    the object proxy across calls."""
    global _object_proxy
    url, db, _, key = _config()
    uid = authenticate()
    if _object_proxy is None:
        _object_proxy = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
    return _object_proxy.execute_kw(
        db, uid, key, model, method, list(args), kwargs
    )


SKILL_TYPE_NAMES = ("Production Skills", "Supervisor Skills")


def fetch_skill_columns() -> list[str]:
    """Return ordered skill names: all skills from the Production type
    (alphabetical), then all from Supervisor (alphabetical)."""
    types = execute(
        "hr.skill.type", "search_read",
        [("name", "in", list(SKILL_TYPE_NAMES))],
        fields=["id", "name"],
    )
    type_order = {name: i for i, name in enumerate(SKILL_TYPE_NAMES)}
    types.sort(key=lambda t: type_order.get(t["name"], 999))
    type_ids = [t["id"] for t in types]
    if not type_ids:
        return []
    skills = execute(
        "hr.skill", "search_read",
        [("skill_type_id", "in", type_ids)],
        fields=["id", "name", "skill_type_id"],
    )
    by_type: dict[int, list[str]] = {tid: [] for tid in type_ids}
    for s in skills:
        tid = s["skill_type_id"][0] if isinstance(s["skill_type_id"], list) else s["skill_type_id"]
        by_type.setdefault(tid, []).append(s["name"])
    out: list[str] = []
    for tid in type_ids:
        out.extend(sorted(by_type.get(tid, []), key=str.lower))
    return out


def fetch_skill_level_buckets() -> dict[int, int]:
    """Map hr.skill.level.id → bucket (0–3) using rank-within-type.

    For each skill type, sort levels ascending by level_progress, assign
    rank index, then bucket = round(rank * 3 / max(N-1, 1)) clamped 0..3.
    """
    levels = execute(
        "hr.skill.level", "search_read",
        [],
        fields=["id", "level_progress", "skill_type_id"],
    )
    by_type: dict[int, list[dict]] = {}
    for lvl in levels:
        tid = lvl["skill_type_id"][0] if isinstance(lvl["skill_type_id"], list) else lvl["skill_type_id"]
        by_type.setdefault(tid, []).append(lvl)
    out: dict[int, int] = {}
    for tid, lvls in by_type.items():
        lvls.sort(key=lambda l: l.get("level_progress", 0))
        n = len(lvls)
        for rank, lvl in enumerate(lvls):
            if n <= 1:
                bucket = 0
            else:
                bucket = round(rank * 3 / (n - 1))
            out[lvl["id"]] = max(0, min(3, bucket))
    return out


def fetch_employees() -> list[dict]:
    """All active hr.employee records with the fields we need."""
    return execute(
        "hr.employee", "search_read",
        [("active", "=", True)],
        fields=["id", "name", "active", "work_email"],
    )


def fetch_skills_for(employee_ids: list[int]) -> dict[int, list[dict]]:
    """Return {employee_id: [{skill_id, skill_name, level_id}, ...]}."""
    if not employee_ids:
        return {}
    rows = execute(
        "hr.employee.skill", "search_read",
        [("employee_id", "in", employee_ids)],
        fields=["id", "employee_id", "skill_id", "skill_level_id"],
    )
    out: dict[int, list[dict]] = {eid: [] for eid in employee_ids}
    for r in rows:
        eid = r["employee_id"][0] if isinstance(r["employee_id"], list) else r["employee_id"]
        sid = r["skill_id"][0]    if isinstance(r["skill_id"], list)    else r["skill_id"]
        lid = r["skill_level_id"][0] if isinstance(r["skill_level_id"], list) else r["skill_level_id"]
        sname = r["skill_id"][1] if isinstance(r["skill_id"], list) else ""
        out.setdefault(eid, []).append({"skill_id": sid, "skill_name": sname, "level_id": lid})
    return out
