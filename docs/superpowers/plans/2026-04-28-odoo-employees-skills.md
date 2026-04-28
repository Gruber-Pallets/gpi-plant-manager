# Odoo Employees + Skills Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire Odoo as the source of truth for the People Matrix. Pull active `hr.employee` records and their skills (from "Production" + "Supervisor" skill types) on a 1-hour TTL plus manual refresh. The matrix becomes view-only for skill cells; reserve flag stays as a local override. One-time migration script renames historical schedule names to match Odoo's `hr.employee.name`.

**Architecture:** Two new modules — `odoo_client.py` (XML-RPC wrapper, env-var-driven auth) and `odoo_sync.py` (cache + TTL + roster write). The skills route calls `sync()` on GET (TTL-checked) and adds a forcing POST endpoint. Template changes drop edit affordances on skill cells and add a "Last synced … [Refresh]" header. Migration is a one-shot CLI script under `scripts/`.

**Tech Stack:** Python 3.12 / FastAPI / `xmlrpc.client` (stdlib) / pytest with mocked XML-RPC.

---

## File Structure

- New: `src/zira_dashboard/odoo_client.py` — XML-RPC client + fetch helpers
- New: `src/zira_dashboard/odoo_sync.py` — sync orchestration + cache TTL + atomic roster write
- New: `scripts/migrate_schedule_names_to_odoo.py` — one-shot CLI migration
- New: `tests/test_odoo_client.py`
- New: `tests/test_odoo_sync.py`
- New: `docs/odoo-setup.md` — env-var setup + first-sync runbook
- Modified: `src/zira_dashboard/routes/skills.py` — call `sync()` on GET, add `/staffing/skills/refresh` POST, drop skill-write logic
- Modified: `src/zira_dashboard/templates/skills.html` — view-only skill cells, last-sync header, refresh button, edit-in-Odoo link per row
- Modified: `src/zira_dashboard/staffing.py` — add optional `employee_id: int | None = None` field on `Person` for the edit-in-Odoo link
- Modified: `.gitignore` — add `.odoo_last_sync`, `roster.json.next`, `schedules.bak/`

---

### Task 1: Odoo client scaffolding + auth

**Files:**
- Create: `src/zira_dashboard/odoo_client.py`
- Test: `tests/test_odoo_client.py`

- [ ] **Step 1: Write failing tests for env-var reads + authenticate**

```python
# tests/test_odoo_client.py
import pytest
from unittest.mock import patch, MagicMock

from zira_dashboard import odoo_client


def test_authenticate_raises_when_env_vars_missing(monkeypatch):
    for k in ("ODOO_URL", "ODOO_DB", "ODOO_LOGIN", "ODOO_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(odoo_client.OdooConfigError):
        odoo_client.authenticate()


def test_authenticate_returns_uid_on_success(monkeypatch):
    monkeypatch.setenv("ODOO_URL", "https://example.odoo.com")
    monkeypatch.setenv("ODOO_DB", "Production")
    monkeypatch.setenv("ODOO_LOGIN", "dale@example.com")
    monkeypatch.setenv("ODOO_API_KEY", "secret-key")
    fake_common = MagicMock()
    fake_common.authenticate.return_value = 42
    with patch("xmlrpc.client.ServerProxy", return_value=fake_common) as proxy:
        odoo_client._reset_cache_for_tests()
        uid = odoo_client.authenticate()
    assert uid == 42
    proxy.assert_called_with("https://example.odoo.com/xmlrpc/2/common")
    fake_common.authenticate.assert_called_with("Production", "dale@example.com", "secret-key", {})


def test_authenticate_raises_on_failure(monkeypatch):
    monkeypatch.setenv("ODOO_URL", "https://example.odoo.com")
    monkeypatch.setenv("ODOO_DB", "Production")
    monkeypatch.setenv("ODOO_LOGIN", "dale@example.com")
    monkeypatch.setenv("ODOO_API_KEY", "wrong")
    fake_common = MagicMock()
    fake_common.authenticate.return_value = False
    with patch("xmlrpc.client.ServerProxy", return_value=fake_common):
        odoo_client._reset_cache_for_tests()
        with pytest.raises(odoo_client.OdooAuthError):
            odoo_client.authenticate()
```

- [ ] **Step 2: Run, verify FAIL** — `pytest tests/test_odoo_client.py -v`

