# Forklift Settings Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the forklift settings page to a friendly slider-per-factor layout that always shows, discreetly, the algorithm's own data-derived recommendation beside the user's adjusted value — on both the settings page and the scheduler card.

**Architecture:** Each tunable is a *nullable override* in `forklift_settings` (NULL = auto / follow the algorithm). `forklift_advisor` computes the algorithm's parameter values (data-derived driver throughput + sensible defaults), then produces TWO recommendations — `algo_recommended` (all algorithm values) and `recommended` (resolved overrides) — used by both the card and the settings page so they never disagree. A new `demand_at_percentile` lets a "Plan for: typical↔busiest hour" slider tune which hour is sized to.

**Tech Stack:** Python 3.13, FastAPI, Jinja2, raw psycopg2, requests, pytest. Worktree `/tmp/gpi-fsr` (branch `feat/forklift-settings-redesign`, off origin/main). Tests: `ZIRA_API_KEY=test PYTHONPATH=$(pwd)/src ~/Projects/gpi-plant-manager/.venv/bin/python -m pytest …`. Lint: `~/Projects/gpi-plant-manager/.venv/bin/python -m ruff check …`. DB-gated tests skip locally.

**Spec:** `docs/superpowers/specs/2026-06-27-forklift-settings-redesign-design.md`

---

## File Structure
- `src/zira_dashboard/forklift_demand.py` (modify) — add pure `demand_at_percentile`.
- `src/zira_dashboard/forklift_store.py` (modify) — add `recent_driver_throughput`.
- `src/zira_dashboard/forklift_settings.py` (rewrite) — nullable overrides + `Resolved` + `resolve`/`algorithm_values`.
- `src/zira_dashboard/_schema.py` (modify) — guarded `ALTER` adding 4 nullable override columns.
- `src/zira_dashboard/forklift_advisor.py` (modify) — algorithm values + dual recommendation; enrich `build_advisor` + `demand_summary`.
- `src/zira_dashboard/routes/settings.py` (modify) — forklift GET ctx (both recs, algo values, ranges, per-hour array) + `POST /settings/forklift` (overrides / auto / reset-all).
- `src/zira_dashboard/templates/settings.html` (modify) — slider-per-factor UI + JS live preview.
- `src/zira_dashboard/templates/staffing.html` (modify) — card baseline "· algorithm: M".
- Tests: `tests/test_forklift_demand.py`, `tests/test_forklift_store.py`, `tests/test_forklift_settings.py`, `tests/test_forklift_advisor.py`, `tests/test_staffing_forklift_card.py`, `tests/test_settings_forklift.py`.

**Cross-task contracts:**
- `forklift_demand.demand_at_percentile(by_hour: dict[int,float], pct: float) -> tuple[int|None, float]` (hour, calls).
- `forklift_settings.Settings` fields: `enabled, throughput_override, utilization_override, plan_for_percentile_override, history_samples_override, include_loading_jockeying, coldstart_calls_per_day`. Module consts `DEFAULT_UTILIZATION=0.65`, `DEFAULT_PLAN_FOR_PERCENTILE=1.0`, `DEFAULT_HISTORY_SAMPLES=8`, `DEFAULT_THROUGHPUT=16.0`.
- `forklift_settings.Resolved(throughput, utilization, percentile, history_samples)` with `.effective_throughput`.
- `forklift_settings.resolve(s, *, algo_throughput) -> Resolved` and `algorithm_values(algo_throughput) -> Resolved`.
- `forklift_store.recent_driver_throughput(days=28) -> float | None`.
- advisor `build_advisor` adds keys `algo_recommended: int|None`, `algo_values: dict`; `demand_summary` adds `algo_recommended`, `algo_values`, `overrides`, `hour_values: list[float]`, slider `ranges`.

---

## Task 1: `demand_at_percentile` (pure)

**Files:** Modify `src/zira_dashboard/forklift_demand.py`; Test `tests/test_forklift_demand.py`.

- [ ] **Step 1: Add the failing tests** (append to `tests/test_forklift_demand.py`):
```python
def test_demand_at_percentile_busiest_typical_and_empty():
    by_hour = {8: 30.0, 9: 70.0, 10: 50.0}  # sorted by calls: 30(8),50(10),70(9)
    assert fd.demand_at_percentile(by_hour, 1.0) == (9, 70.0)   # busiest
    assert fd.demand_at_percentile(by_hour, 0.5) == (10, 50.0)  # median hour
    assert fd.demand_at_percentile(by_hour, 0.0) == (8, 30.0)   # quietest
    assert fd.demand_at_percentile({}, 1.0) == (None, 0.0)
```

