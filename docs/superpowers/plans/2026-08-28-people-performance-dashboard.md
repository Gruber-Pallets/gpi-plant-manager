# People Performance Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a live manager dashboard that shows every person who worked on a selected day, their attendance-backed location and transfer timeline, independent metered-production results and downtime, forklift call performance, and neutral non-metered work in one accessible row per person.

**Architecture:** Persist timestamped forklift completion events and exact adjusted production-downtime intervals, then feed those records plus the canonical Odoo `LocationSpan` timeline into a focused pure assembler. A thin day-data service reuses the existing production segment scorer and forklift score, a FastAPI route renders a full page and refreshable rows fragment, and page-specific Jinja2/CSS/JavaScript provides the shared time axis, accessible details, and state-preserving 30-second refresh.

**Tech Stack:** Python 3.11, FastAPI, Jinja2, PostgreSQL/psycopg2, vanilla JavaScript/CSS, SVG, pytest, Ruff.

## Global Constraints

- Show everyone who clocked in at any point on the selected day, including people who have already clocked out.
- Use one shared time axis from the selected day's configured shift start to shift end.
- Support Today plus one historical day at a time; do not add a combined multi-day view or TV rotation.
- Keep the fixed section order `Metered production`, `Tablet forklift`, `Other non-metered people`; non-metered people always remain below both metered sections.
- Render exactly one row per Odoo employee ID. Current role owns an active row's section and summary; final role owns a completed row's section and summary; earlier roles remain visible in the same timeline.
- Consume `attendance_timeline.timeline_for_range()` and `assignment_windows.work_segments_from_timeline()` from the separately approved Odoo attendance-location project. Never substitute schedule, legacy manual attribution, forklift activity, or a name guess for location truth.
- Reuse `production_segments.credit_work_segments()` and `production_segments.score_work_segments()`; do not create a second unit-credit or goal engine.
- Keep every metered stint independent. A transfer changes the goal rate and does not carry an earlier actual, deficit, or downtime into the next stint.
- Reserve red and green for Behind and Ahead. Use named, stable non-red/green colors for work-center identity; use neutral hatching for planned breaks.
- Use rolling 30-minute windows for production uptime and forklift on-time. Use 15-minute plant-local forklift call buckets. A window with no eligible denominator is a line gap, not `0%` or `100%`.
- Compute overall production goal as `100 * sum(actual_units) / sum(goal_units)`. Compute overall uptime from summed eligible productive minutes and downtime. Return `None` when either denominator is zero.
- Use forklift `createdAt`, matching the existing aggregate path. Calls without explicit on-time/late classification count toward volume but not the on-time denominator.
- Reuse the resolved `forklift_score.daily_score()` configuration and its minimum-call gate; do not introduce a new driver score.
- An ambiguous forklift driver mapping remains unattached and visible as a source warning. It must never be credited to a plant person.
- A source failure, stale source, unknown location, missing goal, or missing denominator displays as stale/uncertain/unavailable, never as zero performance.
- Today uses one `as_of_utc` cap for location, earned goal, uptime, and calls. Historical rows have no open intervals.
- Every interval is keyboard reachable and exposes the same details on hover, focus, and tap. Transfers, Ahead/Behind, line names, and source warnings require text as well as color or shape.
- Every implementation push to `main` includes a short child-readable `CHANGELOG.md` note explaining what changed and how it helps.
- Preserve the user's existing changes in `.superpowers/sdd/task-7-report.md`, `.cursorignore`, `.python-version`, and `uv.lock`; do not stage them unless a later user request explicitly includes them.

## Dependency gate before Task 1

The dashboard may not import or emulate unfinished Odoo-location behavior. Before implementation, verify that Tasks 3-5, 11, and 13 of `docs/superpowers/plans/2026-08-28-odoo-attendance-live-location-truth.md` are on `origin/main` and that these exact interfaces exist:

```python
from zira_dashboard.attendance_timeline import LocationSpan, timeline_for_range
from zira_dashboard.assignment_windows import work_segments_from_timeline
from zira_dashboard.attendance_readiness import build_report
```

Run:

```bash
git fetch origin
git merge-base --is-ancestor origin/main HEAD
uv run python -c "from zira_dashboard.attendance_timeline import LocationSpan, timeline_for_range; from zira_dashboard.assignment_windows import work_segments_from_timeline; from zira_dashboard.attendance_readiness import build_report; print('people-dashboard prerequisite ready')"
```

Expected:

```text
people-dashboard prerequisite ready
```

If an import fails, stop this plan and finish the cited Odoo-location tasks first. Do not add a temporary fallback.

## File structure

| File | Responsibility |
|---|---|
| `src/zira_dashboard/forklift_ingest.py` | Pure normalization of raw completion payloads into timestamped typed events, while retaining existing daily aggregation. |
| `src/zira_dashboard/forklift_event_store.py` | Idempotent event UPSERT and time-range reads only. |
| `src/zira_dashboard/leaderboard.py` | Produce the exact break-adjusted downtime intervals alongside existing station totals. |
| `src/zira_dashboard/production_segments.py` | Carry Odoo employee identity through existing segment scores. |
| `src/zira_dashboard/people_performance.py` | Pure clipping, interval math, summaries, role selection, attention reasons, sectioning, and sorting. |
| `src/zira_dashboard/people_performance_data.py` | Load one day's source families, reuse existing scorers, attach safe identity mappings, and degrade sources independently. |
| `src/zira_dashboard/people_performance_view.py` | Convert immutable domain values to Jinja-ready labels and percentage geometry. |
| `src/zira_dashboard/routes/people_performance.py` | Validate the selected day, render full/partial responses, and apply cache policy. |
| `src/zira_dashboard/templates/people_performance.html` | Page shell, controls, counts, time header, empty/global source states. |
| `src/zira_dashboard/templates/_people_performance_rows.html` | Shared section and person-row markup for initial render and refresh. |
| `src/zira_dashboard/static/people-performance.css` | Desktop/tablet row layout, accessible color/shape system, sticky context, and responsive behavior. |
| `src/zira_dashboard/static/people-performance.js` | Refresh, state restoration, attention filtering, and one shared interval detail popover. |
| `scripts/preview_people_performance.py` | Deterministic busy-fixture preview used for desktop/tablet visual checks. |

---

### Task 1: Persist raw forklift completion events

**Files:**
- Modify: `src/zira_dashboard/_schema.py`
- Modify: `src/zira_dashboard/forklift_ingest.py`
- Create: `src/zira_dashboard/forklift_event_store.py`
- Modify: `src/zira_dashboard/forklift_snapshot.py`
- Modify: `src/zira_dashboard/forklift_backfill.py`
- Modify: `tests/test_forklift_ingest.py`
- Create: `tests/test_forklift_event_store.py`
- Modify: `tests/test_forklift_snapshot.py`
- Modify: `tests/test_forklift_backfill.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: external completion fields `id`, `completedBy`, `createdAt`, `workstationName`, `onTime`, `late`, `responseMs`, and `handlingMs`; `forklift_store.resolve_forklift_to_plant()` remains the later safe identity boundary.
- Produces: `ForkliftCompletionEvent`, `completion_events()`, `upsert_completion_events()`, and `completion_events_for_range()` with the signatures below.

- [ ] **Step 1: Write the failing normalization and schema tests**

Add this typed-event test to `tests/test_forklift_ingest.py`:

```python
from datetime import datetime, UTC


def test_completion_events_keep_timestamp_status_and_unknown_values():
    events = forklift_ingest.completion_events(
        [
            {"id": "c1", "completedBy": "fk-1", "createdAt": 1782484200000,
             "workstationName": "Prosaw #4", "onTime": True, "late": False,
             "responseMs": 120000, "handlingMs": 300000},
            {"id": "c2", "completedBy": "fk-1", "createdAt": 1782485100000},
            {"id": "missing-id", "createdAt": 1782485100000},
        ],
        {"fk-1": "Trent"},
    )
    assert [event.event_id for event in events] == ["c1", "c2"]
    assert events[0].created_at_utc == datetime.fromtimestamp(
        1782484200, tz=UTC
    )
    assert events[0].on_time is True
    assert events[0].late is False
    assert events[1].on_time is None
    assert events[1].late is None
```

Create `tests/test_forklift_event_store.py` with:

```python
from datetime import datetime, UTC

from zira_dashboard import forklift_event_store
from zira_dashboard._schema import SCHEMA_DDL
from zira_dashboard.forklift_ingest import ForkliftCompletionEvent


def test_schema_has_idempotent_forklift_event_table():
    ddl = " ".join(SCHEMA_DDL.split())
    assert "CREATE TABLE IF NOT EXISTS forklift_completion_events" in ddl
    assert "external_id TEXT PRIMARY KEY" in ddl
    assert "created_at_utc TIMESTAMPTZ NOT NULL" in ddl
    assert "idx_forklift_completion_events_time_driver" in ddl


def test_event_upsert_updates_one_external_identity(monkeypatch):
    captured = {}

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return None

    monkeypatch.setattr(forklift_event_store.db, "cursor", lambda: Cursor())
    monkeypatch.setattr(
        forklift_event_store.db,
        "execute_values",
        lambda cur, sql, rows, template: captured.update(
            sql=sql, rows=rows, template=template
        ),
    )
    event = ForkliftCompletionEvent(
        event_id="c1", driver_id="fk-1", driver_name="Trent",
        created_at_utc=datetime(2026, 6, 26, 14, 30, tzinfo=UTC),
        workstation_name="Prosaw #4", on_time=False, late=True,
        response_ms=120000, handling_ms=300000,
    )
    assert forklift_event_store.upsert_completion_events([event]) == 1
    assert "ON CONFLICT (external_id) DO UPDATE" in captured["sql"]
    assert captured["rows"][0][0] == "c1"
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
uv run pytest tests/test_forklift_ingest.py::test_completion_events_keep_timestamp_status_and_unknown_values tests/test_forklift_event_store.py -q
```

Expected: FAIL because the event dataclass, normalizer, table, and store do not exist.

- [ ] **Step 3: Add the event schema and pure normalizer**

Append this schema immediately after `forklift_name_map`:

```sql
CREATE TABLE IF NOT EXISTS forklift_completion_events (
  external_id       TEXT PRIMARY KEY,
  driver_id         TEXT NOT NULL,
  driver_name       TEXT NOT NULL,
  created_at_utc    TIMESTAMPTZ NOT NULL,
  workstation_name  TEXT,
  on_time            BOOLEAN,
  late               BOOLEAN,
  response_ms        BIGINT,
  handling_ms        BIGINT,
  ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_forklift_completion_events_time_driver
  ON forklift_completion_events (created_at_utc, driver_id);
```

Add to `forklift_ingest.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class ForkliftCompletionEvent:
    event_id: str
    driver_id: str
    driver_name: str
    created_at_utc: datetime
    workstation_name: str | None
    on_time: bool | None
    late: bool | None
    response_ms: int | None
    handling_ms: int | None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def completion_events(
    items: list[dict], id_to_name: dict[str, str]
) -> tuple[ForkliftCompletionEvent, ...]:
    events_by_id: dict[str, ForkliftCompletionEvent] = {}
    for item in items or []:
        event_id = item.get("id")
        driver_id = item.get("completedBy")
        created_at = _optional_int(item.get("createdAt"))
        if not event_id or not driver_id or created_at is None:
            continue
        driver_key = str(driver_id)
        event = ForkliftCompletionEvent(
            event_id=str(event_id),
            driver_id=driver_key,
            driver_name=str(id_to_name.get(driver_key) or driver_key),
            created_at_utc=datetime.fromtimestamp(created_at / 1000.0, tz=UTC),
            workstation_name=(str(item["workstationName"])
                              if item.get("workstationName") else None),
            on_time=_optional_bool(item.get("onTime")),
            late=_optional_bool(item.get("late")),
            response_ms=_optional_int(item.get("responseMs")),
            handling_ms=_optional_int(item.get("handlingMs")),
        )
        events_by_id[event.event_id] = event
    return tuple(sorted(
        events_by_id.values(), key=lambda event: (event.created_at_utc, event.event_id)
    ))
```

- [ ] **Step 4: Implement the narrow event store**

Create `forklift_event_store.py`:

```python
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from . import db
from .forklift_ingest import ForkliftCompletionEvent


def upsert_completion_events(events: Sequence[ForkliftCompletionEvent]) -> int:
    rows = list(events)
    if not rows:
        return 0
    sql = """
        INSERT INTO forklift_completion_events (
            external_id, driver_id, driver_name, created_at_utc,
            workstation_name, on_time, late, response_ms, handling_ms,
            ingested_at, updated_at
        ) VALUES %s
        ON CONFLICT (external_id) DO UPDATE SET
            driver_id=EXCLUDED.driver_id,
            driver_name=EXCLUDED.driver_name,
            created_at_utc=EXCLUDED.created_at_utc,
            workstation_name=EXCLUDED.workstation_name,
            on_time=EXCLUDED.on_time,
            late=EXCLUDED.late,
            response_ms=EXCLUDED.response_ms,
            handling_ms=EXCLUDED.handling_ms,
            updated_at=now()
    """
    with db.cursor() as cur:
        db.execute_values(
            cur,
            sql,
            [(e.event_id, e.driver_id, e.driver_name, e.created_at_utc,
              e.workstation_name, e.on_time, e.late, e.response_ms,
              e.handling_ms) for e in rows],
            template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now())",
        )
    return len(rows)


def completion_events_for_range(
    start_utc: datetime, end_utc: datetime
) -> tuple[ForkliftCompletionEvent, ...]:
    rows = db.query(
        "SELECT external_id, driver_id, driver_name, created_at_utc, "
        "workstation_name, on_time, late, response_ms, handling_ms "
        "FROM forklift_completion_events "
        "WHERE created_at_utc >= %s AND created_at_utc < %s "
        "ORDER BY created_at_utc, external_id",
        (start_utc, end_utc),
    )
    return tuple(ForkliftCompletionEvent(
        event_id=row["external_id"], driver_id=row["driver_id"],
        driver_name=row["driver_name"], created_at_utc=row["created_at_utc"],
        workstation_name=row["workstation_name"], on_time=row["on_time"],
        late=row["late"], response_ms=row["response_ms"],
        handling_ms=row["handling_ms"],
    ) for row in rows)
```

- [ ] **Step 5: Wire both ingestion paths and test repeat safety**

In both `snapshot_today()` and `backfill_history()`, normalize immediately after `id2name` is built:

```python
events = forklift_ingest.completion_events(items, id2name)
```

Call `forklift_event_store.upsert_completion_events(events)` after the existing daily aggregate UPSERTs so a raw-event store problem cannot erase the current leaderboard update. Import `forklift_event_store` beside the other forklift modules. Extend the existing snapshot and backfill tests to monkeypatch the UPSERT and assert the exact event IDs. Add a duplicate-`c1` payload case and assert `completion_events()` emits the last `c1` once, so one `execute_values` statement never tries to update the same key twice. Add a store read test that monkeypatches `db.query`, returns a changed `c1`, and asserts only one `ForkliftCompletionEvent` is returned. This establishes update-without-duplication separately from the SQL assertion.

- [ ] **Step 6: Run the focused tests**

Run:

```bash
uv run pytest tests/test_forklift_ingest.py tests/test_forklift_event_store.py tests/test_forklift_snapshot.py tests/test_forklift_backfill.py -q
```

Expected: PASS.

- [ ] **Step 7: Add the patch note, commit, and push**

Add at the top of the current date in `CHANGELOG.md`:

```markdown
### Keep each forklift call's time

