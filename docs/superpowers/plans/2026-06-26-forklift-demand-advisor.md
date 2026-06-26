# Forklift Demand Advisor (Stage 1, Increment A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Snapshot forklift demand + per-driver performance from gpiforklift.com daily, and show a read-only "forklift demand advisor" card in the plant scheduler with a recommended dedicated-driver count and an overload/neglect coverage check.

**Architecture:** A small REST client (`forklift_client`) reads the gpiforklift.com API. Pure transforms (`forklift_ingest`) turn the API payloads into two daily snapshot rows; `forklift_store` UPSERTs/reads them (mirroring `production_daily`). Pure `forklift_demand` predicts the next working day's demand and sizes drivers. An in-process warmer (`forklift_snapshot.snapshot_today`) keeps today's snapshot fresh. `forklift_advisor.build_advisor` assembles the render model, which `staffing_page` injects into a card in the scheduler's right rail under Notes. All new modules follow existing patterns (`slack_client`, `precompute`, `auto_lunch_settings`, the `_WARMERS` registry).

**Tech Stack:** Python 3.12, FastAPI, Jinja2, raw psycopg2 (`db.py` helpers + `execute_values`), `requests`, pytest + `unittest.mock`.

**Scope:** Increment A only (foundation + read-only advisor). Increment B (people-level "dedicate Juan" suggestions) and Stage 2 (leaderboards/trophies) are follow-on plans that reuse `forklift_driver_daily` and `forklift_store`. No writes to gpiforklift.com.

**Spec:** `docs/superpowers/specs/2026-06-26-forklift-demand-staffing-design.md`

---

## File Structure

**New files:**
- `src/zira_dashboard/forklift_client.py` — REST client (env key, GET-only, `ForkliftError`).
- `src/zira_dashboard/forklift_ingest.py` — pure transforms: API payloads → snapshot row dicts.
- `src/zira_dashboard/forklift_store.py` — UPSERT/read the snapshot tables + name-map + overload-responder list.
- `src/zira_dashboard/forklift_demand.py` — pure prediction / recommendation / coverage.
- `src/zira_dashboard/forklift_snapshot.py` — orchestration: fetch → ingest → store (called by the warmer).
- `src/zira_dashboard/forklift_advisor.py` — assemble the scheduler render model.
- `tests/test_forklift_client.py`, `tests/test_forklift_ingest.py`, `tests/test_forklift_store.py`, `tests/test_forklift_demand.py`, `tests/test_forklift_snapshot.py`, `tests/test_forklift_advisor.py`, `tests/test_staffing_forklift_card.py`.

**Modified files:**
- `.env.example` — register `FORKLIFT_API_KEY` / `FORKLIFT_BASE_URL`.
- `src/zira_dashboard/_schema.py` — append three `CREATE TABLE IF NOT EXISTS` blocks to `SCHEMA_DDL`.
- `src/zira_dashboard/app.py` — add `_tick_forklift` and register it in `_WARMERS`.
- `src/zira_dashboard/routes/staffing.py` — build `forklift_advisor` and add it to the render context.
- `src/zira_dashboard/templates/staffing.html` — render the advisor card inside the `.day-context` aside.
- `.github/workflows/tests.yml` — add dummy `FORKLIFT_API_KEY` (only if a module import requires it; it does not by default — see Task 2).

**Cross-task contracts (keep these names/shapes consistent):**
- `forklift_calls_daily` row dict keys: `day, total_calls, urgent_calls, overload_count, neglected_count, by_hour, by_station, by_skill`.
- `forklift_driver_daily` row dict keys: `day, driver_id, name, calls, on_time, late, avg_ms, max_ms, utilization_pct, on_call_ms, available_ms`.
- `forklift_demand.DemandForecast(total_calls: float, by_hour: dict[int, float], peak_hour: int | None, peak_calls: float, basis: str, n_days: int)`.
- `forklift_demand.Coverage(status: str, recommended: int, dedicated: int, certified: int, backups: int, gap: int)`.
- Advisor render dict keys (consumed by the template): `available, day_label, total_calls, peak_label, hours (list[(hour, frac)]), recommended, coverage, basis, n_days, backup_names`.

---

## Task 1: Register config (env vars)

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Add a Forklift block to `.env.example`**

Append this block (match the existing `# ---- Zira ----` style):

```bash
# ---- Forklift (gpiforklift.com) ----
# Read-only integration: forklift call-queue demand + driver performance.
# NOTE: the forklift API currently serves reads without auth; the key is sent
# as a header anyway for forward-compatibility.
FORKLIFT_API_KEY=
FORKLIFT_BASE_URL=https://www.gpiforklift.com
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "chore(forklift): register FORKLIFT_API_KEY/FORKLIFT_BASE_URL in .env.example"
```

---

## Task 2: `forklift_client.py` — REST client

**Files:**
- Create: `src/zira_dashboard/forklift_client.py`
- Test: `tests/test_forklift_client.py`

Modeled on `slack_client.py`: env-var config, `requests` with explicit timeout + `raise_for_status`, a custom error. Module import must NOT require the env var (read it per-call), so CI needs no dummy.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_forklift_client.py
from unittest.mock import MagicMock

import pytest

from zira_dashboard import forklift_client


def _json_response(body):
    r = MagicMock()
    r.json.return_value = body
    r.raise_for_status.return_value = None
    return r


def test_fetch_drivers_calls_api_path_and_returns_json(monkeypatch):
    monkeypatch.setenv("FORKLIFT_BASE_URL", "https://fk.example")
    monkeypatch.setenv("FORKLIFT_API_KEY", "gpifk__test")
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return _json_response([{"id": "fk-1", "name": "Trent"}])

    monkeypatch.setattr(forklift_client.requests, "get", fake_get)

    drivers = forklift_client.fetch_drivers()

    assert drivers == [{"id": "fk-1", "name": "Trent"}]
    assert captured["url"] == "https://fk.example/api/drivers"
    assert captured["headers"]["X-API-Key"] == "gpifk__test"


def test_default_base_url_when_unset(monkeypatch):
    monkeypatch.delenv("FORKLIFT_BASE_URL", raising=False)
    monkeypatch.delenv("FORKLIFT_API_KEY", raising=False)
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        return _json_response({"driverLeaderboard": []})

    monkeypatch.setattr(forklift_client.requests, "get", fake_get)

    forklift_client.fetch_dashboard()
    assert captured["url"] == "https://www.gpiforklift.com/api/dashboard"