- [ ] **Step 3: Implement the module**

Create `src/zira_dashboard/odoo_client.py`:

```python
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
```

- [ ] **Step 4: Run, verify PASS** — `pytest tests/test_odoo_client.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/odoo_client.py tests/test_odoo_client.py
git commit -m "feat(odoo): client scaffolding with env-var-driven auth"
```

---

### Task 2: Fetch skill columns + level buckets

**Files:**
- Modify: `src/zira_dashboard/odoo_client.py`
- Test: `tests/test_odoo_client.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_odoo_client.py`:

```python
def _stub_execute(monkeypatch, responses):
    """Map (model, method) → return value. Calls not in the map raise."""
    calls = []
    def fake(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        key = (model, method)
        if key not in responses:
            raise AssertionError(f"unexpected call: {key}")
        return responses[key]
    monkeypatch.setattr(odoo_client, "execute", fake)
    return calls


def test_fetch_skill_columns_returns_production_then_supervisor(monkeypatch):
    responses = {
        ("hr.skill.type", "search_read"): [
            {"id": 1, "name": "Production"},
            {"id": 2, "name": "Supervisor"},
        ],
        ("hr.skill", "search_read"): [
            {"id": 10, "name": "Repair", "skill_type_id": [1, "Production"]},
            {"id": 11, "name": "Dismantler", "skill_type_id": [1, "Production"]},
            {"id": 12, "name": "Floor Lead", "skill_type_id": [2, "Supervisor"]},
        ],
    }
    _stub_execute(monkeypatch, responses)
    cols = odoo_client.fetch_skill_columns()
    # Production skills first (alphabetical), then Supervisor (alphabetical)
    assert cols == ["Dismantler", "Repair", "Floor Lead"]


def test_fetch_skill_level_buckets_rank_maps_4_levels(monkeypatch):
    responses = {
        ("hr.skill.level", "search_read"): [
            {"id": 100, "level_progress": 0,   "skill_type_id": [1, "Production"]},
            {"id": 101, "level_progress": 33,  "skill_type_id": [1, "Production"]},
            {"id": 102, "level_progress": 67,  "skill_type_id": [1, "Production"]},
            {"id": 103, "level_progress": 100, "skill_type_id": [1, "Production"]},
        ],
    }
    _stub_execute(monkeypatch, responses)
    buckets = odoo_client.fetch_skill_level_buckets()
    assert buckets == {100: 0, 101: 1, 102: 2, 103: 3}


def test_fetch_skill_level_buckets_rank_maps_3_levels(monkeypatch):
    responses = {
        ("hr.skill.level", "search_read"): [
            {"id": 200, "level_progress": 0,   "skill_type_id": [2, "Supervisor"]},
            {"id": 201, "level_progress": 50,  "skill_type_id": [2, "Supervisor"]},
            {"id": 202, "level_progress": 100, "skill_type_id": [2, "Supervisor"]},
        ],
    }
    _stub_execute(monkeypatch, responses)
    buckets = odoo_client.fetch_skill_level_buckets()
    # 3 levels -> rank 0,1,2 -> 0, round(1*3/2)=2, round(2*3/2)=3
    assert buckets == {200: 0, 201: 2, 202: 3}
```

- [ ] **Step 2: Run, verify FAIL**

- [ ] **Step 3: Implement**

Append to `src/zira_dashboard/odoo_client.py`:

```python
SKILL_TYPE_NAMES = ("Production", "Supervisor")


def fetch_skill_columns() -> list[str]:
    """Return ordered skill names: all skills from the Production type
    (alphabetical), then all from Supervisor (alphabetical)."""
    types = execute(
        "hr.skill.type", "search_read",
        [("name", "in", list(SKILL_TYPE_NAMES))],
        fields=["id", "name"],
    )
    type_order = {t["name"]: i for i, t in enumerate(SKILL_TYPE_NAMES)}
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
```

