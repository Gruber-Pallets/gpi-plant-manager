# Forklift SLA Recommender Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-target the forklift driver recommendation from a capacity ratio to an SLA model — recommend the smallest crew whose predicted average time-to-claim stays under a tunable target (default 4 min), via a calibrated Erlang-C queueing model.

**Architecture:** A new pure `forklift_queue.py` computes Erlang-C wait, fits a calibration factor against actual recorded waits, and recommends a crew for a target. `forklift_store` supplies handling time + per-day calibration samples. `forklift_advisor` swaps the capacity ratio for the queue recommender. Settings replace the driver-speed/safety-slack sliders with a target slider. UI shows the predicted wait + a back-test.

**Tech Stack:** FastAPI + Jinja2 + htmx, raw psycopg2, Postgres on Railway. Tests: pytest (`ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest`); DB-gated tests skip without `DATABASE_URL`. Lint: ruff (`F`).

**Spec:** `docs/superpowers/specs/2026-06-29-forklift-sla-recommender-design.md`

**Conventions:** all tests run with `ZIRA_API_KEY=test PYTHONPATH=src <venv>/python -m pytest <path> -v`; DB-gated tests decorated `@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs DB")`. Commit after each task. Before mirroring/extending an existing module, READ it and match its real symbols.

---

## Task 1: `forklift_queue.erlang_c_wait_seconds` (pure)

**Files:**
- Create: `src/zira_dashboard/forklift_queue.py`
- Test: `tests/test_forklift_queue.py`

- [ ] **Step 1: Write the failing tests**

```python
import math
from zira_dashboard import forklift_queue as q


def test_mm1_matches_closed_form():
    # c=1 reduces to M/M/1: W_q = rho/(mu - lambda). lambda=10/hr, handle=180s -> mu=20/hr.
    # rho=0.5 -> W_q = 0.5/(20-10) hr = 0.05 hr = 180 s.
    assert math.isclose(q.erlang_c_wait_seconds(1, 10, 180), 180.0, rel_tol=1e-6)


def test_unstable_returns_inf():
    # lambda=25/hr, handle=180s -> mu=20/hr; one server can't keep up (a=1.25 >= c=1).
    assert q.erlang_c_wait_seconds(1, 25, 180) == math.inf


def test_more_servers_strictly_reduce_wait():
    w5 = q.erlang_c_wait_seconds(5, 97, 180)
    w6 = q.erlang_c_wait_seconds(6, 97, 180)
    w7 = q.erlang_c_wait_seconds(7, 97, 180)
    assert w5 > w6 > w7 >= 0


def test_zero_or_invalid_load_is_zero_wait():
    assert q.erlang_c_wait_seconds(3, 0, 180) == 0.0
    assert q.erlang_c_wait_seconds(3, 50, 0) == 0.0
    assert q.erlang_c_wait_seconds(0, 50, 180) == 0.0
```

- [ ] **Step 2: Run to verify they fail**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_forklift_queue.py -v`
Expected: FAIL (module missing)

- [ ] **Step 3: Implement**

```python
# src/zira_dashboard/forklift_queue.py
"""Pure forklift queueing model: Erlang-C wait, calibration against actual
recorded waits, and a crew recommendation for a time-to-claim target.
No DB, no templates."""
from __future__ import annotations

import math
from dataclasses import dataclass


