
# Automated Recycling Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Automatically update Repair and Dismantle levels from L30 production, with separate threshold settings in the corresponding People Matrix headers and Odoo synchronization.

**Architecture:** A typed settings module persists independent Repair/Dismantler threshold sets and last-run data in app_settings. A domain module calculates eligible L30 attainment from production_daily and applies only changed levels through the existing Odoo-first skill writer. The People Matrix modal saves a group and starts the same run service used by a five-minute, self-throttling daily app tick.

**Tech Stack:** Python 3.12, FastAPI, PostgreSQL JSONB, Jinja2, vanilla JavaScript/CSS, pytest.

## Global Constraints

- Automation is limited to matrix skills Repair and Dismantle; Dismantle maps to Dismantler work centers.
- Initial thresholds are level 3 at 90%, level 2 at 80%, and level 1 at 70%; level 0 is below level 1.
- Evaluate the prior 30 calendar days. Require two qualified days; a qualified group day totals at least four hours.
- Divide current work-center goal and attributed production equally by that center's operator count for the day.
- Normalize qualified daily output using productive_minutes_per_day divided by 60.
- Apply every automatic level change through skill_levels.set_person_skill_level. Odoo must accept before the local mirror changes.
- Keep manual matrix edits available. An eligible automated run may subsequently promote or demote the same two skills.
- Use app_settings for settings and run summaries. Do not alter production attribution or add a schema table.
- Commit only files named in the current task.

## File Structure

| File | Responsibility |
| --- | --- |
| src/zira_dashboard/automated_skill_settings.py | Buckets and run-summary persistence. |
| src/zira_dashboard/automated_skills.py | Pure calculation, production loader, Odoo run service, run lock, and daily gate. |
| src/zira_dashboard/routes/skills.py | Matrix context plus save/recalculate JSON endpoint. |
| src/zira_dashboard/templates/skills.html | Independent header sort controls, gears, and shared modal. |
| src/zira_dashboard/static/skills-page.js | Modal state, live unit preview, and save response handling. |
| src/zira_dashboard/static/skills.css | Hover gear and accessible modal styling. |
| src/zira_dashboard/app.py | Five-minute daily worker registration. |
| tests/test_automated_skill_settings.py | Store tests. |
| tests/test_automated_skills.py | Calculation, run, lock, and daily tests. |
| tests/test_skills_automation_api.py | API and matrix-context tests. |
| tests/test_skills_template_render.py | Template contract. |
| tests/test_skills_static.py | Browser-code contract. |

## Task 1: Add typed group settings

**Files:**

- Create: src/zira_dashboard/automated_skill_settings.py
- Create: tests/test_automated_skill_settings.py

**Interfaces:**

- BucketSettings(level_3_min: float, level_2_min: float, level_1_min: float)
- RunSummary(group, trigger, evaluated, changed, unchanged, skipped, failures, run_at)
- current(group), all_current(), save(group, settings), last_run(group), save_last_run(summary)

- [ ] **Step 1: Write failing tests**

~~~python
from zira_dashboard import automated_skill_settings as store
import pytest


def test_defaults_are_independent_per_group(monkeypatch):
    monkeypatch.setattr(store.app_settings, "get_setting", lambda key: None)

    assert store.current("Repair") == store.BucketSettings(90.0, 80.0, 70.0)
    assert store.current("Dismantler") == store.BucketSettings(90.0, 80.0, 70.0)
    assert store.current("Repair") is not store.current("Dismantler")


def test_invalid_percentages_and_order_are_rejected():
    with pytest.raises(ValueError, match="0 through 100"):
        store.validate(store.BucketSettings(101, 80, 70))
    with pytest.raises(ValueError, match="Level 3 >= Level 2 >= Level 1"):
        store.validate(store.BucketSettings(80, 90, 70))


def test_save_preserves_unedited_group(monkeypatch):
    written = {}
    monkeypatch.setattr(store.app_settings, "get_setting", lambda key: {
        "Repair": {"level_3_min": 91, "level_2_min": 81, "level_1_min": 71}
    })
    monkeypatch.setattr(store.app_settings, "set_setting", lambda key, value: written.update({key: value}))

    store.save("Dismantler", store.BucketSettings(92, 82, 72))

    assert written[store.CONFIG_KEY] == {
        "Repair": {"level_3_min": 91.0, "level_2_min": 81.0, "level_1_min": 71.0},
        "Dismantler": {"level_3_min": 92.0, "level_2_min": 82.0, "level_1_min": 72.0},
    }
~~~

- [ ] **Step 2: Verify the tests fail**

Run: pytest tests/test_automated_skill_settings.py -v

Expected: collection fails because the module does not exist.

- [ ] **Step 3: Implement the store**