- **Plant Manager now keeps the time and result for each forklift call.** This will let managers see when calls happened without changing the current forklift totals.
```

Run:

```bash
git add src/zira_dashboard/_schema.py src/zira_dashboard/forklift_ingest.py src/zira_dashboard/forklift_event_store.py src/zira_dashboard/forklift_snapshot.py src/zira_dashboard/forklift_backfill.py tests/test_forklift_ingest.py tests/test_forklift_event_store.py tests/test_forklift_snapshot.py tests/test_forklift_backfill.py CHANGELOG.md
git commit -m "feat: persist forklift completion events"
git push origin main
```

### Task 2: Build pure production and rolling-uptime math

**Files:**
- Modify: `src/zira_dashboard/production_segments.py`
- Create: `src/zira_dashboard/people_performance.py`
- Modify: `tests/test_production_segments.py`
- Create: `tests/test_people_performance_production.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: dependency-gate `LocationSpan`; existing `SegmentScore`; timestamped downtime-window values supplied by callers; configured break spans.
- Produces: the immutable values and pure functions below. Task 3 extends the same module with row assembly.

```python
RoleKey = Literal["production", "forklift", "other"]
SectionKey = Literal["production", "forklift", "other"]
MetricState = Literal["ahead", "behind", "neutral", "unavailable"]

@dataclass(frozen=True)
class BreakSpan:
    start_utc: datetime
    end_utc: datetime
    label: str

@dataclass(frozen=True)
class RollingPoint:
    at_utc: datetime
    value_pct: float | None

@dataclass(frozen=True)
class ProductionMetric:
    actual_units: float
    goal_units: float
    productive_minutes: float
    downtime_minutes: float
    result: MetricState
    rolling_uptime: tuple[RollingPoint, ...]

productive_windows(start_utc, end_utc, breaks) -> tuple[tuple[datetime, datetime], ...]
rolling_uptime_points(*, start_utc, end_utc, available_windows, downtime_windows,
                      step=timedelta(minutes=5), window=timedelta(minutes=30))
                      -> tuple[RollingPoint, ...]
production_metric(score, *, downtime_windows, breaks, excluded_windows=()) -> ProductionMetric
weighted_production_summary(metrics) -> tuple[float | None, float | None, float]
```

- [ ] **Step 1: Write identity-propagation and production math tests**

Add to `tests/test_production_segments.py`:

```python
def test_segment_score_keeps_odoo_employee_identity():
    credit = SegmentCredit(
        segment_id=1, wc_name="Repair 1", person_name="Alex Worker",
        start_utc=DT0, end_utc=DT1, source="odoo",
        productive_minutes=30, actual_units=20, is_active=False,
        person_odoo_id=44,
    )
    scored = score_work_segments(
        {"Repair 1": (credit,)}, target_per_hour={"Repair 1": 30.0}
    )["Repair 1"][0]
    assert scored.person_odoo_id == 44
```

Create `tests/test_people_performance_production.py`:

```python
from datetime import datetime, timedelta, UTC

import pytest

from zira_dashboard.people_performance import (
    BreakSpan, production_metric, rolling_uptime_points,
    weighted_production_summary,
)
from zira_dashboard.production_segments import SegmentScore


START = datetime(2026, 8, 28, 11, 0, tzinfo=UTC)


def _score(actual, goal, start=START, end=START + timedelta(hours=1)):
    return SegmentScore(
        segment_id=1, wc_name="Repair 1", person_name="Alex Worker",
        start_utc=start, end_utc=end, source="odoo", productive_minutes=60,
        actual_units=actual, goal_units=goal, runway_units=max(actual, goal),
        is_active=False, result="ahead" if actual >= goal else "behind",
        person_odoo_id=44,
    )


def test_rolling_uptime_excludes_lunch_and_does_not_bridge_no_denominator():
    lunch = BreakSpan(
        START + timedelta(minutes=20), START + timedelta(minutes=30), "Lunch"
    )
    points = rolling_uptime_points(
        start_utc=START,
        end_utc=START + timedelta(minutes=45),
        available_windows=(
            (START, START + timedelta(minutes=20)),
            (START + timedelta(minutes=30), START + timedelta(minutes=45)),
        ),
        downtime_windows=((START + timedelta(minutes=10),
                           START + timedelta(minutes=20)),),
        step=timedelta(minutes=5),
        window=timedelta(minutes=30),
    )
    by_time = {point.at_utc: point.value_pct for point in points}
    assert by_time[START] is None
    assert by_time[START + timedelta(minutes=20)] == pytest.approx(50.0)
    assert by_time[START + timedelta(minutes=25)] is None
    assert by_time[START + timedelta(minutes=45)] == pytest.approx(100.0)


def test_production_metric_intersects_stop_with_worker_arrival():
    score = _score(18, 30, START + timedelta(minutes=15), START + timedelta(hours=1))
    metric = production_metric(
        score,
        downtime_windows=((START, START + timedelta(minutes=25)),),
        breaks=(),
    )
    assert metric.downtime_minutes == pytest.approx(10.0)
    assert metric.result == "behind"


def test_weighted_summary_uses_unit_and_goal_sums():
    ahead = production_metric(_score(10, 5), downtime_windows=(), breaks=())
    behind = production_metric(_score(30, 45), downtime_windows=(), breaks=())
    goal_pct, uptime_pct, downtime = weighted_production_summary((ahead, behind))
    assert goal_pct == pytest.approx(80.0)
    assert uptime_pct == pytest.approx(100.0)
    assert downtime == 0


def test_transfer_uses_each_centers_rate_without_carrying_a_deficit():
    repair = production_metric(
        _score(9, 5, START, START + timedelta(minutes=30)),
        downtime_windows=(), breaks=(),
    )
    dismantler = production_metric(
        SegmentScore(
            segment_id=2, wc_name="Dismantler 1", person_name="Alex Worker",
            start_utc=START + timedelta(minutes=30),
            end_utc=START + timedelta(minutes=60), source="odoo",
            productive_minutes=30, actual_units=15, goal_units=20,
            runway_units=20, is_active=True, result="behind",
            person_odoo_id=44,
        ),
        downtime_windows=(), breaks=(),
    )
    assert repair.result == "ahead"
    assert dismantler.result == "behind"
    assert weighted_production_summary((repair, dismantler))[0] == pytest.approx(96.0)


def test_approved_breakdown_is_removed_from_uptime_denominator_and_stop():
    metric = production_metric(
        _score(20, 20),
        downtime_windows=((START, START + timedelta(minutes=30)),),
        excluded_windows=((START + timedelta(minutes=10),
                           START + timedelta(minutes=20)),),
        breaks=(),
    )
    assert metric.downtime_minutes == pytest.approx(20.0)
    assert metric.rolling_uptime[3].value_pct is None
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run pytest tests/test_production_segments.py::test_segment_score_keeps_odoo_employee_identity tests/test_people_performance_production.py -q
```

Expected: FAIL because `SegmentScore` drops Odoo identity and the people-performance module does not exist.

- [ ] **Step 3: Carry Odoo identity through scores**

Add this last field to `SegmentScore`:

```python
    person_odoo_id: int | None = None
```

In the `score_work_segments()` `SegmentScore` construction, add:

```python
person_odoo_id=credit.person_odoo_id,
```

For `_join_display_scores()`, require matching Odoo identity in `_can_join_display_scores()` and set:

```python
person_odoo_id=left.person_odoo_id,
```

This retains the dependency task's last/default `SegmentCredit.person_odoo_id` and does not change legacy name-keyed scores.

- [ ] **Step 4: Implement interval subtraction and union helpers**

Start `people_performance.py` with the declared dataclasses and these pure helpers:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, Sequence

from .production_segments import SegmentScore


def _intersection_minutes(
    left: datetime, right: datetime,
    windows: Sequence[tuple[datetime, datetime]],
) -> float:
    return sum(
        max(0.0, (min(right, end) - max(left, start)).total_seconds() / 60.0)
        for start, end in windows
        if min(right, end) > max(left, start)
    )


def _merge_windows(
    windows: Sequence[tuple[datetime, datetime]],
) -> tuple[tuple[datetime, datetime], ...]:
    merged: list[list[datetime]] = []
    for start, end in sorted((a, b) for a, b in windows if b > a):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return tuple((start, end) for start, end in merged)


def productive_windows(
    start_utc: datetime,
    end_utc: datetime,
    breaks: Sequence[BreakSpan],
) -> tuple[tuple[datetime, datetime], ...]:
    pieces = [(start_utc, end_utc)]
    for item in sorted(breaks, key=lambda value: value.start_utc):
        next_pieces: list[tuple[datetime, datetime]] = []
        for left, right in pieces:
            if item.end_utc <= left or item.start_utc >= right:
                next_pieces.append((left, right))
            else:
                if left < item.start_utc:
                    next_pieces.append((left, min(right, item.start_utc)))
                if item.end_utc < right:
                    next_pieces.append((max(left, item.end_utc), right))
        pieces = next_pieces
    return tuple((left, right) for left, right in pieces if right > left)
```

- [ ] **Step 5: Implement the rolling line and summaries**

Use these exact rules:

```python
def rolling_uptime_points(
    *, start_utc: datetime, end_utc: datetime,
    available_windows: Sequence[tuple[datetime, datetime]],
    downtime_windows: Sequence[tuple[datetime, datetime]],
    step: timedelta = timedelta(minutes=5),
    window: timedelta = timedelta(minutes=30),
) -> tuple[RollingPoint, ...]:
    available = _merge_windows(available_windows)
    downtime = _merge_windows(downtime_windows)
    points: list[RollingPoint] = []
    at = start_utc
    while at <= end_utc:
        current_window = next(
            ((left, right) for left, right in available
             if left < at <= right or (at == left == end_utc)),
            None,
        )
        window_start = (
            max(current_window[0], at - window) if current_window else at
        )
        denominator = _intersection_minutes(window_start, at, available)
        if current_window is None or denominator <= 0:
            value = None
        else:
            stopped = _intersection_minutes(window_start, at, downtime)
            value = 100.0 * max(0.0, denominator - stopped) / denominator
        points.append(RollingPoint(at_utc=at, value_pct=value))
        at += step
    if points[-1].at_utc != end_utc:
        current_window = next(
            ((left, right) for left, right in available if left < end_utc <= right),
            None,
        )
        window_start = max(current_window[0], end_utc - window) if current_window else end_utc
        denominator = _intersection_minutes(window_start, end_utc, available)
        stopped = _intersection_minutes(window_start, end_utc, downtime)
        points.append(RollingPoint(
            end_utc,
            None if current_window is None or denominator <= 0
            else 100.0 * max(0.0, denominator - stopped) / denominator,
        ))
    return tuple(points)


def production_metric(
    score: SegmentScore,
    *, downtime_windows: Sequence[tuple[datetime, datetime]],
    breaks: Sequence[BreakSpan],
    excluded_windows: Sequence[tuple[datetime, datetime]] = (),
) -> ProductionMetric:
    if score.start_utc is None or score.end_utc is None:
        return ProductionMetric(
            score.actual_units, score.goal_units, score.productive_minutes,
            0.0, "unavailable", (),
        )
    exclusions = tuple(breaks) + tuple(
        BreakSpan(left, right, "Approved machine breakdown")
        for left, right in excluded_windows
    )
    available = productive_windows(score.start_utc, score.end_utc, exclusions)
    clipped_stops = _merge_windows(tuple(
        (max(score.start_utc, left), min(score.end_utc, right))
        for left, right in downtime_windows
        if min(score.end_utc, right) > max(score.start_utc, left)
    ))
    eligible_stops = _merge_windows(tuple(
        (max(stop_start, available_start), min(stop_end, available_end))
        for stop_start, stop_end in clipped_stops
        for available_start, available_end in available
        if min(stop_end, available_end) > max(stop_start, available_start)
    ))
    downtime = sum(
        (right - left).total_seconds() / 60.0 for left, right in eligible_stops
    )
    state: MetricState = (
        "neutral" if score.goal_units <= 0
        else "ahead" if score.actual_units >= score.goal_units
        else "behind"
    )
    return ProductionMetric(
        actual_units=score.actual_units,
        goal_units=score.goal_units,
        productive_minutes=score.productive_minutes,
        downtime_minutes=downtime,
        result=state,
        rolling_uptime=rolling_uptime_points(
            start_utc=score.start_utc, end_utc=score.end_utc,
            available_windows=available, downtime_windows=eligible_stops,
        ),
    )


def weighted_production_summary(
    metrics: Sequence[ProductionMetric],
) -> tuple[float | None, float | None, float]:
    scoreable = [metric for metric in metrics if metric.result != "unavailable"]
    actual = sum(metric.actual_units for metric in scoreable)
    goal = sum(metric.goal_units for metric in scoreable)
    available = sum(metric.productive_minutes for metric in scoreable)
    downtime = sum(metric.downtime_minutes for metric in scoreable)
    goal_pct = 100.0 * actual / goal if goal > 0 else None
    uptime_pct = 100.0 * max(0.0, available - downtime) / available if available > 0 else None
    return goal_pct, uptime_pct, downtime
```

- [ ] **Step 6: Run focused tests and lint**

Run:

```bash
uv run pytest tests/test_production_segments.py tests/test_people_performance_production.py -q
uv run ruff check src/zira_dashboard/production_segments.py src/zira_dashboard/people_performance.py tests/test_people_performance_production.py
```

Expected: both commands PASS.

- [ ] **Step 7: Add the patch note, commit, and push**

Add:

```markdown
### Keep each person's production goal separate

- **Plant Manager can now work out one person's goal and uptime for each place they worked.** A move to a new work area starts a fresh result.
```

Run:

```bash
git add src/zira_dashboard/production_segments.py src/zira_dashboard/people_performance.py tests/test_production_segments.py tests/test_people_performance_production.py CHANGELOG.md
git commit -m "feat: calculate people production intervals"
git push origin main
```

### Task 3: Add forklift buckets, mixed-role rows, sections, and attention sorting

**Files:**
- Modify: `src/zira_dashboard/people_performance.py`
- Create: `tests/people_performance_fixtures.py`
- Create: `tests/test_people_performance_forklift.py`
- Create: `tests/test_people_performance_rows.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 1 `ForkliftCompletionEvent`, Task 2 production values, canonical `LocationSpan`, daily forklift score values supplied as `ForkliftDayMetric`.
- Produces: `forklift_call_buckets()` and `assemble_dashboard()` with this stable model:

```python
@dataclass(frozen=True)
class ForkliftBucket:
    start_utc: datetime
    end_utc: datetime
    calls: int
    late_event_times: tuple[datetime, ...]
    rolling_ontime_pct: float | None
    rolling_late_count: int

@dataclass(frozen=True)
class ForkliftDayMetric:
    calls: int
    on_time: int
    late: int
    handling_minutes: float
    score: float | None
    ontime_floor_pct: float
    timeline_available: bool = True

@dataclass(frozen=True)
class TimelineInterval:
    key: str
    start_utc: datetime
    end_utc: datetime
    location_name: str
    location_status: str
    role: RoleKey
    is_transfer: bool
    metric_available: bool = True
    production: ProductionMetric | None = None
    forklift_buckets: tuple[ForkliftBucket, ...] = ()

@dataclass(frozen=True)
class PersonRow:
    employee_odoo_id: int
    person_name: str
    is_active: bool
    status: str
    primary_role: RoleKey
    section: SectionKey
    intervals: tuple[TimelineInterval, ...]
    breaks: tuple[BreakSpan, ...]
    attention_reasons: tuple[str, ...]
    summary: tuple[tuple[str, str], ...]
    sort_key: tuple
    unattached_forklift_calls: int = 0

@dataclass(frozen=True)
class DashboardModel:
    day: date
    is_today: bool
    as_of_utc: datetime
    window_start_utc: datetime
    window_end_utc: datetime
    rows: tuple[PersonRow, ...]
    source_warnings: tuple[str, ...] = ()

def assemble_dashboard(
    *, day: date, as_of_utc: datetime, window_start_utc: datetime,
    window_end_utc: datetime, spans: Sequence[LocationSpan],
    production_scores: Sequence[SegmentScore],
    downtime_by_wc: dict[str, Sequence[tuple[datetime, datetime]]],
    breakdown_exclusions_by_person_wc: dict[
        tuple[str, str], Sequence[tuple[datetime, datetime]]
    ],
    forklift_events_by_person: dict[str, Sequence[ForkliftCompletionEvent]],
    forklift_day_metrics: dict[str, ForkliftDayMetric],
    breaks: Sequence[BreakSpan], metered_wc_names: set[str],
    source_warnings: Sequence[str], production_available: bool = True,
    forklift_available: bool = True,
) -> DashboardModel
```

Create `tests/people_performance_fixtures.py` once in this task so later tests use real typed values rather than hand-shaped dictionaries:

```python
from datetime import date, datetime, timedelta, UTC

from zira_dashboard.attendance_timeline import LocationSpan
from zira_dashboard.forklift_ingest import ForkliftCompletionEvent
from zira_dashboard.people_performance import (
    BreakSpan, ForkliftDayMetric, assemble_dashboard,
)
from zira_dashboard.production_segments import SegmentScore


DAY = date(2026, 8, 28)
START = datetime(2026, 8, 28, 11, 0, tzinfo=UTC)
END = START + timedelta(hours=8)


def span(employee_id, name, start_minute, end_minute, wc, status="valid"):
    return LocationSpan(
        employee_odoo_id=employee_id, employee_name=name,
        start_utc=START + timedelta(minutes=start_minute),
        end_utc=START + timedelta(minutes=end_minute), status=status,
        app_work_center_name=wc,
        odoo_work_center_id=(100 + employee_id if wc else None),
        odoo_work_center_name=wc, attendance_ids=(1000 + employee_id,),
        department_repair=None,
    )


def score(employee_id, name, wc, start_minute, end_minute, actual, goal):
    minutes = end_minute - start_minute
    return SegmentScore(
        segment_id=employee_id, wc_name=wc, person_name=name,
        start_utc=START + timedelta(minutes=start_minute),
        end_utc=START + timedelta(minutes=end_minute), source="odoo",
        productive_minutes=minutes, actual_units=actual, goal_units=goal,
        runway_units=max(actual, goal), is_active=end_minute == 480,
        result="ahead" if actual >= goal else "behind",
        person_odoo_id=employee_id,
    )


def event(name, minute, *, on_time=None, late=None, event_id="call"):
    return ForkliftCompletionEvent(
        event_id=f"{event_id}-{minute}", driver_id=f"driver-{name}",
        driver_name=name, created_at_utc=START + timedelta(minutes=minute),
        workstation_name="Repair 1", on_time=on_time, late=late,
        response_ms=60000, handling_ms=120000,
    )


def driver_metric(calls, on_time, late, *, score_value=82.0):
    return ForkliftDayMetric(
        calls=calls, on_time=on_time, late=late,
        handling_minutes=calls * 2.0, score=score_value,
        ontime_floor_pct=80.0,
    )


def busy_dashboard_model():
    spans = (
        span(44, "Amy Behind", 0, 480, "Repair 1"),
        span(45, "Zed Ahead", 0, 480, "Repair 2"),
        span(46, "Ben Driver", 0, 480, "Tablets"),
        span(47, "Cal Missing", 0, 480, None, "missing_required_location"),
        span(49, "Sam Stale", 0, 480, "Repair 1", "stale_open_location"),
        span(48, "Mia Mixed", 0, 90, "Repair 1"),
        span(48, "Mia Mixed", 90, 95, "Repair 2"),
        span(48, "Mia Mixed", 95, 180, "Tablets"),
    )
    scores = (
        score(44, "Amy Behind", "Repair 1", 0, 480, 120, 180),
        score(45, "Zed Ahead", "Repair 2", 0, 480, 210, 180),
        score(48, "Mia Mixed", "Repair 1", 0, 90, 40, 35),
        score(48, "Mia Mixed", "Repair 2", 90, 95, 1, 2),
    )
    calls = {
        "Ben Driver": (
            event("Ben Driver", 20, on_time=True),
            event("Ben Driver", 465, late=True),
        ),
        "Mia Mixed": (event("Mia Mixed", 120, on_time=True),),
    }
    return assemble_dashboard(
        day=DAY, as_of_utc=END, window_start_utc=START,
        window_end_utc=END, spans=spans, production_scores=scores,
        downtime_by_wc={"Repair 1": ((START + timedelta(minutes=60),
                                      START + timedelta(minutes=75)),),
                          "Repair 2": ()},
        breakdown_exclusions_by_person_wc={},
        forklift_events_by_person=calls,
        forklift_day_metrics={
            "Ben Driver": driver_metric(2, 1, 1),
            "Mia Mixed": driver_metric(1, 1, 0),
        },
        breaks=(BreakSpan(START + timedelta(minutes=270),
                          START + timedelta(minutes=300), "Planned lunch"),),
        metered_wc_names={"Repair 1", "Repair 2"},
        source_warnings=("Forklift data unavailable",),
    )
```

- [ ] **Step 1: Write forklift bucket tests**

Create `tests/test_people_performance_forklift.py`:

```python
from datetime import datetime, timedelta, UTC

import pytest

from zira_dashboard.forklift_ingest import ForkliftCompletionEvent
from zira_dashboard.people_performance import forklift_call_buckets


START = datetime(2026, 8, 28, 11, 0, tzinfo=UTC)


def _event(minutes, *, on_time=None, late=None, event_id="c"):
    return ForkliftCompletionEvent(
        event_id=f"{event_id}-{minutes}", driver_id="d1", driver_name="Alex",
        created_at_utc=START + timedelta(minutes=minutes), workstation_name=None,
        on_time=on_time, late=late, response_ms=None, handling_ms=60000,
    )


def test_calls_use_quarter_hour_buckets_and_unknown_status_is_not_late():
    buckets = forklift_call_buckets(
        (_event(2, on_time=True), _event(14, late=True), _event(16)),
        start_utc=START, end_utc=START + timedelta(minutes=30),
    )
    assert [bucket.calls for bucket in buckets] == [2, 1]
    assert buckets[0].late_event_times == (START + timedelta(minutes=14),)
    assert buckets[1].rolling_ontime_pct == pytest.approx(50.0)
    assert buckets[1].rolling_late_count == 1


def test_no_classified_calls_leaves_rolling_line_gap():
    buckets = forklift_call_buckets(
        (_event(2),), start_utc=START,
        end_utc=START + timedelta(minutes=15),
    )
    assert buckets[0].calls == 1
    assert buckets[0].rolling_ontime_pct is None


def test_unknown_status_counts_as_volume_but_not_ontime_denominator():
    buckets = forklift_call_buckets(
        (_event(2, on_time=True), _event(3, late=True), _event(4)),
        start_utc=START, end_utc=START + timedelta(minutes=15),
    )
    assert buckets[0].calls == 3
    assert buckets[0].rolling_ontime_pct == pytest.approx(50.0)
```

- [ ] **Step 2: Write row and sorting tests**

Create `tests/test_people_performance_rows.py`:

```python
from datetime import timedelta

from zira_dashboard import people_performance
from zira_dashboard.people_performance import BreakSpan, assemble_dashboard

from tests.people_performance_fixtures import (
    DAY, END, START, busy_dashboard_model, driver_metric, event, score, span,
)


def test_mixed_role_person_renders_once_and_final_role_owns_completed_section():
    model = assemble_dashboard(
        day=DAY, as_of_utc=END, window_start_utc=START,
        window_end_utc=END,
        spans=(span(50, "Alex Worker", 0, 60, "Repair 1"),
               span(50, "Alex Worker", 60, 120, "Tablets")),
        production_scores=(score(50, "Alex Worker", "Repair 1", 0, 60, 25, 20),),
        downtime_by_wc={"Repair 1": ()},
        breakdown_exclusions_by_person_wc={},
        forklift_events_by_person={
            "Alex Worker": (event("Alex Worker", 90, late=True),)
        },
        forklift_day_metrics={"Alex Worker": driver_metric(1, 0, 1)},
        breaks=(), metered_wc_names={"Repair 1"},
        source_warnings=(),
    )
    assert len(model.rows) == 1
    row = model.rows[0]
    assert row.section == "forklift"
    assert [interval.role for interval in row.intervals] == ["production", "forklift"]
    assert row.intervals[1].is_transfer is True


def test_fixed_section_order_and_needs_attention_sort_are_stable():
    model = busy_dashboard_model()
    assert [(row.section, row.person_name) for row in model.rows] == [
        ("production", "Sam Stale"), ("production", "Amy Behind"),
        ("production", "Zed Ahead"),
        ("forklift", "Ben Driver"), ("forklift", "Mia Mixed"),
        ("other", "Cal Missing"),
    ]


def test_same_location_across_lunch_does_not_create_transfer():
    lunch = BreakSpan(
        START + timedelta(minutes=60), START + timedelta(minutes=90), "Lunch"
    )
    model = assemble_dashboard(
        day=DAY, as_of_utc=END, window_start_utc=START, window_end_utc=END,
        spans=(span(51, "Lunch Worker", 0, 60, "Repair 1"),
               span(51, "Lunch Worker", 90, 120, "Repair 1")),
        production_scores=(
            score(51, "Lunch Worker", "Repair 1", 0, 60, 20, 20),
            score(51, "Lunch Worker", "Repair 1", 90, 120, 10, 10),
        ),
        downtime_by_wc={"Repair 1": ()}, forklift_events_by_person={},
        breakdown_exclusions_by_person_wc={},
        forklift_day_metrics={}, breaks=(lunch,),
        metered_wc_names={"Repair 1"}, source_warnings=(),
    )
    assert [interval.is_transfer for interval in model.rows[0].intervals] == [False, False]


def test_activity_outside_valid_tablet_span_stays_unattached():
    model = assemble_dashboard(
        day=DAY, as_of_utc=END, window_start_utc=START, window_end_utc=END,
        spans=(span(52, "No Location", 0, 480, None,
                    "missing_required_location"),),
        production_scores=(), downtime_by_wc={},
        breakdown_exclusions_by_person_wc={},
        forklift_events_by_person={
            "No Location": (event("No Location", 30, on_time=True),)
        },
        forklift_day_metrics={"No Location": driver_metric(1, 1, 0)},
        breaks=(), metered_wc_names={"Repair 1"}, source_warnings=(),
    )
    row = model.rows[0]
    assert row.status == "location missing"
    assert row.unattached_forklift_calls == 1
    assert all(not interval.forklift_buckets for interval in row.intervals)


def test_one_metric_failure_keeps_that_person_and_every_other_row(monkeypatch):
    original = people_performance.production_metric
    monkeypatch.setattr(
        people_performance, "production_metric",
        lambda score, **kwargs: (
            (_ for _ in ()).throw(ValueError("bad row"))
            if score.person_odoo_id == 44 else original(score, **kwargs)
        ),
    )
    model = busy_dashboard_model()
    assert {row.employee_odoo_id for row in model.rows} >= {44, 45}
    failed = next(row for row in model.rows if row.employee_odoo_id == 44)
    assert ("metric unavailable",) == failed.attention_reasons
    assert all(value == "N/A" for _label, value in failed.summary[:3])
```

- [ ] **Step 3: Run the new tests and verify they fail**

Run:

```bash
uv run pytest tests/test_people_performance_forklift.py tests/test_people_performance_rows.py -q
```

Expected: FAIL because forklift bucketing and dashboard assembly are absent.

- [ ] **Step 4: Implement forklift buckets**

Add:

```python
def _quarter_hour_start(value: datetime) -> datetime:
    local = value.astimezone(shift_config.SITE_TZ)
    local = local.replace(minute=(local.minute // 15) * 15, second=0, microsecond=0)
    return local.astimezone(value.tzinfo)


def forklift_call_buckets(
    events: Sequence[ForkliftCompletionEvent],
    *, start_utc: datetime, end_utc: datetime,
) -> tuple[ForkliftBucket, ...]:
    relevant = tuple(sorted(
        (event for event in events if start_utc <= event.created_at_utc < end_utc),
        key=lambda event: (event.created_at_utc, event.event_id),
    ))
    buckets: list[ForkliftBucket] = []
    cursor = _quarter_hour_start(start_utc)
    while cursor < end_utc:
        bucket_start = max(cursor, start_utc)
        bucket_end = min(cursor + timedelta(minutes=15), end_utc)
        calls = tuple(event for event in relevant
                      if bucket_start <= event.created_at_utc < bucket_end)
        rolling_start = max(start_utc, bucket_end - timedelta(minutes=30))
        rolling = tuple(event for event in relevant
                        if rolling_start <= event.created_at_utc < bucket_end)
        on_time = sum(event.on_time is True and event.late is not True for event in rolling)
        late = sum(event.late is True for event in rolling)
        denominator = on_time + late
        buckets.append(ForkliftBucket(
            start_utc=bucket_start, end_utc=bucket_end, calls=len(calls),
            late_event_times=tuple(event.created_at_utc for event in calls
                                   if event.late is True),
            rolling_ontime_pct=(100.0 * on_time / denominator
                                if denominator else None),
            rolling_late_count=late,
        ))
        cursor += timedelta(minutes=15)
    return tuple(buckets)
```

- [ ] **Step 5: Implement role selection, transfer rules, summaries, and sort key**

Add `assemble_dashboard()` with the declared signature. Its implementation must follow these exact boundaries:

```python
_SECTION_RANK = {"production": 0, "forklift": 1, "other": 2}


def _role_for_span(span: LocationSpan, metered_wc_names: set[str]) -> RoleKey:
    location_usable_for_section = span.status in {"valid", "stale_open_location"}
    if location_usable_for_section and span.app_work_center_name == "Tablets":
        return "forklift"
    if location_usable_for_section and span.app_work_center_name in metered_wc_names:
        return "production"
    return "other"


def _gap_is_break(
    left: datetime, right: datetime, breaks: Sequence[BreakSpan]
) -> bool:
    return right <= left or any(
        item.start_utc <= left and right <= item.end_utc for item in breaks
    )


def _attention_rank(
    *, is_active: bool, status: str, role: RoleKey,
    current_production: ProductionMetric | None,
    current_forklift: ForkliftBucket | None,
    ontime_floor_pct: float, metric_available: bool,
) -> tuple[int, tuple[str, ...], float, float]:
    if not is_active:
        return 5, (), 0.0, 100.0
    if status in {"location pending", "location missing", "location conflicting",
                  "location unmapped", "source stale"}:
        return 0, (status,), 0.0, 0.0
    if role != "other" and not metric_available:
        return 0, ("metric unavailable",), 0.0, 0.0
    if role == "production" and current_production:
        goal_pct = (100.0 * current_production.actual_units / current_production.goal_units
                    if current_production.goal_units > 0 else None)
        current_uptime = next(
            (point.value_pct for point in reversed(current_production.rolling_uptime)
             if point.value_pct is not None), None
        )
        if goal_pct is not None and goal_pct < 100:
            return 1, ("behind goal",), 100.0 - goal_pct, current_uptime or 0.0
        if current_uptime is not None and current_uptime < 90:
            label = "uptime bad" if current_uptime < 80 else "uptime warning"
            return 2, (label,), 0.0, current_uptime
    if role == "forklift" and current_forklift:
        recent_late = current_forklift.rolling_late_count > 0
        pct = current_forklift.rolling_ontime_pct
        if recent_late or (pct is not None and pct < ontime_floor_pct):
            reasons = tuple(filter(None, (
                "late call in last 30 minutes" if recent_late else "",
                "below on-time floor" if pct is not None and pct < ontime_floor_pct else "",
            )))
            return 3, reasons, 0.0, pct or 0.0
    return 4, (), 0.0, 100.0
```