def erlang_c_wait_seconds(c: int, lambda_per_hr: float, mean_handle_seconds: float) -> float:
    """Expected wait-in-queue (time-to-claim), seconds, for an M/M/c queue with
    `c` servers, arrival rate `lambda_per_hr` (calls/hr), service = handling time.
    Returns 0.0 for no/invalid load, math.inf when the queue is unstable (c<=a)."""
    if c < 1 or lambda_per_hr <= 0 or mean_handle_seconds <= 0:
        return 0.0
    mu = 3600.0 / mean_handle_seconds          # calls/hr per server
    a = lambda_per_hr / mu                      # offered load (Erlangs)
    if c <= a:
        return math.inf
    # Erlang-C probability of waiting
    summ = 0.0
    term = 1.0                                  # a^0 / 0!
    for k in range(c):
        if k > 0:
            term *= a / k
        summ += term                            # sum_{k=0}^{c-1} a^k/k!
    ac_over_cfact = term * (a / c)              # a^c / c!  (term is a^(c-1)/(c-1)!)
    top = ac_over_cfact * (c / (c - a))
    p_wait = top / (summ + top)
    wq_hours = p_wait / (c * mu - lambda_per_hr)
    return wq_hours * 3600.0
```

- [ ] **Step 4: Run to verify pass**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_forklift_queue.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/forklift_queue.py tests/test_forklift_queue.py
git commit -m "feat(forklift): Erlang-C time-to-claim model (pure)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `forklift_queue.recommend_for_target` (pure)

**Files:**
- Modify: `src/zira_dashboard/forklift_queue.py`
- Test: `tests/test_forklift_queue.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_recommend_picks_smallest_crew_under_target():
    # busy hour ~97 calls/hr, 3-min handle, calibration 1.8x, target 240s.
    r = q.recommend_for_target(97, 180, target_seconds=240, k=1.8)
    assert r.drivers == 6 and not r.overloaded
    assert r.predicted_seconds is not None and r.predicted_seconds <= 240


def test_tighter_target_needs_more_drivers():
    loose = q.recommend_for_target(97, 180, target_seconds=300, k=1.8).drivers
    tight = q.recommend_for_target(97, 180, target_seconds=90, k=1.8).drivers
    assert tight > loose


def test_overload_when_cap_too_low():
    r = q.recommend_for_target(97, 180, target_seconds=60, k=1.8, max_drivers=5)
    assert r.drivers is None and r.overloaded is True


def test_near_zero_demand_recommends_one():
    r = q.recommend_for_target(0.0, 180, target_seconds=240, k=1.0)
    assert r.drivers == 1 and not r.overloaded
```

- [ ] **Step 2: Run to verify they fail**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_forklift_queue.py -k recommend -v`
Expected: FAIL (no attribute)

- [ ] **Step 3: Implement** (append to `forklift_queue.py`)

```python
DEFAULT_TARGET_SECONDS = 240.0
MAX_DRIVERS = 12


@dataclass
class RecResult:
    drivers: int | None
    predicted_seconds: float | None
    overloaded: bool


def recommend_for_target(lambda_per_hr: float, mean_handle_seconds: float,
                         target_seconds: float = DEFAULT_TARGET_SECONDS,
                         k: float = 1.0, max_drivers: int = MAX_DRIVERS) -> RecResult:
    """Smallest crew whose calibrated predicted time-to-claim is <= target.
    Returns overloaded=True (drivers=None) if no crew up to max_drivers qualifies."""
    if lambda_per_hr <= 0 or mean_handle_seconds <= 0:
        return RecResult(drivers=1, predicted_seconds=0.0, overloaded=False)
    for c in range(1, max_drivers + 1):
        w = k * erlang_c_wait_seconds(c, lambda_per_hr, mean_handle_seconds)
        if w <= target_seconds:
            return RecResult(drivers=c, predicted_seconds=w, overloaded=False)
    return RecResult(drivers=None, predicted_seconds=None, overloaded=True)
```

- [ ] **Step 4: Run to verify pass**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_forklift_queue.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/forklift_queue.py tests/test_forklift_queue.py
git commit -m "feat(forklift): recommend smallest crew under a time-to-claim target

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `forklift_queue.fit_calibration` (pure)