- [ ] **Step 2: Run → fail.** `ZIRA_API_KEY=test PYTHONPATH=$(pwd)/src ~/Projects/gpi-plant-manager/.venv/bin/python -m pytest tests/test_forklift_demand.py -q` → AttributeError.

- [ ] **Step 3: Implement** (add to `forklift_demand.py`, after `predict_from_history`):
```python
def demand_at_percentile(by_hour: dict[int, float], pct: float) -> tuple[int | None, float]:
    """Per-hour demand at percentile `pct` of the day's hourly call counts.
    pct=1.0 -> busiest hour; 0.5 -> median hour; 0.0 -> quietest. Nearest-rank.
    Returns (hour, calls); (None, 0.0) when there's no data."""
    items = sorted(by_hour.items(), key=lambda kv: (kv[1], kv[0]))
    if not items:
        return (None, 0.0)
    pct = max(0.0, min(1.0, pct))
    idx = round(pct * (len(items) - 1))
    hour, calls = items[idx]
    return (hour, float(calls))
```

- [ ] **Step 4: Run → pass.** Same command. Expected: PASS.

- [ ] **Step 5: Commit.**
```bash
git add src/zira_dashboard/forklift_demand.py tests/test_forklift_demand.py
git commit -m "feat(forklift): add demand_at_percentile (typical<->busiest hour)"
```

---

## Task 2: `recent_driver_throughput` (data-derived driver speed)

**Files:** Modify `src/zira_dashboard/forklift_store.py`; Test `tests/test_forklift_store.py` (DB-gated).

- [ ] **Step 1: Append the DB-gated test** to `tests/test_forklift_store.py` (it already has the `pytestmark` skipif on DATABASE_URL):
```python
def test_recent_driver_throughput_from_driver_daily():
    from zira_dashboard import db
    db.bootstrap_schema()
    d = date(2026, 6, 25)
    db.execute("DELETE FROM forklift_driver_daily WHERE day = %s", (d,))
    # 80 calls over 4 on-call hours (14_400_000 ms) -> 20 calls/hr fleet
    forklift_store.upsert_driver_daily([
        {"day": d, "driver_id": "fk-a", "name": "A", "calls": 80, "on_time": 70,
         "late": 10, "avg_ms": 200000, "max_ms": 700000, "utilization_pct": 90,
         "on_call_ms": 14_400_000, "available_ms": 16_000_000},
    ])
    rate = forklift_store.recent_driver_throughput(days=3650)
    assert rate is not None and 19.0 < rate < 21.0


def test_recent_driver_throughput_none_on_thin_data():
    from zira_dashboard import db
    db.bootstrap_schema()
    db.execute("DELETE FROM forklift_driver_daily")
    assert forklift_store.recent_driver_throughput(days=1) is None
```

- [ ] **Step 2: Run → SKIP locally** (no DATABASE_URL); will FAIL in CI until implemented.

- [ ] **Step 3: Implement** (add to `forklift_store.py`):
```python
# Below this many total on-call hours in the window, the derived rate is too
# noisy to trust -> caller falls back to the default throughput.
_MIN_ONCALL_HOURS = 2.0


def recent_driver_throughput(days: int = 28) -> float | None:
    """Data-derived per-driver throughput (calls/hour) = total completed calls
    / total on-call hours across forklift_driver_daily in the last `days`.
    None when there isn't enough on-call time to be meaningful."""
    from . import db
    rows = db.query(
        "SELECT COALESCE(SUM(calls),0) AS calls, COALESCE(SUM(on_call_ms),0) AS ms "
        "FROM forklift_driver_daily WHERE day >= (CURRENT_DATE - %s::int)",
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

- [ ] **Step 4: Run** (CI/DB) → PASS; SKIP locally.

- [ ] **Step 5: Commit.**
```bash
git add src/zira_dashboard/forklift_store.py tests/test_forklift_store.py
git commit -m "feat(forklift): derive per-driver throughput from forklift_driver_daily"
```

---

## Task 3: `forklift_settings` → nullable overrides + resolver

**Files:** Rewrite `src/zira_dashboard/forklift_settings.py`; Modify `src/zira_dashboard/_schema.py`; Test `tests/test_forklift_settings.py`.

- [ ] **Step 1: Schema migration.** In `_schema.py`, immediately AFTER the existing `forklift_settings` CREATE/seed block, add guarded ALTERs (idempotent; works for fresh + existing installs):
```sql
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS throughput_override NUMERIC;
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS utilization_override NUMERIC;
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS plan_for_percentile_override NUMERIC;
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS history_samples_override INTEGER;
```
(The prior `calls_per_hour`/`target_utilization`/`history_samples` columns are now superseded — left in place, unread.)

- [ ] **Step 2: Write the (non-DB) failing tests** in `tests/test_forklift_settings.py`:
```python
from zira_dashboard import forklift_settings as fs