- [ ] **Step 4: Run, verify PASS**

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/odoo_client.py tests/test_odoo_client.py
git commit -m "feat(odoo): fetch skill columns and rank-based level buckets"
```

---

### Task 3: Fetch employees + their skills

**Files:**
- Modify: `src/zira_dashboard/odoo_client.py`
- Test: `tests/test_odoo_client.py`

- [ ] **Step 1: Write failing tests**

```python
def test_fetch_employees_returns_active_only_with_required_fields(monkeypatch):
    responses = {
        ("hr.employee", "search_read"): [
            {"id": 1, "name": "Alice", "active": True, "work_email": "alice@x"},
            {"id": 2, "name": "Bob",   "active": True, "work_email": False},
        ],
    }
    calls = _stub_execute(monkeypatch, responses)
    out = odoo_client.fetch_employees()
    assert out == [
        {"id": 1, "name": "Alice", "active": True, "work_email": "alice@x"},
        {"id": 2, "name": "Bob",   "active": True, "work_email": False},
    ]
    # Search must filter to active only
    args = calls[0][2]
    assert ("active", "=", True) in args[0]


def test_fetch_skills_for_groups_by_employee_id(monkeypatch):
    responses = {
        ("hr.employee.skill", "search_read"): [
            {"id": 5, "employee_id": [1, "Alice"], "skill_id": [10, "Repair"],     "skill_level_id": [103, "Expert"]},
            {"id": 6, "employee_id": [1, "Alice"], "skill_id": [11, "Dismantler"], "skill_level_id": [101, "Beginner"]},
            {"id": 7, "employee_id": [2, "Bob"],   "skill_id": [10, "Repair"],     "skill_level_id": [102, "Adv"]},
        ],
    }
    _stub_execute(monkeypatch, responses)
    out = odoo_client.fetch_skills_for([1, 2])
    assert out == {
        1: [
            {"skill_id": 10, "skill_name": "Repair",     "level_id": 103},
            {"skill_id": 11, "skill_name": "Dismantler", "level_id": 101},
        ],
        2: [
            {"skill_id": 10, "skill_name": "Repair", "level_id": 102},
        ],
    }
```

- [ ] **Step 2: Run, verify FAIL**

- [ ] **Step 3: Implement**

Append:

```python
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
```

- [ ] **Step 4: Run, verify PASS**

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/odoo_client.py tests/test_odoo_client.py
git commit -m "feat(odoo): fetch employees and their skill records"
```

---

### Task 4: Add `employee_id` to `Person` dataclass

**Files:**
- Modify: `src/zira_dashboard/staffing.py`
- Test: `tests/test_staffing_custom_hours.py` (existing) — verify nothing breaks

- [ ] **Step 1: Add the optional field**

In `src/zira_dashboard/staffing.py`, change the `Person` dataclass:

```python
@dataclass
class Person:
    name: str
    active: bool = True
    reserve: bool = False
    skills: dict[str, int] = field(default_factory=dict)
    employee_id: int | None = None  # Odoo hr.employee.id; None for legacy
```

- [ ] **Step 2: Update `load_roster()` to read the optional field**

Find `load_roster()` and ensure the JSON read accepts `employee_id` if present:

```python
people.append(Person(
    name=row["name"],
    active=row.get("active", True),
    reserve=row.get("reserve", False),
    skills={k: int(v) for k, v in (row.get("skills") or {}).items()},
    employee_id=row.get("employee_id"),
))
```

- [ ] **Step 3: Update `save_roster()` to write it**

Find the inverse function and serialize `employee_id` when set:

```python
out = {"name": p.name, "active": p.active, "reserve": p.reserve, "skills": p.skills}
if p.employee_id is not None:
    out["employee_id"] = p.employee_id
```

- [ ] **Step 4: Run full suite, verify PASS**

```bash
pytest tests/ -v
```

Existing tests should still pass — `employee_id` is optional, defaults to `None`.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/staffing.py
git commit -m "feat(roster): add optional employee_id field for Odoo linking"
```

---

### Task 5: Sync orchestration module

**Files:**
- Create: `src/zira_dashboard/odoo_sync.py`
- Test: `tests/test_odoo_sync.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_odoo_sync.py
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from zira_dashboard import odoo_sync, staffing


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    monkeypatch.setattr(odoo_sync, "ROSTER_PATH", tmp_path / "roster.json")
    monkeypatch.setattr(odoo_sync, "LAST_SYNC_PATH", tmp_path / ".odoo_last_sync")
    monkeypatch.setattr(staffing, "ROSTER_PATH", tmp_path / "roster.json")
    return tmp_path