**Files:**
- Modify: `src/zira_dashboard/forklift_queue.py`
- Test: `tests/test_forklift_queue.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_calibration_is_median_ratio_of_actual_to_predicted():
    # Build samples whose actual wait is ~2x the model's prediction at their (crew, lambda).
    handle = 180
    samples = []
    for lam, crew in [(60, 5), (70, 6), (80, 6), (50, 5), (65, 6)]:
        pred = q.erlang_c_wait_seconds(crew, lam, handle)
        samples.append({"avg_lambda": lam, "crew": crew, "actual_wait_seconds": 2.0 * pred})
    res = q.fit_calibration(samples, handle)
    assert abs(res.k - 2.0) < 1e-6 and res.uncalibrated is False
    assert res.n_samples == 5


def test_calibration_clamps_extremes():
    handle = 180
    s = [{"avg_lambda": 60, "crew": 5,
          "actual_wait_seconds": 999 * q.erlang_c_wait_seconds(5, 60, handle)}] * 6
    assert q.fit_calibration(s, handle).k == 5.0  # clamped to max


def test_too_few_samples_is_uncalibrated():
    handle = 180
    s = [{"avg_lambda": 60, "crew": 5,
          "actual_wait_seconds": 2 * q.erlang_c_wait_seconds(5, 60, handle)}] * 3
    res = q.fit_calibration(s, handle)
    assert res.k == 1.0 and res.uncalibrated is True


def test_unstable_samples_are_skipped():
    handle = 180
    # crew below offered load -> inf prediction -> skipped, leaving too few -> uncalibrated
    s = [{"avg_lambda": 500, "crew": 1, "actual_wait_seconds": 100.0}] * 8
    assert q.fit_calibration(s, handle).uncalibrated is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_forklift_queue.py -k calibration -v`
Expected: FAIL

- [ ] **Step 3: Implement** (append to `forklift_queue.py`)

```python
from statistics import median

MIN_CALIB_SAMPLES = 5
CALIB_CLAMP = (0.5, 5.0)


@dataclass
class CalibResult:
    k: float
    n_samples: int
    mean_actual_seconds: float
    mean_pred_seconds: float       # mean of k * raw prediction (calibrated)
    uncalibrated: bool


def fit_calibration(samples: list[dict], mean_handle_seconds: float) -> CalibResult:
    """Fit k = median(actual / predicted) over historical days, clamped. Falls
    back to k=1.0 (uncalibrated) when fewer than MIN_CALIB_SAMPLES are usable."""
    ratios, preds, actuals = [], [], []
    for s in samples or []:
        pred = erlang_c_wait_seconds(int(s["crew"]), float(s["avg_lambda"]), mean_handle_seconds)
        actual = float(s["actual_wait_seconds"])
        if not math.isfinite(pred) or pred <= 0:
            continue
        ratios.append(actual / pred)
        preds.append(pred)
        actuals.append(actual)
    if len(ratios) < MIN_CALIB_SAMPLES:
        return CalibResult(k=1.0, n_samples=len(ratios),
                           mean_actual_seconds=(sum(actuals) / len(actuals)) if actuals else 0.0,
                           mean_pred_seconds=(sum(preds) / len(preds)) if preds else 0.0,
                           uncalibrated=True)
    k = max(CALIB_CLAMP[0], min(CALIB_CLAMP[1], median(ratios)))
    return CalibResult(
        k=k, n_samples=len(ratios),
        mean_actual_seconds=sum(actuals) / len(actuals),
        mean_pred_seconds=k * (sum(preds) / len(preds)),
        uncalibrated=False,
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_forklift_queue.py -v`
Expected: PASS (all queue tests)

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/forklift_queue.py tests/test_forklift_queue.py
git commit -m "feat(forklift): calibrate queue model against actual recorded waits

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `forklift_store` — handling time + calibration samples

**Files:**
- Modify: `src/zira_dashboard/forklift_store.py`
- Test: `tests/test_forklift_store.py`

- [ ] **Step 1: Write the failing tests (DB-gated)**