Group spans by `employee_odoo_id`, clip them to the shared window, and never group by display name. Match production scores on `(person_odoo_id, wc_name, start_utc, end_utc)`. A valid Tablet interval receives only events whose mapped person name matches and whose timestamp falls inside that interval. Keep every mapped event outside valid Tablet intervals in `unattached_forklift_calls`.

For each production score, call `production_metric()` with `breakdown_exclusions_by_person_wc.get((score.person_name, score.wc_name), ())`. Clamp open exclusions to `as_of_utc` before calling it. This makes the line denominator and downtime intersection use the same approved machine-breakdown exclusions as the existing goal scorer.

Create each stable interval key as `"{employee_odoo_id}:{role}:{location_name}:{start_utc.isoformat()}:{end_utc.isoformat()}"`. Map `pending_first_location` to `location pending`, `missing_required_location` to `location missing`, `conflicting_location` to `location conflicting`, `unmapped_location` to `location unmapped`, and `stale_open_location` to `source stale`. A valid or exempt span uses `working now` or `clocked out at <local time>`. A stale span keeps its last mapped location for section/ribbon identity but has `metric_available=False`, so it cannot earn production or imply current forklift performance.

When `production_available` is false, retain production-role intervals with `ProductionMetric.result="unavailable"` and use `N/A` for Goal, Uptime, and Downtime rather than constructing zero-valued metrics. When `forklift_available` is false, retain Tablet intervals with no call bars/line and use `N/A` for all four forklift summary values. When a driver's `ForkliftDayMetric.timeline_available` is false, show that person's four forklift summary values and Tablet timeline as unavailable instead of drawing partial event bars or a false zero.

Build each employee row through a private `_assemble_person_row()` boundary. Catch `ValueError`, `TypeError`, and arithmetic errors around that employee's metric attachment only; rebuild the same location intervals with `metric_available=False`, use `N/A` for the role's metric summary, and set `attention_reasons=("metric unavailable",)`. Never drop the attendance row or suppress another employee because one row's metric payload is malformed.

Set `is_transfer=True` only when the prior and current location or role differs; a same-location gap wholly inside a `BreakSpan` remains false. For each row, select the last clipped interval as the current/final interval. An interval ending at the common Today cap is active only if the source span is open at that cap; every historical row is completed.

Build role-specific summaries exactly as follows:

```python
production_summary = (
    ("Goal", _pct_or_na(goal_pct)),
    ("Uptime", _pct_or_na(uptime_pct)),
    ("Downtime", f"{downtime_minutes:.0f} min"),
    ("Centers", str(len(distinct_metered_centers))),
)
forklift_summary = (
    ("Calls", str(driver_day.calls)),
    ("On time", _pct_or_na(whole_day_ontime_pct)),
    ("Handling", f"{driver_day.handling_minutes:.0f} min"),
    ("Score", "N/A" if driver_day.score is None else f"{driver_day.score:.0f}"),
)
other_summary = (
    ("Clocked", _duration_label(clocked_minutes)),
    ("Location", final_location_name),
    ("Locations", str(len(distinct_locations))),
    ("Status", "Working now" if is_active else clocked_out_label),
)
```

Use `sort_key=(_SECTION_RANK[section], attention_rank, -deficit_pct, rolling_tiebreak, person_name.casefold(), employee_odoo_id)`. The section rank is always first, so an uncertain non-metered person never rises above production or forklift.

- [ ] **Step 6: Run pure tests and lint**

Run:

```bash
uv run pytest tests/test_people_performance_production.py tests/test_people_performance_forklift.py tests/test_people_performance_rows.py -q
uv run ruff check src/zira_dashboard/people_performance.py tests/test_people_performance_forklift.py tests/test_people_performance_rows.py
```

Expected: both commands PASS.

- [ ] **Step 7: Add the patch note, commit, and push**

Add:

```markdown
### Put every worker in the right dashboard group

- **Plant Manager can now place production workers, tablet drivers, and other workers in the right order.** A person who moves still stays on one line, and late calls or low results can move them to the top of their group.
```

Run:

```bash
git add src/zira_dashboard/people_performance.py tests/people_performance_fixtures.py tests/test_people_performance_forklift.py tests/test_people_performance_rows.py CHANGELOG.md
git commit -m "feat: assemble people performance rows"
git push origin main
```

### Task 4: Expose exact adjusted downtime intervals

**Files:**
- Modify: `src/zira_dashboard/leaderboard.py`
- Modify: `src/zira_dashboard/production_history.py`
- Modify: `src/zira_dashboard/_zira_persist.py`
- Modify: `tests/test_leaderboard_downtime.py`
- Modify: `tests/test_production_history.py`
- Modify: `tests/test_zira_persist.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: existing Zira stop rows, production samples, `shift_config.breaks_for()`, and the current transfer-gap rule.
- Produces: `StationTotal.downtime_intervals: tuple[tuple[datetime, datetime], ...]` and public `production_history.metered_station_totals(client, day, now_utc=None)`.

- [ ] **Step 1: Write failing interval tests around existing downtime cases**

Add to `tests/test_leaderboard_downtime.py`:

```python
def test_adjusted_downtime_intervals_split_around_lunch(_lunch_1130_to_1200):
    samples = [
        (_utc(11, 20), 1),
        (_utc(12, 10), 1),
    ]
    rows = [(_utc(12, 5), 40)]
    intervals = leaderboard._adjusted_downtime_intervals(
        rows, samples, _utc(14, 30)
    )
    assert intervals == (
        (_utc(11, 25), _utc(11, 30)),
        (_utc(12, 0), _utc(12, 5)),
    )
    assert leaderboard._adjusted_downtime(rows, samples, _utc(14, 30)) == 10


def test_station_total_keeps_interval_contract_backward_compatible():
    station = Station("1", "Repair 1", "Repair", "Recycling")
    total = leaderboard.StationTotal(
        station, 1, 1, False, 0, 0, None, None, (), ()
    )
    assert total.downtime_intervals == ()
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
uv run pytest tests/test_leaderboard_downtime.py -q
```

Expected: FAIL because interval output is not exposed.

- [ ] **Step 3: Refactor downtime into break-adjusted intervals without changing totals**

Add the new last field to `StationTotal` so positional callers remain valid:

```python
    downtime_intervals: tuple[tuple[datetime, datetime], ...] = ()
```

Add these helpers to `leaderboard.py` and make `_adjusted_downtime()` sum their durations:

```python
def _subtract_breaks(
    start_utc: datetime,
    end_utc: datetime,
    breaks_by_day: dict[date, Any],
) -> tuple[tuple[datetime, datetime], ...]:
    pieces = [(start_utc, end_utc)]
    local_start = start_utc.astimezone(SITE_TZ).date()
    local_end = end_utc.astimezone(SITE_TZ).date()
    day = local_start
    while day <= local_end:
        if day not in breaks_by_day:
            try:
                breaks_by_day[day] = breaks_for(day) or []
            except Exception:
                breaks_by_day[day] = []
        for shift_break in breaks_by_day[day]:
            break_start = datetime.combine(day, shift_break.start, tzinfo=SITE_TZ).astimezone(UTC)
            break_end = datetime.combine(day, shift_break.end, tzinfo=SITE_TZ).astimezone(UTC)
            next_pieces: list[tuple[datetime, datetime]] = []
            for left, right in pieces:
                if break_end <= left or break_start >= right:
                    next_pieces.append((left, right))
                    continue
                if left < break_start:
                    next_pieces.append((left, min(right, break_start)))
                if break_end < right:
                    next_pieces.append((max(left, break_end), right))
            pieces = next_pieces
        day += timedelta(days=1)
    return tuple((left, right) for left, right in pieces if right > left)


def _adjusted_downtime_intervals(
    downtime_rows: list[tuple[datetime, int]],
    samples: list[tuple[datetime, int]],
    end_of_day: datetime,
) -> tuple[tuple[datetime, datetime], ...]:
    active = _active_intervals(samples, end_of_day)
    if not active:
        return ()
    breaks_by_day: dict[date, Any] = {}
    sample_times = sorted(ts for ts, _units in samples)
    adjusted: list[tuple[datetime, datetime]] = []
    for event_end, duration_min in downtime_rows:
        event_start = event_end - timedelta(minutes=duration_min)
        sample_idx = bisect_left(sample_times, event_end) - 1
        if sample_idx >= 0:
            event_start = max(event_start, sample_times[sample_idx])
        for active_start, active_end in active:
            left = max(event_start, active_start)
            right = min(event_end, active_end)
            if right > left:
                adjusted.extend(_subtract_breaks(left, right, breaks_by_day))
    return tuple(sorted(adjusted))


def _adjusted_downtime(
    downtime_rows: list[tuple[datetime, int]],
    samples: list[tuple[datetime, int]],
    end_of_day: datetime,
) -> int:
    return int(sum(
        (right - left).total_seconds() / 60.0
        for left, right in _adjusted_downtime_intervals(
            downtime_rows, samples, end_of_day
        )
    ))
```

In `fetch_station_day()`, compute `adjusted_intervals` once, derive `downtime` from it, and pass `downtime_intervals=adjusted_intervals` to `StationTotal`.

- [ ] **Step 4: Version the persistent station cache so historical lines stay exact**

Extend `_serialize_total()` with:

```python
"payload_version": 2,
"downtime_intervals": [
    [_serialize_dt(start), _serialize_dt(end)]
    for start, end in total.downtime_intervals
],
```

Extend `_deserialize_total()` with:

```python
downtime_intervals=tuple(
    (_deserialize_dt(start), _deserialize_dt(end))
    for start, end in payload.get("downtime_intervals", [])
),
```

In `load_day()`, after JSON decoding and before deserialization, return `None` when `int(payload.get("payload_version", 1)) < 2`. This makes the existing leaderboard path refetch and replace old historical payloads once, rather than claiming that an old aggregate-only cache has a zero-downtime timeline.

Add tests that a version-2 round trip preserves exact interval timestamps and an old payload makes `load_day()` return `None`.

- [ ] **Step 5: Add the public all-metered loader**

In `production_history.py`, replace the private-only entry point with:

```python
def metered_station_totals(client, day: date, now_utc: datetime | None = None):
    from . import staffing
    from .leaderboard import cached_leaderboard
    from .stations import Station

    stations = [
        Station(loc.meter_id, loc.name, loc.skill, loc.bay)
        for loc in staffing.LOCATIONS if loc.meter_id
    ]
    return cached_leaderboard(client, stations, day, now_utc) if stations else []


def _metered_leaderboard(client, day: date):
    return metered_station_totals(client, day)
```

Add a test that passes a fixed `now_utc`, monkeypatches `cached_leaderboard`, and asserts the cap is forwarded and all `LOCATIONS` with a meter become stations.

- [ ] **Step 6: Run focused and regression tests**

Run:

```bash
uv run pytest tests/test_leaderboard_downtime.py tests/test_leaderboard.py tests/test_production_history.py tests/test_zira_persist.py -q
```

Expected: PASS with all existing downtime totals unchanged.

- [ ] **Step 7: Add the patch note, commit, and push**

Add:

```markdown
### Remember exactly when a machine stopped

- **Plant Manager can now keep the exact parts of a stop that count as downtime.** Break and lunch time stay out, so the new people view can draw an honest uptime line.
```

Run:

```bash
git add src/zira_dashboard/leaderboard.py src/zira_dashboard/production_history.py src/zira_dashboard/_zira_persist.py tests/test_leaderboard_downtime.py tests/test_production_history.py tests/test_zira_persist.py CHANGELOG.md
git commit -m "feat: expose adjusted downtime intervals"
git push origin main
```

### Task 5: Load and reconcile one selected day without cross-source guessing

**Files:**
- Create: `src/zira_dashboard/people_performance_data.py`
- Modify: `src/zira_dashboard/production_history.py`
- Create: `tests/test_people_performance_data.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 1 event store, Task 2/3 pure assembler, Task 4 station downtime, canonical Odoo spans, current production testing/breakdown exclusions, existing forklift daily rows and score configuration.
- Produces: `load_dashboard(day: date, client, *, now_utc: datetime | None = None) -> DashboardModel` and `production_history.production_scores_for_timeline()` below.

- [ ] **Step 1: Write failing happy-path and failure-isolation tests**

Create `tests/test_people_performance_data.py` using monkeypatched seams rather than a live API or database:

```python
from datetime import timedelta
from types import SimpleNamespace

from zira_dashboard import people_performance_data as data
from zira_dashboard import forklift_score
from tests.people_performance_fixtures import DAY, END, START, event, span


NOW = START + timedelta(hours=5)


def install_sources(
    monkeypatch, *, spans, events=(), driver_rows=(), resolved=None,
):
    monkeypatch.setattr(data, "_bounds", lambda day, now: (START, END, NOW, True))
    monkeypatch.setattr(data, "_breaks", lambda day: ())
    monkeypatch.setattr(
        data.attendance_timeline, "timeline_for_range",
        lambda start, end, as_of_utc=None: tuple(spans),
    )
    monkeypatch.setattr(
        data.production_history, "metered_station_totals", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        data.production_history, "production_scores_for_timeline",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setattr(
        data.forklift_event_store, "completion_events_for_range",
        lambda start, end: tuple(events),
    )
    monkeypatch.setattr(
        data.forklift_store, "driver_rows_for_day", lambda day: list(driver_rows)
    )
    monkeypatch.setattr(
        data.forklift_store, "resolve_forklift_to_plant",
        lambda names: dict(resolved or {name: name for name in names}),
    )
    monkeypatch.setattr(data.forklift_settings, "current", lambda: object())
    monkeypatch.setattr(
        data.forklift_settings, "resolve",
        lambda settings, algo_throughput: SimpleNamespace(
            score_config=lambda: forklift_score.DEFAULT_SCORE_CONFIG
        ),
    )
    monkeypatch.setattr(
        data.attendance_readiness, "build_report",
        lambda now: SimpleNamespace(
            ready=True, mirror_age_seconds=1.0, blockers=()
        ),
    )


TRENT_SPAN = span(60, "Trent Iverson", 0, 300, "Tablets")
TRENT_CALL = event("Trent", 30, on_time=True)
TRENT_DRIVER_ROW = {
    "driver_id": "driver-Trent", "name": "Trent", "calls": 1,
    "on_time": 1, "late": 0, "on_call_ms": 120000,
    "avg_ms": 60000, "utilization_pct": 50,
}


def test_load_dashboard_uses_one_cap_and_safe_forklift_name_map(monkeypatch):
    seen = {}
    install_sources(
        monkeypatch, spans=(TRENT_SPAN,), events=(TRENT_CALL,),
        driver_rows=(TRENT_DRIVER_ROW,),
        resolved={"Trent": "Trent Iverson"},
    )
    monkeypatch.setattr(
        data.attendance_timeline, "timeline_for_range",
        lambda start, end, as_of_utc=None: seen.update(
            start=start, end=end, attendance_cap=as_of_utc
        ) or (TRENT_SPAN,),
    )
    monkeypatch.setattr(
        data.production_history, "metered_station_totals",
        lambda client, day, now_utc=None: seen.update(production_cap=now_utc) or [],
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert seen["attendance_cap"] == seen["production_cap"] == NOW
    assert seen["end"] == END
    assert model.rows[0].person_name == "Trent Iverson"
    assert model.rows[0].summary[0] == ("Calls", "1")


def test_production_failure_is_unavailable_not_zero(monkeypatch):
    install_sources(
        monkeypatch, spans=(span(61, "Repair Worker", 0, 300, "Repair 1"),)
    )
    monkeypatch.setattr(
        data.production_history, "metered_station_totals",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("zira down")),
    )
    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)
    row = model.rows[0]
    assert "Production data unavailable" in model.source_warnings
    assert any(value == "N/A" for _label, value in row.summary)
    assert all(
        interval.production is None or interval.production.result == "unavailable"
        for interval in row.intervals
    )


def test_forklift_failure_does_not_hide_attendance_or_production(monkeypatch):
    install_sources(monkeypatch, spans=(TRENT_SPAN,))
    monkeypatch.setattr(
        data.forklift_event_store, "completion_events_for_range",
        lambda *args: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)
    assert len(model.rows) == 1
    assert "Forklift data unavailable" in model.source_warnings


def test_ambiguous_forklift_name_is_not_attached(monkeypatch):
    install_sources(
        monkeypatch,
        spans=(span(62, "Jesus Ramos", 0, 300, "Tablets"),),
        events=(event("Jesus", 30, on_time=True),),
        resolved={"Jesus": "Jesus"},
    )
    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)
    assert model.rows[0].unattached_forklift_calls == 0
    assert "Unmatched forklift calls: 1" in model.source_warnings


def test_incomplete_event_history_does_not_draw_a_false_empty_timeline(monkeypatch):
    install_sources(
        monkeypatch, spans=(TRENT_SPAN,), events=(),
        driver_rows=({**TRENT_DRIVER_ROW, "calls": 12},),
        resolved={"Trent": "Trent Iverson"},
    )
    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)
    row = model.rows[0]
    assert row.summary[0] == ("Calls", "N/A")
    assert "Forklift timeline incomplete" in model.source_warnings
    assert all(not interval.forklift_buckets for interval in row.intervals)
```

Also test a historical day caps at shift end and sets `is_today=False`, and test that an `attendance_readiness.build_report()` stale state becomes a source warning while the returned stale spans stay visible.

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
uv run pytest tests/test_people_performance_data.py -q
```

Expected: FAIL because the day-data service does not exist.

- [ ] **Step 3: Add one reusable production-scoring entry point**

In `production_history.py`, add:

```python
def production_scores_for_timeline(
    client,
    day: date,
    spans,
    *,
    now_utc: datetime,
    station_totals=None,
    breakdown_windows=None,
):
    from . import (
        assignment_windows, machine_breakdown, production_segments,
        settings_store, shift_config, wc_attributions,
    )

    shift_start = datetime.combine(
        day, shift_config.shift_start_for(day), tzinfo=shift_config.SITE_TZ
    ).astimezone(UTC)
    shift_end = datetime.combine(
        day, shift_config.shift_end_for(day), tzinfo=shift_config.SITE_TZ
    ).astimezone(UTC)
    cap_utc = min(now_utc, shift_end)
    segments = assignment_windows.work_segments_from_timeline(
        spans, window_start_utc=shift_start, window_end_utc=cap_utc
    )
    if not segments:
        return ()
    totals = list(station_totals) if station_totals is not None else list(
        metered_station_totals(client, day, cap_utc)
    )
    wc_totals = {total.station.name: float(total.units) for total in totals}
    samples = {total.station.name: list(total.samples) for total in totals}
    testing = wc_attributions.testing_windows_for_day(day)
    if testing:
        tuple_totals = {
            total.station.name: (total.units, total.downtime_minutes)
            for total in totals
        }
        adjusted = _apply_testing_offsets(tuple_totals, samples, testing)
        wc_totals = {name: float(value[0]) for name, value in adjusted.items()}
        samples = _without_testing_samples(samples, testing)
    for wc_name, total_units in wc_totals.items():
        sampled_units = sum(float(units or 0) for _timestamp, units in samples.get(wc_name, ()))
        if abs(total_units - sampled_units) > 1e-6:
            raise ProductionSourceUnavailable(
                f"Timestamped samples for {wc_name} do not match its source total"
            )
    breakdowns = (
        wc_attributions.breakdown_windows_for_day(day)
        if breakdown_windows is None else breakdown_windows
    )

    def productive(person: str, wc_name: str, start: datetime, end: datetime) -> float:
        raw = shift_config.productive_minutes_in_window(day, start, end)
        excluded = machine_breakdown.excluded_minutes_overlapping(
            breakdowns.get((person, wc_name), ()), start, end, now_utc, day,
            shift_config.productive_minutes_in_window,
        )
        return max(0.0, raw - excluded)

    credits = production_segments.credit_work_segments(
        segments, wc_totals=wc_totals, samples_by_wc=samples,
        productive_minutes=productive,
        live_cap_utc=(cap_utc
                      if day == now_utc.astimezone(shift_config.SITE_TZ).date()
                      else None),
    )
    target_per_hour = {
        total.station.name: settings_store.station_target(total.station)
        for total in totals
    }
    scores_by_wc = production_segments.score_work_segments(
        credits, target_per_hour=target_per_hour
    )
    return tuple(
        score
        for wc_scores in scores_by_wc.values()
        for score in wc_scores
    )
```

Move this function's public import into `people_performance_data.py` through the module, not a copied scoring formula. Add focused tests that monkeypatch `credit_work_segments()` and assert testing samples and breakdown minutes are removed before scoring, and that a positive total without matching timestamped samples raises `ProductionSourceUnavailable` instead of using the legacy worked-minute fallback.

- [ ] **Step 4: Implement source payloads and the common day cap**

Create `people_performance_data.py` with:

```python
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, UTC

from . import (
    attendance_readiness, attendance_timeline, forklift_event_store,
    forklift_score, forklift_settings, forklift_store, production_history,
    shift_config, wc_attributions,
)
from .people_performance import (
    BreakSpan, DashboardModel, ForkliftDayMetric, assemble_dashboard,
)


_log = logging.getLogger(__name__)
_LOAD_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="people-performance")


def _bounds(day: date, now_utc: datetime) -> tuple[datetime, datetime, datetime, bool]:
    start = datetime.combine(
        day, shift_config.shift_start_for(day), tzinfo=shift_config.SITE_TZ
    ).astimezone(UTC)
    end = datetime.combine(
        day, shift_config.shift_end_for(day), tzinfo=shift_config.SITE_TZ
    ).astimezone(UTC)
    is_today = day == now_utc.astimezone(shift_config.SITE_TZ).date()
    cap = min(now_utc, end) if is_today else end
    return start, end, max(start, cap), is_today


def _breaks(day: date) -> tuple[BreakSpan, ...]:
    return tuple(BreakSpan(
        datetime.combine(day, item.start, tzinfo=shift_config.SITE_TZ).astimezone(UTC),
        datetime.combine(day, item.end, tzinfo=shift_config.SITE_TZ).astimezone(UTC),
        getattr(item, "name", None) or "Planned break",
    ) for item in shift_config.breaks_for(day))
```

- [ ] **Step 5: Implement parallel reads and independent degradation**

`load_dashboard()` must submit these three source families with the same bounds and cap:

```python
attendance_future = _LOAD_POOL.submit(
    attendance_timeline.timeline_for_range, start, end, as_of_utc=cap
)
production_future = _LOAD_POOL.submit(
    production_history.metered_station_totals, client, day, cap
)
forklift_future = _LOAD_POOL.submit(
    _load_forklift_source, day, start, cap
)
```

Resolve each future in its own `try` block. Attendance failure produces an empty page plus `Attendance data unavailable`; it must not call staffing or schedule. Production failure keeps spans, sets every production region unavailable, and adds `Production data unavailable`. Forklift failure keeps spans and production and adds `Forklift data unavailable`.

For a successful production read, load `breakdown_windows = wc_attributions.breakdown_windows_for_day(day)` once. Clamp each `None` end to `cap`, and call `production_history.production_scores_for_timeline(client, day, spans, now_utc=cap, station_totals=station_totals, breakdown_windows=breakdown_windows)`. Build `downtime_by_wc` directly from each `StationTotal.downtime_intervals`, and pass the same closed breakdown map to `assemble_dashboard()` as `breakdown_exclusions_by_person_wc`.

For forklift data, read events from plant-local midnight through the lesser of the next midnight and `now_utc`, so completeness can be reconciled to `forklift_driver_daily`. Then keep only events inside the shared shift display window for the dashboard metrics and timeline. Resolve names once:

```python
resolved = forklift_store.resolve_forklift_to_plant(
    {event.driver_name for event in events}
    | {str(row["name"]) for row in driver_rows}
)
known_people = {span.employee_name for span in spans}
events_by_person: dict[str, list] = {}
unmatched = 0
for event in events:
    plant_name = resolved.get(event.driver_name, event.driver_name)
    if plant_name not in known_people:
        unmatched += 1
        continue
    events_by_person.setdefault(plant_name, []).append(event)
```

Apply the same rule to daily rows. For each safely mapped person, derive calls, classified on-time/late counts, and handling minutes from that person's raw events inside `[start, cap)`, including mapped calls that remain unattached to a Tablet span. Resolve the current score config using the saved daily row only for the existing weighted score:

```python
cfg = forklift_settings.resolve(
    forklift_settings.current(), algo_throughput=0.0
).score_config()
breakdown = forklift_score.daily_score(row, cfg)
```

Construct `ForkliftDayMetric` with the display-window event counts, summed raw `handling_ms / 60000`, `breakdown.score if breakdown else None`, and `cfg.ontime_floor`. Add `Unmatched forklift calls: N` when `unmatched > 0`.

For every safely mapped driver, compare the number of stored events inside the selected plant-local calendar day with `forklift_driver_daily.calls`. If they differ, set that driver's `ForkliftDayMetric.timeline_available=False`, add `Forklift timeline incomplete`, show all four driver summary values as `N/A`, and do not draw a partial call timeline. The saved daily tables remain unchanged and authoritative for the existing forklift leaderboard. Pass `production_available` and `forklift_available` explicitly to `assemble_dashboard()` so source failure cannot be inferred from an empty collection.

For Today only, call `attendance_readiness.build_report(cap)` best-effort. Add `Attendance source stale` when `mirror_age_seconds` exceeds 90 seconds or the report has a freshness blocker. Historical days use the mirrored source versions stored for that day and do not inherit a warning from today's polling freshness. Do not reinterpret open conflicts/missing/unmapped counts as a global source outage; those remain per-row status.

- [ ] **Step 6: Run focused tests and lint**

Run:

```bash
uv run pytest tests/test_people_performance_data.py tests/test_production_history.py tests/test_production_history_odoo_strict.py -q
uv run ruff check src/zira_dashboard/people_performance_data.py src/zira_dashboard/production_history.py tests/test_people_performance_data.py
```

Expected: both commands PASS.

- [ ] **Step 7: Add the patch note, commit, and push**

Add:

```markdown
### Bring each worker's day together safely

- **Plant Manager can now bring work times, production, stops, and forklift calls into one safe day view.** If one source is down, the other facts stay visible and the missing part says it is not available.
```

Run:

```bash
git add src/zira_dashboard/people_performance_data.py src/zira_dashboard/production_history.py tests/test_people_performance_data.py CHANGELOG.md
git commit -m "feat: load people performance day data"
git push origin main
```

### Task 6: Add the People route, presenter, navigation, and semantic row markup

**Files:**
- Create: `src/zira_dashboard/people_performance_view.py`
- Create: `src/zira_dashboard/routes/people_performance.py`
- Modify: `src/zira_dashboard/app.py`
- Modify: `src/zira_dashboard/routes/README.md`
- Modify: `src/zira_dashboard/templates/_performance_subnav.html`
- Modify: `src/zira_dashboard/templates/_staffing_base.html`
- Create: `src/zira_dashboard/templates/people_performance.html`
- Create: `src/zira_dashboard/templates/_people_performance_rows.html`
- Create: `tests/test_people_performance_view.py`
- Create: `tests/test_people_performance_route.py`
- Create: `tests/test_people_performance_template.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 5 `load_dashboard()` and immutable `DashboardModel`.
- Produces: `dashboard_context(model, *, attention_only=False) -> dict`, `GET /people-performance`, and `GET /people-performance/rows`.

- [ ] **Step 1: Write presenter geometry tests**

Create `tests/test_people_performance_view.py`:

```python
from zira_dashboard.people_performance_view import dashboard_context
from tests.people_performance_fixtures import busy_dashboard_model


def test_presenter_uses_one_axis_and_preserves_short_intervals():
    model = busy_dashboard_model()
    context = dashboard_context(model)
    row = next(
        row
        for section in context["sections"]
        for row in section["rows"]
        if row["person_name"] == "Mia Mixed"
    )
    assert context["axis_labels"][0]["left_pct"] == 0.0
    assert context["axis_labels"][-1]["left_pct"] == 100.0
    short = next(item for item in row["intervals"]
                 if item["location_name"] == "Repair 2")
    assert short["width_pct"] > 0
    assert short["aria_label"].startswith("Transferred to Repair 2")


def test_attention_filter_keeps_every_reason_state_inside_fixed_sections():
    context = dashboard_context(busy_dashboard_model(), attention_only=True)
    assert [section["key"] for section in context["sections"]] == [
        "production", "forklift", "other"
    ]
    reasons = {
        reason
        for section in context["sections"]
        for row in section["rows"]
        for reason in row["attention_reasons"]
    }
    assert {"behind goal", "late call in last 30 minutes", "location missing"} <= reasons
```

- [ ] **Step 2: Run presenter tests and verify they fail**

Run: `uv run pytest tests/test_people_performance_view.py -q`

Expected: FAIL because the presenter does not exist.

- [ ] **Step 3: Implement deterministic view geometry and labels**

Create `people_performance_view.py` with:

```python
from __future__ import annotations

from datetime import datetime, timedelta

from . import shift_config
from .people_performance import DashboardModel, PersonRow, TimelineInterval


_SECTION_LABELS = {
    "production": "Metered production",
    "forklift": "Tablet forklift",
    "other": "Other non-metered people",
}
_LOCATION_CLASSES = (
    "location-1", "location-2", "location-3", "location-4",
    "location-5", "location-6", "location-7", "location-8",
)


def _pct(value: datetime, start: datetime, end: datetime) -> float:
    total = (end - start).total_seconds()
    return 0.0 if total <= 0 else max(
        0.0, min(100.0, 100.0 * (value - start).total_seconds() / total)
    )


def _time(value: datetime) -> str:
    return value.astimezone(shift_config.SITE_TZ).strftime("%-I:%M %p")


def _line_runs(points: list[dict]) -> tuple[tuple[dict, ...], ...]:
    runs: list[list[dict]] = []
    current: list[dict] = []
    for point in points:
        if point["y"] is None:
            if current:
                runs.append(current)
                current = []
        else:
            current.append(point)
    if current:
        runs.append(current)
    return tuple(tuple(run) for run in runs if len(run) >= 2)


def _interval_detail(item: TimelineInterval) -> str:
    header = f"{item.location_name}, {_time(item.start_utc)} to {_time(item.end_utc)}"
    if not item.metric_available:
        return f"{header}. {item.role.title()} data unavailable."
    if item.production:
        metric = item.production
        if metric.result == "unavailable":
            return f"{header}. Production data unavailable."
        return (
            f"{header}. {metric.result.title()}. "
            f"{metric.actual_units:.1f} credited units against a {metric.goal_units:.1f} goal. "
            f"{metric.productive_minutes:.0f} productive minutes and "
            f"{metric.downtime_minutes:.0f} downtime minutes."
        )
    calls = sum(bucket.calls for bucket in item.forklift_buckets)
    if item.role == "forklift":
        return f"{header}. {calls} completed forklift calls in this interval."
    return f"{header}. No production or forklift metric applies."


def _row_view(
    row: PersonRow, model: DashboardModel, location_classes: dict[str, str]
) -> dict:
    intervals = []
    for item in row.intervals:
        left = _pct(item.start_utc, model.window_start_utc, model.window_end_utc)
        right = _pct(item.end_utc, model.window_start_utc, model.window_end_utc)
        line_points = []
        if item.production:
            line_points = [
                {
                    "x": _pct(point.at_utc, item.start_utc, item.end_utc),
                    "y": None if point.value_pct is None else 100.0 - point.value_pct,
                }
                for point in item.production.rolling_uptime
            ]
        elif item.role == "forklift":
            line_points = [
                {
                    "x": _pct(bucket.end_utc, item.start_utc, item.end_utc),
                    "y": (None if bucket.rolling_ontime_pct is None
                          else 100.0 - bucket.rolling_ontime_pct),
                }
                for bucket in item.forklift_buckets
            ]
        max_calls = max((bucket.calls for bucket in item.forklift_buckets), default=0)
        buckets = [{
            "left_pct": _pct(bucket.start_utc, item.start_utc, item.end_utc),
            "width_pct": max(0.0, _pct(bucket.end_utc, item.start_utc, item.end_utc)
                             - _pct(bucket.start_utc, item.start_utc, item.end_utc)),
            "height_pct": (100.0 * bucket.calls / max_calls if max_calls else 0.0),
            "late_markers": [
                _pct(value, item.start_utc, item.end_utc)
                for value in bucket.late_event_times
            ],
        } for bucket in item.forklift_buckets]
        detail = _interval_detail(item)
        intervals.append({
            "key": item.key,
            "left_pct": left,
            "width_pct": max(0.01, right - left),
            "location_name": item.location_name,
            "location_class": location_classes[item.location_name],
            "role": item.role,
            "state": ("unavailable" if not item.metric_available
                      else item.production.result if item.production else "neutral"),
            "is_transfer": item.is_transfer,
            "line_runs": _line_runs(line_points),
            "buckets": buckets,
            "detail": detail,
            "aria_label": (f"Transferred to {detail}" if item.is_transfer else detail),
        })
    return {
        "employee_odoo_id": row.employee_odoo_id,
        "person_name": row.person_name,
        "is_active": row.is_active,
        "status": row.status,
        "primary_role": row.primary_role,
        "attention_reasons": row.attention_reasons,
        "intervals": intervals,
        "breaks": [{
            "left_pct": _pct(item.start_utc, model.window_start_utc, model.window_end_utc),
            "width_pct": _pct(item.end_utc, model.window_start_utc, model.window_end_utc)
                         - _pct(item.start_utc, model.window_start_utc, model.window_end_utc),
            "label": item.label,
        } for item in row.breaks],
        "summary": row.summary,
        "unattached_forklift_calls": row.unattached_forklift_calls,
    }


def dashboard_context(model: DashboardModel, *, attention_only: bool = False) -> dict:
    names = sorted({item.location_name for row in model.rows for item in row.intervals})
    location_classes = {
        name: _LOCATION_CLASSES[
            sum(ord(character) for character in name.casefold()) % len(_LOCATION_CLASSES)
        ]
        for name in names
    }
    rows = [row for row in model.rows
            if not attention_only or row.attention_reasons]
    sections = [{
        "key": key,
        "label": _SECTION_LABELS[key],
        "rows": [_row_view(row, model, location_classes)
                 for row in rows if row.section == key],
    } for key in ("production", "forklift", "other")]
    total_minutes = int((model.window_end_utc - model.window_start_utc).total_seconds() / 60)
    axis_step = 60 if total_minutes > 360 else 30
    axis_labels = []
    value = model.window_start_utc
    while value <= model.window_end_utc:
        axis_labels.append({"label": _time(value), "left_pct": _pct(
            value, model.window_start_utc, model.window_end_utc
        )})
        value += timedelta(minutes=axis_step)
    if axis_labels[-1]["left_pct"] != 100.0:
        axis_labels.append({"label": _time(model.window_end_utc), "left_pct": 100.0})
    return {
        "day": model.day.isoformat(), "is_today": model.is_today,
        "as_of": _time(model.as_of_utc), "sections": sections,
        "axis_labels": axis_labels, "source_warnings": model.source_warnings,
        "working_now": sum(row.is_active for row in model.rows),
        "worked_earlier": sum(not row.is_active for row in model.rows),
        "needs_attention": sum(bool(row.attention_reasons) for row in model.rows),
        "attention_only": attention_only,
    }
```

- [ ] **Step 4: Write route and template tests**

Create `tests/test_people_performance_route.py`:

```python
from datetime import timedelta

from tests.people_performance_fixtures import busy_dashboard_model
from zira_dashboard.routes import people_performance as route


MODEL = busy_dashboard_model()


def test_people_page_defaults_to_today_and_registers_performance_nav(client, monkeypatch):
    monkeypatch.setattr(route, "load_dashboard", lambda day, client, now_utc=None: MODEL)
    response = client.get("/people-performance")
    assert response.status_code == 200
    assert 'href="/people-performance"' in response.text
    assert 'class="subnav-item active"' in response.text


def test_future_day_is_rejected(client):
    response = client.get("/people-performance?day=2999-01-01")
    assert response.status_code == 400


def test_rows_partial_is_not_cached(client, monkeypatch):
    monkeypatch.setattr(route, "load_dashboard", lambda day, client, now_utc=None: MODEL)
    response = client.get("/people-performance/rows?day=2026-08-28&attention=1")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert '<div id="people-performance-rows"' in response.text


def test_historical_page_revalidates_so_source_corrections_appear(client, monkeypatch):
    monkeypatch.setattr(route, "load_dashboard", lambda day, client, now_utc=None: MODEL)
    historical = (route.plant_today() - timedelta(days=1)).isoformat()
    response = client.get(f"/people-performance?day={historical}")
    assert response.headers["cache-control"] == "private, no-cache"
```

Create `tests/test_people_performance_template.py`:

```python
import pytest

from tests.people_performance_fixtures import busy_dashboard_model
from zira_dashboard.routes import people_performance as route


@pytest.fixture
def rendered_html(client, monkeypatch):
    monkeypatch.setattr(
        route, "load_dashboard", lambda day, client, now_utc=None: busy_dashboard_model()
    )
    response = client.get("/people-performance?day=2026-08-28")
    assert response.status_code == 200
    return response.text


def test_all_metric_states_are_named_and_keyboard_reachable(rendered_html):
    for text in (
        "Metered production", "Tablet forklift", "Other non-metered people",
        "Ahead", "Behind", "Planned break", "Transfer", "Unavailable",
        "location missing", "source stale",
    ):
        assert text.lower() in rendered_html.lower()
    assert 'type="button" class="pp-interval-trigger' in rendered_html
    assert 'aria-label="Transferred to' in rendered_html


def test_mixed_role_person_has_one_row(rendered_html):
    assert rendered_html.count('data-person-id="48"') == 1
```

- [ ] **Step 5: Run route/template tests and verify they fail**

Run:

```bash
uv run pytest tests/test_people_performance_route.py tests/test_people_performance_template.py -q
```

Expected: FAIL because the route, templates, and nav entry are absent.

- [ ] **Step 6: Register the route and cache policy**

Create `routes/people_performance.py`:

```python
from __future__ import annotations

from datetime import date, datetime, UTC

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .. import _http_cache
from ..deps import client as zira_client, templates
from ..people_performance_data import load_dashboard
from ..people_performance_view import dashboard_context
from ..plant_day import today as plant_today


router = APIRouter()


def _selected_day(raw: date | None) -> date:
    value = raw or plant_today()
    if value > plant_today():
        raise HTTPException(400, "Choose today or an earlier day")
    return value


def _context(day: date, attention: bool) -> dict:
    model = load_dashboard(day, zira_client, now_utc=datetime.now(UTC))
    return {
        **dashboard_context(model, attention_only=attention),
        "active": "people",
        "active_dashboard_key": "people",
        "today": plant_today().isoformat(),
    }


@router.get("/people-performance", response_class=HTMLResponse)
def people_performance(
    request: Request,
    day: date | None = Query(default=None),
    attention: bool = Query(default=False),
):
    selected = _selected_day(day)
    response = templates.TemplateResponse(
        request, "people_performance.html", _context(selected, attention)
    )
    if selected == plant_today():
        _http_cache.set_cache_headers(response, includes_today=True)
    else:
        response.headers["Cache-Control"] = "private, no-cache"
    return response


@router.get("/people-performance/rows", response_class=HTMLResponse)
def people_performance_rows(
    request: Request,
    day: date | None = Query(default=None),
    attention: bool = Query(default=False),
):
    selected = _selected_day(day)
    response = templates.TemplateResponse(
        request, "_people_performance_rows.html", _context(selected, attention)
    )
    response.headers["Cache-Control"] = "no-store"
    return response
```

Import `people_performance` in `app.py`'s route import tuple and call `app.include_router(people_performance.router)` beside the other Performance routes. Add the module and its URLs to `routes/README.md`.

In `_staffing_base.html`, add `people` to both Performance lists. In `_performance_subnav.html`, document `people` and add after Operator:

```jinja2
<a href="/people-performance"
   class="subnav-item {% if active_dashboard_key == 'people' %}active{% endif %}">
  People
</a>
```

- [ ] **Step 7: Add the full page and shared row partial**

Create `people_performance.html`:

```jinja2
{% extends "_staffing_base.html" %}
{% block title %}People performance{% endblock %}
{% block extra_head %}
<link rel="stylesheet" href="/static/people-performance.css?v={{ static_v('people-performance.css') }}">
{% endblock %}
{% block content %}
<section class="pp-page" data-day="{{ day }}" data-today="{{ 1 if is_today else 0 }}">
  <header class="pp-toolbar">
    <div>
      <p class="pp-eyebrow">People performance</p>
      <h1>{{ 'Today' if is_today else day }}</h1>
      <p class="pp-updated" id="pp-live-status" aria-live="polite">Updated through {{ as_of }}</p>
    </div>
    <form class="pp-controls" action="/people-performance" method="get">
      <label>Date <input type="date" name="day" value="{{ day }}" max="{{ today }}"></label>
      <label class="pp-check"><input type="checkbox" name="attention" value="1" {% if attention_only %}checked{% endif %}> Needs attention</label>
      <button type="submit">Apply</button>
      <a href="/people-performance">Today</a>
    </form>
  </header>
  {% include "_people_performance_rows.html" %}
</section>
{% endblock %}
{% block scripts %}
<script src="/static/people-performance.js?v={{ static_v('people-performance.js') }}"></script>
{% endblock %}
```

Create `_people_performance_rows.html` with one shared row shape:

```jinja2
<div id="people-performance-live" data-day="{{ day }}" data-attention="{{ 1 if attention_only else 0 }}">
<div class="pp-counts" aria-label="Day totals">
  <span><strong>{{ working_now }}</strong> working now</span>
  <span><strong>{{ worked_earlier }}</strong> worked earlier</span>
  <span><strong>{{ needs_attention }}</strong> need attention</span>
</div>
{% if source_warnings %}
<aside class="pp-source-warnings" role="status">
  {% for warning in source_warnings %}<span>{{ warning }}</span>{% endfor %}
</aside>
{% endif %}
<div class="pp-axis" aria-hidden="true">
  <span class="pp-axis-spacer"></span>
  <div class="pp-axis-track">
    {% for tick in axis_labels %}<span style="left:{{ tick.left_pct }}%">{{ tick.label }}</span>{% endfor %}
  </div>
  <span class="pp-axis-summary">Summary</span>
</div>
<div id="people-performance-rows">
{% for section in sections %}
  <section class="pp-section" data-section="{{ section.key }}">
    <h2>{{ section.label }} <span>{{ section.rows|length }}</span></h2>
    {% if not section.rows %}<p class="pp-empty">No people in this group.</p>{% endif %}
    {% for row in section.rows %}
    <article class="pp-row{% if not row.is_active %} is-complete{% endif %}" data-person-id="{{ row.employee_odoo_id }}">
      <header class="pp-identity">
        <strong>{{ row.person_name }}</strong>
        <span>{{ row.status }}</span>
        {% for reason in row.attention_reasons %}<em>{{ reason }}</em>{% endfor %}
        {% if row.unattached_forklift_calls %}<em>{{ row.unattached_forklift_calls }} forklift call{% if row.unattached_forklift_calls != 1 %}s{% endif %} not placed because the Tablet location was not valid</em>{% endif %}
      </header>
      <div class="pp-timeline" aria-label="{{ row.person_name }} work timeline">
        {% for item in row.intervals %}
        <button type="button" class="pp-interval-trigger {{ item.location_class }} role-{{ item.role }} state-{{ item.state }}"
                style="left:{{ item.left_pct }}%;width:{{ item.width_pct }}%"
                data-interval-key="{{ item.key }}" data-detail="{{ item.detail|e }}"
                aria-label="{{ item.aria_label|e }}" aria-expanded="false">
          <span class="pp-location-ribbon">{{ item.location_name }}{% if item.is_transfer %}<b aria-hidden="true">→</b><span class="sr-only">Transfer</span>{% endif %}</span>
          <span class="pp-metric-track">
            {% if item.role == 'production' %}
              <span class="pp-result-text">{{ item.state|title }}</span>
              <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="Rolling 30-minute uptime">
              {% for run in item.line_runs %}<polyline points="{% for point in run %}{{ point.x }},{{ point.y }} {% endfor %}"></polyline>{% endfor %}
              </svg>
            {% elif item.role == 'forklift' %}
              {% if item.state == 'unavailable' %}<span class="pp-result-text">Unavailable</span>
              {% else %}
                {% for bucket in item.buckets %}<i class="pp-call-bar" style="left:{{ bucket.left_pct }}%;width:{{ bucket.width_pct }}%;height:{{ bucket.height_pct }}%"></i>{% endfor %}
                {% for bucket in item.buckets %}{% for marker in bucket.late_markers %}<i class="pp-late-marker" style="left:{{ marker }}%" aria-label="Late call"></i>{% endfor %}{% endfor %}
                <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="Rolling 30-minute on-time percentage">
                {% for run in item.line_runs %}<polyline points="{% for point in run %}{{ point.x }},{{ point.y }} {% endfor %}"></polyline>{% endfor %}
                </svg>
              {% endif %}
            {% else %}<span class="pp-neutral-label">No metered goal</span>{% endif %}
          </span>
        </button>
        {% endfor %}
        {% for item in row.breaks %}<span class="pp-break" style="left:{{ item.left_pct }}%;width:{{ item.width_pct }}%"><span class="sr-only">{{ item.label }}</span></span>{% endfor %}
      </div>
      <dl class="pp-summary">
        {% for label, value in row.summary %}<div><dt>{{ label }}</dt><dd>{{ value }}</dd></div>{% endfor %}
      </dl>
    </article>
    {% endfor %}
  </section>
{% endfor %}
</div>
</div>
```