def test_sync_skips_when_within_ttl(tmp_env, monkeypatch):
    (tmp_env / "roster.json").write_text("[]")
    (tmp_env / ".odoo_last_sync").write_text(datetime.now(timezone.utc).isoformat())
    called = {"odoo": False}
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employees",
                        lambda: (_ for _ in ()).throw(AssertionError("should not call")))
    result = odoo_sync.sync(force=False)
    assert result.refreshed is False
    assert result.ok is True


def test_sync_force_refreshes_even_within_ttl(tmp_env, monkeypatch):
    (tmp_env / ".odoo_last_sync").write_text(datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employees",
                        lambda: [{"id": 1, "name": "Alice", "active": True, "work_email": False}])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skills_for",
                        lambda ids: {1: [{"skill_id": 10, "skill_name": "Repair", "level_id": 103}]})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_columns",
                        lambda: ["Repair", "Dismantler"])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_level_buckets",
                        lambda: {103: 3})
    result = odoo_sync.sync(force=True)
    assert result.refreshed is True
    assert result.employee_count == 1
    assert result.skill_column_count == 2
    roster = json.loads((tmp_env / "roster.json").read_text())
    assert roster[0]["name"] == "Alice"
    assert roster[0]["skills"]["Repair"] == 3
    assert roster[0]["skills"]["Dismantler"] == 0
    assert roster[0]["employee_id"] == 1


def test_sync_preserves_local_reserve_flag(tmp_env, monkeypatch):
    (tmp_env / "roster.json").write_text(json.dumps([
        {"name": "Alice", "active": True, "reserve": True,
         "skills": {"Repair": 0}, "employee_id": 1},
    ]))
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employees",
                        lambda: [{"id": 1, "name": "Alice", "active": True, "work_email": False}])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skills_for",
                        lambda ids: {1: []})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_columns",
                        lambda: ["Repair"])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_level_buckets",
                        lambda: {})
    odoo_sync.sync(force=True)
    roster = json.loads((tmp_env / "roster.json").read_text())
    assert roster[0]["reserve"] is True


def test_sync_returns_error_on_odoo_failure(tmp_env, monkeypatch):
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employees",
                        lambda: (_ for _ in ()).throw(odoo_sync.odoo_client.OdooAuthError("nope")))
    result = odoo_sync.sync(force=True)
    assert result.ok is False
    assert "nope" in (result.error or "")
    assert result.refreshed is False