~~~python
from __future__ import annotations
from dataclasses import asdict, dataclass
from . import app_settings

CONFIG_KEY = "automated_skills.bucket_settings"
RUNS_KEY = "automated_skills.last_runs"
GROUPS = ("Repair", "Dismantler")


@dataclass(frozen=True)
class BucketSettings:
    level_3_min: float = 90.0
    level_2_min: float = 80.0
    level_1_min: float = 70.0


@dataclass(frozen=True)
class RunSummary:
    group: str
    trigger: str
    evaluated: int
    changed: int
    unchanged: int
    skipped: int
    failures: tuple[dict[str, str], ...]
    run_at: str | None


def validate(value: BucketSettings) -> BucketSettings:
    numbers = (value.level_3_min, value.level_2_min, value.level_1_min)
    if any(not isinstance(item, (int, float)) or isinstance(item, bool) or not 0 <= float(item) <= 100 for item in numbers):
        raise ValueError("Skill bucket percentages must be numbers from 0 through 100.")
    result = BucketSettings(*(float(item) for item in numbers))
    if not result.level_3_min >= result.level_2_min >= result.level_1_min:
        raise ValueError("Skill buckets must satisfy Level 3 >= Level 2 >= Level 1.")
    return result


def current(group: str) -> BucketSettings:
    if group not in GROUPS:
        raise ValueError("Unsupported automated-skill group: " + group)
    raw = app_settings.get_setting(CONFIG_KEY)
    item = raw.get(group) if isinstance(raw, dict) else None
    try:
        return validate(BucketSettings(**item)) if isinstance(item, dict) else BucketSettings()
    except (TypeError, ValueError):
        return BucketSettings()


def all_current() -> dict[str, BucketSettings]:
    return {group: current(group) for group in GROUPS}


def save(group: str, value: BucketSettings) -> None:
    if group not in GROUPS:
        raise ValueError("Unsupported automated-skill group: " + group)
    payload = {name: asdict(current(name)) for name in GROUPS}
    payload[group] = asdict(validate(value))
    app_settings.set_setting(CONFIG_KEY, payload)
~~~

Implement last_run and save_last_run with RUNS_KEY. The stored summary must use a JSON list for failures and restore it as a tuple.

- [ ] **Step 4: Add the run-summary test and verify**

~~~python
def test_last_run_round_trips_failures(monkeypatch):
    writes = {}
    monkeypatch.setattr(store.app_settings, "get_setting", lambda key: writes.get(key))
    monkeypatch.setattr(store.app_settings, "set_setting", lambda key, value: writes.update({key: value}))
    expected = store.RunSummary("Repair", "manual", 4, 1, 2, 1,
        ({"name": "Ana", "error": "Odoo down"},), "2026-07-13T18:00:00+00:00")

    store.save_last_run(expected)

    assert store.last_run("Repair") == expected
~~~

Run: pytest tests/test_automated_skill_settings.py -v

Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add src/zira_dashboard/automated_skill_settings.py tests/test_automated_skill_settings.py
git commit -m "feat: persist automated skill buckets"
~~~

## Task 2: Calculate L30 group attainment

**Files:**

- Create: src/zira_dashboard/automated_skills.py
- Create: tests/test_automated_skills.py

**Interfaces:**

- DailyRecord(day, person_id, name, wc_name, units, hours, operator_count)
- Evaluation(person_id, name, days, attainment_pct, level)
- bucket_for(attainment_pct, settings)
- evaluate(records, goals, settings, standard_full_day_hours, min_hours=4.0, min_days=2)

- [ ] **Step 1: Write failing calculation tests**

~~~python
from datetime import date
from zira_dashboard import automated_skills as subject
from zira_dashboard.automated_skill_settings import BucketSettings


def record(day, name, units, hours, operators=1, wc="Repair 1", person_id=1):
    return subject.DailyRecord(date.fromisoformat(day), person_id, name, wc, units, hours, operators)


def test_bucket_boundaries_are_inclusive():
    config = BucketSettings(90, 80, 70)
    assert subject.bucket_for(90.0, config) == 3
    assert subject.bucket_for(80.0, config) == 2
    assert subject.bucket_for(70.0, config) == 1
    assert subject.bucket_for(69.99, config) == 0


def test_two_qualified_days_average_attainment():
    rows = [record("2026-07-01", "Ana", 90, 8), record("2026-07-02", "Ana", 100, 8)]

    assert subject.evaluate(rows, {"Repair 1": 100}, BucketSettings(), 8) == [
        subject.Evaluation(1, "Ana", 2, 95.0, 3)
    ]