def test_resolve_uses_algorithm_values_when_overrides_none():
    s = fs.Settings()  # all overrides None
    r = fs.resolve(s, algo_throughput=18.0)
    assert r.throughput == 18.0
    assert r.utilization == fs.DEFAULT_UTILIZATION == 0.65
    assert r.percentile == fs.DEFAULT_PLAN_FOR_PERCENTILE == 1.0
    assert r.history_samples == fs.DEFAULT_HISTORY_SAMPLES == 8
    assert round(r.effective_throughput, 2) == round(18.0 * 0.65, 2)


def test_resolve_prefers_overrides():
    s = fs.Settings(throughput_override=24.0, utilization_override=0.8,
                    plan_for_percentile_override=0.5, history_samples_override=4)
    r = fs.resolve(s, algo_throughput=18.0)
    assert (r.throughput, r.utilization, r.percentile, r.history_samples) == (24.0, 0.8, 0.5, 4)


def test_algorithm_values_ignores_overrides():
    s = fs.Settings(throughput_override=24.0, utilization_override=0.9)
    a = fs.algorithm_values(s, algo_throughput=18.0)
    assert a.throughput == 18.0 and a.utilization == 0.65 and a.percentile == 1.0
```

- [ ] **Step 3: Run → fail** (`ImportError`/`AttributeError`).

- [ ] **Step 4: Rewrite `forklift_settings.py`:**
```python
"""Forklift demand-advisor settings. Each tunable is a NULLABLE OVERRIDE:
NULL = "auto" (follow the algorithm's own value). Singleton row (id=1), cached
in process, invalidated on save() — same pattern as auto_lunch_settings.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

# The algorithm's own default values for the policy knobs (driver throughput is
# data-derived elsewhere and passed in). These are the grey "tick" values.
DEFAULT_UTILIZATION = 0.65
DEFAULT_PLAN_FOR_PERCENTILE = 1.0   # 1.0 = busiest hour; lower = more typical
DEFAULT_HISTORY_SAMPLES = 8
DEFAULT_THROUGHPUT = 16.0           # fallback when no data-derived rate yet


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    throughput_override: float | None = None
    utilization_override: float | None = None
    plan_for_percentile_override: float | None = None
    history_samples_override: int | None = None
    include_loading_jockeying: bool = False
    coldstart_calls_per_day: float = 0.0


@dataclass(frozen=True)
class Resolved:
    throughput: float
    utilization: float
    percentile: float
    history_samples: int

    @property
    def effective_throughput(self) -> float:
        return max(0.1, self.throughput * self.utilization)


def resolve(s: Settings, *, algo_throughput: float) -> Resolved:
    """Effective parameters: each override if set, else the algorithm's value."""
    return Resolved(
        throughput=s.throughput_override if s.throughput_override is not None else algo_throughput,
        utilization=s.utilization_override if s.utilization_override is not None else DEFAULT_UTILIZATION,
        percentile=s.plan_for_percentile_override if s.plan_for_percentile_override is not None else DEFAULT_PLAN_FOR_PERCENTILE,
        history_samples=s.history_samples_override if s.history_samples_override is not None else DEFAULT_HISTORY_SAMPLES,
    )


def algorithm_values(s: Settings, *, algo_throughput: float) -> Resolved:
    """The algorithm's own values, ignoring overrides (the baseline / ticks)."""
    return resolve(Settings(enabled=s.enabled), algo_throughput=algo_throughput)


DEFAULT = Settings()

_lock = RLock()
_cache: Settings | None = None