```

- [ ] **Step 2: Run, verify FAIL**

- [ ] **Step 3: Implement `odoo_sync.py`**

```python
"""Odoo → roster.json sync with TTL cache.

Single public entrypoint: sync(force=False). Returns SyncResult.
On TTL hit (default 1 hour), no Odoo call is made and the existing
roster file is left alone. On force or stale, fetches employees + skills
from Odoo and atomically rewrites roster.json. The local `reserve` flag
on existing entries is preserved across syncs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import odoo_client

ROSTER_PATH = Path("roster.json")
LAST_SYNC_PATH = Path(".odoo_last_sync")
TTL = timedelta(hours=1)


@dataclass(frozen=True)
class SyncResult:
    ok: bool
    refreshed: bool
    employee_count: int
    skill_column_count: int
    last_sync_at: datetime | None
    error: str | None = None


def _read_last_sync() -> datetime | None:
    if not LAST_SYNC_PATH.exists():
        return None
    try:
        return datetime.fromisoformat(LAST_SYNC_PATH.read_text().strip())
    except (ValueError, OSError):
        return None


def _read_existing_reserves() -> dict[str, bool]:
    if not ROSTER_PATH.exists():
        return {}
    try:
        rows = json.loads(ROSTER_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {r["name"]: bool(r.get("reserve", False))
            for r in rows if isinstance(r, dict) and r.get("name")}


def sync(force: bool = False) -> SyncResult:
    last = _read_last_sync()
    now = datetime.now(timezone.utc)
    if not force and last is not None and (now - last) < TTL:
        return SyncResult(
            ok=True, refreshed=False, employee_count=0,
            skill_column_count=0, last_sync_at=last,
        )

    try:
        columns = odoo_client.fetch_skill_columns()
        buckets = odoo_client.fetch_skill_level_buckets()
        employees = odoo_client.fetch_employees()
        emp_ids = [e["id"] for e in employees]
        emp_skills = odoo_client.fetch_skills_for(emp_ids)
    except Exception as e:  # OdooConfigError, OdooAuthError, network, etc.
        return SyncResult(
            ok=False, refreshed=False, employee_count=0,
            skill_column_count=0, last_sync_at=last, error=str(e),
        )

    reserves = _read_existing_reserves()
    rows = []
    for emp in employees:
        skills_for_emp = {col: 0 for col in columns}
        for s in emp_skills.get(emp["id"], []):
            if s["skill_name"] in skills_for_emp:
                skills_for_emp[s["skill_name"]] = buckets.get(s["level_id"], 0)
        rows.append({
            "name": emp["name"],
            "active": bool(emp.get("active", True)),
            "reserve": reserves.get(emp["name"], False),
            "skills": skills_for_emp,
            "employee_id": emp["id"],
        })
    rows.sort(key=lambda r: r["name"].lower())

    tmp = ROSTER_PATH.with_suffix(ROSTER_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(rows, indent=2))
    os.replace(tmp, ROSTER_PATH)
    LAST_SYNC_PATH.write_text(now.isoformat())

    return SyncResult(
        ok=True, refreshed=True, employee_count=len(rows),
        skill_column_count=len(columns), last_sync_at=now,
    )
```

- [ ] **Step 4: Run, verify PASS**

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/odoo_sync.py tests/test_odoo_sync.py
git commit -m "feat(odoo): sync orchestration with TTL and atomic roster write"
```

---

### Task 6: Wire sync into the skills route

**Files:**
- Modify: `src/zira_dashboard/routes/skills.py`
- Test: existing `tests/` should still pass; add a route smoke test

- [ ] **Step 1: Read the existing handler**

```bash
sed -n '1,80p' src/zira_dashboard/routes/skills.py
```

Identify:
- The GET handler that reads `roster.json` and renders `skills.html`
- The POST handler that saves matrix edits

- [ ] **Step 2: Update GET to call sync (TTL-checked)**

Add at the top of the GET handler:

```python
from .. import odoo_sync
sync_result = odoo_sync.sync(force=False)
```

Pass to template context:

```python
"sync_ok": sync_result.ok,
"sync_last_at": sync_result.last_sync_at.isoformat() if sync_result.last_sync_at else None,
"sync_error": sync_result.error,
```

- [ ] **Step 3: Add a forcing refresh endpoint**

```python
@router.post("/staffing/skills/refresh", response_class=HTMLResponse)
def staffing_skills_refresh():
    from .. import odoo_sync
    odoo_sync.sync(force=True)
    return RedirectResponse("/staffing/skills", status_code=303)
```

(Add `RedirectResponse` import at the top.)

- [ ] **Step 4: Drop the skill-write logic from POST**

In the POST handler, remove any line that writes `skill__{name}__{skill}` form fields. Keep only the `reserve__*` and `active_present__*` writes (active will become read-only display in Task 7 but the POST logic stays defensive).

- [ ] **Step 5: Run tests + manual smoke**

```bash
pytest tests/ -v
```

Then in a browser: hit `/staffing/skills` (without env vars set, should render with cache + a banner; with env vars set, should render with live data).

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/skills.py
git commit -m "feat(odoo): trigger sync on /staffing/skills GET; add manual refresh"
```

---

### Task 7: View-only matrix UI

**Files:**
- Modify: `src/zira_dashboard/templates/skills.html`

- [ ] **Step 1: Add the last-sync header bar**

Above the `<table class="skills-table">`, insert:

```html
<div class="sync-bar">
  <span>
    {% if sync_last_at %}
      Last synced: {{ sync_last_at | replace('T', ' ') | truncate(16, end='') }}
    {% else %}
      Never synced
    {% endif %}
    {% if sync_error %}
      · <span class="sync-error">⚠ {{ sync_error }}</span>
    {% endif %}
  </span>
  <form method="post" action="/staffing/skills/refresh" style="display:inline">
    <button type="submit" class="sync-btn">Refresh from Odoo</button>
  </form>
</div>
```

CSS in the `<style>` block:

```css
.sync-bar { display: flex; justify-content: space-between; align-items: center;
            margin: 0 0 0.6rem; padding: 0.4rem 0.6rem; font-size: 0.82rem;
            color: var(--muted); background: var(--panel); border-radius: 6px; }
.sync-error { color: var(--bad); }
.sync-btn { background: var(--panel-2); color: var(--fg); border: 1px solid var(--border);
            border-radius: 6px; padding: 0.3rem 0.7rem; font-size: 0.8rem; cursor: pointer; }
.sync-btn:hover { border-color: var(--accent); color: var(--accent); }
```

- [ ] **Step 2: Replace skill `<select>` with read-only span**

Find the `<select class="skill-sel">` block (around line 217) and replace with:

```html
<span class="skill-display lvl-{{ lvl }}">{{ lvl if lvl > 0 else '—' }}</span>
```

CSS for the new class — match the existing `lvl-N` palette so the display is visually identical:

```css
.skill-display { display: inline-block; min-width: 1.6rem; text-align: center;
                 padding: 0.15rem 0.35rem; border-radius: 4px;
                 background: transparent; border: 1px solid var(--border);
                 font-variant-numeric: tabular-nums; }
.skill-display.lvl-3 { background: var(--accent-dim); border-color: var(--accent-dim); color: var(--accent); }
.skill-display.lvl-2 { background: var(--neutral-pill); border-color: var(--border); color: var(--fg); }
.skill-display.lvl-1 { background: var(--warn-dim); border-color: var(--warn-dim); color: var(--warn); }
.skill-display.lvl-0 { color: var(--muted); }
```

- [ ] **Step 3: Make the Active checkbox a read-only badge**

Find `<input class="active-check" type="checkbox" name="active__{{ p.name }}" ...>` and replace with:

```html
<span class="active-badge {% if p.active %}on{% else %}off{% endif %}">
  {% if p.active %}✓{% else %}✗{% endif %}
</span>
```

CSS:

```css
.active-badge.on  { color: var(--accent); }
.active-badge.off { color: var(--muted); }
```

(Keep the hidden `active_present__` input so the POST handler still has the check, but it's now informational.)

- [ ] **Step 4: Add the "edit in Odoo" hover link per row**

In the `<td class="name">` cell, after the existing name `<a class="name-link">`, add:

```html
{% if p.employee_id %}
  <a class="odoo-link" href="{{ odoo_url }}/web#id={{ p.employee_id }}&model=hr.employee&view_type=form" target="_blank" rel="noopener" title="Open in Odoo">↗</a>
{% endif %}
```

CSS:

```css
.odoo-link { opacity: 0; margin-left: 0.4rem; color: var(--muted); text-decoration: none; }
.skills-table tr:hover .odoo-link { opacity: 0.7; }
.odoo-link:hover { color: var(--accent); opacity: 1 !important; }
```

Pass `odoo_url = os.environ.get("ODOO_URL", "")` to the template context from the route GET handler.

- [ ] **Step 5: Remove the "+ Add person" / "Remove person" buttons**

Find and delete the `<button>` elements that trigger person adds/removes from the matrix. People come from Odoo now.

- [ ] **Step 6: Manual verify in browser**

Hit `/staffing/skills`. Confirm:
- Sync bar shows at top
- Skill cells render as read-only spans with the right colors
- Active is a badge, not a checkbox
- Reserve is still an editable checkbox
- Hovering a row reveals the Odoo arrow link
- Refresh button POSTs and the page comes back with a fresh "Last synced" time

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/templates/skills.html src/zira_dashboard/routes/skills.py
git commit -m "feat(odoo): view-only People Matrix with refresh button and Odoo links"
```

---

### Task 8: Schedule-name migration script

**Files:**
- Create: `scripts/migrate_schedule_names_to_odoo.py`
- Modified: `.gitignore`

- [ ] **Step 1: Add `.gitignore` entries**

Append to `.gitignore`:

```
.odoo_last_sync
roster.json.next
schedules.bak/
```

- [ ] **Step 2: Implement the migration script**

```python
#!/usr/bin/env python3
"""One-time migration: rename people in schedules/ to match Odoo names.