The presenter must supply `line_runs` for production rolling uptime and forklift rolling on-time. The `pp-break` overlay uses `pointer-events:none`; the interval button remains the accessible detail target.

- [ ] **Step 8: Run focused route/render tests**

Run:

```bash
uv run pytest tests/test_people_performance_view.py tests/test_people_performance_route.py tests/test_people_performance_template.py tests/test_page_views.py -q
```

Expected: PASS.

- [ ] **Step 9: Add the patch note, commit, and push**

Add:

```markdown
### Add a people-first performance page

- **Managers now have a People tab ready to show one workday person by person.** It keeps production workers, tablet drivers, and other workers in separate easy-to-scan groups.
```

Run:

```bash
git add src/zira_dashboard/people_performance_view.py src/zira_dashboard/routes/people_performance.py src/zira_dashboard/app.py src/zira_dashboard/routes/README.md src/zira_dashboard/templates/_performance_subnav.html src/zira_dashboard/templates/_staffing_base.html src/zira_dashboard/templates/people_performance.html src/zira_dashboard/templates/_people_performance_rows.html tests/test_people_performance_view.py tests/test_people_performance_route.py tests/test_people_performance_template.py CHANGELOG.md
git commit -m "feat: add people performance page"
git push origin main
```

### Task 7: Style the desktop/tablet timeline and preserve interaction state during refresh

**Files:**
- Create: `src/zira_dashboard/static/people-performance.css`
- Create: `src/zira_dashboard/static/people-performance.js`
- Create: `tests/test_people_performance_static.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 6 semantic classes/data attributes and the partial route.
- Produces: `createPeoplePerformanceController(document, window)` with `init()`, `refreshRows()`, and `destroy()`; no framework dependency.

- [ ] **Step 1: Write failing CSS and interaction-contract tests**

Create `tests/test_people_performance_static.py`:

```python
from pathlib import Path
import json
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "src/zira_dashboard/static/people-performance.css").read_text()
SCRIPT = ROOT / "src/zira_dashboard/static/people-performance.js"


def test_color_shape_and_tablet_contracts_are_present():
    assert "--pp-ahead: #15803d" in CSS
    assert "--pp-behind: #c2413b" in CSS
    assert ".state-ahead" in CSS and ".state-behind" in CSS
    assert ".pp-result-text" in CSS
    assert re.search(r"\.pp-break\s*\{[^}]*repeating-linear-gradient", CSS)
    assert ".pp-interval-trigger:focus-visible" in CSS
    assert "@media (max-width: 1100px)" in CSS
    assert "minmax(30rem, 1fr)" in CSS


def test_refresh_and_details_preserve_state():
    source = SCRIPT.read_text()
    for token in (
        "people-performance/rows", "window.scrollY", "data-interval-key",
        "document.activeElement", "aria-expanded", "Escape",
        "document.visibilityState", "30000",
    ):
        assert token in source