def _row_to_settings(row: dict) -> Settings:
    def _f(v):
        return float(v) if v is not None else None
    def _i(v):
        return int(v) if v is not None else None
    return Settings(
        enabled=bool(row.get("enabled", True)),
        throughput_override=_f(row.get("throughput_override")),
        utilization_override=_f(row.get("utilization_override")),
        plan_for_percentile_override=_f(row.get("plan_for_percentile_override")),
        history_samples_override=_i(row.get("history_samples_override")),
        include_loading_jockeying=bool(row.get("include_loading_jockeying", False)),
        coldstart_calls_per_day=float(row.get("coldstart_calls_per_day") or 0.0),
    )


def _load_from_db() -> Settings:
    from . import db
    rows = db.query(
        "SELECT enabled, throughput_override, utilization_override, "
        "plan_for_percentile_override, history_samples_override, "
        "include_loading_jockeying, coldstart_calls_per_day "
        "FROM forklift_settings WHERE id = 1"
    )
    return _row_to_settings(rows[0]) if rows else DEFAULT


def current() -> Settings:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load_from_db()
        return _cache


def save(s: Settings) -> None:
    global _cache
    from . import db
    db.execute(
        "INSERT INTO forklift_settings "
        "(id, enabled, throughput_override, utilization_override, "
        "plan_for_percentile_override, history_samples_override, "
        "include_loading_jockeying, coldstart_calls_per_day) "
        "VALUES (1, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET enabled = EXCLUDED.enabled, "
        "throughput_override = EXCLUDED.throughput_override, "
        "utilization_override = EXCLUDED.utilization_override, "
        "plan_for_percentile_override = EXCLUDED.plan_for_percentile_override, "
        "history_samples_override = EXCLUDED.history_samples_override, "
        "include_loading_jockeying = EXCLUDED.include_loading_jockeying, "
        "coldstart_calls_per_day = EXCLUDED.coldstart_calls_per_day",
        (s.enabled, s.throughput_override, s.utilization_override,
         s.plan_for_percentile_override, s.history_samples_override,
         s.include_loading_jockeying, s.coldstart_calls_per_day),
    )
    with _lock:
        _cache = s


def reload() -> Settings:
    global _cache
    with _lock:
        _cache = _load_from_db()
        return _cache
```

- [ ] **Step 5: Run → pass** the three non-DB tests. Add a DB-gated round-trip test (override set + NULL) mirroring the existing DB-gated style; SKIP locally.

- [ ] **Step 6: ruff + commit.**
```bash
~/Projects/gpi-plant-manager/.venv/bin/python -m ruff check src/zira_dashboard/forklift_settings.py src/zira_dashboard/_schema.py tests/test_forklift_settings.py
git add src/zira_dashboard/forklift_settings.py src/zira_dashboard/_schema.py tests/test_forklift_settings.py
git commit -m "feat(forklift): settings as nullable overrides + resolver (auto vs override)"
```

---

## Task 4: `forklift_advisor` — algorithm values + dual recommendation

**Files:** Modify `src/zira_dashboard/forklift_advisor.py`; Test `tests/test_forklift_advisor.py`.

**Design:** `_forecast` takes a `history_samples` int (not the whole cfg) so it can be called for both the resolved and algorithm windows. Compute `algo_throughput = forklift_store.recent_driver_throughput() or DEFAULT_THROUGHPUT` (wrapped). Resolve params; build the resolved forecast; compute `recommended` from `demand_at_percentile(by_hour, resolved.percentile)` ÷ `resolved.effective_throughput`. Compute `algo_recommended` the same way with `algorithm_values` (default percentile/util, algo_throughput) — reusing the same forecast if the history window matches (it does unless the user overrode the window).

- [ ] **Step 1: Add/adjust tests** in `tests/test_forklift_advisor.py`. Existing tests call `build_advisor(target_day, scheduled, backups)` and monkeypatch `forklift_advisor.forklift_store.calls_daily_for_weekday` + `app_settings.get_setting`; they must keep passing. Add:
```python
def test_build_advisor_reports_algo_and_user_recommendations(monkeypatch):
    monkeypatch.setattr(forklift_advisor.forklift_store, "calls_daily_for_weekday",
                        lambda wd, limit=8: [{"day": date(2026, 6, 19), "total_calls": 420,
                                              "by_hour": {"9": {"calls": 70}, "8": {"calls": 30}},
                                              "by_station": {}}])
    monkeypatch.setattr(forklift_advisor.forklift_store, "recent_driver_throughput",
                        lambda days=28: None)  # -> DEFAULT_THROUGHPUT 16
    monkeypatch.setattr(forklift_advisor.app_settings, "get_setting", lambda k: [])
    # user overrides driver speed way up -> their recommendation drops below algo's
    monkeypatch.setattr(forklift_advisor, "_cfg",
                        lambda: forklift_advisor.forklift_settings.Settings(throughput_override=70.0))
    adv = forklift_advisor.build_advisor(date(2026, 6, 26), scheduled=2, backups=0)
    assert adv["available"] is True
    assert adv["algo_recommended"] == 7      # ceil(70 / (16*0.65=10.4))
    assert adv["recommended"] == 2           # ceil(70 / (70*0.65=45.5)) = 2
    assert adv["recommended"] < adv["algo_recommended"]
