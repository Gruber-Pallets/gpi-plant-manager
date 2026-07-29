# Forklift Throughput History Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Calculate forklift staffing suggestions from paired daily demand and on-call history so incomplete per-driver call rows cannot inflate the recommendation.

**Architecture:** Keep the advisor and recommendation formula unchanged. Replace the store-level throughput aggregate with a daily join between `forklift_calls_daily.total_calls` and summed `forklift_driver_daily.on_call_ms`, ignoring days where either side has no usable signal and retaining the existing fallback when paired history is thin.

**Tech Stack:** Python 3.11+, PostgreSQL, psycopg2, pytest, Ruff

## Global Constraints

- Do not rewrite or delete historical forklift rows.
- Keep the 28-day default lookback and two-hour minimum signal threshold.
- Keep the existing 16-calls-per-hour fallback when paired history is unavailable.
- Do not change forecasting, utilization, planned-hour selection, coverage counting, or recent claim-time display.
- Add a simple user-facing `CHANGELOG.md` note describing the corrected suggestion.

---

## File Structure

- `src/zira_dashboard/forklift_store.py` owns the paired-history throughput query.
- `tests/test_forklift_store_pure.py` provides the DB-free regression test and fallback test.
- `tests/test_forklift_store.py` keeps the PostgreSQL integration contract aligned with the paired sources.
- `CHANGELOG.md` records the user-visible fix in plain language.

### Task 1: Pair Daily Forklift Demand With Driver On-Call Time

**Files:**
- Modify: `tests/test_forklift_store_pure.py`
- Modify: `tests/test_forklift_store.py`
- Modify: `src/zira_dashboard/forklift_store.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `forklift_calls_daily(day, total_calls)` and `forklift_driver_daily(day, on_call_ms)`.
- Produces: `forklift_store.recent_driver_throughput(days: int = 28) -> float | None`.

- [ ] **Step 1: Write the failing DB-free regression test**

Add to `tests/test_forklift_store_pure.py`:

```python
import pytest


def test_recent_driver_throughput_pairs_daily_demand_with_on_call_time(monkeypatch):
    from zira_dashboard import db

    seen = {}

    def fake_query(sql, params):
        seen["sql"] = sql
        seen["params"] = params
        if "JOIN forklift_calls_daily" in sql:
            return [{"calls": 11_536, "ms": 2_183_328_000}]
        # Production failure shape: only today's driver calls divided by all
        # reconstructed on-call hours.
        return [{"calls": 437, "ms": 2_183_328_000}]

    monkeypatch.setattr(db, "query", fake_query)

    rate = forklift_store.recent_driver_throughput(days=28)

    assert rate == pytest.approx(19.02, abs=0.01)
    assert "JOIN forklift_calls_daily" in seen["sql"]
    assert seen["params"] == (28,)
```

- [ ] **Step 2: Run the regression test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_forklift_store_pure.py::test_recent_driver_throughput_pairs_daily_demand_with_on_call_time -q
```

Expected: FAIL because the current query reads only `forklift_driver_daily`, returns about `0.72`, and does not contain the daily-demand join.

- [ ] **Step 3: Implement the paired-history query**

Replace `recent_driver_throughput()` in `src/zira_dashboard/forklift_store.py` with:

```python
def recent_driver_throughput(days: int = 28) -> float | None:
    """Fleet calls/hour from days with both demand and on-call history.

    Daily call totals are authoritative in ``forklift_calls_daily``. Historical
    utilization reconstruction can create ``forklift_driver_daily`` rows with
    valid on-call time but no per-driver calls, so using that table for both
    sides would understate throughput. Return None when the paired signal is too
    thin so the advisor keeps its existing default-throughput fallback.
    """
    from . import db

    rows = db.query(
        "WITH driver_days AS ("
        "  SELECT day, COALESCE(SUM(on_call_ms), 0) AS ms "
        "  FROM forklift_driver_daily "
        "  WHERE day >= (CURRENT_DATE - %s::int) "
        "  GROUP BY day"
        ") "
        "SELECT COALESCE(SUM(c.total_calls), 0) AS calls, "
        "       COALESCE(SUM(d.ms), 0) AS ms "
        "FROM driver_days d "
        "JOIN forklift_calls_daily c ON c.day = d.day "
        "WHERE d.ms > 0 AND c.total_calls > 0",
        (days,),
    )
    if not rows:
        return None
    calls = float(rows[0]["calls"] or 0)
    hours = float(rows[0]["ms"] or 0) / 3.6e6
    if hours < _MIN_ONCALL_HOURS or calls <= 0:
        return None
    return calls / hours
```