Pulls Odoo employees, builds a best-effort mapping from current local
roster names to Odoo names (exact match → fuzzy match), prints a preview,
and on confirmation:
  1. Backs up schedules/ to schedules.bak/
  2. Rewrites every JSON file under schedules/ using the mapping
  3. Replaces roster.json with the Odoo-derived version

Run from the project root:
  python -m scripts.migrate_schedule_names_to_odoo
"""

from __future__ import annotations

import json
import shutil
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from zira_dashboard import odoo_sync, staffing  # noqa: E402

ROSTER_PATH = ROOT / "roster.json"
ROSTER_NEXT = ROOT / "roster.json.next"
SCHEDULES = ROOT / "schedules"
SCHEDULES_BAK = ROOT / "schedules.bak"


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _build_mapping(local_names: list[str], odoo_names: list[str]) -> dict[str, str]:
    """Return {local_name: odoo_name} via exact-then-fuzzy match. Locals
    with no plausible Odoo match are absent from the result."""
    odoo_lower = {n.lower().strip(): n for n in odoo_names}
    mapping: dict[str, str] = {}
    for local in local_names:
        key = local.lower().strip()
        if key in odoo_lower:
            mapping[local] = odoo_lower[key]
            continue
        # Fuzzy: find best match if similarity >= 0.7 OR shared 5-prefix
        best = max(
            ((n, _similarity(local, n)) for n in odoo_names),
            key=lambda p: p[1], default=(None, 0.0),
        )
        if best[0] and (best[1] >= 0.7 or local[:5].lower() == best[0][:5].lower()):
            mapping[local] = best[0]
    return mapping


def main() -> int:
    if SCHEDULES_BAK.exists():
        print(f"ERROR: {SCHEDULES_BAK} already exists. The migration has already been run.")
        print("       Remove it first if you really want to re-run (you'll lose the original backup).")
        return 1
    if not SCHEDULES.exists():
        print(f"ERROR: {SCHEDULES} does not exist. Nothing to migrate.")
        return 1

    print("Running fresh sync to roster.json.next ...")
    # Redirect sync's roster path temporarily to roster.json.next.
    odoo_sync.ROSTER_PATH = ROSTER_NEXT
    odoo_sync.LAST_SYNC_PATH = ROOT / ".odoo_last_sync_migrate"
    result = odoo_sync.sync(force=True)
    if not result.ok:
        print(f"ERROR: Odoo sync failed: {result.error}")
        return 1
    print(f"OK: pulled {result.employee_count} employees, {result.skill_column_count} skill columns.")

    odoo_roster = json.loads(ROSTER_NEXT.read_text())
    odoo_names = [r["name"] for r in odoo_roster]
    local_roster = []
    if ROSTER_PATH.exists():
        local_roster = json.loads(ROSTER_PATH.read_text())
    local_names = [r["name"] for r in local_roster]
    mapping = _build_mapping(local_names, odoo_names)

    matched = sorted(mapping.items())
    new_in_odoo = sorted(n for n in odoo_names if n not in mapping.values())
    missing_local = sorted(n for n in local_names if n not in mapping)

    print("\n=== Proposed mapping ===")
    for old, new in matched:
        print(f"  {old!r} -> {new!r}")
    print(f"\n=== New in Odoo ({len(new_in_odoo)}) ===")
    for n in new_in_odoo:
        print(f"  + {n}")
    print(f"\n=== Local-only (not in Odoo, will not appear in future schedules) ({len(missing_local)}) ===")
    for n in missing_local:
        print(f"  - {n}")

    print("\nProceed with migration? Type 'yes' to continue.")
    if input("> ").strip().lower() != "yes":
        print("Aborted.")
        ROSTER_NEXT.unlink(missing_ok=True)
        return 1

    print(f"Backing up {SCHEDULES} -> {SCHEDULES_BAK} ...")
    shutil.copytree(SCHEDULES, SCHEDULES_BAK)

    rewritten = 0
    for path in SCHEDULES.rglob("*.json"):
        content = path.read_text()
        new_content = content
        for old, new in mapping.items():
            # JSON-escaped name occurrences. Names appear as JSON strings.
            new_content = new_content.replace(f'"{old}"', f'"{new}"')
        if new_content != content:
            path.write_text(new_content)
            rewritten += 1

    print(f"Rewrote {rewritten} schedule files.")
    ROSTER_NEXT.replace(ROSTER_PATH)
    print(f"Replaced {ROSTER_PATH}.")
    print("\nMigration complete. Review schedules/ and commit when ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Manual dry-run guidance**

The script is interactive — after env vars are set on the dev box, run:

```bash
python -m scripts.migrate_schedule_names_to_odoo
```

Review the preview output carefully before typing `yes`. The `schedules.bak/` directory is the safety net.

- [ ] **Step 4: Commit**

```bash
git add scripts/migrate_schedule_names_to_odoo.py .gitignore
git commit -m "feat(odoo): one-time schedule-name migration script"
```

---

### Task 9: Setup runbook

**Files:**
- Create: `docs/odoo-setup.md`

- [ ] **Step 1: Write the doc**

```markdown
# Odoo Integration Setup

The People Matrix pulls from Odoo (`hr_skills` module). Source of truth is
Odoo; the dashboard caches in `roster.json` with a 1-hour TTL.

## Required env vars (Railway)

- `ODOO_URL` — base URL like `https://gruber-pallets.odoo.com` (no `/odoo`)
- `ODOO_DB` — database name (e.g. `Production`)
- `ODOO_LOGIN` — username (email)
- `ODOO_API_KEY` — API key from Odoo Settings → Users → Account Security
  → New API Key

Set in Railway → Variables. Never commit.

## Skill type setup in Odoo

The dashboard reads two skill types: **"Production"** and **"Supervisor"**.
Skills under those types become matrix columns. Levels under each type
are bucketed to 0–3 by rank (lowest level = 0, highest = 3).

To add a column to the matrix: add a skill to one of those types in Odoo.

To add a level: add a level to the type in Odoo. The bucket math
re-distributes 0–3 across all levels in rank order.

## First-time migration

After env vars are set, run the schedule-name migration once:

```bash
python -m scripts.migrate_schedule_names_to_odoo
```

This pulls Odoo employees, proposes a mapping from current local names to
Odoo names, and on your confirmation, rewrites every file under
`schedules/`. The original schedules are backed up to `schedules.bak/`.

## Routine refresh

The matrix auto-refreshes once an hour on first page load. Click
**"Refresh from Odoo"** at the top of the matrix to force an immediate
sync.

## What if Odoo is down?

The dashboard serves the last-cached `roster.json` and shows a warning
banner. Refresh button retries on demand.
```

- [ ] **Step 2: Commit**

```bash
git add docs/odoo-setup.md
git commit -m "docs(odoo): setup + migration runbook"
```

---

### Task 10: End-to-end smoke test against live Odoo

**Files:**
- (No code change — manual verification step)

- [ ] **Step 1: Set env vars locally**

Export the four `ODOO_*` env vars in your shell (or use a `.env` loader).
Use a freshly rotated API key.

- [ ] **Step 2: Run a one-shot sync from a Python REPL**

```bash
python -c "from zira_dashboard import odoo_sync; print(odoo_sync.sync(force=True))"
```

Expected: `SyncResult(ok=True, refreshed=True, employee_count=N, skill_column_count=M, ...)` where N and M match what's in Odoo.

If field paths in the Odoo response don't match what the client expects,
adjust `odoo_client.py` based on actual response shapes and re-run.

- [ ] **Step 3: Run the migration script (still local)**

```bash
python -m scripts.migrate_schedule_names_to_odoo
```

Review the proposed mapping. If anything looks wrong, type `no` and tune
the mapping logic before re-running. Once you confirm, the schedules and
roster are updated.

- [ ] **Step 4: Start the dev server**

```bash
python -m uvicorn zira_dashboard.app:app --reload
```

Hit `/staffing/skills`. Confirm:

- "Last synced" timestamp is today
- Matrix shows Odoo employees, sorted by name
- Skill cells render with bucketed levels
- Hover reveals the ↗ Odoo link per row; click it opens the Odoo employee
- Click "Refresh from Odoo" — page reloads with a new timestamp

- [ ] **Step 5: Commit any field-shape fixes from Step 2**

If you adjusted `odoo_client.py` based on real Odoo responses:

```bash
git add src/zira_dashboard/odoo_client.py
git commit -m "fix(odoo): align field paths with live API responses"
```

- [ ] **Step 6: Push to Railway**

Set the four env vars on Railway and push. Verify the production
`/staffing/skills` works the same way.

---

## Done criteria

- All 10 tasks committed; `pytest tests/ -v` is green.
- Live Odoo sync works against your instance (Step 2 + 4 of Task 10).
- `schedules/` rewritten to use Odoo names; `schedules.bak/` is safe to
  keep around as a one-shot rollback.
- Matrix is view-only for skill cells, editable for `reserve`, badge-only
  for `active`. Refresh button works. Hover reveals Odoo links.
- Railway env vars set; production page renders correctly with cache
  fallback when Odoo blips.