```python
@DBGATE  # same skipif(not os.getenv("DATABASE_URL")) used by the other store tests
def test_mean_handle_seconds_is_total_oncall_over_calls():
    day = dt.date(2026, 5, 1)
    forklift_store.upsert_driver_daily([
        {"day": day, "driver_id": "d1", "name": "A", "calls": 10, "on_time": 0, "late": 0,
         "avg_ms": 0, "max_ms": 0, "utilization_pct": 0, "on_call_ms": 1_800_000, "available_ms": 0},
        {"day": day, "driver_id": "d2", "name": "B", "calls": 10, "on_time": 0, "late": 0,
         "avg_ms": 0, "max_ms": 0, "utilization_pct": 0, "on_call_ms": 1_800_000, "available_ms": 0},
    ])
    # 3,600,000 ms over 20 calls = 180,000 ms = 180 s mean handle
    assert abs(forklift_store.mean_handle_seconds(window_days=3650) - 180.0) < 1.0


@DBGATE
def test_calibration_samples_shape():
    rows = forklift_store.calibration_samples(window_days=3650)
    if rows:
        r = rows[0]
        assert {"day", "avg_lambda", "crew", "actual_wait_seconds"} <= set(r)
```

- [ ] **Step 2: Run to verify they fail**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_forklift_store.py -k "mean_handle or calibration_samples" -v`
Expected: FAIL (no attribute) — or SKIP locally; logic verified by reading.

- [ ] **Step 3: Implement**

First **read** `forklift_store.py` for the `db` import + query helper style, and read `_schema.py` for the real `forklift_calls_daily` columns (it stores per-day-per-hour call counts; confirm the day/hour/calls column names). Then add, mirroring the existing query style:

```python
def mean_handle_seconds(window_days: int = 90) -> float | None:
    """Mean handling time per call (seconds) over the window: sum(on_call_ms)/sum(calls)/1000.
    None when there are no calls."""
    from . import db
    rows = db.query(
        "SELECT SUM(on_call_ms) AS ms, SUM(calls) AS calls FROM forklift_driver_daily "
        "WHERE day >= (CURRENT_DATE - %s::int)",
        (window_days,),
    )
    if not rows or not rows[0]["calls"]:
        return None
    return float(rows[0]["ms"]) / float(rows[0]["calls"]) / 1000.0


def calibration_samples(window_days: int = 90) -> list[dict]:
    """One row per eligible historical day: avg arrival rate (calls/operating-hour),
    crew (distinct drivers with calls), and the actual calls-weighted mean time-to-claim.
    Eligible = >= 20 calls and >= 1 active driver that day."""
    from . import db
    # operating hours + total calls per day from the hourly calls table
    hours = db.query(
        "SELECT day, COUNT(*) FILTER (WHERE calls > 0) AS op_hours, SUM(calls) AS total "
        "FROM forklift_calls_daily WHERE day >= (CURRENT_DATE - %s::int) GROUP BY day",
        (window_days,),
    )
    hmap = {r["day"]: r for r in hours}
    drv = db.query(
        "SELECT day, COUNT(DISTINCT driver_id) FILTER (WHERE calls > 0) AS crew, "
        "SUM(avg_ms * calls) AS wms, SUM(calls) AS calls "
        "FROM forklift_driver_daily WHERE day >= (CURRENT_DATE - %s::int) GROUP BY day",
        (window_days,),
    )
    out = []
    for r in drv:
        h = hmap.get(r["day"])
        if not h or not h["op_hours"] or not h["total"]:
            continue
        calls = int(r["calls"] or 0)
        crew = int(r["crew"] or 0)
        if calls < 20 or crew < 1:
            continue
        avg_lambda = float(h["total"]) / float(h["op_hours"])
        actual_wait = (float(r["wms"]) / calls / 1000.0) if calls else 0.0
        out.append({"day": r["day"], "avg_lambda": avg_lambda,
                    "crew": crew, "actual_wait_seconds": actual_wait})
    return out