- [ ] **Step 4: Run the regression test and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_forklift_store_pure.py::test_recent_driver_throughput_pairs_daily_demand_with_on_call_time -q
```

Expected: `1 passed`.

- [ ] **Step 5: Add a DB-free fallback regression**

Add to `tests/test_forklift_store_pure.py`:

```python
def test_recent_driver_throughput_returns_none_for_thin_paired_history(monkeypatch):
    from zira_dashboard import db

    monkeypatch.setattr(
        db,
        "query",
        lambda *args, **kwargs: [{"calls": 10, "ms": 3_600_000}],
    )

    assert forklift_store.recent_driver_throughput(days=28) is None
```

Run:

```bash
.venv/bin/python -m pytest tests/test_forklift_store_pure.py -q
```

Expected: all tests in the file pass.

- [ ] **Step 6: Align the PostgreSQL integration test with the paired sources**

Replace `test_recent_driver_throughput_from_driver_daily` in
`tests/test_forklift_store.py` with:

```python
def test_recent_driver_throughput_pairs_daily_calls_and_on_call_time():
    from zira_dashboard import db

    db.bootstrap_schema()
    d = date(2026, 6, 25)
    db.execute("DELETE FROM forklift_driver_daily WHERE day = %s", (d,))
    db.execute("DELETE FROM forklift_calls_daily WHERE day = %s", (d,))
    forklift_store.upsert_calls_daily({
        "day": d,
        "total_calls": 80,
        "urgent_calls": 0,
        "overload_count": 0,
        "neglected_count": 0,
        "by_hour": {},
        "by_station": {},
        "by_skill": {},
    })
    # Reconstructed driver rows may have valid time while per-driver calls are
    # missing. The daily demand table supplies the numerator.
    forklift_store.upsert_driver_daily([
        {
            "day": d,
            "driver_id": "fk-a",
            "name": "A",
            "calls": 0,
            "on_time": 70,
            "late": 10,
            "avg_ms": 0,
            "max_ms": 0,
            "utilization_pct": 90,
            "on_call_ms": 14_400_000,
            "available_ms": 16_000_000,
        },
    ])

    rate = forklift_store.recent_driver_throughput(days=3650)

    assert rate is not None and 19.0 < rate < 21.0
    db.execute("DELETE FROM forklift_driver_daily WHERE day = %s", (d,))
    db.execute("DELETE FROM forklift_calls_daily WHERE day = %s", (d,))
```

Run:

```bash
.venv/bin/python -m pytest tests/test_forklift_store.py -q
```

Expected locally without `DATABASE_URL`: skipped. Expected in the database-backed CI job: pass.

- [ ] **Step 7: Add the user-facing patch note**

Under the 2026-07-29 `### Fixes` heading in `CHANGELOG.md`, add:

```markdown
- **Forklift staffing suggestions use complete history now.** Missing old driver details no longer make the schedule ask for huge crews such as 160 people.
```

- [ ] **Step 8: Run focused verification**

Run:

```bash
.venv/bin/python -m pytest tests/test_forklift_store_pure.py tests/test_forklift_advisor.py tests/test_staffing_forklift_card.py -q
.venv/bin/ruff check src/zira_dashboard/forklift_store.py tests/test_forklift_store_pure.py tests/test_forklift_store.py
git diff --check
```

Expected: all focused tests pass, Ruff reports no errors, and `git diff --check` exits cleanly.

- [ ] **Step 9: Run the full test suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: the full local suite passes with only the repository's intentional skips.

- [ ] **Step 10: Verify the production data shape read-only**

Run:

```bash
.venv/bin/python -c 'from dotenv import load_dotenv; load_dotenv(); from datetime import date; from zira_dashboard import db,forklift_advisor,forklift_store; db.init_pool(1,2); print(round(forklift_store.recent_driver_throughput(),2)); print(forklift_advisor.demand_summary(date(2026,7,30))["recommended"]); db.shutdown_pool()'
```

Expected with the current paired production history: approximately `19.02` calls/hour and approximately `6` suggested drivers.

- [ ] **Step 11: Commit and push the implementation**

```bash
git add src/zira_dashboard/forklift_store.py tests/test_forklift_store_pure.py tests/test_forklift_store.py CHANGELOG.md
git commit -m "fix: pair forklift demand and driver history"
git push origin main
```