def test_under_four_hour_day_does_not_qualify():
    rows = [record("2026-07-01", "Ana", 100, 8), record("2026-07-02", "Ana", 100, 3)]

    assert subject.evaluate(rows, {"Repair 1": 100}, BucketSettings(), 8) == [
        subject.Evaluation(1, "Ana", 1, None, None)
    ]
~~~

- [ ] **Step 2: Verify failure**

Run: pytest tests/test_automated_skills.py -v

Expected: collection fails because the module does not exist.

- [ ] **Step 3: Implement the pure calculator**

~~~python
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from .automated_skill_settings import BucketSettings


@dataclass(frozen=True)
class DailyRecord:
    day: date
    person_id: int
    name: str
    wc_name: str
    units: float
    hours: float
    operator_count: int


@dataclass(frozen=True)
class Evaluation:
    person_id: int
    name: str
    days: int
    attainment_pct: float | None
    level: int | None


def bucket_for(value: float, settings: BucketSettings) -> int:
    if value >= settings.level_3_min:
        return 3
    if value >= settings.level_2_min:
        return 2
    if value >= settings.level_1_min:
        return 1
    return 0


def evaluate(records, goals, settings, standard_full_day_hours, min_hours=4.0, min_days=2):
    by_day = defaultdict(lambda: {"units": 0.0, "hours": 0.0, "goal": 0.0})
    names = {}
    for item in records:
        if item.operator_count <= 0 or item.wc_name not in goals:
            continue
        totals = by_day[(item.person_id, item.day)]
        totals["units"] += float(item.units)
        totals["hours"] += float(item.hours)
        totals["goal"] += float(goals[item.wc_name]) / item.operator_count
        names[item.person_id] = item.name
    scores = defaultdict(list)
    for (person_id, day), totals in by_day.items():
        if totals["hours"] < min_hours or totals["goal"] <= 0:
            continue
        normalized = totals["units"] / totals["hours"] * standard_full_day_hours
        scores[person_id].append(normalized / totals["goal"] * 100)
    result = []
    for person_id in sorted(names, key=lambda value: names[value].lower()):
        values = scores[person_id]
        if len(values) < min_days:
            result.append(Evaluation(person_id, names[person_id], len(values), None, None))
        else:
            average = sum(values) / len(values)
            result.append(Evaluation(person_id, names[person_id], len(values), average, bucket_for(average, settings)))
    return result
~~~

- [ ] **Step 4: Add fairness tests**

~~~python
def test_two_people_split_goal_and_output_equally():
    rows = [record("2026-07-01", "Ana", 50, 8, operators=2),
            record("2026-07-02", "Ana", 45, 8, operators=2)]

    assert subject.evaluate(rows, {"Repair 1": 100}, BucketSettings(), 8)[0] == (
        subject.Evaluation(1, "Ana", 2, 95.0, 3)
    )


def test_same_day_multiple_centers_combine_goal_shares():
    rows = [
        record("2026-07-01", "Ana", 50, 4, wc="Repair 1"),
        record("2026-07-01", "Ana", 100, 4, wc="Repair 2"),
        record("2026-07-02", "Ana", 150, 8, wc="Repair 2"),
    ]

    assert subject.evaluate(rows, {"Repair 1": 50, "Repair 2": 150}, BucketSettings(), 8)[0] == (
        subject.Evaluation(1, "Ana", 2, 100.0, 3)
    )
~~~

- [ ] **Step 5: Run and commit**

Run: pytest tests/test_automated_skills.py -v

Expected: PASS.

~~~bash
git add src/zira_dashboard/automated_skills.py tests/test_automated_skills.py
git commit -m "feat: calculate automated recycling skill levels"
~~~

## Task 3: Load production and synchronize changed skills

**Files:**

- Modify: src/zira_dashboard/automated_skills.py
- Modify: tests/test_automated_skills.py
- Modify: src/zira_dashboard/app.py

**Interfaces:**

- GROUP_TO_SKILL = {"Repair": "Repair", "Dismantler": "Dismantle"}
- records_for_group(group, start, end), goals_for_group(group), run_group(group, trigger, through_day), run_daily_if_due(now)
- RunInProgress on lock contention
- app tick named automated skills at 300 seconds

- [ ] **Step 1: Write failing loader and orchestration tests**

~~~python
def test_records_for_group_counts_non_absent_operators(monkeypatch):
    captured = {}
    def fake_query(sql, params):
        captured["sql"] = sql
        return [{"day": date(2026, 7, 1), "person_id": 7, "name": "Ana",
                 "wc_name": "Repair 1", "units": 50, "hours": 8, "operator_count": 2}]
    monkeypatch.setattr(subject.db, "query", fake_query)
    monkeypatch.setattr(subject, "work_centers_for_group", lambda group: {"Repair 1"})

    result = subject.records_for_group("Repair", date(2026, 6, 2), date(2026, 7, 1))

    assert result == [subject.DailyRecord(date(2026, 7, 1), 7, "Ana", "Repair 1", 50.0, 8.0, 2)]
    assert "COUNT(*) OVER (PARTITION BY pd.day, pd.wc_name)" in captured["sql"]
    assert "manual_absences" in captured["sql"]