```

(Adjust column names to the real `forklift_calls_daily` schema if they differ. If a `db.query` date-window idiom already exists in this module, match it.)

- [ ] **Step 4: Run to verify pass/skip**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_forklift_store.py -v && /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/ruff check src/zira_dashboard/forklift_store.py`
Expected: PASS or SKIP (DB-gated), ruff clean

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/forklift_store.py tests/test_forklift_store.py
git commit -m "feat(forklift): store reads for handling time + calibration samples

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: `forklift_settings` — `target_claim_seconds` override

**Files:**
- Modify: `src/zira_dashboard/_schema.py`
- Modify: `src/zira_dashboard/forklift_settings.py`
- Test: `tests/test_forklift_settings.py`

- [ ] **Step 1: Write the failing test**

```python
def test_target_claim_seconds_default_and_override():
    s = fset.Settings()  # all overrides None
    r = fset.resolve(s, algo_throughput=16)
    assert r.target_claim_seconds == 240.0   # 4 min default
    s2 = fset.Settings(target_claim_seconds=300)
    assert fset.resolve(s2, algo_throughput=16).target_claim_seconds == 300.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_forklift_settings.py -k target_claim -v`
Expected: FAIL

- [ ] **Step 3: Implement**

In `_schema.py`, alongside the existing forklift_settings override ALTERs, add (guarded, idempotent):

```python
cur.execute("ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS target_claim_seconds NUMERIC NULL")
```

In `forklift_settings.py` (match the existing nullable-override pattern): add `target_claim_seconds: float | None = None` to `Settings`, thread it through the DB load `SELECT`/`_row_to_settings` and `save()` UPSERT, add `target_claim_seconds` to `Resolved` resolved in `resolve()` as `_f(s.target_claim_seconds, 240.0)`, and expose 240.0 as the default in `algorithm_values()`.

- [ ] **Step 4: Run to verify pass**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_forklift_settings.py -v`
Expected: PASS (pure resolution); DB round-trip SKIPs locally

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/_schema.py src/zira_dashboard/forklift_settings.py tests/test_forklift_settings.py
git commit -m "feat(forklift): target_claim_seconds setting (default 240s)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: `forklift_advisor` — swap capacity ratio for the SLA recommender

**Files:**
- Modify: `src/zira_dashboard/forklift_advisor.py`
- Test: `tests/test_forklift_advisor.py`

- [ ] **Step 1: Write the failing test**

First READ `forklift_advisor.py` to learn the current `build_advisor`/`demand_summary` return shape and how it reads settings + the forecast. Then add a test that monkeypatches the data seams so it needs no DB and asserts the SLA wiring:

```python
def test_build_advisor_uses_sla_recommender(monkeypatch):
    from zira_dashboard import forklift_advisor as adv
    from zira_dashboard import forklift_demand as dem
    # Force a known forecast (busy hour 97 calls/hr), handling time, and calibration.
    monkeypatch.setattr(adv, "_forecast", lambda day: dem.DemandForecast(
        total_calls=500, by_hour={9: 97.0, 10: 40.0}, peak_hour=9, peak_calls=97.0,
        basis="history", n_days=8), raising=False)
    monkeypatch.setattr("zira_dashboard.forklift_store.mean_handle_seconds", lambda window_days=90: 180.0)
    monkeypatch.setattr("zira_dashboard.forklift_store.calibration_samples", lambda window_days=90: [])
    out = adv.build_advisor(__import__("datetime").date(2026, 6, 30), scheduled=3, backups=2)
    # target default 240s, k=1 (no samples) -> recommend ~5-6 with predicted under target
    assert out["recommended"] in (5, 6)
    assert out["target_seconds"] == 240.0
    assert out["predicted_claim_seconds"] is not None
    assert "backtest" in out
```

(Adapt the monkeypatch targets to the REAL internal helper names you find in `forklift_advisor.py` — e.g. however it currently obtains the forecast and settings. The assertions on `recommended`/`target_seconds`/`predicted_claim_seconds`/`backtest` are the contract to honor.)

- [ ] **Step 2: Run to verify it fails**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_forklift_advisor.py -k sla -v`
Expected: FAIL