def test_http_error_is_wrapped_in_forklift_error(monkeypatch):
    monkeypatch.setenv("FORKLIFT_BASE_URL", "https://fk.example")

    def fake_get(url, **kwargs):
        r = MagicMock()
        r.raise_for_status.side_effect = RuntimeError("boom")
        return r

    monkeypatch.setattr(forklift_client.requests, "get", fake_get)
    with pytest.raises(forklift_client.ForkliftError):
        forklift_client.fetch_drivers()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_client.py -v`
Expected: FAIL (`ModuleNotFoundError: zira_dashboard.forklift_client`).

- [ ] **Step 3: Write the implementation**

```python
# src/zira_dashboard/forklift_client.py
"""Read-only REST client for the GPI Forklift app (gpiforklift.com).

The forklift app runs a call-and-dispatch queue; this client pulls demand and
driver-performance data into the Plant Manager. GET-only — we never write.

Config (read per-call, so importing this module has no side effects):
  FORKLIFT_API_KEY   - sent as the X-API-Key header (best-effort; the API
                       currently serves reads without auth).
  FORKLIFT_BASE_URL  - defaults to https://www.gpiforklift.com
"""
from __future__ import annotations

import os
from typing import Any

import requests

DEFAULT_BASE_URL = "https://www.gpiforklift.com"
_TIMEOUT = 15


class ForkliftError(Exception):
    """Raised on any forklift API failure."""