def test_run_changes_only_eligible_mismatched_level(monkeypatch):
    monkeypatch.setattr(subject, "records_for_group", lambda group, start, end: [
        record("2026-07-01", "Ana", 100, 8), record("2026-07-02", "Ana", 100, 8),
        record("2026-07-01", "Ben", 100, 8, person_id=2),
    ])
    monkeypatch.setattr(subject, "goals_for_group", lambda group: {"Repair 1": 100})
    monkeypatch.setattr(subject.shift_config, "productive_minutes_per_day", lambda: 480)
    monkeypatch.setattr(subject.settings_store, "current", lambda group: BucketSettings())
    monkeypatch.setattr(subject, "current_levels", lambda group: {1: (10, 2), 2: (20, 3)})
    writes = []
    monkeypatch.setattr(subject.skill_levels, "set_person_skill_level",
                        lambda person, skill, level: writes.append((person, skill, level)))

    summary = subject.run_group("Repair", "manual", date(2026, 7, 2))

    assert writes == [(1, 10, 3)]
    assert (summary.evaluated, summary.changed, summary.unchanged, summary.skipped) == (1, 1, 0, 1)
~~~

- [ ] **Step 2: Verify failure**

Run: pytest tests/test_automated_skills.py -k "records_for_group or run_group" -v

Expected: FAIL because the loader and run functions do not exist.

- [ ] **Step 3: Implement database loading and Odoo-first run**

~~~python
from datetime import UTC, datetime, timedelta
from threading import Lock
from . import app_settings, automated_skill_settings as settings_store, db, skill_levels, staffing, work_centers_store, shift_config
from .plant_day import today as plant_today

GROUP_TO_SKILL = {"Repair": "Repair", "Dismantler": "Dismantle"}
_run_lock = Lock()


class RunInProgress(RuntimeError):
    pass


def work_centers_for_group(group):
    if group not in GROUP_TO_SKILL:
        raise ValueError("Unsupported automated-skill group: " + group)
    return {location.name for location in staffing.LOCATIONS if location.skill == group}


def goals_for_group(group):
    return {location.name: float(work_centers_store.goal_per_day(location))
            for location in staffing.LOCATIONS if location.skill == group}


def records_for_group(group, start, end):
    rows = db.query("""
        SELECT pd.day, pd.emp_id::int AS person_id, pd.name, pd.wc_name, pd.units, pd.hours,
               COUNT(*) OVER (PARTITION BY pd.day, pd.wc_name) AS operator_count
        FROM production_daily pd
        WHERE pd.day BETWEEN %s AND %s AND pd.wc_name = ANY(%s)
          AND NOT EXISTS (SELECT 1 FROM manual_absences ma WHERE ma.day = pd.day AND ma.name = pd.name)
    """, (start, end, sorted(work_centers_for_group(group))))
    return [DailyRecord(item["day"], int(item["person_id"]), item["name"], item["wc_name"],
                        float(item["units"]), float(item["hours"]), int(item["operator_count"]))
            for item in rows]


def current_levels(group):
    rows = db.query("""
        SELECT p.id AS person_id, s.id AS skill_id, COALESCE(ps.level, 0) AS level
        FROM people p JOIN skills s ON s.name = %s
        LEFT JOIN person_skills ps ON ps.person_id = p.id AND ps.skill_id = s.id
        WHERE p.active = TRUE AND p.excluded = FALSE
    """, (GROUP_TO_SKILL[group],))
    return {int(item["person_id"]): (int(item["skill_id"]), int(item["level"])) for item in rows}
~~~

~~~python
def run_group(group, trigger, through_day):
    if not _run_lock.acquire(blocking=False):
        raise RunInProgress("An automated skill run is already in progress.")
    try:
        evaluations = evaluate(
            records_for_group(group, through_day - timedelta(days=29), through_day),
            goals_for_group(group),
            settings_store.current(group),
            shift_config.productive_minutes_per_day() / 60.0,
        )
        levels = current_levels(group)
        changed, unchanged, skipped, failures = 0, 0, 0, []
        for item in evaluations:
            if item.level is None:
                skipped += 1
                continue
            current = levels.get(item.person_id)
            if current is None or current[1] == item.level:
                unchanged += 1
                continue
            try:
                skill_levels.set_person_skill_level(item.person_id, current[0], item.level)
                changed += 1
            except skill_levels.SkillSyncError as exc:
                failures.append({"name": item.name, "error": str(exc)})
        summary = settings_store.RunSummary(
            group, trigger, len(evaluations) - skipped, changed, unchanged, skipped,
            tuple(failures), datetime.now(UTC).isoformat(),
        )
        settings_store.save_last_run(summary)
        return summary
    finally:
        _run_lock.release()