- [ ] **Step 3: Implement**

Rewire `build_advisor` (keep it defensive — never raise into the request path):
1. Build the forecast as today; get `resolved` settings (with `target_claim_seconds`, `plan_for_percentile`, `history_samples`).
2. `lam = forklift_demand.demand_at_percentile(forecast.by_hour, resolved.percentile)[1]`.
3. `mean_handle = forklift_store.mean_handle_seconds(window) or <fallback>` — pick a sane fallback (e.g. derive from forecast or skip recommendation if None).
4. `calib = forklift_queue.fit_calibration(forklift_store.calibration_samples(window), mean_handle)`.
5. `rec = forklift_queue.recommend_for_target(lam, mean_handle, resolved.target_claim_seconds, calib.k)`.
6. Return a dict carrying at least: `recommended` (rec.drivers, or None when overloaded), `overloaded` (rec.overloaded), `predicted_claim_seconds` (rec.predicted_seconds), `target_seconds` (resolved.target_claim_seconds), `backtest` ({n_samples, mean_actual_seconds, mean_pred_seconds, uncalibrated} from calib), and the **algorithm baseline** = the same computation with the DEFAULT target (240) for the discreet "algorithm: M" display. Preserve any existing keys the template/route still read (coverage, demand summary, etc.).
7. Keep the no-data path: if `mean_handle` is None or the forecast has no `by_hour`, return the existing "recommendation builds as history accrues" advisor.

- [ ] **Step 4: Run to verify pass**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_forklift_advisor.py -v && /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/ruff check src/zira_dashboard/forklift_advisor.py`
Expected: PASS, ruff clean

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/forklift_advisor.py tests/test_forklift_advisor.py
git commit -m "feat(forklift): advisor recommends crew for the time-to-claim target

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Settings page — target slider replaces driver-speed/safety-slack + back-test

**Files:**
- Modify: `src/zira_dashboard/routes/settings.py`
- Modify: `src/zira_dashboard/templates/settings.html`
- Test: `tests/test_settings_forklift.py`

- [ ] **Step 1: Write the failing test**

```python
def test_forklift_panel_has_target_slider_not_capacity_sliders(client):
    page = client.get("/settings").text
    assert 'name="target_claim_seconds"' in page or 'id="target-claim"' in page
    assert "Target time-to-claim" in page
    # capacity knobs retired from the panel
    assert "Driver speed" not in page and "Safety slack" not in page


def test_post_sets_and_clears_target(client):
    assert client.post("/settings/forklift", data={"target_claim_seconds": "300"}).status_code in (200, 303)
    assert client.post("/settings/forklift", data={"target_claim_seconds": "auto"}).status_code in (200, 303)
```

(Use the existing settings-test fixtures/conventions in `tests/test_settings_forklift.py`; mark DB-gated if those tests are.)

- [ ] **Step 2: Run to verify it fails**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_settings_forklift.py -k target -v`
Expected: FAIL

- [ ] **Step 3: Implement**

READ `routes/settings.py` `_parse_forklift_overrides` + the forklift GET context, and the forklift slider section in `settings.html`. Then:
- Parse `target_claim_seconds` (blank/"auto" → None; input is **minutes** in the UI, convert to seconds; clamp 30–1200s i.e. 0.5–20 min).
- **Remove** the Driver-speed (throughput) and Safety-slack (utilization) slider blocks from the forklift panel. **Keep** Plan-for + History-window. Add a **Target time-to-claim** slider (minutes, 1–10, step 0.5; grey tick at 4 min; ↺ reset) and a manual override of the final recommended number (reuse the existing override control if present).
- Surface the advisor's **back-test** read-only: "Over the last N days: model predicts ~X min vs. actual ~Y min" + an "(uncalibrated — building history)" note when `backtest.uncalibrated`. Show the recommendation line "Recommend N to keep time-to-claim under 4 min · predicted ~X min" (or the overloaded message).
- No client-side Erlang-C: the recommendation/predicted/back-test are server-rendered for the saved settings; only the slider's minutes readout updates live. (Deliberate — avoids JS/Python drift.)