```
(Keep the existing `test_build_advisor_with_history` etc. — verify they still pass; with all-auto settings `recommended == algo_recommended == 7`.)

- [ ] **Step 2: Run → fail** (`algo_recommended` KeyError / `_cfg` returns old Settings shape).

- [ ] **Step 3: Implement.** Refactor `_forecast(target_day, history_samples)` to take an int; in `build_advisor`:
```python
def build_advisor(target_day: date, scheduled: int, backups: int) -> dict:
    cfg = _cfg()
    if not cfg.enabled:
        return {"available": False}

    try:
        algo_throughput = forklift_store.recent_driver_throughput()
    except Exception:
        algo_throughput = None
    algo_throughput = algo_throughput or forklift_settings.DEFAULT_THROUGHPUT

    resolved = forklift_settings.resolve(cfg, algo_throughput=algo_throughput)
    algo = forklift_settings.algorithm_values(cfg, algo_throughput=algo_throughput)

    forecast = _forecast(target_day, resolved.history_samples)
    if forecast.basis == "none" or forecast.total_calls <= 0:
        return {"available": False}

    def _rec(params):
        _, demand = forklift_demand.demand_at_percentile(forecast.by_hour, params.percentile)
        return forklift_demand.recommend_drivers(demand, params.effective_throughput) if demand > 0 else None

    recommended = _rec(resolved)
    algo_recommended = _rec(algo)
    coverage = forklift_demand.assess_coverage(recommended, scheduled, backups) if recommended else None
    backup_names = app_settings.get_setting("forklift_overload_responders") or []
    peak = forecast.peak_calls or 1.0
    hours = [(h, round(c / peak, 3)) for h, c in sorted(forecast.by_hour.items())]
    peak_label = (f"{forecast.peak_hour}:00–{forecast.peak_hour + 1}:00"
                  if forecast.peak_hour is not None else "—")
    return {
        "available": True,
        "day_label": target_day.strftime("%a %b %-d"),
        "total_calls": int(round(forecast.total_calls)),
        "peak_label": peak_label,
        "hours": hours,
        "recommended": recommended,
        "algo_recommended": algo_recommended,
        "coverage": coverage,
        "basis": forecast.basis,
        "n_days": forecast.n_days,
        "backup_names": backup_names,
    }
```
Note: if `resolved.history_samples != algo.history_samples` (user overrode the window), `algo_recommended` should use the algorithm-window forecast. For v1 keep it simple and acceptable: both use the resolved forecast (the window override mainly affects demand smoothing, second-order for the baseline display). Add a code comment saying so.

- [ ] **Step 4:** Update `demand_summary(target_day)` to also return `algo_recommended`, `algo_values` (dict of the algorithm's throughput/utilization/percentile/history_samples), `overrides` (the cfg override fields, None=auto), `hour_values` (sorted list of per-hour call counts for the JS preview), and slider `ranges` (constants). Compute via the same `_forecast` + resolve/algorithm_values. Must never raise.

- [ ] **Step 5: Run → pass** the new + existing advisor tests. ruff.

- [ ] **Step 6: Commit.**
```bash
git add src/zira_dashboard/forklift_advisor.py tests/test_forklift_advisor.py
git commit -m "feat(forklift): advisor computes algorithm baseline + user recommendation"
```

---

## Task 5: Settings route — GET ctx + POST overrides/auto/reset

**Files:** Modify `src/zira_dashboard/routes/settings.py`; Test `tests/test_settings_forklift.py`.

**Read the existing PR #11 forklift handling first** (the `forklift` ctx build in `settings_page` and `POST /settings/forklift`) and adapt it.

- [ ] **Step 1: Failing test** (`tests/test_settings_forklift.py`) for the POST parsing helper. Factor parsing into a pure helper `_parse_forklift_overrides(form: dict) -> forklift_settings.Settings` so it's unit-testable without a request:
```python
from zira_dashboard import forklift_settings
from zira_dashboard.routes import settings as settings_route