~~~

- [ ] **Step 4: Add concrete Odoo failure, demotion, and daily tests**

~~~python
def test_one_odoo_failure_does_not_block_following_person(monkeypatch):
    from zira_dashboard import skill_levels
    records = [
        record("2026-07-01", "Ana", 100, 8),
        record("2026-07-02", "Ana", 100, 8),
        record("2026-07-01", "Ben", 100, 8, person_id=2),
        record("2026-07-02", "Ben", 100, 8, person_id=2),
    ]
    monkeypatch.setattr(subject, "records_for_group", lambda group, start, end: records)
    monkeypatch.setattr(subject, "goals_for_group", lambda group: {"Repair 1": 100})
    monkeypatch.setattr(subject, "current_levels", lambda group: {1: (10, 0), 2: (10, 0)})
    monkeypatch.setattr(subject.shift_config, "productive_minutes_per_day", lambda: 480)
    monkeypatch.setattr(subject.settings_store, "current", lambda group: BucketSettings())
    monkeypatch.setattr(subject.settings_store, "save_last_run", lambda summary: None)
    writes = []
    def writer(person_id, skill_id, level):
        if person_id == 1:
            raise skill_levels.SkillSyncError("Odoo down")
        writes.append((person_id, skill_id, level))
    monkeypatch.setattr(subject.skill_levels, "set_person_skill_level", writer)

    summary = subject.run_group("Repair", "manual", date(2026, 7, 2))

    assert writes == [(2, 10, 3)]
    assert summary.changed == 1
    assert summary.failures == ({"name": "Ana", "error": "Odoo down"},)


def test_eligible_low_attainment_can_demote(monkeypatch):
    rows = [record("2026-07-01", "Ana", 60, 8), record("2026-07-02", "Ana", 60, 8)]
    monkeypatch.setattr(subject, "records_for_group", lambda group, start, end: rows)
    monkeypatch.setattr(subject, "goals_for_group", lambda group: {"Repair 1": 100})
    monkeypatch.setattr(subject, "current_levels", lambda group: {1: (10, 3)})
    monkeypatch.setattr(subject.shift_config, "productive_minutes_per_day", lambda: 480)
    monkeypatch.setattr(subject.settings_store, "current", lambda group: BucketSettings())
    monkeypatch.setattr(subject.settings_store, "save_last_run", lambda summary: None)
    writes = []
    monkeypatch.setattr(subject.skill_levels, "set_person_skill_level",
                        lambda person_id, skill_id, level: writes.append((person_id, skill_id, level)))

    summary = subject.run_group("Repair", "manual", date(2026, 7, 2))

    assert writes == [(1, 10, 0)]
    assert summary.changed == 1


def test_daily_gate_runs_once_after_shift_end(monkeypatch):
    from datetime import datetime, time
    from zoneinfo import ZoneInfo
    writes, calls = {}, []
    monkeypatch.setattr(subject, "plant_today", lambda: date(2026, 7, 13))
    monkeypatch.setattr(subject.shift_config, "shift_end_for", lambda day: time(16, 0))
    monkeypatch.setattr(subject.app_settings, "get_setting", lambda key: writes.get(key))
    monkeypatch.setattr(subject.app_settings, "set_setting", lambda key, value: writes.update({key: value}))
    monkeypatch.setattr(subject, "run_group", lambda group, trigger, day: calls.append((group, trigger, day)) or object())

    assert subject.run_daily_if_due(datetime(2026, 7, 13, 15, 59, tzinfo=ZoneInfo("America/Chicago"))) == []
    subject.run_daily_if_due(datetime(2026, 7, 13, 16, 1, tzinfo=ZoneInfo("America/Chicago")))
    subject.run_daily_if_due(datetime(2026, 7, 13, 16, 2, tzinfo=ZoneInfo("America/Chicago")))

    assert calls == [("Repair", "daily", date(2026, 7, 13)), ("Dismantler", "daily", date(2026, 7, 13))]
    assert writes["automated_skills.last_daily_day"] == {"day": "2026-07-13"}
~~~

- [ ] **Step 5: Implement daily gate and worker**