- [ ] **Step 4: Run to verify pass**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_settings_forklift.py -v && /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/ruff check src/zira_dashboard/routes/settings.py`
Expected: PASS, ruff clean

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/routes/settings.py src/zira_dashboard/templates/settings.html tests/test_settings_forklift.py
git commit -m "feat(forklift): settings target-time-to-claim slider + back-test

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Scheduler card copy

**Files:**
- Modify: `src/zira_dashboard/templates/staffing.html`
- Modify (if needed): `src/zira_dashboard/routes/staffing.py`
- Test: `tests/test_staffing_forklift_card.py`

- [ ] **Step 1: Write the failing test**

```python
def test_card_shows_time_to_claim_target(client, monkeypatch):
    from zira_dashboard import forklift_advisor as adv
    monkeypatch.setattr(adv, "build_advisor", lambda *a, **k: {
        "recommended": 6, "overloaded": False, "predicted_claim_seconds": 174.0,
        "target_seconds": 240.0, "algo_recommended": 6,
        "backtest": {"n_samples": 40, "mean_actual_seconds": 165.0,
                     "mean_pred_seconds": 170.0, "uncalibrated": False},
        # plus whatever keys the card already reads (coverage, etc.) — keep them
    })
    page = client.get("/staffing").text  # or the real scheduler route
    assert "time-to-claim" in page.lower()
    assert "4 min" in page or "under 4" in page
```

(Adapt the route path + the monkeypatched return to the REAL `build_advisor` contract and the keys the card reads; read `routes/staffing.py` + the advisor block in `staffing.html` first.)

- [ ] **Step 2: Run to verify it fails**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_staffing_forklift_card.py -k claim -v`
Expected: FAIL

- [ ] **Step 3: Implement**

In the forklift advisor block of `staffing.html`, change the recommendation copy to: "Recommend **N** drivers to keep time-to-claim under **{{ target in min }}** · predicted **~{{ predicted in min }}**", with the overloaded branch ("busiest hour is overloaded — can't hit the target even with 12") and the discreet "· algorithm: M" when the algo baseline differs. Convert seconds→minutes in the template (or pass minutes from the route). Keep the existing coverage line.

- [ ] **Step 4: Run to verify pass**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_staffing_forklift_card.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/staffing.html src/zira_dashboard/routes/staffing.py tests/test_staffing_forklift_card.py
git commit -m "feat(forklift): scheduler card shows the time-to-claim target + predicted wait

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Verification

**Files:** none

- [ ] **Step 1: Full suite**

Run: `ZIRA_API_KEY=test PYTHONPATH=src /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest -q`
Expected: all PASS (DB-gated SKIP locally)

- [ ] **Step 2: Lint**

Run: `/Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/ruff check src/ tests/`
Expected: no `F` errors

- [ ] **Step 3: Note** in the PR/summary that DB-gated paths (store reads, settings round-trip) and the live recommendation exercise on prod, where the warmer + history feed the model.

---

## Notes for the implementer
- **Read before mirroring:** Tasks 4/6/7/8 say "read X first" — actually open `forklift_store.py`, `forklift_advisor.py`, `routes/settings.py`+`settings.html`, `routes/staffing.py`+`staffing.html` and match real symbols, the `forklift_calls_daily` columns, and the advisor's existing return keys.
- **Defensive contract:** `forklift_advisor` must never raise into the request path; the queue functions are pure and already total (return inf/0/None, never throw).
- **No JS/Python duplication:** the settings panel is server-rendered for the SLA numbers (no client-side Erlang-C) — deliberate, to avoid the divergence trap.
- **Don't delete** the old `recommend_drivers`/capacity columns in this plan (spec §9 — later cleanup).