def _base_url() -> str:
    return (os.environ.get("FORKLIFT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _get(path: str) -> Any:
    """GET {base}{path} with the API key header; return parsed JSON.
    Wraps any transport/HTTP error in ForkliftError."""
    url = f"{_base_url()}{path}"
    headers = {}
    key = os.environ.get("FORKLIFT_API_KEY")
    if key:
        headers["X-API-Key"] = key
    try:
        r = requests.get(url, headers=headers, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001 - normalize to one error type
        raise ForkliftError(f"GET {path} failed: {e}") from e


def fetch_dashboard() -> dict:
    """Today's precomputed analytics: driverLeaderboard, hourlyClaimAvgs, etc."""
    return _get("/api/dashboard")


def fetch_queue_history() -> list[dict]:
    """Today's call records (the API only exposes 'today')."""
    return _get("/api/queue/history")


def fetch_drivers() -> list[dict]:
    """Forklift drivers: {id, name, isOverloadResponder, skills}."""
    return _get("/api/drivers")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_client.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/forklift_client.py tests/test_forklift_client.py
git commit -m "feat(forklift): add read-only REST client for gpiforklift.com"
```

---

## Task 3: Schema — snapshot + mapping tables

**Files:**
- Modify: `src/zira_dashboard/_schema.py` (append to the `SCHEMA_DDL` string, before its closing `"""`)
- Test: `tests/test_forklift_store.py` (schema-bootstrap check; DB-gated)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forklift_store.py
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs DATABASE_URL"
)


def test_schema_creates_forklift_tables():
    from zira_dashboard import db
    db.bootstrap_schema()
    rows = db.query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name IN "
        "('forklift_calls_daily','forklift_driver_daily','forklift_name_map')"
    )
    names = {r["table_name"] for r in rows}
    assert names == {"forklift_calls_daily", "forklift_driver_daily", "forklift_name_map"}
```

- [ ] **Step 2: Run test to verify it fails (or skips locally)**

Run: `.venv/bin/python -m pytest tests/test_forklift_store.py::test_schema_creates_forklift_tables -v`
Expected: SKIP locally (no `DATABASE_URL`); FAIL in CI (tables don't exist yet).

- [ ] **Step 3: Append the DDL to `SCHEMA_DDL`**

Add this block to the `SCHEMA_DDL` string in `_schema.py` (place it after the `production_daily` section, before the closing `"""`):

```sql
-- Forklift integration (gpiforklift.com) -------------------------------
-- Daily snapshots of forklift demand + per-driver performance. The API
-- only exposes "today", so a warmer writes one row per day and history
-- accumulates here (mirrors production_daily).
CREATE TABLE IF NOT EXISTS forklift_calls_daily (
  day              DATE PRIMARY KEY,
  total_calls      INTEGER NOT NULL DEFAULT 0,
  urgent_calls     INTEGER NOT NULL DEFAULT 0,
  overload_count   INTEGER NOT NULL DEFAULT 0,
  neglected_count  INTEGER NOT NULL DEFAULT 0,
  by_hour          JSONB NOT NULL DEFAULT '{}'::jsonb,
  by_station       JSONB NOT NULL DEFAULT '{}'::jsonb,
  by_skill         JSONB NOT NULL DEFAULT '{}'::jsonb,
  computed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS forklift_driver_daily (
  day              DATE NOT NULL,
  driver_id        TEXT NOT NULL,
  name             TEXT NOT NULL,
  calls            INTEGER NOT NULL DEFAULT 0,
  on_time          INTEGER NOT NULL DEFAULT 0,
  late             INTEGER NOT NULL DEFAULT 0,
  avg_ms           BIGINT NOT NULL DEFAULT 0,
  max_ms           BIGINT NOT NULL DEFAULT 0,
  utilization_pct  NUMERIC NOT NULL DEFAULT 0,
  on_call_ms       BIGINT NOT NULL DEFAULT 0,
  available_ms     BIGINT NOT NULL DEFAULT 0,
  computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, driver_id)
);
CREATE INDEX IF NOT EXISTS idx_forklift_driver_daily_name_day
  ON forklift_driver_daily (name, day);
-- Override map for the few forklift names that don't match the plant roster
-- (driver -> plant person) or work centers (workstation -> WC).
CREATE TABLE IF NOT EXISTS forklift_name_map (
  kind           TEXT NOT NULL,   -- 'driver' | 'workstation'
  forklift_name  TEXT NOT NULL,
  plant_name     TEXT NOT NULL,
  PRIMARY KEY (kind, forklift_name)
);
```

- [ ] **Step 4: Run test in CI / against a DB**

Run (if a local `DATABASE_URL` is available): `.venv/bin/python -m pytest tests/test_forklift_store.py::test_schema_creates_forklift_tables -v`
Expected: PASS (or remains SKIP locally; verified in CI).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/_schema.py tests/test_forklift_store.py
git commit -m "feat(forklift): add forklift_calls_daily/driver_daily/name_map tables"
```

---

## Task 4: `forklift_ingest.py` — pure payload transforms

**Files:**
- Create: `src/zira_dashboard/forklift_ingest.py`
- Test: `tests/test_forklift_ingest.py`

Pure functions (no I/O) turning API payloads into snapshot row dicts. Easy to unit-test with the real shapes from the spec's API map.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_forklift_ingest.py
from datetime import date

from zira_dashboard import forklift_ingest

DASHBOARD = {
    "driverLeaderboard": [
        {"driverId": "fk-1", "name": "Trent", "total": 86, "onTime": 85, "late": 1,
         "avgMs": 190000, "maxMs": 700000, "utilizationPct": 95,
         "totalOnCallMs": 17000000, "availableMs": 17900000},
    ],
    "hourlyClaimAvgs": [
        {"slot": 8, "avgMinutes": 3.0, "calls": 40, "overloadCount": 0, "neglectedCount": 0},
        {"slot": 9, "avgMinutes": 5.0, "calls": 70, "overloadCount": 2, "neglectedCount": 1},
    ],
}
HISTORY = [
    {"workstationName": "Prosaw #4", "requiredSkillId": "sk-2", "priority": "urgent", "status": "completed"},
    {"workstationName": "Prosaw #4", "requiredSkillId": "sk-2", "priority": "normal", "status": "completed"},
    {"workstationName": "Junior #3", "requiredSkillId": "sk-1", "priority": "normal", "status": "completed"},
    {"workstationName": "Junior #3", "requiredSkillId": "sk-1", "priority": "normal", "status": "canceled"},
]


def test_build_calls_daily_aggregates_history_and_hours():
    row = forklift_ingest.build_calls_daily(date(2026, 6, 26), DASHBOARD, HISTORY)
    assert row["day"] == date(2026, 6, 26)
    assert row["total_calls"] == 3          # completed only
    assert row["urgent_calls"] == 1
    assert row["by_station"] == {"Prosaw #4": 2, "Junior #3": 1}
    assert row["by_skill"] == {"sk-2": 2, "sk-1": 1}
    assert row["overload_count"] == 2       # summed across hourly slots
    assert row["neglected_count"] == 1
    assert row["by_hour"]["9"]["calls"] == 70


def test_build_driver_daily_maps_leaderboard_rows():
    rows = forklift_ingest.build_driver_daily(date(2026, 6, 26), DASHBOARD)
    assert len(rows) == 1
    r = rows[0]
    assert r["driver_id"] == "fk-1"
    assert r["name"] == "Trent"
    assert r["calls"] == 86 and r["on_time"] == 85 and r["late"] == 1
    assert r["avg_ms"] == 190000 and r["utilization_pct"] == 95
    assert r["on_call_ms"] == 17000000 and r["available_ms"] == 17900000


def test_build_calls_daily_handles_empty_payloads():
    row = forklift_ingest.build_calls_daily(date(2026, 6, 26), {}, [])
    assert row["total_calls"] == 0 and row["by_station"] == {} and row["by_hour"] == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_ingest.py -v`
Expected: FAIL (`ModuleNotFoundError: zira_dashboard.forklift_ingest`).

- [ ] **Step 3: Write the implementation**

```python
# src/zira_dashboard/forklift_ingest.py
"""Pure transforms: gpiforklift.com API payloads -> snapshot row dicts.

No I/O. Keys match the forklift_calls_daily / forklift_driver_daily columns.
JSONB hour keys are stored as strings (slot number) for stable round-tripping.
"""
from __future__ import annotations

from collections import Counter
from datetime import date


def build_calls_daily(day: date, dashboard: dict, history: list[dict]) -> dict:
    completed = [c for c in history if c.get("status") == "completed"]
    by_station = Counter(c.get("workstationName") for c in completed if c.get("workstationName"))
    by_skill = Counter(c.get("requiredSkillId") for c in completed if c.get("requiredSkillId"))
    urgent = sum(1 for c in completed if c.get("priority") == "urgent")

    by_hour: dict[str, dict] = {}
    overload = neglected = 0
    for slot in (dashboard or {}).get("hourlyClaimAvgs", []) or []:
        key = str(slot.get("slot"))
        by_hour[key] = {
            "calls": int(slot.get("calls") or 0),
            "overload": int(slot.get("overloadCount") or 0),
            "neglected": int(slot.get("neglectedCount") or 0),
            "avg_minutes": float(slot.get("avgMinutes") or 0),
        }
        overload += int(slot.get("overloadCount") or 0)
        neglected += int(slot.get("neglectedCount") or 0)

    return {
        "day": day,
        "total_calls": len(completed),
        "urgent_calls": urgent,
        "overload_count": overload,
        "neglected_count": neglected,
        "by_hour": by_hour,
        "by_station": dict(by_station),
        "by_skill": dict(by_skill),
    }


def build_driver_daily(day: date, dashboard: dict) -> list[dict]:
    rows = []
    for d in (dashboard or {}).get("driverLeaderboard", []) or []:
        rows.append({
            "day": day,
            "driver_id": str(d.get("driverId") or d.get("name")),
            "name": d.get("name") or "",
            "calls": int(d.get("total") or 0),
            "on_time": int(d.get("onTime") or 0),
            "late": int(d.get("late") or 0),
            "avg_ms": int(d.get("avgMs") or 0),
            "max_ms": int(d.get("maxMs") or 0),
            "utilization_pct": float(d.get("utilizationPct") or 0),
            "on_call_ms": int(d.get("totalOnCallMs") or 0),
            "available_ms": int(d.get("availableMs") or 0),
        })
    return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_ingest.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/forklift_ingest.py tests/test_forklift_ingest.py
git commit -m "feat(forklift): add pure payload->snapshot transforms"
```

---

## Task 5: `forklift_store.py` — UPSERT + read snapshots

**Files:**
- Create: `src/zira_dashboard/forklift_store.py`
- Test: append to `tests/test_forklift_store.py` (DB-gated)

- [ ] **Step 1: Write the failing tests (append to `tests/test_forklift_store.py`)**

```python
from datetime import date

from zira_dashboard import forklift_store


def test_upsert_and_read_calls_daily_roundtrip():
    from zira_dashboard import db
    db.bootstrap_schema()
    day = date(2026, 6, 24)  # a Wednesday
    db.execute("DELETE FROM forklift_calls_daily WHERE day = %s", (day,))
    row = {"day": day, "total_calls": 400, "urgent_calls": 30,
           "overload_count": 5, "neglected_count": 2,
           "by_hour": {"9": {"calls": 70}}, "by_station": {"Prosaw #4": 120},
           "by_skill": {"sk-2": 260}}
    forklift_store.upsert_calls_daily(row)
    forklift_store.upsert_calls_daily({**row, "total_calls": 410})  # idempotent update

    got = forklift_store.calls_daily_for_weekday(2, limit=10)  # 2 == Wednesday
    mine = [r for r in got if r["day"] == day]
    assert mine and mine[0]["total_calls"] == 410
    assert mine[0]["by_hour"]["9"]["calls"] == 70


def test_name_map_overrides():
    from zira_dashboard import db
    db.bootstrap_schema()
    db.execute("DELETE FROM forklift_name_map WHERE forklift_name = %s", ("Luke",))
    db.execute(
        "INSERT INTO forklift_name_map (kind, forklift_name, plant_name) "
        "VALUES ('driver', 'Luke', 'Luke Gruber')"
    )
    assert forklift_store.name_map("driver")["Luke"] == "Luke Gruber"
```

- [ ] **Step 2: Run tests to verify they fail (or skip locally)**

Run: `.venv/bin/python -m pytest tests/test_forklift_store.py -v`
Expected: SKIP locally; FAIL in CI (`forklift_store` missing).

- [ ] **Step 3: Write the implementation**

```python
# src/zira_dashboard/forklift_store.py
"""Read/write the forklift snapshot tables. Mirrors the precompute/store
patterns: execute_values batch UPSERT for drivers, single-row UPSERT for the
day summary, plain reads for the advisor. JSONB columns round-trip via
psycopg2's Json adapter."""
from __future__ import annotations

import json
from datetime import date

from psycopg2.extras import Json


def upsert_calls_daily(row: dict) -> None:
    from . import db
    db.execute(
        """
        INSERT INTO forklift_calls_daily (
            day, total_calls, urgent_calls, overload_count, neglected_count,
            by_hour, by_station, by_skill, computed_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (day) DO UPDATE SET
            total_calls=EXCLUDED.total_calls, urgent_calls=EXCLUDED.urgent_calls,
            overload_count=EXCLUDED.overload_count,
            neglected_count=EXCLUDED.neglected_count,
            by_hour=EXCLUDED.by_hour, by_station=EXCLUDED.by_station,
            by_skill=EXCLUDED.by_skill, computed_at=now()
        """,
        (row["day"], row["total_calls"], row["urgent_calls"],
         row["overload_count"], row["neglected_count"],
         Json(row["by_hour"]), Json(row["by_station"]), Json(row["by_skill"])),
    )


def upsert_driver_daily(rows: list[dict]) -> int:
    from . import db
    rows = list(rows)
    if not rows:
        return 0
    sql = """
        INSERT INTO forklift_driver_daily (
            day, driver_id, name, calls, on_time, late, avg_ms, max_ms,
            utilization_pct, on_call_ms, available_ms, computed_at
        ) VALUES %s
        ON CONFLICT (day, driver_id) DO UPDATE SET
            name=EXCLUDED.name, calls=EXCLUDED.calls, on_time=EXCLUDED.on_time,
            late=EXCLUDED.late, avg_ms=EXCLUDED.avg_ms, max_ms=EXCLUDED.max_ms,
            utilization_pct=EXCLUDED.utilization_pct,
            on_call_ms=EXCLUDED.on_call_ms, available_ms=EXCLUDED.available_ms,
            computed_at=now()
    """
    with db.cursor() as cur:
        db.execute_values(cur, sql, [
            (r["day"], r["driver_id"], r["name"], r["calls"], r["on_time"],
             r["late"], r["avg_ms"], r["max_ms"], r["utilization_pct"],
             r["on_call_ms"], r["available_ms"])
            for r in rows
        ], template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())")
    return len(rows)


def _coerce_json(value):
    """psycopg2 returns JSONB as dict already; tolerate str just in case."""
    return json.loads(value) if isinstance(value, str) else (value or {})


def calls_daily_for_weekday(weekday: int, limit: int = 8) -> list[dict]:
    """Most-recent `limit` snapshots whose day-of-week == weekday (Mon=0)."""
    from . import db
    rows = db.query(
        "SELECT * FROM forklift_calls_daily "
        "WHERE EXTRACT(ISODOW FROM day) = %s "   # ISODOW: Mon=1..Sun=7
        "ORDER BY day DESC LIMIT %s",
        (weekday + 1, limit),
    )
    for r in rows:
        r["by_hour"] = _coerce_json(r["by_hour"])
        r["by_station"] = _coerce_json(r["by_station"])
        r["by_skill"] = _coerce_json(r["by_skill"])
    return rows


def name_map(kind: str) -> dict[str, str]:
    from . import db
    rows = db.query(
        "SELECT forklift_name, plant_name FROM forklift_name_map WHERE kind = %s",
        (kind,),
    )
    return {r["forklift_name"]: r["plant_name"] for r in rows}
```

- [ ] **Step 4: Run tests (CI / DB)**

Run: `.venv/bin/python -m pytest tests/test_forklift_store.py -v`
Expected: PASS where `DATABASE_URL` is set (CI); SKIP locally.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/forklift_store.py tests/test_forklift_store.py
git commit -m "feat(forklift): add snapshot store (upsert + weekday reads + name map)"
```

---

## Task 6: `forklift_demand.py` — predict, recommend, coverage

**Files:**
- Create: `src/zira_dashboard/forklift_demand.py`
- Test: `tests/test_forklift_demand.py`

Pure logic. v1: median total over recent same-weekday snapshots; mean per-hour shape; recommend = ceil(peak / throughput); coverage compares to dedicated count.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_forklift_demand.py
import math

from zira_dashboard import forklift_demand as fd


def _snap(total, by_hour):
    return {"total_calls": total, "by_hour": by_hour, "by_station": {}}


def test_predict_from_history_uses_median_and_peak():
    snaps = [
        _snap(400, {"8": {"calls": 30}, "9": {"calls": 70}}),
        _snap(420, {"8": {"calls": 40}, "9": {"calls": 60}}),
        _snap(440, {"8": {"calls": 50}, "9": {"calls": 80}}),
    ]
    f = fd.predict_from_history(snaps)
    assert f.total_calls == 420            # median of 400,420,440
    assert f.peak_hour == 9
    assert f.peak_calls == 70              # mean of 70,60,80
    assert f.basis == "history" and f.n_days == 3


def test_predict_from_history_empty_returns_zero_basis_none():
    f = fd.predict_from_history([])
    assert f.total_calls == 0 and f.basis == "none" and f.peak_hour is None


def test_bootstrap_from_trends_divides_week_by_operating_days():
    trends = {"weeks": [
        {"claimedCalls": 2000}, {"claimedCalls": 2100},
    ]}
    f = fd.bootstrap_from_trends(trends, operating_days=5)
    # mean weekly = 2050 -> per day = 410
    assert f.total_calls == 410 and f.basis == "bootstrap"


def test_recommend_drivers_ceils_peak_over_throughput():
    assert fd.recommend_drivers(peak_calls=70, throughput_per_hour=30) == 3
    assert fd.recommend_drivers(peak_calls=0, throughput_per_hour=30) == 1  # floor of 1


def test_assess_coverage_ok_and_short():
    ok = fd.assess_coverage(recommended=3, dedicated=3, certified=5, backups=3)
    assert ok.status == "ok" and ok.gap == 0
    short = fd.assess_coverage(recommended=4, dedicated=2, certified=5, backups=3)
    assert short.status == "short" and short.gap == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_demand.py -v`
Expected: FAIL (`ModuleNotFoundError: zira_dashboard.forklift_demand`).

- [ ] **Step 3: Write the implementation**

```python
# src/zira_dashboard/forklift_demand.py
"""Pure forklift demand prediction + driver recommendation + coverage check.

v1 model (calibrated as history accumulates):
  - predict next working day from recent same-weekday snapshots (median total,
    mean per-hour shape); fall back to bootstrapping from the app's weekly
    trends when there is no same-weekday history yet.
  - recommend = ceil(busiest-hour calls / per-driver hourly throughput), min 1.
  - coverage compares the recommendation to dedicated drivers scheduled.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median

# Default per-driver throughput (calls/hour). Derived loosely from observed
# data (~70 calls in the busiest hour handled by ~2-3 drivers). Override-able
# later via settings; calibrated against overload/neglect history.
DEFAULT_THROUGHPUT_PER_HOUR = 30.0


@dataclass
class DemandForecast:
    total_calls: float = 0.0
    by_hour: dict[int, float] = field(default_factory=dict)
    peak_hour: int | None = None
    peak_calls: float = 0.0
    basis: str = "none"          # 'history' | 'bootstrap' | 'none'
    n_days: int = 0


@dataclass
class Coverage:
    status: str                   # 'ok' | 'short'
    recommended: int
    dedicated: int
    certified: int
    backups: int
    gap: int


def predict_from_history(snapshots: list[dict]) -> DemandForecast:
    if not snapshots:
        return DemandForecast()
    totals = [float(s.get("total_calls") or 0) for s in snapshots]
    # mean calls per hour-slot across snapshots
    sums: dict[int, float] = {}
    for s in snapshots:
        for slot, payload in (s.get("by_hour") or {}).items():
            hour = int(slot)
            calls = float((payload or {}).get("calls") or 0)
            sums[hour] = sums.get(hour, 0.0) + calls
    by_hour = {h: round(v / len(snapshots), 1) for h, v in sums.items()}
    peak_hour = max(by_hour, key=by_hour.get) if by_hour else None
    peak_calls = by_hour[peak_hour] if peak_hour is not None else 0.0
    return DemandForecast(
        total_calls=median(totals), by_hour=by_hour,
        peak_hour=peak_hour, peak_calls=peak_calls,
        basis="history", n_days=len(snapshots),
    )


def bootstrap_from_trends(weekly_trends: dict, operating_days: int = 5) -> DemandForecast:
    weeks = (weekly_trends or {}).get("weeks") or []
    claimed = [float(w.get("claimedCalls") or 0) for w in weeks if w.get("claimedCalls")]
    if not claimed or operating_days <= 0:
        return DemandForecast()
    per_day = (sum(claimed) / len(claimed)) / operating_days
    return DemandForecast(total_calls=round(per_day, 1), basis="bootstrap", n_days=0)


def recommend_drivers(peak_calls: float, throughput_per_hour: float = DEFAULT_THROUGHPUT_PER_HOUR) -> int:
    if throughput_per_hour <= 0:
        return 1
    return max(1, math.ceil(peak_calls / throughput_per_hour))


def assess_coverage(recommended: int, dedicated: int, certified: int, backups: int) -> Coverage:
    gap = max(0, recommended - dedicated)
    return Coverage(
        status="ok" if gap == 0 else "short",
        recommended=recommended, dedicated=dedicated,
        certified=certified, backups=backups, gap=gap,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_demand.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/forklift_demand.py tests/test_forklift_demand.py
git commit -m "feat(forklift): add pure demand prediction + recommendation + coverage"
```

---

## Task 7: `forklift_snapshot.py` — orchestration (fetch → ingest → store)

**Files:**
- Create: `src/zira_dashboard/forklift_snapshot.py`
- Test: `tests/test_forklift_snapshot.py`

Thin orchestrator the warmer calls. Also persists the current overload-responder driver names to `app_settings` (the advisor's "backups" list) — reuses the generic JSON config store.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forklift_snapshot.py
from datetime import date

from zira_dashboard import forklift_snapshot


def test_snapshot_today_fetches_transforms_and_stores(monkeypatch):
    calls = {}
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_dashboard",
                        lambda: {"driverLeaderboard": [
                            {"driverId": "fk-1", "name": "Trent", "total": 10}],
                            "hourlyClaimAvgs": [{"slot": 9, "calls": 5}]})
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_queue_history",
                        lambda: [{"workstationName": "Prosaw #4", "status": "completed",
                                  "priority": "normal", "requiredSkillId": "sk-2"}])
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_drivers",
                        lambda: [{"name": "Louie", "isOverloadResponder": True},
                                 {"name": "Trent", "isOverloadResponder": False}])
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_calls_daily",
                        lambda row: calls.setdefault("calls", row))
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_driver_daily",
                        lambda rows: calls.setdefault("drivers", rows) or len(rows))
    saved = {}
    monkeypatch.setattr(forklift_snapshot.app_settings, "set_setting",
                        lambda k, v: saved.update({k: v}))

    out = forklift_snapshot.snapshot_today(client=None, day=date(2026, 6, 26))

    assert calls["calls"]["total_calls"] == 1
    assert calls["drivers"][0]["driver_id"] == "fk-1"
    assert saved["forklift_overload_responders"] == ["Louie"]
    assert out["day"] == "2026-06-26"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_snapshot.py -v`
Expected: FAIL (`ModuleNotFoundError: zira_dashboard.forklift_snapshot`).

- [ ] **Step 3: Write the implementation**

```python
# src/zira_dashboard/forklift_snapshot.py
"""Orchestrate one day's forklift snapshot: fetch -> ingest -> store.

Called by the background warmer (and usable from a backfill script). The
`client` arg is accepted for symmetry with precompute_day but unused — the
forklift_client functions read config from env per-call.
"""
from __future__ import annotations

from datetime import date

from . import app_settings, forklift_client, forklift_ingest, forklift_store


def snapshot_today(client, day: date) -> dict:
    dashboard = forklift_client.fetch_dashboard()
    history = forklift_client.fetch_queue_history()
    drivers = forklift_client.fetch_drivers()

    calls_row = forklift_ingest.build_calls_daily(day, dashboard, history)
    driver_rows = forklift_ingest.build_driver_daily(day, dashboard)

    forklift_store.upsert_calls_daily(calls_row)
    n = forklift_store.upsert_driver_daily(driver_rows)

    backups = [d.get("name") for d in (drivers or [])
               if d.get("isOverloadResponder") and d.get("name")]
    app_settings.set_setting("forklift_overload_responders", backups)

    return {"day": day.isoformat(), "calls": calls_row["total_calls"], "drivers": n}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_snapshot.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/forklift_snapshot.py tests/test_forklift_snapshot.py
git commit -m "feat(forklift): add daily snapshot orchestration"
```

---

## Task 8: Register the warmer in `app.py`

**Files:**
- Modify: `src/zira_dashboard/app.py` (add `_tick_forklift` near the other `_tick_*` defs ~line 168; add a `_WARMERS` entry ~line 185)

No new unit test (warmers are exercised via `forklift_snapshot` tests). Verify by import.

- [ ] **Step 1: Add the tick coroutine**

Insert after `_tick_missed_punch_out` (before the `_WARMERS` list):

```python
async def _tick_forklift():
    """Snapshot today's forklift demand + driver performance into Postgres.
    No-ops gracefully (logs+swallows via _run_warmer) if the forklift API is
    unreachable. Runs off the event loop because the client makes blocking
    HTTP calls."""
    from . import forklift_snapshot
    today = plant_today()
    await asyncio.to_thread(forklift_snapshot.snapshot_today, None, today)
```

- [ ] **Step 2: Register it in `_WARMERS`**

Add to the `_WARMERS` list (10-minute cadence keeps today fresh and captures the full day on the last run before close):

```python
    ("forklift snapshot", _tick_forklift, 600),
```

- [ ] **Step 3: Verify the app imports cleanly**

Run: `ZIRA_API_KEY=test .venv/bin/python -c "import zira_dashboard.app as a; assert any(n == 'forklift snapshot' for n, _, _ in a._WARMERS); print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/app.py
git commit -m "feat(forklift): run daily snapshot warmer (10-min cadence)"
```

---

## Task 9: `forklift_advisor.py` — assemble the render model

**Files:**
- Create: `src/zira_dashboard/forklift_advisor.py`
- Test: `tests/test_forklift_advisor.py`

Builds the dict the template renders. Reads same-weekday snapshots via the store, predicts, recommends, assesses coverage against the caller-supplied scheduled counts. Returns `{"available": False, ...}` when there's no data so the template shows a quiet fallback.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_forklift_advisor.py
from datetime import date

from zira_dashboard import forklift_advisor


def test_build_advisor_with_history(monkeypatch):
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [
                            {"day": date(2026, 6, 19), "total_calls": 420,
                             "by_hour": {"9": {"calls": 70}, "8": {"calls": 30}},
                             "by_station": {}}])
    monkeypatch.setattr(forklift_advisor.app_settings, "get_setting",
                        lambda k: ["Louie", "Juan", "Luke"])

    adv = forklift_advisor.build_advisor(
        target_day=date(2026, 6, 26),   # Friday
        dedicated=3, certified=4, backups=3,
    )
    assert adv["available"] is True
    assert adv["total_calls"] == 420
    assert adv["recommended"] == 3            # ceil(70/30)
    assert adv["coverage"].status == "ok"
    assert adv["basis"] == "history"
    assert "9" in adv["peak_label"] or "9" in str(adv["peak_label"])


def test_build_advisor_no_data_returns_unavailable(monkeypatch):
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [])
    monkeypatch.setattr(forklift_advisor, "_weekly_trends_or_none", lambda: None)
    adv = forklift_advisor.build_advisor(
        target_day=date(2026, 6, 26), dedicated=0, certified=0, backups=0)
    assert adv["available"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_advisor.py -v`
Expected: FAIL (`ModuleNotFoundError: zira_dashboard.forklift_advisor`).

- [ ] **Step 3: Write the implementation**

```python
# src/zira_dashboard/forklift_advisor.py
"""Assemble the forklift advisor render model for the scheduler card.

Reads accumulated same-weekday snapshots, predicts demand, sizes drivers, and
assesses coverage against the scheduled dedicated/certified/backup counts the
caller passes in. Returns a dict with available=False when there is no signal
so the template degrades quietly.
"""
from __future__ import annotations

from datetime import date

from . import app_settings, forklift_demand, forklift_store


def _weekly_trends_or_none() -> dict | None:
    """Best-effort cold-start source; never raises into the request path."""
    try:
        from . import forklift_client
        return forklift_client.fetch_weekly_trends()
    except Exception:
        return None


def build_advisor(target_day: date, dedicated: int, certified: int, backups: int) -> dict:
    weekday = target_day.weekday()  # Mon=0
    snaps = []
    try:
        snaps = forklift_store.calls_daily_for_weekday(weekday, limit=8)
    except Exception:
        snaps = []

    forecast = forklift_demand.predict_from_history(snaps)
    if forecast.basis == "none":
        trends = _weekly_trends_or_none()
        if trends:
            forecast = forklift_demand.bootstrap_from_trends(trends)

    if forecast.basis == "none" or forecast.total_calls <= 0:
        return {"available": False}

    recommended = forklift_demand.recommend_drivers(forecast.peak_calls)
    coverage = forklift_demand.assess_coverage(recommended, dedicated, certified, backups)
    backup_names = app_settings.get_setting("forklift_overload_responders") or []

    # sparkline data: list of (hour, fraction-of-peak) sorted by hour
    peak = forecast.peak_calls or 1.0
    hours = [(h, round(c / peak, 3)) for h, c in sorted(forecast.by_hour.items())]
    peak_label = (
        f"{forecast.peak_hour}:00–{forecast.peak_hour + 1}:00"
        if forecast.peak_hour is not None else "—"
    )

    return {
        "available": True,
        "day_label": target_day.strftime("%a %b %-d"),
        "total_calls": int(round(forecast.total_calls)),
        "peak_label": peak_label,
        "hours": hours,
        "recommended": recommended,
        "coverage": coverage,
        "basis": forecast.basis,
        "n_days": forecast.n_days,
        "backup_names": backup_names,
    }
```

- [ ] **Step 4: Add `fetch_weekly_trends` to `forklift_client.py`**

The advisor's cold-start path calls it. Add to `forklift_client.py`:

```python
def fetch_weekly_trends() -> dict:
    """8-week aggregate trends (cold-start demand source)."""
    return _get("/api/report/weekly-trends")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_advisor.py tests/test_forklift_client.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/forklift_advisor.py src/zira_dashboard/forklift_client.py tests/test_forklift_advisor.py
git commit -m "feat(forklift): assemble scheduler advisor render model"
```

---

## Task 10: Scheduler integration (route + template)

**Files:**
- Modify: `src/zira_dashboard/routes/staffing.py` (build advisor before the render block ~line 252; add to context dict ~line 287)
- Modify: `src/zira_dashboard/templates/staffing.html` (inside `.day-context` aside, after the `.day-notes` div, ~line 316)
- Test: `tests/test_staffing_forklift_card.py`

The route computes the scheduled counts from data already in `staffing_page`: `sched.assignments` (a `dict[wc_name -> list[name]]`) and `person_certs` (already built). Dedicated = people on the two forklift WCs; certified = scheduled people whose certs include "Forklift Certified"; backups = those certified people who are in the overload-responder list.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_staffing_forklift_card.py
from datetime import date

from zira_dashboard import forklift_advisor


def test_scheduled_counts_helper_counts_dedicated_certified_backups():
    from zira_dashboard.routes import staffing
    assignments = {
        "Loading/Jockeying": ["Juan"],
        "Tablets": ["Luke"],
        "Prosaw #4": ["Trent"],
    }
    person_certs = {"Juan": ["Forklift Certified"], "Luke": ["Forklift Certified"],
                    "Trent": ["Forklift Certified"], "Iban": []}
    counts = staffing._forklift_scheduled_counts(
        assignments, person_certs, overload_responders={"Juan", "Luke", "Louie"})
    assert counts["dedicated"] == 2          # Juan + Luke on forklift WCs
    assert counts["certified"] == 3          # Juan, Luke, Trent scheduled & certified
    assert counts["backups"] == 2            # Juan, Luke are overload responders


def test_build_advisor_short_when_under_dedicated(monkeypatch):
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [
                            {"day": date(2026, 6, 19), "total_calls": 500,
                             "by_hour": {"9": {"calls": 120}}, "by_station": {}}])
    monkeypatch.setattr(forklift_advisor.app_settings, "get_setting", lambda k: [])
    adv = forklift_advisor.build_advisor(date(2026, 6, 26), dedicated=1, certified=2, backups=0)
    assert adv["recommended"] == 4 and adv["coverage"].status == "short" and adv["coverage"].gap == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_forklift_card.py -v`
Expected: FAIL (`AttributeError: module ... has no attribute '_forklift_scheduled_counts'`).

- [ ] **Step 3: Add the counts helper + advisor build in `routes/staffing.py`**

Add this module-level helper (near the other small helpers, after `_next_working_day`):

```python
FORKLIFT_WC_NAMES = ("Loading/Jockeying", "Tablets")
FORKLIFT_CERT = "Forklift Certified"


def _forklift_scheduled_counts(assignments, person_certs, overload_responders):
    """Derive dedicated/certified/backup counts from the draft schedule.
    - dedicated: people assigned to the forklift work centers
    - certified: scheduled people holding the Forklift Certified cert
    - backups: certified scheduled people flagged as overload responders
    """
    dedicated = set()
    for wc in FORKLIFT_WC_NAMES:
        dedicated.update(assignments.get(wc, []) or [])
    scheduled = {n for names in assignments.values() for n in (names or [])}
    certified = {n for n in scheduled if FORKLIFT_CERT in (person_certs.get(n) or [])}
    backups = {n for n in certified if n in overload_responders}
    return {"dedicated": len(dedicated), "certified": len(certified), "backups": len(backups)}
```

In `staffing_page`, just before the `with _Phase(phases, "render"):` block, build the advisor (degrade to unavailable on any error):

```python
    # Forklift demand advisor (read-only; never blocks scheduling).
    try:
        from .. import app_settings, forklift_advisor
        _overload = set(app_settings.get_setting("forklift_overload_responders") or [])
        _counts = _forklift_scheduled_counts(sched.assignments, person_certs, _overload)
        forklift_advisor_model = forklift_advisor.build_advisor(
            target_day=d, dedicated=_counts["dedicated"],
            certified=_counts["certified"], backups=_counts["backups"],
        )
    except Exception:
        forklift_advisor_model = {"available": False}
```

Add to the `TemplateResponse` context dict (after `"attributions_by_wc": attributions_by_wc,`):

```python
                "forklift_advisor": forklift_advisor_model,
```

- [ ] **Step 4: Run the route helper tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_forklift_card.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Render the card in `staffing.html`**

Inside the `.day-context` aside, after the closing `</div>` of `.day-notes` (line ~316) and before `</aside>`:

```html
    {% if forklift_advisor and forklift_advisor.available %}
    <div class="forklift-advisor" style="border:1px solid #f5c518;border-top:4px solid #f5c518;border-radius:8px;padding:9px;margin-top:12px;background:rgba(245,197,24,.10)">
      <div style="display:flex;align-items:center;gap:6px"><span style="font-size:16px">🚜</span><strong>Forklift demand</strong></div>
      <div style="opacity:.85;margin-top:3px">~{{ forklift_advisor.total_calls }} calls · busiest {{ forklift_advisor.peak_label }}</div>
      <div style="display:flex;align-items:flex-end;gap:2px;height:30px;margin:7px 0">
        {% for hour, frac in forklift_advisor.hours %}
        <div title="{{ hour }}:00 — {{ (frac * 100)|round|int }}% of peak" style="flex:1;background:#f5c518;height:{{ (frac * 100)|round|int }}%;min-height:2px"></div>
        {% endfor %}
      </div>
      <div style="font-size:14px;margin:2px 0"><strong>Recommend {{ forklift_advisor.recommended }} dedicated driver{{ forklift_advisor.recommended != 1 and 's' or '' }}</strong></div>
      {% if forklift_advisor.coverage.status == 'ok' %}
      <div style="color:#137333;font-weight:600">✅ Coverage OK</div>
      {% else %}
      <div style="color:#b54708;font-weight:600">⚠️ Short {{ forklift_advisor.coverage.gap }} — {{ forklift_advisor.coverage.dedicated }} dedicated of {{ forklift_advisor.recommended }}</div>
      {% endif %}
      <div style="opacity:.8;margin-top:2px">{{ forklift_advisor.coverage.certified }} certified scheduled{% if forklift_advisor.backup_names %} · backups: {{ forklift_advisor.backup_names|join(', ') }}{% endif %}</div>
      <div style="opacity:.55;margin-top:5px;font-size:11px">{% if forklift_advisor.basis == 'history' %}based on {{ forklift_advisor.n_days }} recent {{ forklift_advisor.day_label.split(' ')[0] }}s{% else %}early estimate (building history){% endif %}</div>
    </div>
    {% endif %}
```

- [ ] **Step 6: Write a route render test**

```python
# append to tests/test_staffing_forklift_card.py
def test_staffing_page_renders_card_when_advisor_available(monkeypatch):
    import os
    os.environ.setdefault("AUTH_DISABLED", "1")
    from starlette.testclient import TestClient
    from zira_dashboard.app import app
    from zira_dashboard.routes import staffing

    # Force a known advisor model regardless of DB/API state.
    monkeypatch.setattr(
        "zira_dashboard.forklift_advisor.build_advisor",
        lambda **kw: {"available": True, "day_label": "Sat Jun 27", "total_calls": 420,
                      "peak_label": "9:00–10:00", "hours": [(8, 0.5), (9, 1.0)],
                      "recommended": 3,
                      "coverage": staffing.forklift_demand.assess_coverage(3, 3, 4, 3),
                      "basis": "history", "n_days": 4, "backup_names": ["Louie", "Juan"]},
    )
    with TestClient(app) as client:
        r = client.get("/staffing")
    assert r.status_code == 200
    assert "Forklift demand" in r.text and "Recommend 3 dedicated" in r.text
```

Note: this test needs `staffing` to expose `forklift_demand` — it is imported in Step 3's advisor block via `forklift_advisor`, but for the test's `assess_coverage` reference add `from .. import forklift_demand` at the top of `routes/staffing.py` if not already imported. (If the DB-backed `/staffing` render can't run without `DATABASE_URL`, mark this test with the same `DATABASE_URL` skip guard as the store tests.)

- [ ] **Step 7: Run the full forklift suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_client.py tests/test_forklift_ingest.py tests/test_forklift_demand.py tests/test_forklift_snapshot.py tests/test_forklift_advisor.py tests/test_staffing_forklift_card.py -v`
Expected: PASS (DB-gated tests SKIP locally).

- [ ] **Step 8: Lint + commit**

```bash
.venv/bin/python -m ruff check src tests
git add src/zira_dashboard/routes/staffing.py src/zira_dashboard/templates/staffing.html tests/test_staffing_forklift_card.py
git commit -m "feat(forklift): show demand advisor card in the scheduler right rail"
```

---

## Self-Review

**1. Spec coverage:**
- Spec §4 components → Tasks 2 (`forklift_client`), 3 (schema), 4 (`forklift_ingest`), 5 (`forklift_store`), 6 (`forklift_demand`), 7+8 (snapshot warmer), 9 (`forklift_advisor`), 10 (route + template). ✔
- Spec §5 demand model (predict / cold-start bootstrap / recommend / coverage) → Task 6 + Task 9. ✔ (Schedule-responsive scaling by which lines run is captured as `by_station` in the snapshot and the forecast; the *scaling* step itself is deferred to Increment B with the people-level work — see note below.)
- Spec §6 mapping → `forklift_store.name_map` (Task 5) + the forklift WC + cert constants (Task 10). Driver→person name match is used implicitly by name; the override table exists for stragglers. ✔
- Spec §7 UI (card under Notes) → Task 10. ✔
- Spec §8 reliability → warmer swallows errors (Task 8 via `_run_warmer`), advisor degrades to `available:False` (Tasks 9, 10). ✔
- Spec §9 testing → per-task tests. ✔
- Spec §11 config → Task 1. ✔

**Gap noted & intentionally deferred:** Spec §5's *schedule-responsive demand scaling* (drop demand for unstaffed lines) — the data (`by_station`) is captured now, but applying the scaling needs the workstation→WC mapping wired through and is most valuable alongside Increment B's per-station reasoning. Tracked for the Increment B plan. The advisor is fully functional without it (predicts from same-weekday history).

**2. Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N". Every code step shows complete code. ✔

**3. Type consistency:** `DemandForecast`/`Coverage` fields, the row-dict keys, `build_advisor(target_day, dedicated, certified, backups)`, `calls_daily_for_weekday(weekday, limit)`, and the template keys (`available, total_calls, peak_label, hours, recommended, coverage.{status,gap,dedicated,certified}, backup_names, basis, n_days, day_label`) are consistent across Tasks 6, 9, 10. `forklift_client` gains `fetch_weekly_trends` in Task 9 Step 4 (used by the advisor). ✔

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-26-forklift-demand-advisor.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