~~~python
def run_daily_if_due(now):
    day = plant_today()
    local_now = now.astimezone(shift_config.SITE_TZ)
    if local_now.time() < shift_config.shift_end_for(day):
        return []
    if app_settings.get_setting("automated_skills.last_daily_day") == {"day": day.isoformat()}:
        return []
    summaries = []
    for group in GROUP_TO_SKILL:
        try:
            summaries.append(run_group(group, "daily", day))
        except RunInProgress:
            return summaries
    app_settings.set_setting("automated_skills.last_daily_day", {"day": day.isoformat()})
    return summaries
~~~

~~~python
# app.py
async def _tick_automated_skills():
    from . import automated_skills
    await asyncio.to_thread(automated_skills.run_daily_if_due, datetime.now(UTC))


# append to _WARMERS
("automated skills", _tick_automated_skills, 300),
~~~

- [ ] **Step 6: Run and commit**

Run: pytest tests/test_automated_skill_settings.py tests/test_automated_skills.py -v

Expected: PASS.

~~~bash
git add src/zira_dashboard/automated_skills.py src/zira_dashboard/app.py tests/test_automated_skills.py
git commit -m "feat: sync automated recycling skills to Odoo"
~~~

## Task 4: Expose the modal API and matrix context

**Files:**

- Modify: src/zira_dashboard/routes/skills.py
- Create: tests/test_skills_automation_api.py
- Modify: tests/test_staffing_rotations.py

**Interfaces:**

- POST /staffing/skills/automation/{group} accepts level_3_min, level_2_min, level_1_min.
- Success JSON contains ok, settings, summary. Invalid JSON is 400. Lock contention is 409.
- Matrix context adds automation_groups keyed by Repair and Dismantle.

- [ ] **Step 1: Write failing API tests**

~~~python
from fastapi import FastAPI
from fastapi.testclient import TestClient
from zira_dashboard.routes.skills import router


def make_client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_save_recalculate_returns_summary(monkeypatch):
    from zira_dashboard.routes import skills
    saved = []
    monkeypatch.setattr(skills.automated_skill_settings, "save", lambda group, value: saved.append((group, value)))
    monkeypatch.setattr(skills.automated_skills, "run_group",
        lambda group, trigger, day: skills.automated_skill_settings.RunSummary(
            group, trigger, 4, 1, 2, 1, (), "2026-07-13T18:00:00+00:00"))

    response = make_client().post("/staffing/skills/automation/Repair",
        json={"level_3_min": 91, "level_2_min": 81, "level_1_min": 71})

    assert response.status_code == 200
    assert saved[0][0] == "Repair"
    assert response.json()["summary"]["changed"] == 1


def test_invalid_group_and_order_are_400():
    assert make_client().post("/staffing/skills/automation/Trim%20Saw", json={}).status_code == 400
    response = make_client().post("/staffing/skills/automation/Repair",
        json={"level_3_min": 80, "level_2_min": 90, "level_1_min": 70})
    assert response.status_code == 400
    assert response.json()["ok"] is False
~~~

- [ ] **Step 2: Verify failure**

Run: pytest tests/test_skills_automation_api.py -v

Expected: FAIL because the endpoint is absent.

- [ ] **Step 3: Implement context and endpoint**

~~~python
from dataclasses import asdict
from .. import automated_skill_settings, automated_skills
from ..plant_day import today as plant_today


def _automation_context():
    configs = automated_skill_settings.all_current()
    result = {}
    for group, matrix_skill in automated_skills.GROUP_TO_SKILL.items():
        goals = automated_skills.goals_for_group(group)
        last = automated_skill_settings.last_run(group)
        result[matrix_skill] = {
            "group": group,
            "settings": asdict(configs[group]),
            "last_run": asdict(last) if last else None,
            "work_centers": [{"name": location.name, "goal": goals[location.name]}
                             for location in staffing.LOCATIONS if location.skill == group],
        }
    return result


@router.post("/staffing/skills/automation/{group}")
async def save_automated_skill_settings(group: str, request: Request):
    try:
        body = await request.json()
        value = automated_skill_settings.validate(automated_skill_settings.BucketSettings(
            level_3_min=body["level_3_min"], level_2_min=body["level_2_min"], level_1_min=body["level_1_min"]))
        automated_skill_settings.save(group, value)
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc) or "Three numeric thresholds are required."}, status_code=400)
    try:
        summary = await asyncio.to_thread(automated_skills.run_group, group, "manual", plant_today())
    except automated_skills.RunInProgress as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    _http_cache.invalidate_today_cache()
    _http_cache.invalidate_stable_cache()
    return JSONResponse({"ok": True, "settings": asdict(value), "summary": asdict(summary)})
~~~

Call _automation_context in staffing_skills and pass its output as automation_groups. If this optional context read raises, use an empty dict so a setting outage cannot take down the People Matrix.

- [ ] **Step 4: Run and commit**