```

Add a Node harness, following `tests/test_worker_stint_popover_static.py`, that supplies two `.pp-interval-trigger` objects and verifies:

1. pointer hover opens unpinned details;
2. focus opens the same details;
3. click pins them;
4. outside pointer closes;
5. Escape closes and restores focus only for pinned details;
6. a refresh replacement finds the same `data-interval-key`, restores focus and pin, and restores `window.scrollY`;
7. when the key disappeared, refresh closes details without moving focus to an unrelated interval.

- [ ] **Step 2: Run static tests and verify they fail**

Run: `uv run pytest tests/test_people_performance_static.py -q`

Expected: FAIL because the CSS and JavaScript do not exist.

- [ ] **Step 3: Add the complete responsive visual system**

Create `people-performance.css` with these base rules, retaining the exact selectors and values tested above:

```css
.pp-page {
  --pp-ahead: #15803d;
  --pp-behind: #c2413b;
  --pp-calls: #2563eb;
  --pp-neutral: #64748b;
  --pp-warning: #b45309;
  min-width: 0;
  color: var(--fg);
}
.pp-toolbar { display:flex; align-items:flex-end; justify-content:space-between; gap:1rem; margin-bottom:.75rem; }
.pp-eyebrow { margin:0 0 .2rem; color:var(--muted); font-size:.75rem; font-weight:800; letter-spacing:.08em; text-transform:uppercase; }
.pp-toolbar h1 { margin:0; font-size:1.55rem; }
.pp-updated { margin:.2rem 0 0; color:var(--muted); font-size:.78rem; }
.pp-controls { display:flex; flex-wrap:wrap; align-items:flex-end; gap:.65rem; }
.pp-controls label { display:grid; gap:.2rem; color:var(--muted); font-size:.75rem; }
.pp-controls input[type="date"] { min-height:2.3rem; padding:.35rem .55rem; border:1px solid var(--border); border-radius:8px; background:var(--panel); color:var(--fg); }
.pp-controls .pp-check { display:flex; align-items:center; gap:.35rem; min-height:2.3rem; }
.pp-controls button, .pp-controls a { min-height:2.3rem; display:inline-flex; align-items:center; padding:.35rem .75rem; border:1px solid var(--border); border-radius:8px; background:var(--panel); color:var(--fg); text-decoration:none; }
.pp-counts { display:flex; flex-wrap:wrap; gap:.55rem; margin-bottom:.65rem; }
.pp-counts span { padding:.38rem .65rem; border:1px solid var(--border); border-radius:999px; background:var(--panel); color:var(--muted); font-size:.78rem; }
.pp-counts strong { color:var(--fg); }
.pp-source-warnings { display:flex; flex-wrap:wrap; gap:.4rem; margin-bottom:.65rem; }
.pp-source-warnings span, .pp-identity em { color:#7c2d12; background:#ffedd5; border:1px solid #fdba74; border-radius:999px; font-style:normal; }
.pp-source-warnings span { padding:.35rem .65rem; font-size:.77rem; }
.pp-axis, .pp-row { display:grid; grid-template-columns:minmax(10.5rem,.85fr) minmax(34rem,4fr) minmax(16rem,1.35fr); gap:.75rem; }
.pp-axis { position:sticky; top:0; z-index:20; padding:.4rem .75rem; border:1px solid var(--border); background:color-mix(in srgb, var(--panel) 96%, transparent); backdrop-filter:blur(8px); }
.pp-axis-track { position:relative; min-height:1.2rem; }
.pp-axis-track span { position:absolute; transform:translateX(-50%); color:var(--muted); font-size:.66rem; white-space:nowrap; }
.pp-axis-track span:first-child { transform:none; }
.pp-axis-track span:last-child { transform:translateX(-100%); }
.pp-axis-summary { color:var(--muted); font-size:.7rem; font-weight:700; text-transform:uppercase; }
.pp-section { margin-top:.8rem; }
.pp-section > h2 { display:flex; align-items:center; gap:.45rem; margin:0; padding:.5rem .65rem; border-radius:8px 8px 0 0; background:color-mix(in srgb, var(--panel) 86%, var(--accent) 14%); }
.pp-section > h2 span { color:var(--muted); font-size:.75rem; }
.pp-empty { margin:0; padding:.9rem; border:1px solid var(--border); background:var(--panel); color:var(--muted); }
.pp-row { min-height:5.6rem; padding:.65rem .75rem; border:1px solid var(--border); border-top:0; background:var(--panel); align-items:stretch; }
.pp-row.is-complete { background:color-mix(in srgb, var(--panel) 91%, var(--bg)); }
.pp-identity { display:flex; min-width:0; flex-direction:column; justify-content:center; gap:.18rem; }
.pp-identity strong { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.pp-identity > span { color:var(--muted); font-size:.76rem; }
.pp-identity em { align-self:flex-start; max-width:100%; padding:.15rem .42rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:.66rem; }
.pp-timeline { position:relative; min-width:0; min-height:4.3rem; overflow:hidden; border:1px solid var(--border); border-radius:8px; background:repeating-linear-gradient(90deg, transparent 0, transparent calc(12.5% - 1px), color-mix(in srgb, var(--border) 55%, transparent) calc(12.5% - 1px), color-mix(in srgb, var(--border) 55%, transparent) 12.5%); }
.pp-interval-trigger { position:absolute; top:0; bottom:0; min-width:2px; margin:0; padding:0; overflow:hidden; border:0; border-right:1px solid color-mix(in srgb, var(--fg) 28%, transparent); background:transparent; color:inherit; cursor:pointer; }
.pp-interval-trigger:focus-visible { z-index:12; outline:3px solid var(--accent); outline-offset:-3px; }
.pp-location-ribbon { position:absolute; inset:0 0 auto; height:1.35rem; padding:.16rem .3rem; overflow:hidden; color:#0f172a; font-size:.66rem; font-weight:800; text-overflow:ellipsis; white-space:nowrap; }
.pp-location-ribbon b { float:right; font-size:.9rem; }
.location-1 .pp-location-ribbon { background:#93c5fd; }
.location-2 .pp-location-ribbon { background:#c4b5fd; }
.location-3 .pp-location-ribbon { background:#67e8f9; }
.location-4 .pp-location-ribbon { background:#f0abfc; }
.location-5 .pp-location-ribbon { background:#fde68a; }
.location-6 .pp-location-ribbon { background:#a5b4fc; }
.location-7 .pp-location-ribbon { background:#99f6e4; }
.location-8 .pp-location-ribbon { background:#fed7aa; }
.pp-metric-track { position:absolute; inset:1.35rem 0 0; display:block; background:color-mix(in srgb, var(--pp-neutral) 20%, transparent); }
.state-ahead .pp-metric-track { background:color-mix(in srgb, var(--pp-ahead) 40%, transparent); }
.state-behind .pp-metric-track { background:color-mix(in srgb, var(--pp-behind) 42%, transparent); }
.state-unavailable .pp-metric-track { background:repeating-linear-gradient(135deg, rgba(100,116,139,.22) 0 5px, rgba(148,163,184,.08) 5px 10px); }
.pp-result-text, .pp-neutral-label { position:absolute; z-index:3; left:.3rem; bottom:.18rem; font-size:.62rem; font-weight:800; }
.state-ahead .pp-result-text { color:#052e16; }
.state-behind .pp-result-text { color:#450a0a; }
.pp-metric-track svg { position:absolute; z-index:4; inset:0; width:100%; height:100%; overflow:visible; pointer-events:none; }
.pp-metric-track polyline { fill:none; stroke:#111827; stroke-width:3; vector-effect:non-scaling-stroke; }
.role-forklift .pp-metric-track { background:color-mix(in srgb, var(--pp-calls) 12%, transparent); }
.pp-call-bar { position:absolute; bottom:0; min-height:2px; background:var(--pp-calls); opacity:.76; }
.pp-late-marker { position:absolute; z-index:5; top:0; bottom:0; width:3px; background:var(--pp-behind); }
.pp-break { position:absolute; z-index:10; top:0; bottom:0; pointer-events:none; background:repeating-linear-gradient(135deg, rgba(71,85,105,.34) 0 4px, rgba(255,255,255,.12) 4px 8px); border-inline:1px dashed var(--muted); }
.pp-summary { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:.35rem .6rem; margin:0; align-content:center; }
.pp-summary div { min-width:0; }
.pp-summary dt { color:var(--muted); font-size:.64rem; font-weight:700; text-transform:uppercase; }
.pp-summary dd { margin:.08rem 0 0; overflow:hidden; font-size:.82rem; font-weight:800; text-overflow:ellipsis; white-space:nowrap; }
.pp-detail-popover { position:absolute; z-index:1000; max-width:min(23rem, calc(100vw - 1rem)); padding:.55rem .7rem; border:1px solid var(--border); border-radius:8px; background:#111827; color:#fff; box-shadow:0 10px 30px rgba(15,23,42,.28); font-size:.76rem; line-height:1.4; }
.pp-detail-popover[hidden] { display:none; }
.sr-only { position:absolute!important; width:1px!important; height:1px!important; padding:0!important; margin:-1px!important; overflow:hidden!important; clip:rect(0,0,0,0)!important; white-space:nowrap!important; border:0!important; }
@media (max-width: 1100px) {
  .pp-toolbar { align-items:flex-start; flex-direction:column; }
  .pp-axis, .pp-row { grid-template-columns:9rem minmax(30rem, 1fr); }
  .pp-axis-summary { display:none; }
  .pp-summary { grid-column:2; grid-template-columns:repeat(4, minmax(0,1fr)); padding-top:.35rem; }
  .pp-page { overflow-x:auto; }
}
@media (max-width: 760px) {
  main { padding:.7rem; }
  .pp-axis, .pp-row { grid-template-columns:8rem minmax(30rem, 1fr); }
  .pp-summary { grid-template-columns:repeat(2, minmax(0,1fr)); }
}
```

- [ ] **Step 4: Implement one controller for details and state-safe refresh**

Create `people-performance.js` as a CommonJS-testable factory wrapped for the browser:

```javascript
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory;
  else root.createPeoplePerformanceController = factory;
}(typeof window !== 'undefined' ? window : this, function (document, window) {
  'use strict';
  var triggerSelector = '.pp-interval-trigger';
  var pinned = null;
  var active = null;
  var timer = null;
  var popover = null;

  function triggerFor(node) {
    return node && node.closest ? node.closest(triggerSelector) : null;
  }
  function ensurePopover() {
    if (popover) return popover;
    popover = document.createElement('div');
    popover.id = 'pp-detail-popover';
    popover.className = 'pp-detail-popover';
    popover.setAttribute('role', 'tooltip');
    popover.hidden = true;
    document.body.appendChild(popover);
    return popover;
  }
  function position(trigger) {
    var box = trigger.getBoundingClientRect();
    var tip = ensurePopover();
    var left = Math.max(8, Math.min(box.left + window.scrollX,
      window.scrollX + window.innerWidth - tip.getBoundingClientRect().width - 8));
    tip.style.left = left + 'px';
    tip.style.top = (box.bottom + window.scrollY + 6) + 'px';
  }
  function open(trigger, pin) {
    if (!trigger) return;
    if (active && active !== trigger) active.setAttribute('aria-expanded', 'false');
    active = trigger;
    if (pin) pinned = trigger;
    var tip = ensurePopover();
    tip.textContent = trigger.dataset.detail || trigger.getAttribute('aria-label') || '';
    tip.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    trigger.setAttribute('aria-describedby', tip.id);
    position(trigger);
  }
  function close(restoreFocus) {
    var oldPinned = pinned;
    if (active) {
      active.setAttribute('aria-expanded', 'false');
      active.removeAttribute('aria-describedby');
    }
    active = null;
    pinned = null;
    if (popover) popover.hidden = true;
    if (restoreFocus && oldPinned && oldPinned.focus) oldPinned.focus();
  }
  function captureState() {
    var focused = triggerFor(document.activeElement);
    return {
      scrollY: window.scrollY,
      focusKey: focused && focused.dataset.intervalKey,
      pinnedKey: pinned && pinned.dataset.intervalKey,
    };
  }
  function escapeSelector(value) {
    return String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  }
  function restoreState(state) {
    window.scrollTo(window.scrollX, state.scrollY);
    if (state.focusKey) {
      var focusTarget = document.querySelector(
        '[data-interval-key="' + escapeSelector(state.focusKey) + '"]'
      );
      if (focusTarget) focusTarget.focus();
    }
    if (state.pinnedKey) {
      var pinTarget = document.querySelector(
        '[data-interval-key="' + escapeSelector(state.pinnedKey) + '"]'
      );
      if (pinTarget) open(pinTarget, true);
      else close(false);
    }
  }
  function refreshRows() {
    var rows = document.getElementById('people-performance-live');
    if (!rows || document.visibilityState === 'hidden') return Promise.resolve(false);
    var state = captureState();
    var url = '/people-performance/rows?day=' + encodeURIComponent(rows.dataset.day) +
      '&attention=' + encodeURIComponent(rows.dataset.attention || '0');
    var fetcher = window.gpiFetch || window.fetch.bind(window);
    return fetcher(url, {cache: 'no-store'}).then(function (response) {
      if (!response.ok) throw new Error('refresh failed');
      return response.text();
    }).then(function (html) {
      var parsed = new window.DOMParser().parseFromString(html, 'text/html');
      var replacement = parsed.getElementById('people-performance-live');
      if (!replacement) throw new Error('rows missing');
      rows.replaceWith(replacement);
      restoreState(state);
      var status = document.getElementById('pp-live-status');
      if (status) status.textContent = 'Updated just now';
      return true;
    }).catch(function () {
      var status = document.getElementById('pp-live-status');
      if (status) status.textContent = 'Update paused — showing the last good view';
      return false;
    });
  }
  function scheduleRefresh() {
    var page = document.querySelector('.pp-page[data-today="1"]');
    if (!page) return;
    timer = window.setInterval(refreshRows, 30000);
  }
  function init() {
    ensurePopover();
    document.addEventListener('pointerover', function (event) {
      var trigger = triggerFor(event.target);
      if (trigger && !pinned) open(trigger, false);
    });
    document.addEventListener('pointerout', function (event) {
      var trigger = triggerFor(event.target);
      if (trigger && !pinned && !trigger.contains(event.relatedTarget)) close(false);
    });
    document.addEventListener('focusin', function (event) {
      var trigger = triggerFor(event.target);
      if (trigger && !pinned) open(trigger, false);
    });
    document.addEventListener('focusout', function (event) {
      var trigger = triggerFor(event.target);
      if (trigger && !pinned && !trigger.contains(event.relatedTarget)) close(false);
    });
    document.addEventListener('click', function (event) {
      var trigger = triggerFor(event.target);
      if (trigger) {
        event.preventDefault();
        open(trigger, true);
      }
    });
    document.addEventListener('pointerdown', function (event) {
      if (pinned && !pinned.contains(event.target) &&
          (!popover || !popover.contains(event.target))) close(false);
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && active) close(Boolean(pinned));
    });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') refreshRows();
    });
    scheduleRefresh();
  }
  function destroy() {
    if (timer) window.clearInterval(timer);
    timer = null;
    close(false);
  }
  return {init: init, refreshRows: refreshRows, destroy: destroy};
}));

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  window.createPeoplePerformanceController(document, window).init();
}
```

- [ ] **Step 5: Run interaction tests and route regressions**

Run:

```bash
uv run pytest tests/test_people_performance_static.py tests/test_people_performance_template.py tests/test_people_performance_route.py -q
```

Expected: PASS.

- [ ] **Step 6: Add the patch note, commit, and push**

Add:

```markdown
### Keep the People page easy to scan

- **The People page now fits manager computers and tablets, explains each part when selected, and refreshes without losing your place.** Breaks, moves, late calls, and good or low results all have words as well as colors.
```

Run:

```bash
git add src/zira_dashboard/static/people-performance.css src/zira_dashboard/static/people-performance.js tests/test_people_performance_static.py CHANGELOG.md
git commit -m "feat: finish people dashboard interaction"
git push origin main
```

### Task 8: Add a realistic preview and complete the cross-source validation gate

**Files:**
- Create: `scripts/preview_people_performance.py`
- Create: `tests/test_preview_people_performance.py`
- Create: `tests/test_people_performance_end_to_end.py`
- Modify: `docs/superpowers/specs/2026-08-28-people-performance-dashboard-design.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: the real route, templates, CSS, JavaScript, and all production data seams.
- Produces: static busy-day preview at `scripts/_preview_out/people_performance/index.html`; no production runtime interface.

- [ ] **Step 1: Write the failing preview and end-to-end tests**

Create `tests/test_preview_people_performance.py`:

```python
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "scripts/_preview_out/people_performance"


def _render_preview():
    env = os.environ | {
        "AUTH_DISABLED": "1", "ZIRA_API_KEY": "test",
        "PYTHONPATH": str(ROOT / "src"),
    }
    return subprocess.run(
        [sys.executable, "scripts/preview_people_performance.py"],
        cwd=ROOT, env=env, check=True, capture_output=True, text=True,
    )


def test_preview_contains_busy_people_fixture():
    result = _render_preview()
    assert result.stdout.strip() == str(OUT)
    html = (OUT / "index.html").read_text(encoding="utf-8")
    assert html.count('class="pp-row') >= 10
    assert "Metered production" in html
    assert "Tablet forklift" in html
    assert "Other non-metered people" in html
    assert "location missing" in html
    assert "source stale" in html
    assert "Late call" in html
    assert "Planned break" in html


def test_preview_fits_desktop_and_tablet_and_keeps_sticky_time_context():
    _render_preview()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for width, height in ((1440, 900), (1024, 768)):
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto((OUT / "index.html").as_uri(), wait_until="load")
                before = page.locator(".pp-axis").bounding_box()
                page.evaluate("window.scrollTo(0, 900)")
                after = page.locator(".pp-axis").bounding_box()
                assert before and after
                assert after["y"] >= 0
                assert page.locator(".pp-timeline").first.bounding_box()["width"] >= 480
                assert page.evaluate("document.documentElement.scrollWidth") <= width
                page.close()
        finally:
            browser.close()


def test_preview_details_work_with_keyboard_pointer_and_escape():
    _render_preview()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto((OUT / "index.html").as_uri(), wait_until="load")
            trigger = page.locator(".pp-interval-trigger").first
            trigger.focus()
            assert page.locator("#pp-detail-popover").is_visible()
            trigger.click()
            assert trigger.get_attribute("aria-expanded") == "true"
            page.keyboard.press("Escape")
            assert not page.locator("#pp-detail-popover").is_visible()
        finally:
            browser.close()
```

Create `tests/test_people_performance_end_to_end.py` with a full route test that monkeypatches the canonical timeline, station totals, raw forklift events, daily driver row, score config, and readiness report. It must assert:

```python
from datetime import timedelta

import pytest

from tests.people_performance_fixtures import START, event, score, span
from tests.test_people_performance_data import install_sources
from zira_dashboard import people_performance_data
from zira_dashboard.stations import Station
from zira_dashboard.leaderboard import StationTotal


@pytest.fixture
def installed_sources(monkeypatch):
    spans = (
        span(44, "Alex Worker", 0, 60, "Repair 1"),
        span(44, "Alex Worker", 60, 300, "Tablets"),
    )
    driver_row = {
        "driver_id": "driver-Alex", "name": "Alex", "calls": 1,
        "on_time": 0, "late": 1, "on_call_ms": 120000,
        "avg_ms": 60000, "utilization_pct": 40,
    }
    install_sources(
        monkeypatch, spans=spans,
        events=(event("Alex", 90, late=True),),
        driver_rows=(driver_row,), resolved={"Alex": "Alex Worker"},
    )
    total = StationTotal(
        station=Station("m1", "Repair 1", "Repair", "Recycling"),
        units=10, reading_count=1, truncated=False, downtime_minutes=0,
        active_minutes=60, last_reading_at=START, last_status="Working",
        samples=((START + timedelta(minutes=30), 10),),
        active_intervals=((START, START + timedelta(minutes=60)),),
        downtime_intervals=(),
    )
    monkeypatch.setattr(
        people_performance_data.production_history,
        "metered_station_totals", lambda *args, **kwargs: [total],
    )
    monkeypatch.setattr(
        people_performance_data.production_history,
        "production_scores_for_timeline",
        lambda *args, **kwargs: (
            score(44, "Alex Worker", "Repair 1", 0, 60, 10, 20),
        ),
    )


def test_people_dashboard_cross_source_day(client, installed_sources):
    response = client.get("/people-performance?day=2026-08-28")
    assert response.status_code == 200
    html = response.text
    assert html.count('data-person-id="44"') == 1
    assert "Repair 1" in html and "Tablets" in html
    assert "Transferred to Tablets" in html
    assert "Behind" in html
    assert "Late call" in html
    assert "goal" in html.lower() and "uptime" in html.lower()


def test_each_source_degrades_without_false_zero(client, installed_sources, monkeypatch):
    monkeypatch.setattr(
        people_performance_data.production_history,
        "metered_station_totals",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    response = client.get("/people-performance?day=2026-08-28")
    assert response.status_code == 200
    assert "Production data unavailable" in response.text
    assert ">N/A<" in response.text
    assert ">0%<" not in response.text


def test_attendance_failure_never_calls_schedule(client, installed_sources, monkeypatch):
    called = {"schedule": False}
    monkeypatch.setattr(
        people_performance_data.attendance_timeline, "timeline_for_range",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(
        "zira_dashboard.staffing.load_schedule",
        lambda *args, **kwargs: called.update(schedule=True),
    )
    response = client.get("/people-performance?day=2026-08-28")
    assert response.status_code == 200
    assert "Attendance data unavailable" in response.text
    assert called["schedule"] is False


def test_forklift_failure_keeps_production(client, installed_sources, monkeypatch):
    monkeypatch.setattr(
        people_performance_data.forklift_event_store,
        "completion_events_for_range",
        lambda *args: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    response = client.get("/people-performance?day=2026-08-28")
    assert response.status_code == 200
    assert "Forklift data unavailable" in response.text
    assert "Repair 1" in response.text and "Behind" in response.text
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
uv run pytest tests/test_preview_people_performance.py tests/test_people_performance_end_to_end.py -q
```

Expected: FAIL because the preview script and installed-source fixtures are absent.

- [ ] **Step 3: Implement the deterministic preview renderer**

Create `scripts/preview_people_performance.py` following the existing preview scripts. It must:

```python
from __future__ import annotations

import os
from pathlib import Path
import shutil
from unittest.mock import patch

os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("SESSION_SECRET", "preview-secret-32-bytes-of-data")
os.environ.setdefault("ZIRA_API_KEY", "preview-dummy")

from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard.deps import templates
from zira_dashboard.routes import people_performance


OUT = Path("scripts/_preview_out/people_performance")
STATIC = Path("src/zira_dashboard/static")
```

Build a `_context()` dictionary with date `2026-08-28`, eight hourly axis labels, three fixed sections, and at least ten rows:

- four production rows: active behind with 35-minute downtime, active ahead, a three-transfer mixed-role row with one five-minute interval, and a completed row;
- three forklift rows: high call volume with one late marker, low on-time needing attention, and a completed driver;
- three other rows: active neutral, location missing, and source stale;
- the same 11:30-12:00 `Planned break` overlay on every row;
- one production `line_runs` series that starts at 100, dips to 40, and recovers;
- one forklift `line_runs` series with a gap between classified-call windows;
- four summary values on every row.

Use helper functions with exact output keys already consumed by `_people_performance_rows.html`:

```python
def _interval(key, left, width, location, role, state="neutral",
              transfer=False, detail="", line_runs=(), buckets=()):
    return {
        "key": key, "left_pct": left, "width_pct": width,
        "location_name": location,
        "location_class": "location-" + str((sum(map(ord, location)) % 8) + 1),
        "role": role, "state": state, "is_transfer": transfer,
        "line_runs": line_runs, "buckets": buckets,
        "detail": detail,
        "aria_label": ("Transferred to " if transfer else "") + detail,
    }


def _row(person_id, name, status, role, intervals, summary,
         reasons=(), active=True):
    return {
        "employee_odoo_id": person_id, "person_name": name,
        "is_active": active, "status": status, "primary_role": role,
        "attention_reasons": reasons, "intervals": intervals,
        "breaks": [{"left_pct": 50.0, "width_pct": 6.25,
                    "label": "Planned break"}],
        "summary": summary, "unattached_forklift_calls": 0,
    }
```

Patch `people_performance._context` to return that dictionary plus `active="people"`, `active_dashboard_key="people"`, and `today="2026-08-28"`. Patch `templates.env.globals["nav_inbox_summary"]` to the existing empty summary. Request `/people-performance?day=2026-08-28`, rewrite `/static/` references to `static/`, write `index.html`, copy the real static directory to `OUT/static`, and print `OUT.resolve()`.

- [ ] **Step 4: Finish the installed-source fixtures and run the full focused matrix**

Implement the end-to-end fixtures using immutable values from Tasks 1-4. Do not patch `load_dashboard()` in this test: the purpose is to exercise source reconciliation, scoring, presentation, routing, and template rendering together.

Run:

```bash
uv run pytest \
  tests/test_forklift_ingest.py \
  tests/test_forklift_event_store.py \
  tests/test_forklift_snapshot.py \
  tests/test_forklift_backfill.py \
  tests/test_leaderboard_downtime.py \
  tests/test_production_segments.py \
  tests/test_people_performance_production.py \
  tests/test_people_performance_forklift.py \
  tests/test_people_performance_rows.py \
  tests/test_people_performance_data.py \
  tests/test_people_performance_view.py \
  tests/test_people_performance_route.py \
  tests/test_people_performance_template.py \
  tests/test_people_performance_static.py \
  tests/test_people_performance_end_to_end.py \
  tests/test_preview_people_performance.py -q
```

Expected: PASS.

- [ ] **Step 5: Run repository-wide verification**

Run:

```bash
uv run ruff check .
uv run pytest -q
uv run python scripts/preview_people_performance.py
git diff --check
```

Expected:

- Ruff exits 0.
- The full pytest suite passes with no new failure.
- The preview command prints the absolute `scripts/_preview_out/people_performance` path.
- `git diff --check` prints nothing and exits 0.

- [ ] **Step 6: Record validation in the design and inspect generated pages**

Append this section to the design document:

```markdown
## Implementation validation

- Pure interval, production, downtime, forklift, mixed-role, sorting, and source-failure tests pass.
- Route, partial-render, accessibility, refresh-state, and full cross-source tests pass.
- The busy fixture passes automated geometry and interaction checks at 1440×900 desktop and 1024×768 tablet sizes.
- The full repository test suite and Ruff pass before release.
```

Open `scripts/_preview_out/people_performance/index.html` at both target sizes and check these exact outcomes: fixed section order, legible names and summaries, accurate short-span geometry, visible transfer arrow, no line through lunch/no-data, late-call marker, readable completed rows, sticky time axis while scrolling, and no horizontal document overflow.

- [ ] **Step 7: Add the release patch note, commit, and push**

Add:

```markdown
### See each person's workday in one place

- **Managers can now open People under Performance to see who worked, where they moved, whether each production goal was met, machine stops, and forklift calls.** People without a measured goal stay in a separate group below the measured workers.
```

Run:

```bash
git add scripts/preview_people_performance.py tests/test_preview_people_performance.py tests/test_people_performance_end_to_end.py docs/superpowers/specs/2026-08-28-people-performance-dashboard-design.md CHANGELOG.md
git commit -m "test: validate people performance dashboard"
git push origin main
```