def test_parse_forklift_overrides_auto_vs_set():
    # "auto" / blank -> None (follow algorithm); a value -> override
    s = settings_route._parse_forklift_overrides({
        "enabled": "on", "throughput": "auto", "utilization_pct": "70",
        "plan_for": "0.8", "history_samples": "", "include_loading_jockeying": "on",
        "coldstart_calls_per_day": "0",
    })
    assert s.enabled is True
    assert s.throughput_override is None          # "auto"
    assert s.utilization_override == 0.70         # 70% -> 0.70
    assert s.plan_for_percentile_override == 0.8
    assert s.history_samples_override is None     # blank -> auto
    assert s.include_loading_jockeying is True


def test_parse_forklift_overrides_clamps():
    s = settings_route._parse_forklift_overrides({"utilization_pct": "999", "throughput": "0"})
    assert s.utilization_override == 1.0          # clamp <=100%
    assert s.throughput_override == 0.1 or s.throughput_override >= 1  # clamp >0 (define floor 1.0)
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement** `_parse_forklift_overrides(form)` (module-level in `routes/settings.py`): each numeric field reads the form value; the literal string `"auto"` or empty → `None`; otherwise parse+clamp (throughput 1–60; utilization_pct 5–100 → /100 → 0.05–1.0; plan_for 0.5–1.0; history_samples 2–20). Checkboxes via truthiness. Returns `forklift_settings.Settings(...)`. Then `POST /settings/forklift` calls it + `forklift_settings.save(...)` + 303 redirect to `?saved=1&section=forklift`. A "reset all to algorithm" is just a POST with every numeric field = `"auto"` (the template's reset link/button submits that), so no separate route needed.

- [ ] **Step 4:** In `settings_page` GET, replace the forklift ctx with `forklift_advisor.demand_summary(next_working_day)` (which now carries `algo_recommended`, `algo_values`, `overrides`, `hour_values`, `ranges`) merged with the current `forklift_settings.current()` overrides, all wrapped in try/except → a minimal `{"enabled": ...}` so the page never 500s.

- [ ] **Step 5: Run → pass.** ruff.

- [ ] **Step 6: Commit.**
```bash
git add src/zira_dashboard/routes/settings.py tests/test_settings_forklift.py
git commit -m "feat(forklift): settings route reads/writes nullable overrides (auto/override/reset)"
```

---

## Task 6: Settings page UI — sliders + live preview

**Files:** Modify `src/zira_dashboard/templates/settings.html`.

Replace the current forklift section panel (number inputs) with the approved direction-B layout. **Match the approved mockup** at `.superpowers/brainstorm/84992-1782733887/content/redesign-full-B.html`.

- [ ] **Step 1:** Headline card — big `forklift.recommended` "dedicated drivers — {day_label}"; discreet grey "↳ the algorithm would recommend **{forklift.algo_recommended}** · *match it*" (the "match it" link triggers Reset-all). Coverage + demand lines as today.

- [ ] **Step 2:** Four `<input type="range">` sliders inside the POST form, each with: a friendly `<label>` + a value readout `<output>`; the thumb at the effective value; a grey tick at the algorithm value (a positioned marker element computed from `algo_values`); a `↺` reset control that sets that field's value to `"auto"`; min/max from `ranges`. Field names: `throughput`, `utilization_pct`, `plan_for`, `history_samples` (match Task 5's parser). For each, when its override is None (auto), render the slider at the algorithm value AND include a hidden marker that the field is "auto" (e.g., the readout shows "(auto)"); when the user drags, JS clears the auto state.

- [ ] **Step 3:** Toggles (`enabled`, `include_loading_jockeying`); Advanced `<details>` with `coldstart_calls_per_day`; Save + "Reset all to algorithm" buttons.