Run: pytest tests/test_skills_automation_api.py tests/test_skills_cell_update.py tests/test_skills_cache.py tests/test_staffing_rotations.py -v

Expected: PASS, with only DATABASE_URL-gated skips.

~~~bash
git add src/zira_dashboard/routes/skills.py tests/test_skills_automation_api.py tests/test_staffing_rotations.py
git commit -m "feat: recalculate automated skills from matrix"
~~~

## Task 5: Add sortable header gears and shared modal

**Files:**

- Modify: src/zira_dashboard/templates/skills.html
- Modify: src/zira_dashboard/static/skills-page.js
- Modify: src/zira_dashboard/static/skills.css
- Modify: tests/test_skills_template_render.py
- Modify: tests/test_skills_static.py

**Interfaces:**

- Each sort label is a matrix-sort-trigger button. Only Repair and Dismantle have automation-settings-trigger buttons.
- Header th retains aria-sort; nested interactive controls are avoided.
- The modal consumes window.AUTOMATION_GROUPS and posts to Task 4.

- [ ] **Step 1: Write failing template and static tests**

~~~python
def test_repair_header_has_sort_and_automation_buttons():
    html = _render_skills_html()

    assert 'class="matrix-sort-trigger"' in html
    assert 'class="automation-settings-trigger" data-automation-skill="Repair"' in html
    assert 'aria-label="Configure automatic Repair skills"' in html
    assert 'id="automation-modal-backdrop"' in html
~~~

~~~python
def test_settings_gear_does_not_trigger_sort():
    js = Path("src/zira_dashboard/static/skills-page.js").read_text()

    assert "automation-settings-trigger" in js
    assert "event.stopPropagation()" in js
    assert "matrix-sort-trigger" in js


def test_modal_posts_and_restores_focus():
    js = Path("src/zira_dashboard/static/skills-page.js").read_text()

    assert "'/staffing/skills/automation/' + group" in js
    assert "lastAutomationTrigger.focus()" in js
~~~

- [ ] **Step 2: Verify failure**

Run: pytest tests/test_skills_template_render.py tests/test_skills_static.py -v

Expected: FAIL because the controls and modal do not exist.

- [ ] **Step 3: Implement template controls and modal**

~~~jinja2
<th data-skill="{{ s }}" data-type="{{ type_by_skill.get(s, '') }}"
    class="skill-col{% if s in hidden_skills %} col-hidden{% endif %}" aria-sort="none">
  <span class="matrix-header-controls">
    <button type="button" class="matrix-sort-trigger" aria-label="Sort by {{ s }}">{{ s }}</button>
    {% if s in automation_groups %}
      <button type="button" class="automation-settings-trigger" data-automation-skill="{{ s }}"
              aria-label="Configure automatic {{ s }} skills" aria-haspopup="dialog"
              aria-controls="automation-modal-backdrop">⚙</button>
    {% endif %}
  </span>
</th>

<div class="automation-modal-backdrop" id="automation-modal-backdrop" hidden>
  <div class="automation-modal" role="dialog" aria-modal="true" aria-labelledby="automation-modal-title">
    <header class="automation-modal-head">
      <h3 id="automation-modal-title"></h3>
      <button type="button" id="automation-modal-close" aria-label="Close automatic skill settings">✕</button>
    </header>
    <p>L30 performance; two 4+ hour days; equal goal shares; evaluated daily and synced to Odoo.</p>
    <div id="automation-bucket-grid"></div>
    <div id="automation-preview"></div>
    <p id="automation-run-status" role="status" aria-live="polite"></p>
    <button type="button" id="automation-save-btn">Save &amp; Recalculate</button>
  </div>
</div>
~~~

Use the sort trigger structure for Name, Active, Reserve, and all skills. This prevents a button inside a th that itself acts like a button. Bootstrap the modal data before skills-page.js:

~~~jinja2
window.AUTOMATION_GROUPS = {{ automation_groups | tojson if automation_groups is defined else '{}' }};
~~~

- [ ] **Step 4: Implement JavaScript behavior**

~~~javascript
const automationBackdrop = document.getElementById('automation-modal-backdrop');
let lastAutomationTrigger = null;
let activeAutomationSkill = null;

function closeAutomationModal() {
  automationBackdrop.hidden = true;
  if (lastAutomationTrigger) lastAutomationTrigger.focus();
}

document.querySelectorAll('.automation-settings-trigger').forEach((trigger) => {
  trigger.addEventListener('click', (event) => {
    event.stopPropagation();
    lastAutomationTrigger = trigger;
    activeAutomationSkill = trigger.dataset.automationSkill;
    renderAutomationModal(window.AUTOMATION_GROUPS[activeAutomationSkill]);
    automationBackdrop.hidden = false;
    document.querySelector('#automation-bucket-grid input').focus();
  });
});