- [ ] **Step 4: Live preview JS** (inline `<script>`): embed `data-hour-values="{{ forklift.hour_values|tojson }}"` and the algorithm constants. On any slider `input`, compute `pct = plan_for`, `demand = nearestRankPercentile(hourValues, pct)`, `eff = throughput * utilization`, `rec = max(1, Math.ceil(demand / eff))`, and update the headline "Recommend N" + each readout. Mirror `demand_at_percentile`'s nearest-rank (sort ascending, `idx = round(pct*(n-1))`). If `hourValues` is empty, leave the headline and show "recommendation builds as history accrues".

- [ ] **Step 5: Verify the template compiles + renders.** `ZIRA_API_KEY=test PYTHONPATH=$(pwd)/src ~/Projects/gpi-plant-manager/.venv/bin/python -c "from zira_dashboard.deps import templates; templates.get_template('settings.html'); print('ok')"`. Render the forklift section block with a stub `forklift` ctx through the Jinja env (extract-and-render, like `tests/test_staffing_forklift_card.py` does) and assert the sliders + both numbers appear. Add that as a test in `tests/test_settings_forklift.py`.

- [ ] **Step 6: Commit.**
```bash
git add src/zira_dashboard/templates/settings.html tests/test_settings_forklift.py
git commit -m "feat(forklift): slider-per-factor settings UI with algorithm ticks + live preview"
```

---

## Task 7: Scheduler card baseline

**Files:** Modify `src/zira_dashboard/templates/staffing.html`; Test `tests/test_staffing_forklift_card.py`.

- [ ] **Step 1: Update the render test** in `tests/test_staffing_forklift_card.py`: the stub advisor model now includes `algo_recommended`; assert the card shows the discreet "algorithm: M" when it differs from `recommended`:
```python
# add algo_recommended to the stub model in test_forklift_block_renders_card_from_advisor_model
model["algo_recommended"] = 6   # recommended is 3 -> show "algorithm: 6"
...
assert "algorithm: 6" in rendered
```

- [ ] **Step 2: Run → fail** (string absent).

- [ ] **Step 3: Implement** in `staffing.html`: in the recommend line, after "Recommend {{ recommended }} dedicated driver(s)", add
```html
{% if forklift_advisor.algo_recommended and forklift_advisor.algo_recommended != forklift_advisor.recommended %}<span style="color:#888;font-weight:400"> · algorithm: {{ forklift_advisor.algo_recommended }}</span>{% endif %}
```

- [ ] **Step 4: Run → pass.** Confirm the full forklift+staffing+settings suite still passes; ruff clean; `staffing.html` + `settings.html` compile.

- [ ] **Step 5: Commit.**
```bash
git add src/zira_dashboard/templates/staffing.html tests/test_staffing_forklift_card.py
git commit -m "feat(forklift): show algorithm baseline on the scheduler card"
```

---

## Self-Review
**Spec coverage:** §2 dual-recommendation + auto/override → Tasks 3,4. §3 four knobs (incl. new Plan-for) → Tasks 1 (percentile), 3 (fields), 6 (sliders). §4 data model (nullable overrides + migration) → Task 3. §5 algorithm changes (`demand_at_percentile`, `recent_driver_throughput`, dual recs, `demand_summary`) → Tasks 1,2,4. §6 UI (settings sliders + live preview; card baseline) → Tasks 6,7. §8 testing → per task. ✔ No gaps.

**Placeholder scan:** logic tasks (1–4) have complete code; UI tasks (5,6) specify exact field names, ranges, the JS algorithm (nearest-rank mirror), and a compile+render test. No "TBD"/"handle edge cases".

**Type consistency:** `Settings` override fields + `Resolved`/`resolve`/`algorithm_values` consistent across Tasks 3,4,5. Field names `throughput`/`utilization_pct`/`plan_for`/`history_samples` match between Task 5 parser and Task 6 template. `demand_at_percentile` signature consistent (Task 1 ↔ Task 4 ↔ Task 6 JS). `algo_recommended` key consistent (Task 4 ↔ Task 7).

---

## Execution Handoff
Plan saved to `docs/superpowers/plans/2026-06-27-forklift-settings-redesign.md`. Two execution options: **(1) Subagent-Driven (recommended)** — fresh subagent per task + review; **(2) Inline Execution** — batch with checkpoints. (Given the prior settings feature shipped well as one well-specified implementer + a rigorous review, that hybrid is also on the table.)