async function saveAutomationSettings() {
  const config = window.AUTOMATION_GROUPS[activeAutomationSkill];
  const payload = Object.fromEntries([...document.querySelectorAll('[data-automation-level]')]
    .map((input) => [input.dataset.automationLevel, Number(input.value)]));
  const response = await fetch('/staffing/skills/automation/' + config.group, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Could not save automatic skill settings.');
  config.settings = data.settings;
  renderAutomationRunSummary(data.summary);
}
~~~

Attach sorting only to matrix-sort-trigger click and Enter/Space. renderAutomationModal must create inputs for level_3_min, level_2_min, level_1_min; each input redraws the full-center thresholds plus one- and two-operator shares for every work center. Disable Save while awaiting the request, report failure with role alert, and restore the button in finally. Escape, backdrop click, and close button all call closeAutomationModal without sorting.

- [ ] **Step 5: Add CSS**

~~~css
.matrix-header-controls { display: inline-flex; align-items: center; gap: .15rem; }
.matrix-sort-trigger { border: 0; padding: 0; background: transparent; color: inherit; font: inherit; text-transform: inherit; letter-spacing: inherit; cursor: pointer; }
.automation-settings-trigger { opacity: 0; border: 0; background: transparent; color: var(--muted); cursor: pointer; }
.skills-table thead th:hover .automation-settings-trigger,
.automation-settings-trigger:focus-visible { opacity: 1; color: var(--accent); outline: 2px solid var(--accent); outline-offset: 2px; }
.automation-modal-backdrop { position: fixed; inset: 0; z-index: 1000; display: flex; align-items: flex-start; justify-content: center; padding: 4vh 1rem; overflow-y: auto; background: rgba(0,0,0,.55); }
.automation-modal-backdrop[hidden] { display: none; }
.automation-modal { width: min(44rem, 100%); background: var(--panel); color: var(--fg); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 12px 36px rgba(0,0,0,.5); padding: 1rem 1.25rem; }
.automation-preview table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
~~~

- [ ] **Step 6: Run and commit**

Run: pytest tests/test_skills_template_render.py tests/test_skills_static.py tests/test_skills_automation_api.py tests/test_staffing_rotations.py -v

Expected: PASS.

~~~bash
git add src/zira_dashboard/templates/skills.html src/zira_dashboard/static/skills-page.js src/zira_dashboard/static/skills.css tests/test_skills_template_render.py tests/test_skills_static.py
git commit -m "feat: configure automated skills from matrix headers"
~~~

## Task 6: Document and verify

**Files:**

- Modify: README.md

- [ ] **Step 1: Add supervisor documentation after the People Matrix workflow**

~~~markdown
### Automatic Repair and Dismantle skill levels

In **People Matrix**, hover the **Repair** or **Dismantle** header and select the
settings icon. Each group has separate level thresholds. Saving runs the L30
calculation immediately and future daily runs keep eligible employees in sync
with Odoo. A person needs two group days with at least four hours each before
automation changes their level.
~~~

- [ ] **Step 2: Run complete automated verification**

Run: pytest tests/test_automated_skill_settings.py tests/test_automated_skills.py tests/test_skills_automation_api.py tests/test_skills_template_render.py tests/test_skills_static.py -v

Expected: PASS.

Run: pytest -q

Expected: PASS, apart from environment-gated skips.

- [ ] **Step 3: Perform signed-in smoke test**

Run: uv run uvicorn zira_dashboard.app:app --reload

Expected: server starts and reports its local URL.

Verify header text sorts; hover and keyboard focus reveal the gear; the gear does not sort; changing a percentage updates whole-center and one/two-person previews; bad threshold order never saves; Save & Recalculate reports counters; an Odoo failure remains visible without hiding other results; Escape returns focus to the gear.

- [ ] **Step 4: Commit documentation**

~~~bash
git add README.md
git commit -m "docs: explain automatic recycling skills"
git status --short
~~~

Expected: unrelated files remain unstaged and untouched.

## Plan Self-Review

- **Spec coverage:** Tasks 1 and 2 cover group settings and L30 math. Task 3 covers equal shares, promotion/demotion, Odoo-first behavior, failure isolation, locking, and daily execution. Task 4 provides immediate recalculation. Task 5 covers the hover gear, preserved sorting, live unit equivalents, and accessibility. Task 6 covers documentation and system verification.
- **Placeholder scan:** Storage keys, types, route, payload fields, test commands, expected results, and commit scopes are named.
- **Type consistency:** Group keys remain Repair and Dismantler; the corresponding matrix/Odoo names are Repair and Dismantle. All payloads use level_3_min, level_2_min, and level_1_min. Every run returns RunSummary.
