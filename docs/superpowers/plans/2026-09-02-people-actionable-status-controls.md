# People Actionable Status Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the People page totals filter the workforce and make every source warning explain its impact and offer the next safe action, including an audited workflow for unresolved forklift identities.

**Architecture:** Keep the People page server-rendered and URL-driven. Replace display-only warning strings with typed domain records, render compact warning summaries in the live rows partial, and lazy-load a server-rendered anchored panel by stable warning key. Add a focused, transactional external-driver-to-Odoo-employee mapping store and Settings subsection; the People controller coordinates filters, panel state, safe refreshes, and polling without moving business rules into JavaScript.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, PostgreSQL/psycopg2, vanilla JavaScript and CSS, pytest, Node runtime harnesses, and Playwright.

## Global Constraints

- Preserve the existing compact two-band People manager strip: totals and controls first, wrapping warnings below.
- `status=working` and `status=earlier` are mutually exclusive; `attention=1` is independent and may combine with either.
- Day totals always describe the complete selected day, even when rows are filtered.
- Warning labels are presentation text only; neither templates nor JavaScript may parse them to discover type or actions.
- Hover is supplemental. Every warning detail and action must work with click, touch, focus, Enter, Space, and Escape.
- **Check again** refreshes truthful source data; it never dismisses or force-clears a warning.
- Identity assignments require an explicit active employee, server-side conflict checks, an authenticated actor, and append-only audit history.
- Never expose raw source payloads, exception text, secrets, or a list of every unmatched call in warning details.
- Preserve existing attention rules, attendance truth, production calculations, forklift scoring, person-row content, timelines, and section ordering.
- Every implementation push to `main` must add a short, child-readable `CHANGELOG.md` entry describing what changed and how it helps.

---

## File Structure

### New files

- `src/zira_dashboard/people_performance_warnings.py` — typed warning/action records, opaque stable keys, and safe warning builders.
- `src/zira_dashboard/forklift_identity_store.py` — transactional one-to-one driver mappings and append-only audits.
- `src/zira_dashboard/forklift_identity_view.py` — manager-safe unresolved-identity and mapping presentation data for one plant day.
- `src/zira_dashboard/routes/forklift_identities.py` — authenticated save/remove endpoints for the Settings workflow.
- `src/zira_dashboard/templates/_people_performance_warning_panel.html` — server-rendered loaded, resolved, and failed-safe panel content.
- `src/zira_dashboard/templates/_settings_forklift_identities.html` — focused unresolved/current identity review UI.
- `tests/test_people_performance_warnings.py` — warning type, key, copy, capability, and redaction tests.
- `tests/test_forklift_identity_store.py` — schema-independent unit tests plus database-gated transactional round trips.
- `tests/test_forklift_identity_settings.py` — Settings context, route validation, audit-actor, and template tests.

### Existing files to modify

- `src/zira_dashboard/_schema.py` — mapping and audit tables with one-to-one constraints.
- `src/zira_dashboard/app.py` — register the focused forklift-identity route module.
- `src/zira_dashboard/forklift_store.py` — prefer explicit ID mappings before conservative name inference.
- `src/zira_dashboard/people_performance.py` — type `DashboardModel.source_warnings` as structured warnings and deduplicate by key.
- `src/zira_dashboard/people_performance_data.py` — classify source failures and aggregate safe diagnostic facts.
- `src/zira_dashboard/people_performance_view.py` — apply status/attention filters and build warning summary/detail views.
- `src/zira_dashboard/routes/people_performance.py` — validate status filters and serve warning-detail partials.
- `src/zira_dashboard/routes/settings.py` — load the focused identity context only for the Forklift Settings section.
- `src/zira_dashboard/templates/people_performance.html` — host the persistent warning panel outside the polled partial.
- `src/zira_dashboard/templates/_people_performance_rows.html` — count buttons, filter summary, structured warning buttons, and no duplicate checkbox.
- `src/zira_dashboard/templates/settings.html` — include the identity subsection inside Forklift Settings.
- `src/zira_dashboard/static/people-performance.js` — URL filters, warning preview/pin/load/check-again behavior, and polling restoration.
- `src/zira_dashboard/static/people-performance.css` — selected/disabled controls and responsive anchored-panel styling.
- `src/zira_dashboard/static/settings.css` — identity table/cards, conflict/error, and narrow-screen styles.
- `scripts/preview_people_performance.py` — structured warning fixture and deterministic warning-detail response data.
- `tests/people_performance_fixtures.py` — typed warning fixtures.
- `tests/test_people_performance_data.py` — reason/fact/action assertions for each source condition.
- `tests/test_people_performance_rows.py` — structured-warning assembly and deduplication assertions.
- `tests/test_people_performance_view.py` — filter combinations, full-day totals, and result summaries.
- `tests/test_people_performance_route.py` — query validation, detail endpoint, cache, auth, and stale-key contracts.
- `tests/test_people_performance_template.py` — semantic buttons, pressed/expanded state, and removed checkbox.
- `tests/test_people_performance_static.py` — Node interaction/race/state harness.
- `tests/test_preview_people_performance.py` — five-width geometry, keyboard, pointer, panel, and overflow checks.
- `tests/test_settings_forklift.py` — identity subsection inclusion without regressing the demand advisor.
- `CHANGELOG.md` — plain-language implementation note in the final task.

---

### Task 1: Replace Warning Strings With a Typed Domain Contract

**Files:**
- Create: `src/zira_dashboard/people_performance_warnings.py`
- Create: `tests/test_people_performance_warnings.py`
- Modify: `src/zira_dashboard/people_performance.py:13-20,111-119,1110-1230`
- Modify: `src/zira_dashboard/people_performance_data.py:110-410,430-560`
- Modify: `src/zira_dashboard/templates/_people_performance_rows.html:31-34`
- Modify: `tests/people_performance_fixtures.py:145-165`
- Modify: `tests/test_people_performance_data.py`
- Modify: `tests/test_people_performance_rows.py`
- Modify: `tests/test_people_performance_template.py`

**Interfaces:**
- Produces: `WarningAction`, `DashboardWarning`, `warning_key()`, and the eight builder functions listed below.
- Produces: `DashboardModel.source_warnings: tuple[DashboardWarning, ...]` with first-seen key deduplication.
- Consumes: existing source facts from `people_performance_data._ProductionSource` and `_ForkliftSource`.

- [ ] **Step 1: Write failing warning-contract tests**

Create `tests/test_people_performance_warnings.py` with concrete tests for stable opaque keys, action capabilities, safe facts, and timestamps:

```python
from datetime import UTC, date, datetime

from zira_dashboard.people_performance_warnings import (
    production_metric_warning,
    unmatched_forklift_warning,
    warning_key,
)


CHECKED = datetime(2026, 9, 2, 14, 30, tzinfo=UTC)
DAY = date(2026, 9, 2)


def test_warning_key_is_stable_opaque_and_subject_specific():
    first = warning_key("production_metric_unavailable", "Trim Saw 1")
    assert first == warning_key("production_metric_unavailable", "Trim Saw 1")
    assert first != warning_key("production_metric_unavailable", "Hand Build #1")
    assert len(first) == 24
    assert "Trim" not in first


def test_missing_goal_warning_exposes_only_relevant_actions():
    warning = production_metric_warning(
        station_name="Trim Saw 1",
        reason_code="missing_goal",
        checked_at_utc=CHECKED,
        day=DAY,
    )
    assert warning.kind == "production_metric_unavailable"
    assert warning.label == "Production metric unavailable: Trim Saw 1"
    assert [action.action_id for action in warning.actions] == [
        "check_again", "open_work_center", "review_settings"
    ]
    assert warning.actions[1].href == "/wc/trim-saw-1?day=2026-09-02"


def test_unmatched_warning_aggregates_identities_without_raw_events():
    warning = unmatched_forklift_warning(
        call_count=135,
        identities=(("driver-7", ("Sam",), 130), ("driver-8", ("Alex", "A."), 5)),
        first_call_utc=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        last_call_utc=datetime(2026, 9, 2, 14, 0, tzinfo=UTC),
        checked_at_utc=CHECKED,
        last_success_at_utc=CHECKED,
        day=DAY,
    )
    assert warning.label == "Unmatched forklift calls: 135"
    assert ("Distinct identities", "2") in warning.facts
    assert all("event" not in value.lower() for _, value in warning.facts)
    assert [action.action_id for action in warning.actions] == [
        "check_again", "review_identities"
    ]
```

- [ ] **Step 2: Run the new test to prove the contract is missing**

Run: `.venv/bin/pytest tests/test_people_performance_warnings.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'zira_dashboard.people_performance_warnings'`.

- [ ] **Step 3: Implement the warning records and builders**

Create `src/zira_dashboard/people_performance_warnings.py` with these exact public types and validation rules:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import blake2s
from typing import Literal, TypeAlias

from . import shift_config
from .wc_dashboard_data import dashboard_url_for_wc_day


WarningKind: TypeAlias = Literal[
    "production_metric_unavailable", "production_data_unavailable",
    "forklift_data_unavailable", "forklift_identity_conflict",
    "unmatched_forklift_calls", "forklift_timeline_incomplete",
    "attendance_source_stale", "attendance_data_unavailable",
]
WarningActionId: TypeAlias = Literal[
    "check_again", "open_work_center", "review_settings",
    "review_identities", "open_diagnostics",
]


@dataclass(frozen=True)
class WarningAction:
    action_id: WarningActionId
    label: str
    href: str | None = None


@dataclass(frozen=True)
class DashboardWarning:
    key: str
    kind: WarningKind
    label: str
    title: str
    summary: str
    source: str
    subject: str
    reason_code: str
    impact: str
    checked_at_utc: datetime
    last_success_at_utc: datetime | None = None
    facts: tuple[tuple[str, str], ...] = ()
    actions: tuple[WarningAction, ...] = ()


def warning_key(kind: WarningKind, subject: str) -> str:
    clean_subject = str(subject).strip()
    if not clean_subject:
        raise ValueError("warning subject is required")
    return blake2s(
        f"{kind}\0{clean_subject}".encode("utf-8"), digest_size=12
    ).hexdigest()


def _checked(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise TypeError("warning timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _action(action_id: WarningActionId, label: str, href: str | None = None):
    return WarningAction(action_id, label, href)


def _local_time(value: datetime) -> str:
    return _checked(value).astimezone(shift_config.SITE_TZ).strftime("%-I:%M %p")


def _make_warning(*, kind: WarningKind, label: str, title: str, summary: str,
                  source: str, subject: str, reason_code: str, impact: str,
                  checked_at_utc: datetime,
                  last_success_at_utc: datetime | None = None,
                  facts: tuple[tuple[str, str], ...] = (),
                  actions: tuple[WarningAction, ...] = ()) -> DashboardWarning:
    return DashboardWarning(
        key=warning_key(kind, subject), kind=kind, label=label, title=title,
        summary=summary, source=source, subject=subject,
        reason_code=reason_code, impact=impact,
        checked_at_utc=_checked(checked_at_utc),
        last_success_at_utc=(
            _checked(last_success_at_utc) if last_success_at_utc is not None else None
        ),
        facts=facts, actions=actions,
    )
```

Add these complete builders below the shared helpers:

```python
_PRODUCTION_REASON_COPY = {
    "missing_totals": "No production total was available for this work center.",
    "incomplete_data": "The latest production total is incomplete.",
    "duplicate_data": "More than one production total claimed this work center.",
    "missing_goal": "This work center does not have an active production goal.",
    "metric_mismatch": "The production total did not match this work center's meter.",
    "calculation_failure": "Plant Manager could not safely calculate this production result.",
}


def production_metric_warning(*, station_name: str, reason_code: str,
                              checked_at_utc: datetime, day: date) -> DashboardWarning:
    explanation = _PRODUCTION_REASON_COPY.get(reason_code)
    if explanation is None:
        raise ValueError("unknown production warning reason")
    check = _action("check_again", "Check again")
    center = _action(
        "open_work_center", "Open work center dashboard",
        dashboard_url_for_wc_day(station_name, day),
    )
    actions = (check, center)
    if reason_code == "missing_goal":
        actions += (_action(
            "review_settings", "Review settings", "/settings?section=work_centers"
        ),)
    return _make_warning(
        kind="production_metric_unavailable",
        label=f"Production metric unavailable: {station_name}",
        title=f"{station_name} production is unavailable",
        summary=explanation,
        source="production", subject=station_name, reason_code=reason_code,
        impact="Production, goal progress, uptime, and downtime are hidden for this work center.",
        checked_at_utc=checked_at_utc,
        facts=(("Work center", station_name),), actions=actions,
    )


def production_source_warning(*, checked_at_utc: datetime) -> DashboardWarning:
    return _make_warning(
        kind="production_data_unavailable", label="Production data unavailable",
        title="Production data is unavailable",
        summary="Plant Manager could not read the production source.",
        source="production", subject="production-source",
        reason_code="source_unavailable",
        impact="Production values are hidden while attendance and forklift information stay visible.",
        checked_at_utc=checked_at_utc,
        actions=(_action("check_again", "Check again"),
                 _action("open_diagnostics", "Open diagnostics", "/settings?section=diagnostics")),
    )


def forklift_source_warning(*, checked_at_utc: datetime,
                            last_success_at_utc: datetime | None) -> DashboardWarning:
    return _make_warning(
        kind="forklift_data_unavailable", label="Forklift data unavailable",
        title="Forklift data is unavailable",
        summary="Plant Manager does not have a complete forklift call snapshot.",
        source="forklift", subject="forklift-source", reason_code="source_unavailable",
        impact="Forklift calls, on-time results, handling time, and scores are hidden.",
        checked_at_utc=checked_at_utc, last_success_at_utc=last_success_at_utc,
        actions=(_action("check_again", "Check again"),
                 _action("open_diagnostics", "Open diagnostics", "/settings?section=diagnostics")),
    )


def forklift_identity_conflict_warning(*, identity_count: int,
                                       checked_at_utc: datetime,
                                       last_success_at_utc: datetime | None,
                                       day: date) -> DashboardWarning:
    return _make_warning(
        kind="forklift_identity_conflict", label="Forklift driver identity conflict",
        title="Forklift driver identities conflict",
        summary="One or more outside driver identities cannot be assigned safely.",
        source="forklift", subject="forklift-identity-conflict",
        reason_code="identity_conflict",
        impact="Conflicting drivers do not receive forklift calls or scores on the People page.",
        checked_at_utc=checked_at_utc, last_success_at_utc=last_success_at_utc,
        facts=(("Conflicting identities", str(identity_count)),),
        actions=(
            _action("check_again", "Check again"),
            _action("review_identities", "Review identities",
                    f"/settings?section=forklift&identity_day={day.isoformat()}#forklift-identities"),
        ),
    )


def unmatched_forklift_warning(*, call_count: int,
                               identities: tuple[tuple[str, tuple[str, ...], int], ...],
                               first_call_utc: datetime, last_call_utc: datetime,
                               checked_at_utc: datetime,
                               last_success_at_utc: datetime | None,
                               day: date) -> DashboardWarning:
    shown_identities = identities[:20]
    identity_summary = "; ".join(
        f"{driver_id} ({', '.join(names) or 'name unavailable'}) — {count} calls"
        for driver_id, names, count in shown_identities
    )
    if len(identities) > len(shown_identities):
        identity_summary += f"; +{len(identities) - len(shown_identities)} more"
    return _make_warning(
        kind="unmatched_forklift_calls", label=f"Unmatched forklift calls: {call_count}",
        title="Forklift calls need an employee match",
        summary="Forklift calls could not be matched to active employees.",
        source="forklift", subject="unmatched-forklift-calls",
        reason_code="identity_unmatched",
        impact="These calls and their results are not credited to a person.",
        checked_at_utc=checked_at_utc, last_success_at_utc=last_success_at_utc,
        facts=(("Unmatched calls", str(call_count)),
               ("Distinct identities", str(len(identities))),
               ("External identities", identity_summary),
               ("First call", _local_time(first_call_utc)),
               ("Last call", _local_time(last_call_utc))),
        actions=(
            _action("check_again", "Check again"),
            _action("review_identities", "Review identities",
                    f"/settings?section=forklift&identity_day={day.isoformat()}#forklift-identities"),
        ),
    )


def forklift_timeline_warning(*, checked_at_utc: datetime,
                              last_success_at_utc: datetime | None) -> DashboardWarning:
    return _make_warning(
        kind="forklift_timeline_incomplete", label="Forklift timeline incomplete",
        title="Forklift timeline is incomplete",
        summary="Stored driver totals do not match the available call details.",
        source="forklift", subject="forklift-timeline", reason_code="incomplete_data",
        impact="Affected forklift totals may show, but timeline and score details stay unavailable.",
        checked_at_utc=checked_at_utc, last_success_at_utc=last_success_at_utc,
        actions=(_action("check_again", "Check again"),
                 _action("open_diagnostics", "Open diagnostics", "/settings?section=diagnostics")),
    )


def attendance_stale_warning(*, blocker_count: int,
                             checked_at_utc: datetime) -> DashboardWarning:
    return _make_warning(
        kind="attendance_source_stale", label="Attendance source stale",
        title="Attendance has not updated on time",
        summary="Plant Manager is keeping the last safe attendance snapshot.",
        source="attendance", subject="attendance-source", reason_code="stale_source",
        impact="People locations may be older than the check time until attendance catches up.",
        checked_at_utc=checked_at_utc,
        facts=(("Freshness checks blocked", str(blocker_count)),),
        actions=(_action("check_again", "Check again"),
                 _action("open_diagnostics", "Open diagnostics", "/settings?section=diagnostics")),
    )


def attendance_source_warning(*, checked_at_utc: datetime) -> DashboardWarning:
    return _make_warning(
        kind="attendance_data_unavailable", label="Attendance data unavailable",
        title="Attendance data is unavailable",
        summary="Plant Manager could not load a safe attendance snapshot.",
        source="attendance", subject="attendance-source", reason_code="source_unavailable",
        impact="The People list is empty because attendance owns page membership and location.",
        checked_at_utc=checked_at_utc,
        actions=(_action("check_again", "Check again"),
                 _action("open_diagnostics", "Open diagnostics", "/settings?section=diagnostics")),
    )
```

Use manager-safe reason copy for `missing_totals`, `incomplete_data`,
`duplicate_data`, `missing_goal`, `metric_mismatch`, `calculation_failure`,
`identity_conflict`, `stale_source`, and `source_unavailable`. Reject unknown
production reason codes with `ValueError`; do not include exception messages.

- [ ] **Step 4: Type the dashboard model and deduplicate warnings by key**

Import `DashboardWarning` into `people_performance.py`, change both warning
annotations to `Sequence[DashboardWarning]` / `tuple[DashboardWarning, ...]`,
and replace string-based deduplication in `assemble_dashboard()` with:

```python
deduplicated_warnings: dict[str, DashboardWarning] = {}
for warning in source_warnings:
    if not isinstance(warning, DashboardWarning):
        raise TypeError("source_warnings must contain DashboardWarning values")
    deduplicated_warnings.setdefault(warning.key, warning)

return DashboardModel(
    day=day,
    is_today=is_today,
    as_of_utc=as_of_utc,
    window_start_utc=window_start_utc,
    window_end_utc=window_end_utc,
    rows=tuple(sorted(rows, key=lambda row: row.sort_key)),
    breaks=tuple(breaks),
    source_warnings=tuple(deduplicated_warnings.values()),
)
```

- [ ] **Step 5: Classify every existing data-loader warning**

In `people_performance_data.py`, replace all eight string append/return sites
with the builders above. Calculate the production reason before the availability
branch in this order so the most precise truth wins:

```python
if station.name in duplicate_total_names:
    reason_code = "duplicate_data"
elif total is None:
    reason_code = "missing_totals"
elif bool(getattr(total, "truncated", True)):
    reason_code = "incomplete_data"
elif station.name not in goal_based_names:
    reason_code = "missing_goal"
elif total.station.meter_id != station.meter_id or total.station.name != station.name:
    reason_code = "metric_mismatch"
else:
    reason_code = ""
if reason_code:
    warnings.append(production_metric_warning(
        station_name=station.name,
        reason_code=reason_code,
        checked_at_utc=cap,
        day=day,
    ))
    continue
```

Add `day: date` to `_forklift_values()` and pass the selected `day` from
`load_dashboard()`. Use `cap` as every warning's `checked_at_utc`, and use
`source.coverage.successful_at if source.coverage is not None else None` as
the forklift last-success timestamp.

On scorer exceptions use `calculation_failure`. For unmatched forklift calls,
aggregate only unresolved in-shift events:

```python
unmatched_events = tuple(
    event for event in source.events
    if start <= event.created_at_utc < cap and event.driver_id not in resolved
)
identity_counts = Counter(event.driver_id for event in unmatched_events)
identity_names = {
    driver_id: tuple(sorted({event.driver_name for event in unmatched_events
                             if event.driver_id == driver_id}))
    for driver_id in identity_counts
}
identities = tuple(
    (driver_id, identity_names[driver_id], identity_counts[driver_id])
    for driver_id in sorted(identity_counts)
)
```

For the identity-conflict count, use
`len(unsafe_driver_ids) + sum(count > 1 for count in claimed.values())`. For
attendance freshness, expose only `len(attendance.freshness_blockers)`, not the
raw blocker payload. Keep current fail-closed data behavior unchanged.

- [ ] **Step 6: Update fixtures and assertions to use warning fields**

Replace literal warning tuples in `tests/people_performance_fixtures.py` and
`tests/test_people_performance_rows.py` with builder-created records. Change
data tests from string membership to field assertions, for example:

```python
warning = next(item for item in model.source_warnings
               if item.kind == "production_metric_unavailable")
assert warning.subject == "Repair 2"
assert warning.reason_code == "missing_totals"

warning = next(item for item in model.source_warnings
               if item.kind == "unmatched_forklift_calls")
assert warning.label == "Unmatched forklift calls: 1"
assert ("Distinct identities", "1") in warning.facts
```

Add this reusable fixture helper for Task 5:

```python
def unmatched_warning_fixture(call_count: int = 1):
    return unmatched_forklift_warning(
        call_count=call_count,
        identities=(("driver-unknown", ("Unknown",), call_count),),
        first_call_utc=START,
        last_call_utc=END,
        checked_at_utc=END,
        last_success_at_utc=END,
        day=DAY,
    )
```

Until Task 5 makes the warnings interactive, change the existing warning loop
to `<span>{{ warning.label }}</span>` and assert the rendered text remains the
same. This keeps Task 1 independently shippable while removing all template
dependence on dataclass string conversion.

Run `rg -n 'source_warnings=.*"|in model.source_warnings' tests scripts` and
convert every remaining People fixture/assertion; the command must return no
legacy string-based People warning assertions.

- [ ] **Step 7: Run focused warning and data tests**

Run: `.venv/bin/pytest tests/test_people_performance_warnings.py tests/test_people_performance_data.py tests/test_people_performance_rows.py -q`

Expected: PASS.

- [ ] **Step 8: Commit the typed warning contract**

```bash
git add src/zira_dashboard/people_performance_warnings.py src/zira_dashboard/people_performance.py src/zira_dashboard/people_performance_data.py src/zira_dashboard/templates/_people_performance_rows.html tests/test_people_performance_warnings.py tests/people_performance_fixtures.py tests/test_people_performance_data.py tests/test_people_performance_rows.py tests/test_people_performance_template.py
git commit -m "refactor: structure People source warnings"
```

### Task 2: Add URL-Driven Count Filters and Result Summaries

**Files:**
- Modify: `src/zira_dashboard/people_performance_view.py:382-423`
- Modify: `src/zira_dashboard/routes/people_performance.py:32-91`
- Modify: `src/zira_dashboard/templates/_people_performance_rows.html:1-40`
- Modify: `src/zira_dashboard/static/people-performance.css:53-145`
- Modify: `tests/test_people_performance_view.py`
- Modify: `tests/test_people_performance_route.py`
- Modify: `tests/test_people_performance_template.py`

**Interfaces:**
- Consumes: `DashboardModel` and structured `source_warnings` from Task 1.
- Produces: `dashboard_context(model, *, status_filter: str | None, attention_only: bool)`.
- Produces: live-root data attributes `data-status` and `data-attention` for Task 6.

- [ ] **Step 1: Write failing presenter tests for every filter combination**

Add these cases to `tests/test_people_performance_view.py`:

```python
@pytest.mark.parametrize(
    ("status_filter", "attention_only", "expected_names"),
    (
        (None, False, {"Amy Behind", "Zed Ahead", "Ben Driver", "Cal Missing", "Sam Stale", "Mia Mixed"}),
        ("working", False, {"Amy Behind", "Zed Ahead", "Ben Driver", "Cal Missing", "Sam Stale"}),
        ("earlier", False, {"Mia Mixed"}),
        ("working", True, {"Amy Behind", "Ben Driver", "Cal Missing", "Sam Stale"}),
        ("earlier", True, set()),
    ),
)
def test_status_and_attention_filters_compose(status_filter, attention_only, expected_names):
    context = dashboard_context(
        busy_dashboard_model(),
        status_filter=status_filter,
        attention_only=attention_only,
    )
    names = {row["person_name"] for section in context["sections"] for row in section["rows"]}
    assert names == expected_names
    assert context["working_now"] == 5
    assert context["worked_earlier"] == 1
    assert context["total_people"] == 6
    assert context["visible_people"] == len(expected_names)
```

Add one assertion that the `earlier + attention` case sets
`filtered_empty is True` and supplies the exact summary `"Showing 0 of 1 worked earlier who need attention."`.

- [ ] **Step 2: Run the presenter tests to verify the missing argument fails**

Run: `.venv/bin/pytest tests/test_people_performance_view.py -q`

Expected: FAIL with `TypeError: dashboard_context() got an unexpected keyword argument 'status_filter'`.

- [ ] **Step 3: Implement deterministic row filtering and summary copy**

Change the presenter signature and filtering block to:

```python
def dashboard_context(
    model: DashboardModel,
    *,
    status_filter: str | None = None,
    attention_only: bool = False,
) -> dict:
    if status_filter not in (None, "working", "earlier"):
        raise ValueError("unknown People status filter")
    status_rows = tuple(
        row for row in model.rows
        if status_filter is None
        or (status_filter == "working" and row.is_active)
        or (status_filter == "earlier" and not row.is_active)
    )
    rows = tuple(
        row for row in status_rows
        if not attention_only or row.attention_reasons
    )
```

Return `status_filter`, `attention_only`, `total_people`, `visible_people`,
`filtered_empty`, and `filter_summary`. Build the summary from the full-day
status denominator and append `" who need attention"` only when the attention
filter is active. No filters means `filter_summary` is an empty string.

Use this helper so copy and denominators stay deterministic:

```python
def _filter_summary(*, status_filter: str | None, attention_only: bool,
                    visible: int, total: int, working: int, earlier: int) -> str:
    if status_filter is None and not attention_only:
        return ""
    if status_filter == "working":
        denominator, label = working, "working now"
    elif status_filter == "earlier":
        denominator, label = earlier, "worked earlier"
    else:
        denominator, label = total, "people"
    attention = " who need attention" if attention_only else ""
    return f"Showing {visible} of {denominator} {label}{attention}."
```

- [ ] **Step 4: Validate and pass the status query through both routes**

Add a route helper and parameter:

```python
def _selected_status(raw: str | None) -> str | None:
    if raw in (None, "", "working", "earlier"):
        return raw or None
    raise HTTPException(status_code=400, detail="Unknown People status filter")
```

Both `/people-performance` and `/people-performance/rows` accept
`status: str | None = Query(default=None)`, call `_selected_status(status)`, and
pass `status_filter` into `_context()` and `dashboard_context()`. Add route tests
that `status=working&attention=1` reaches the presenter and `status=other`
returns 400 before loading the dashboard.

- [ ] **Step 5: Replace static counts and the checkbox with semantic controls**

In `_people_performance_rows.html`, add `data-status` to the live root, keep the
date form's active filters in hidden inputs, and render these button contracts:

```html
<div class="pp-counts" aria-label="Filter people by day totals">
  <button type="button" data-pp-count-filter="status" data-filter-value="working"
          data-pp-control-key="working"
          aria-pressed="{{ 'true' if status_filter == 'working' else 'false' }}"
          {% if working_now == 0 %}disabled aria-describedby="pp-working-empty"{% endif %}>
    {% if status_filter == 'working' %}<span class="pp-filter-selected" aria-hidden="true">✓</span><span class="sr-only">Selected filter.</span>{% endif %}
    <strong>{{ working_now }}</strong> working now
  </button>
  <button type="button" data-pp-count-filter="status" data-filter-value="earlier"
          data-pp-control-key="earlier"
          aria-pressed="{{ 'true' if status_filter == 'earlier' else 'false' }}"
          {% if worked_earlier == 0 %}disabled aria-describedby="pp-earlier-empty"{% endif %}>
    {% if status_filter == 'earlier' %}<span class="pp-filter-selected" aria-hidden="true">✓</span><span class="sr-only">Selected filter.</span>{% endif %}
    <strong>{{ worked_earlier }}</strong> worked earlier
  </button>
  <button type="button" data-pp-count-filter="attention" data-filter-value="1"
          data-pp-control-key="attention"
          aria-pressed="{{ 'true' if attention_only else 'false' }}"
          {% if needs_attention == 0 %}disabled aria-describedby="pp-attention-empty"{% endif %}>
    {% if attention_only %}<span class="pp-filter-selected" aria-hidden="true">✓</span><span class="sr-only">Selected filter.</span>{% endif %}
    <strong>{{ needs_attention }}</strong> need attention
  </button>
</div>
```

Add three `sr-only` descriptions with “There are no … people to show.” Remove
the `.pp-check` label and checkbox entirely. Keep hidden `status` and
`attention=1` inputs only when active so changing the date preserves filters.
Below the manager strip, render `filter_summary` in `role="status"`; when
`filtered_empty` is true, include a `/people-performance?day={{ day }}` clear
link instead of three generic “No people in this group” messages.

Insert this branch immediately inside `#people-performance-rows`, before the
existing `{% for section in sections %}` line:

```html
{% if filter_summary %}<p class="pp-filter-summary" role="status">{{ filter_summary }}</p>{% endif %}
{% if filtered_empty %}
<div class="pp-filter-empty">
  <p>No people match these filters.</p>
  <a href="/people-performance?day={{ day }}">Clear filters</a>
</div>
{% else %}
{% for section in sections %}
```

Keep the current section loop body verbatim, then add `{% endif %}` immediately
after its existing `{% endfor %}`.

- [ ] **Step 6: Style selected, disabled, and focus-visible count buttons**

Replace the `.pp-counts span` rules with `.pp-counts button`, retain the pill
shape, add `min-height:44px`, `cursor:pointer`, inherited font, and:

```css
.pp-counts button[aria-pressed="true"] {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 14%, var(--panel));
  color: var(--fg);
}
.pp-counts button:disabled {
  cursor: not-allowed;
  opacity: .55;
}
.pp-counts button:focus-visible {
  outline: 3px solid color-mix(in srgb, var(--accent) 65%, transparent);
  outline-offset: 2px;
}
```

- [ ] **Step 7: Run the server and template filter tests**

Run: `.venv/bin/pytest tests/test_people_performance_view.py tests/test_people_performance_route.py tests/test_people_performance_template.py -q`

Expected: PASS, including no `name="attention"` checkbox and exactly three
`data-pp-count-filter` buttons.

- [ ] **Step 8: Commit count filtering**

```bash
git add src/zira_dashboard/people_performance_view.py src/zira_dashboard/routes/people_performance.py src/zira_dashboard/templates/_people_performance_rows.html src/zira_dashboard/static/people-performance.css tests/test_people_performance_view.py tests/test_people_performance_route.py tests/test_people_performance_template.py
git commit -m "feat: filter People rows from day totals"
```

### Task 3: Add Transactional Forklift Identity Mappings and Audit History

**Files:**
- Modify: `src/zira_dashboard/_schema.py:2150-2170`
- Create: `src/zira_dashboard/forklift_identity_store.py`
- Create: `tests/test_forklift_identity_store.py`
- Modify: `src/zira_dashboard/forklift_store.py:270-325`
- Modify: `tests/test_forklift_identity.py`

**Interfaces:**
- Produces: `DriverIdentityMapping`, `MappingConflict`, `list_mappings()`, `mapping_ids()`, `save_mapping()`, and `remove_mapping()`.
- Consumes: authenticated actor values from Task 4 and active `people.odoo_id` rows.
- Produces: explicit mappings consumed by `forklift_store.resolve_forklift_driver_ids()` and warning re-evaluation.

- [ ] **Step 1: Write failing schema and store tests**

Create unit tests that inspect `_schema.SCHEMA_DDL` for both new tables and test
normalization/validation without a database. Add database-gated tests using the
existing `DATABASE_URL` skip pattern:

```python
import os

import pytest

from zira_dashboard import db
from zira_dashboard.forklift_identity_store import (
    audit_rows,
    list_mappings,
    remove_mapping,
    save_mapping,
)


DB_ONLY = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs DATABASE_URL")


def test_schema_declares_one_to_one_mapping_and_append_only_audit():
    from zira_dashboard._schema import SCHEMA_DDL
    assert "CREATE TABLE IF NOT EXISTS forklift_driver_identity_map" in SCHEMA_DDL
    assert "employee_odoo_id INTEGER NOT NULL REFERENCES people(odoo_id)" in SCHEMA_DDL
    assert "UNIQUE (employee_odoo_id)" in SCHEMA_DDL
    assert "CREATE TABLE IF NOT EXISTS forklift_driver_identity_audit" in SCHEMA_DDL


@pytest.fixture
def identity_people():
    db.bootstrap_schema()
    db.execute("DELETE FROM forklift_driver_identity_map WHERE external_driver_id='driver-7'")
    db.execute("DELETE FROM forklift_driver_identity_audit WHERE external_driver_id='driver-7'")
    db.execute("DELETE FROM people WHERE odoo_id IN (700, 701)")
    db.execute(
        "INSERT INTO people (odoo_id, name, active, excluded) VALUES "
        "(700, 'Identity Test One', TRUE, FALSE), "
        "(701, 'Identity Test Two', TRUE, FALSE)"
    )
    yield
    db.execute("DELETE FROM forklift_driver_identity_map WHERE external_driver_id='driver-7'")
    db.execute("DELETE FROM forklift_driver_identity_audit WHERE external_driver_id='driver-7'")
    db.execute("DELETE FROM people WHERE odoo_id IN (700, 701)")


@DB_ONLY
def test_save_change_remove_round_trip_records_each_audit(identity_people):
    first = save_mapping("driver-7", "Sam", 700, expected_version=None,
                         actor_upn="manager@example.com", actor_name="Manager")
    changed = save_mapping("driver-7", "Samuel", 701, expected_version=first.version,
                           actor_upn="manager@example.com", actor_name="Manager")
    remove_mapping("driver-7", expected_version=changed.version,
                   actor_upn="manager@example.com", actor_name="Manager")
    assert list_mappings() == ()
    assert [row["action"] for row in audit_rows("driver-7")] == [
        "create", "change", "remove"
    ]
```

Also test blank IDs, bool/non-positive employee IDs, inactive/excluded employees,
two driver IDs claiming one employee, stale `expected_version`, and removal of a
missing mapping.

- [ ] **Step 2: Run the store test to prove the module/schema are absent**

Run: `.venv/bin/pytest tests/test_forklift_identity_store.py -q`

Expected: FAIL during import or schema assertions.

- [ ] **Step 3: Add idempotent mapping and audit schema**

Append immediately after `forklift_name_map`:

```sql
CREATE TABLE IF NOT EXISTS forklift_driver_identity_map (
  external_driver_id TEXT PRIMARY KEY CHECK (btrim(external_driver_id) <> ''),
  source_name TEXT NOT NULL DEFAULT '',
  employee_odoo_id INTEGER NOT NULL REFERENCES people(odoo_id),
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by_upn TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by_upn TEXT NOT NULL,
  UNIQUE (employee_odoo_id)
);
CREATE TABLE IF NOT EXISTS forklift_driver_identity_audit (
  id BIGSERIAL PRIMARY KEY,
  external_driver_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('create', 'change', 'remove')),
  before_employee_odoo_id INTEGER,
  after_employee_odoo_id INTEGER,
  before_source_name TEXT,
  after_source_name TEXT,
  actor_upn TEXT NOT NULL,
  actor_name TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS forklift_driver_identity_audit_driver_idx
  ON forklift_driver_identity_audit (external_driver_id, created_at DESC, id DESC);
```

Do not add a foreign key to audit employee IDs; the audit must survive later
roster changes.

- [ ] **Step 4: Implement fail-closed transactional mutations**

Create the store dataclass and exception. Expose `audit_rows()` only as the
read-only audit accessor used by tests and the Settings history label:

```python
@dataclass(frozen=True)
class DriverIdentityMapping:
    external_driver_id: str
    source_name: str
    employee_odoo_id: int
    employee_name: str
    version: int
    created_at: datetime
    created_by_upn: str
    updated_at: datetime
    updated_by_upn: str


class MappingConflict(ValueError):
    pass


def audit_rows(external_driver_id: str) -> tuple[dict, ...]:
    return tuple(db.query(
        "SELECT action, before_employee_odoo_id, after_employee_odoo_id, "
        "before_source_name, after_source_name, actor_upn, actor_name, created_at "
        "FROM forklift_driver_identity_audit WHERE external_driver_id=%s "
        "ORDER BY created_at, id",
        (_required_text(external_driver_id, "external driver ID"),),
    ))
```

`save_mapping()` must normalize strings, open one `db.cursor()` transaction,
lock the existing mapping with `FOR UPDATE`, validate the selected person using:

```sql
SELECT odoo_id, name FROM people
WHERE odoo_id = %s AND active = TRUE AND excluded = FALSE
FOR SHARE
```

Reject a different mapped driver that already owns the employee. On create,
require `expected_version is None`; on change, require an exact existing
version. Write the map and one audit row in the same transaction, returning the
row joined to `people`.

`remove_mapping()` locks by driver ID, requires the exact version, deletes the
row, and appends the `remove` audit in the same transaction. Convert database
unique-constraint races into `MappingConflict("That employee is already mapped to another forklift identity.")`.

Use these exact public signatures and transaction structure:

```python
def list_mappings() -> tuple[DriverIdentityMapping, ...]:
    rows = db.query(
        "SELECT m.external_driver_id, m.source_name, m.employee_odoo_id, "
        "p.name AS employee_name, m.version, m.created_at, m.created_by_upn, "
        "m.updated_at, m.updated_by_upn "
        "FROM forklift_driver_identity_map m JOIN people p ON p.odoo_id=m.employee_odoo_id "
        "ORDER BY lower(p.name), m.external_driver_id"
    )
    return tuple(_mapping(row) for row in rows)


def mapping_ids() -> dict[str, int]:
    return {
        row["external_driver_id"]: int(row["employee_odoo_id"])
        for row in db.query(
            "SELECT external_driver_id, employee_odoo_id "
            "FROM forklift_driver_identity_map"
        )
    }


def save_mapping(external_driver_id: str, source_name: str, employee_odoo_id: int,
                 *, expected_version: int | None, actor_upn: str,
                 actor_name: str | None) -> DriverIdentityMapping:
    driver_id = _required_text(external_driver_id, "external driver ID")
    source = str(source_name or "").strip()[:200]
    employee_id = _positive_int(employee_odoo_id, "employee Odoo ID")
    actor = _required_text(actor_upn, "actor UPN")
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT external_driver_id, source_name, employee_odoo_id, version, "
                "created_at, created_by_upn FROM forklift_driver_identity_map "
                "WHERE external_driver_id=%s FOR UPDATE",
                (driver_id,),
            )
            before = cur.fetchone()
            if before is None and expected_version is not None:
                raise MappingConflict("This forklift identity changed. Reload and try again.")
            if before is not None and int(before["version"]) != expected_version:
                raise MappingConflict("This forklift identity changed. Reload and try again.")
            cur.execute(
                "SELECT odoo_id, name FROM people "
                "WHERE odoo_id=%s AND active=TRUE AND excluded=FALSE FOR SHARE",
                (employee_id,),
            )
            person = cur.fetchone()
            if person is None:
                raise MappingConflict("Choose an active employee.")
            cur.execute(
                "SELECT external_driver_id FROM forklift_driver_identity_map "
                "WHERE employee_odoo_id=%s AND external_driver_id<>%s FOR UPDATE",
                (employee_id, driver_id),
            )
            if cur.fetchone() is not None:
                raise MappingConflict(
                    "That employee is already mapped to another forklift identity."
                )
            if before is None:
                cur.execute(
                    "INSERT INTO forklift_driver_identity_map "
                    "(external_driver_id, source_name, employee_odoo_id, created_by_upn, updated_by_upn) "
                    "VALUES (%s,%s,%s,%s,%s) RETURNING *",
                    (driver_id, source, employee_id, actor, actor),
                )
                action = "create"
            else:
                cur.execute(
                    "UPDATE forklift_driver_identity_map SET source_name=%s, "
                    "employee_odoo_id=%s, version=version+1, updated_at=now(), "
                    "updated_by_upn=%s WHERE external_driver_id=%s RETURNING *",
                    (source, employee_id, actor, driver_id),
                )
                action = "change"
            saved = cur.fetchone()
            _append_audit(
                cur, driver_id=driver_id, action=action,
                before_employee_id=(before["employee_odoo_id"] if before else None),
                after_employee_id=employee_id,
                before_source_name=(before["source_name"] if before else None),
                after_source_name=source, actor_upn=actor, actor_name=actor_name,
            )
            return _mapping({**saved, "employee_name": person["name"]})
    except UniqueViolation as exc:
        raise MappingConflict(
            "That employee is already mapped to another forklift identity."
        ) from exc


def remove_mapping(external_driver_id: str, *, expected_version: int,
                   actor_upn: str, actor_name: str | None) -> None:
    driver_id = _required_text(external_driver_id, "external driver ID")
    version = _positive_int(expected_version, "mapping version")
    actor = _required_text(actor_upn, "actor UPN")
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM forklift_driver_identity_map "
            "WHERE external_driver_id=%s FOR UPDATE", (driver_id,)
        )
        before = cur.fetchone()
        if before is None or int(before["version"]) != version:
            raise MappingConflict("This forklift identity changed. Reload and try again.")
        cur.execute(
            "DELETE FROM forklift_driver_identity_map WHERE external_driver_id=%s",
            (driver_id,),
        )
        _append_audit(
            cur, driver_id=driver_id, action="remove",
            before_employee_id=before["employee_odoo_id"], after_employee_id=None,
            before_source_name=before["source_name"], after_source_name=None,
            actor_upn=actor, actor_name=actor_name,
        )
```

Define the helpers directly above the public functions and import
`psycopg2.errors.UniqueViolation`:

```python
def _required_text(value: object, label: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{label} is required")
    return clean


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return parsed


def _mapping(row: dict) -> DriverIdentityMapping:
    return DriverIdentityMapping(
        external_driver_id=row["external_driver_id"],
        source_name=row["source_name"],
        employee_odoo_id=int(row["employee_odoo_id"]),
        employee_name=row["employee_name"],
        version=int(row["version"]),
        created_at=row["created_at"],
        created_by_upn=row["created_by_upn"],
        updated_at=row["updated_at"],
        updated_by_upn=row["updated_by_upn"],
    )


def _append_audit(cur, *, driver_id: str, action: str,
                  before_employee_id: int | None, after_employee_id: int | None,
                  before_source_name: str | None, after_source_name: str | None,
                  actor_upn: str, actor_name: str | None) -> None:
    cur.execute(
        "INSERT INTO forklift_driver_identity_audit "
        "(external_driver_id, action, before_employee_odoo_id, after_employee_odoo_id, "
        "before_source_name, after_source_name, actor_upn, actor_name) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (driver_id, action, before_employee_id, after_employee_id,
         before_source_name, after_source_name, actor_upn, actor_name),
    )
```

- [ ] **Step 5: Prefer explicit mappings during forklift resolution**

At the start of `resolve_forklift_driver_ids()`, load `mapping_ids()` and keep
only mappings whose employee is active and inside `allowed_employee_ids`. Reserve
those employee IDs before automatic full-name/unique-first-name inference:

```python
active_people_by_id = {int(person.employee_id): person for person in people}
explicit = {
    driver_id: employee_id
    for driver_id, employee_id in forklift_identity_store.mapping_ids().items()
    if driver_id in names_by_driver_id
    and employee_id in active_people_by_id
    and (allowed is None or employee_id in allowed)
}
reserved_employee_ids = set(explicit.values())
proposed: dict[str, int] = {}
for raw_driver_id, raw_names in names_by_driver_id.items():
    driver_id = str(raw_driver_id).strip()
    if not driver_id or driver_id in explicit:
        continue
    names = {
        str(value).strip() for value in raw_names
        if value is not None and str(value).strip()
    }
    if len(names) != 1:
        continue
    source_name = next(iter(names))
    target_name = str(overrides.get(source_name, source_name)).strip()
    exact = by_full_name.get(target_name.casefold(), [])
    candidates = exact if exact else by_first_name.get(target_name.casefold(), [])
    candidates = [
        person for person in candidates
        if int(person.employee_id) not in reserved_employee_ids
    ]
    if len(candidates) == 1:
        proposed[driver_id] = int(candidates[0].employee_id)
claimed = Counter(proposed.values())
inferred = {
    driver_id: employee_id for driver_id, employee_id in proposed.items()
    if claimed[employee_id] == 1
}
return {**explicit, **inferred}
```

Add tests proving an explicit mapping resolves an ambiguous name, an inactive
target fails closed, an automatic match cannot steal an explicitly reserved
employee, and existing unique-name inference still works.

- [ ] **Step 6: Run focused schema, store, and resolution tests**

Run: `.venv/bin/pytest tests/test_forklift_identity_store.py tests/test_forklift_identity.py tests/test_forklift_store.py -q`

Expected: PASS. Database-gated cases may show SKIPPED only when `DATABASE_URL`
is unset.

- [ ] **Step 7: Commit identity persistence**

```bash
git add src/zira_dashboard/_schema.py src/zira_dashboard/forklift_identity_store.py src/zira_dashboard/forklift_store.py tests/test_forklift_identity_store.py tests/test_forklift_identity.py
git commit -m "feat: store audited forklift identities"
```

### Task 4: Build the Focused Forklift Identity Settings Workflow

**Files:**
- Create: `src/zira_dashboard/forklift_identity_view.py`
- Create: `src/zira_dashboard/routes/forklift_identities.py`
- Create: `src/zira_dashboard/templates/_settings_forklift_identities.html`
- Create: `tests/test_forklift_identity_settings.py`
- Modify: `src/zira_dashboard/app.py:25-65,721-760`
- Modify: `src/zira_dashboard/routes/settings.py:271-580`
- Modify: `src/zira_dashboard/templates/settings.html:970-990`
- Modify: `src/zira_dashboard/static/settings.css`
- Modify: `tests/test_settings_forklift.py`

**Interfaces:**
- Consumes: Task 3 store APIs and current forklift completion events.
- Produces: `identity_context(day: date) -> dict` with `mappings`, `unresolved`, and `employee_options`.
- Produces: `POST /settings/forklift-identities` for `save` and `remove` actions.
- Produces: `/settings?section=forklift&identity_day=YYYY-MM-DD#forklift-identities`, linked by warning actions.

- [ ] **Step 1: Write failing pure view tests**

Test that one day's events are aggregated by external ID without exposing raw
events, current mappings remain visible even without calls that day, and active
non-excluded Odoo employees are sorted by name:

```python
context = identity_context(DAY)
assert context["day"] == "2026-09-02"
assert context["unresolved"] == ({
    "external_driver_id": "driver-8",
    "source_names": ("Alex", "A."),
    "call_count": 5,
    "first_call": "7:10 AM",
    "last_call": "8:45 AM",
    "name_conflict": True,
    "version": None,
},)
assert context["mappings"][0]["employee_name"] == "Sam Rivera"
assert [row["employee_name"] for row in context["employee_options"]] == [
    "Alex Chen", "Sam Rivera"
]
```

- [ ] **Step 2: Run the view tests to prove the module is absent**

Run: `.venv/bin/pytest tests/test_forklift_identity_settings.py::test_identity_context_aggregates_unresolved_calls -q`

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement manager-safe identity context**

Use local-day midnight bounds in `shift_config.SITE_TZ`, query
`forklift_event_store.completion_events_for_range()`, group by `driver_id`, and
call `forklift_store.resolve_forklift_driver_ids()` using active roster Odoo IDs.
Return only unresolved aggregates plus `forklift_identity_store.list_mappings()`.
Format call times in the site timezone. Never put event IDs, response payloads,
or per-call rows into the returned context.

Implement the module with this complete derivation:

```python
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from . import (
    forklift_event_store,
    forklift_identity_store,
    forklift_store,
    shift_config,
    staffing,
)


def _time_label(value: datetime) -> str:
    return value.astimezone(shift_config.SITE_TZ).strftime("%-I:%M %p")


def _changed_label(value: datetime) -> str:
    return value.astimezone(shift_config.SITE_TZ).strftime("%b %-d, %-I:%M %p")


def identity_context(day: date) -> dict:
    if type(day) is not date:
        raise TypeError("day must be a date")
    start = datetime.combine(day, time.min, tzinfo=shift_config.SITE_TZ).astimezone(UTC)
    end = datetime.combine(
        day + timedelta(days=1), time.min, tzinfo=shift_config.SITE_TZ
    ).astimezone(UTC)
    events = forklift_event_store.completion_events_for_range(start, end)
    people = tuple(
        person for person in staffing.load_roster()
        if person.active and person.employee_id is not None
    )
    employee_rows = tuple(
        {"employee_odoo_id": int(person.employee_id), "employee_name": person.name}
        for person in sorted(people, key=lambda item: item.name.casefold())
    )
    events_by_driver: dict[str, list] = {}
    names_by_driver: dict[str, list[str]] = {}
    for event in events:
        events_by_driver.setdefault(event.driver_id, []).append(event)
        names = names_by_driver.setdefault(event.driver_id, [])
        if event.driver_name and event.driver_name not in names:
            names.append(event.driver_name)
    evidence = {
        driver_id: set(names) for driver_id, names in names_by_driver.items()
    }
    resolved = forklift_store.resolve_forklift_driver_ids(
        evidence,
        allowed_employee_ids={int(person.employee_id) for person in people},
    )
    unresolved_rows = tuple(
        {
            "external_driver_id": driver_id,
            "source_names": tuple(names_by_driver.get(driver_id, ())),
            "call_count": len(driver_events),
            "first_call": _time_label(min(item.created_at_utc for item in driver_events)),
            "last_call": _time_label(max(item.created_at_utc for item in driver_events)),
            "name_conflict": len(names_by_driver.get(driver_id, ())) != 1,
            "version": None,
        }
        for driver_id, driver_events in sorted(events_by_driver.items())
        if driver_id not in resolved
    )
    mapping_rows = tuple(
        {
            "external_driver_id": item.external_driver_id,
            "source_name": item.source_name,
            "employee_odoo_id": item.employee_odoo_id,
            "employee_name": item.employee_name,
            "version": item.version,
            "updated_at": _changed_label(item.updated_at),
            "updated_by_upn": item.updated_by_upn,
        }
        for item in forklift_identity_store.list_mappings()
    )
    return {
        "day": day.isoformat(),
        "mappings": mapping_rows,
        "unresolved": unresolved_rows,
        "employee_options": employee_rows,
    }
```

- [ ] **Step 4: Write failing route tests for save, remove, and conflicts**

Use `TestClient`, monkeypatch the store, and set `request.state` through the
test auth middleware pattern. Assert:

```python
response = client.post(
    "/settings/forklift-identities",
    data={"action": "save", "external_driver_id": "driver-8",
          "employee_odoo_id": "708", "expected_version": "",
          "day": "2026-09-02"},
    follow_redirects=False,
)
assert response.status_code == 303
assert response.headers["location"] == (
    "/settings?section=forklift&identity_day=2026-09-02&identity_saved=1"
    "#forklift-identities"
)
assert saved_call["actor_upn"] == "manager@example.com"
```

Also assert malformed IDs return 422 JSON, store conflicts return 409 JSON or a
redirect with `identity_error`, and removal passes the posted version.

- [ ] **Step 5: Implement the focused mutation route and register it**

Create `routes/forklift_identities.py` with one POST endpoint. Parse and validate
`action`, `day`, IDs, and versions before entering `asyncio.to_thread()`. Read
the actor using `inbox_log.actor_from(request)` and use `"auth-disabled"` only
when tests explicitly run with `AUTH_DISABLED=1`; authenticated production
requests must have a real actor UPN. Return JSON for `Accept: application/json`
and otherwise redirect to the anchored Settings URL.

Use this route structure:

```python
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import forklift_identity_store, forklift_identity_view, inbox_log
from ..plant_day import today as plant_today


router = APIRouter()


def _wants_json(request: Request) -> bool:
    return (request.headers.get("accept") or "").startswith("application/json")


def _redirect(day: date, *, saved: bool = False, error: str = ""):
    query = {"section": "forklift", "identity_day": day.isoformat()}
    if saved:
        query["identity_saved"] = "1"
    if error:
        query["identity_error"] = error
    return RedirectResponse(
        url=f"/settings?{urlencode(query)}#forklift-identities", status_code=303
    )


def _error(request: Request, day: date, message: str, status_code: int):
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": message}, status_code=status_code)
    return _redirect(day, error=message)


def _optional_version(raw: object) -> int | None:
    value = str(raw or "").strip()
    if not value:
        return None
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("mapping version must be positive")
    return parsed


def _observed_source_name(context: dict, driver_id: str) -> str:
    unresolved = next(
        (row for row in context["unresolved"]
         if row["external_driver_id"] == driver_id),
        None,
    )
    if unresolved is not None:
        return " / ".join(unresolved["source_names"])
    current = next(
        (row for row in context["mappings"]
         if row["external_driver_id"] == driver_id),
        None,
    )
    if current is not None:
        return current["source_name"]
    raise ValueError("Choose an identity shown in Settings.")


@router.post("/settings/forklift-identities")
async def save_forklift_identity(request: Request):
    form = await request.form()
    today = plant_today()
    try:
        selected_day = date.fromisoformat(str(form.get("day") or ""))
    except ValueError:
        selected_day = today
        return _error(request, selected_day, "Choose a valid day.", 422)
    if selected_day > today:
        return _error(request, today, "Choose today or an earlier day.", 422)
    action = str(form.get("action") or "").strip()
    driver_id = str(form.get("external_driver_id") or "").strip()
    actor_upn, actor_name = inbox_log.actor_from(request)
    if not actor_upn and os.environ.get("AUTH_DISABLED") == "1":
        actor_upn = "auth-disabled"
    if not actor_upn:
        return _error(request, selected_day, "Sign in again before changing identities.", 401)
    try:
        version = _optional_version(form.get("expected_version"))
        if action == "save":
            employee_id = int(str(form.get("employee_odoo_id") or ""))
            identity_context = await asyncio.to_thread(
                forklift_identity_view.identity_context, selected_day
            )
            source_name = _observed_source_name(identity_context, driver_id)
            await asyncio.to_thread(
                forklift_identity_store.save_mapping,
                driver_id,
                source_name,
                employee_id,
                expected_version=version,
                actor_upn=actor_upn,
                actor_name=actor_name,
            )
            try:
                await asyncio.to_thread(
                    forklift_identity_view.identity_context, selected_day
                )
            except Exception:
                logging.warning(
                    "Forklift identity saved but immediate re-resolution failed",
                    exc_info=True,
                )
        elif action == "remove" and version is not None:
            await asyncio.to_thread(
                forklift_identity_store.remove_mapping,
                driver_id,
                expected_version=version,
                actor_upn=actor_upn,
                actor_name=actor_name,
            )
        else:
            raise ValueError("Choose a valid identity action.")
    except forklift_identity_store.MappingConflict as exc:
        return _error(request, selected_day, str(exc), 409)
    except (TypeError, ValueError):
        return _error(request, selected_day, "Choose a valid active employee.", 422)
    except Exception:
        logging.warning("Forklift identity change unavailable", exc_info=True)
        return _error(
            request, selected_day,
            "Forklift identities are unavailable right now. No change was made.",
            503,
        )
    if _wants_json(request):
        return JSONResponse({"ok": True})
    return _redirect(selected_day, saved=True)
```

Register the module in the existing parenthesized route import and alongside
the Settings router in `app.py`:

```python
from .routes import (
    forklift_identities,
    settings,
)

app.include_router(settings.router)
app.include_router(forklift_identities.router)
```

- [ ] **Step 6: Load identity context only for Forklift Settings**

Add these query parameters to `settings_page()`:

```python
identity_day: date | None = Query(default=None),
identity_saved: int = Query(default=0),
identity_error: str = Query(default=""),
```

When `section == "forklift"`, reject a future identity day, load
`forklift_identity_view.identity_context(identity_day or plant_today())`, and
catch source errors into a display-only unavailable message without breaking
the existing demand advisor. Add `forklift_identities`, `identity_saved`,
`identity_error`, and `today=plant_today().isoformat()` to the template context.

Use one captured plant day and this focused block:

```python
settings_today = plant_today()
forklift_identities_ctx: dict | None = None
if section == "forklift":
    selected_identity_day = identity_day or settings_today
    if selected_identity_day > settings_today:
        raise HTTPException(status_code=400, detail="Choose today or an earlier day")
    try:
        from .. import forklift_identity_view
        forklift_identities_ctx = forklift_identity_view.identity_context(
            selected_identity_day
        )
    except Exception:
        logging.warning("Forklift identity Settings context unavailable", exc_info=True)
        forklift_identities_ctx = {
            "day": selected_identity_day.isoformat(),
            "mappings": (),
            "unresolved": (),
            "employee_options": (),
            "unavailable": "Forklift identities are unavailable right now. Try again later.",
        }
```

Import `date` and `HTTPException`, and render `forklift_identities.unavailable`
as a `role="alert"` message before the identity lists when that key is present.

- [ ] **Step 7: Render explicit, reversible mapping forms**

Create `_settings_forklift_identities.html` with:

```html
<div id="forklift-identities" class="forklift-identities">
  <h3>Forklift identities</h3>
  <p class="help">Match an outside forklift driver ID to one active employee. Names are clues only; your choice is saved and recorded.</p>
  <form method="get" action="/settings" class="forklift-identity-day">
    <input type="hidden" name="section" value="forklift">
    <label>Calls from <input type="date" name="identity_day" value="{{ forklift_identities.day }}" max="{{ today }}"></label>
    <button type="submit">Show day</button>
  </form>
  {% if identity_error %}<p class="defaults-error" role="alert">{{ identity_error }}</p>{% endif %}
  {% if identity_saved %}<p class="saved-flash" role="status">Identity mapping saved.</p>{% endif %}
  {% if forklift_identities.unavailable is defined %}<p class="defaults-error" role="alert">{{ forklift_identities.unavailable }}</p>{% endif %}
  <h4>Needs a match</h4>
  <div class="forklift-identity-list">
    {% for row in forklift_identities.unresolved %}
    <article class="forklift-identity-card">
      <h5>{{ row.source_names|join(', ') or 'Name unavailable' }}</h5>
      <p><code>{{ row.external_driver_id }}</code> · {{ row.call_count }} call{% if row.call_count != 1 %}s{% endif %} · {{ row.first_call }} to {{ row.last_call }}</p>
      {% if row.name_conflict %}<p class="hint">This ID used more than one name. Choose the employee from the roster.</p>{% endif %}
      <form method="post" action="/settings/forklift-identities">
        <input type="hidden" name="action" value="save">
        <input type="hidden" name="external_driver_id" value="{{ row.external_driver_id }}">
        <input type="hidden" name="expected_version" value="">
        <input type="hidden" name="day" value="{{ forklift_identities.day }}">
        <label>Active employee
          <select name="employee_odoo_id" required>
            <option value="">Choose an employee</option>
            {% for person in forklift_identities.employee_options %}
            <option value="{{ person.employee_odoo_id }}">{{ person.employee_name }}</option>
            {% endfor %}
          </select>
        </label>
        <button type="submit">Save match</button>
      </form>
    </article>
    {% else %}<p class="help">No unmatched forklift identities for this day.</p>{% endfor %}
  </div>
  <h4>Current matches</h4>
  <div class="forklift-identity-list">
    {% for row in forklift_identities.mappings %}
    <article class="forklift-identity-card">
      <h5><code>{{ row.external_driver_id }}</code></h5>
      <p>Last name seen: {{ row.source_name or 'Name unavailable' }}</p>
      <p>Last changed {{ row.updated_at }} by {{ row.updated_by_upn }}</p>
      <form method="post" action="/settings/forklift-identities">
        <input type="hidden" name="action" value="save">
        <input type="hidden" name="external_driver_id" value="{{ row.external_driver_id }}">
        <input type="hidden" name="expected_version" value="{{ row.version }}">
        <input type="hidden" name="day" value="{{ forklift_identities.day }}">
        <label>Active employee
          <select name="employee_odoo_id" required>
            {% for person in forklift_identities.employee_options %}
            <option value="{{ person.employee_odoo_id }}" {% if person.employee_odoo_id == row.employee_odoo_id %}selected{% endif %}>{{ person.employee_name }}</option>
            {% endfor %}
          </select>
        </label>
        <button type="submit">Change match</button>
      </form>
      <form method="post" action="/settings/forklift-identities">
        <input type="hidden" name="action" value="remove">
        <input type="hidden" name="external_driver_id" value="{{ row.external_driver_id }}">
        <input type="hidden" name="expected_version" value="{{ row.version }}">
        <input type="hidden" name="day" value="{{ forklift_identities.day }}">
        <button type="submit" onclick="return confirm('Remove this forklift identity mapping?')">Remove match</button>
      </form>
    </article>
    {% else %}<p class="help">No saved forklift identity matches.</p>{% endfor %}
  </div>
</div>
```

Include the partial at the top of the existing Forklift panel, change its main
heading to “Forklift”, and keep “Demand Advisor” as a subsection so current
controls and tests remain.

- [ ] **Step 8: Add responsive Settings styles and render tests**

Add `.forklift-identities`, `.forklift-identity-list`, and
`.forklift-identity-card` styles using grid/flex wrapping, visible focus rings,
44px controls, and `overflow-wrap:anywhere` for external IDs. At widths below
760px, stack each mapping card's form fields and actions. Tests must assert
active employees only, version fields on current mappings, no raw event IDs,
the save/remove actions, and that the Demand Advisor and GOAT Score still
render.

Use this baseline CSS:

```css
.forklift-identities {
  margin-bottom: 1.5rem;
  padding-bottom: 1.25rem;
  border-bottom: 1px solid var(--border);
}
.forklift-identity-day,
.forklift-identity-card form {
  display: flex;
  align-items: end;
  flex-wrap: wrap;
  gap: .6rem;
}
.forklift-identity-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(22rem, 100%), 1fr));
  gap: .75rem;
}
.forklift-identity-card {
  min-width: 0;
  padding: .8rem;
  border: 1px solid var(--border);
  border-radius: .5rem;
}
.forklift-identity-card code { overflow-wrap: anywhere; }
.forklift-identity-card button,
.forklift-identity-card select,
.forklift-identity-day button,
.forklift-identity-day input { min-height: 44px; }
.forklift-identity-card :focus-visible,
.forklift-identity-day :focus-visible { outline: 3px solid var(--accent);outline-offset:2px; }
@media (max-width: 760px) {
  .forklift-identity-day,
  .forklift-identity-card form { align-items:stretch;flex-direction:column; }
}
```

- [ ] **Step 9: Run focused Settings tests**

Run: `.venv/bin/pytest tests/test_forklift_identity_settings.py tests/test_settings_forklift.py tests/test_settings_context.py -q`

Expected: PASS.

- [ ] **Step 10: Commit the Settings workflow**

```bash
git add src/zira_dashboard/app.py src/zira_dashboard/forklift_identity_view.py src/zira_dashboard/routes/forklift_identities.py src/zira_dashboard/routes/settings.py src/zira_dashboard/templates/_settings_forklift_identities.html src/zira_dashboard/templates/settings.html src/zira_dashboard/static/settings.css tests/test_forklift_identity_settings.py tests/test_settings_forklift.py
git commit -m "feat: review forklift identities in Settings"
```

### Task 5: Serve Safe Lazy Warning Details

**Files:**
- Modify: `src/zira_dashboard/people_performance_view.py:382-430`
- Modify: `src/zira_dashboard/routes/people_performance.py:1-110`
- Create: `src/zira_dashboard/templates/_people_performance_warning_panel.html`
- Modify: `src/zira_dashboard/templates/people_performance.html:7-18`
- Modify: `src/zira_dashboard/templates/_people_performance_rows.html:30-38`
- Modify: `tests/test_people_performance_view.py`
- Modify: `tests/test_people_performance_route.py`
- Modify: `tests/test_people_performance_template.py`

**Interfaces:**
- Consumes: Task 1 `DashboardWarning` records and action capabilities.
- Produces: `warning_summary_view()` and `warning_detail_context()` dictionaries.
- Produces: `GET /people-performance/warnings/{key}?day=YYYY-MM-DD` with response marker `warning-detail`.
- Produces: stable warning trigger attributes consumed by Task 6.

- [ ] **Step 1: Write failing warning presenter tests**

Add tests that assert summaries contain only list-safe fields and details format
timestamps in the plant timezone:

```python
def test_warning_summary_does_not_eagerly_expose_diagnostic_facts():
    warning = unmatched_warning_fixture(call_count=135)
    summary = warning_summary_view(warning)
    assert summary == {
        "key": warning.key,
        "kind": "unmatched_forklift_calls",
        "label": "Unmatched forklift calls: 135",
        "summary": "Forklift calls could not be matched to active employees.",
    }
    assert "facts" not in summary


def test_warning_detail_formats_safe_facts_actions_and_times():
    detail = warning_detail_context(unmatched_warning_fixture(call_count=135))
    assert detail["state"] == "open"
    assert detail["checked_at"] == "9:30 AM"
    assert detail["last_success_at"] == "9:30 AM"
    assert detail["facts"][0] == ("Unmatched calls", "135")
    assert detail["actions"][0]["action_id"] == "check_again"
```

- [ ] **Step 2: Run the presenter tests to verify the functions are missing**

Run: `.venv/bin/pytest tests/test_people_performance_view.py -q`

Expected: FAIL importing `warning_summary_view` and `warning_detail_context`.

- [ ] **Step 3: Add summary and detail presenters**

Implement these public functions in `people_performance_view.py`:

```python
def warning_summary_view(warning: DashboardWarning) -> dict:
    return {
        "key": warning.key,
        "kind": warning.kind,
        "label": warning.label,
        "summary": warning.summary,
    }


def warning_detail_context(warning: DashboardWarning | None) -> dict:
    if warning is None:
        return {
            "state": "cleared",
            "title": "Issue cleared",
            "summary": "Plant Manager checked again and this warning is no longer active.",
            "impact": "The People page now shows the latest available information.",
            "facts": (),
            "checked_at": "",
            "last_success_at": "",
            "actions": (),
        }
    return {
        "state": "open",
        "key": warning.key,
        "kind": warning.kind,
        "title": warning.title,
        "summary": warning.summary,
        "impact": warning.impact,
        "subject": warning.subject,
        "facts": warning.facts,
        "checked_at": _time(warning.checked_at_utc),
        "last_success_at": (
            _time(warning.last_success_at_utc)
            if warning.last_success_at_utc is not None else ""
        ),
        "actions": tuple(
            {"action_id": action.action_id, "label": action.label, "href": action.href}
            for action in warning.actions
        ),
    }
```

Change `dashboard_context()` to expose
`tuple(warning_summary_view(item) for item in model.source_warnings)` rather
than full warning objects.

- [ ] **Step 4: Write failing detail-route tests**

Add route cases for an active warning, a valid stale key, malformed key, future
day, no-store caching, response marker, and authentication classification:

```python
def test_warning_detail_returns_marked_no_store_partial(client, dashboard_loader):
    warning = busy_dashboard_model().source_warnings[0]
    response = client.get(
        f"/people-performance/warnings/{warning.key}?day={DAY.isoformat()}"
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-people-performance-response"] == "warning-detail"
    assert 'data-warning-state="open"' in response.text
    assert warning.title in response.text


def test_missing_warning_key_returns_cleared_partial(client, dashboard_loader):
    response = client.get(
        f"/people-performance/warnings/{'0' * 24}?day={DAY.isoformat()}"
    )
    assert response.status_code == 200
    assert 'data-warning-state="cleared"' in response.text
    assert "Issue cleared" in response.text
```

- [ ] **Step 5: Implement the authenticated detail route**

Add this route after the rows endpoint:

```python
@router.get("/people-performance/warnings/{warning_key_value}", response_class=HTMLResponse)
def people_performance_warning(
    request: Request,
    warning_key_value: str,
    day: date | None = Query(default=None),
):
    if re.fullmatch(r"[0-9a-f]{24}", warning_key_value) is None:
        raise HTTPException(status_code=400, detail="Invalid warning key")
    now_utc, today = _request_clock()
    selected = _selected_day(day, today=today)
    model = load_dashboard(selected, zira_client, now_utc=now_utc)
    warning = next(
        (item for item in model.source_warnings if item.key == warning_key_value),
        None,
    )
    response = templates.TemplateResponse(
        request,
        "_people_performance_warning_panel.html",
        {"warning": warning_detail_context(warning)},
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-People-Performance-Response"] = "warning-detail"
    return response
```

Import `re` and `warning_detail_context`. Add
`_is_bypass_path("/people-performance/warnings/" + "0" * 24) is False` to the
auth test.

- [ ] **Step 6: Render the complete panel partial and persistent host**

Create `_people_performance_warning_panel.html`:

```html
<section id="pp-warning-panel-content"
         data-warning-state="{{ warning.state }}"
         {% if warning.key is defined %}data-warning-key="{{ warning.key }}"{% endif %}
         aria-labelledby="pp-warning-title">
  <header>
    <h2 id="pp-warning-title">{{ warning.title }}</h2>
    <button type="button" data-pp-warning-close aria-label="Close warning details">×</button>
  </header>
  <p>{{ warning.summary }}</p>
  <p class="pp-warning-impact"><strong>People page impact:</strong> {{ warning.impact }}</p>
  {% if warning.facts %}
  <dl>
    {% for label, value in warning.facts %}<div><dt>{{ label }}</dt><dd>{{ value }}</dd></div>{% endfor %}
  </dl>
  {% endif %}
  {% if warning.checked_at %}<p class="pp-warning-time">Checked {{ warning.checked_at }}{% if warning.last_success_at %} · Last successful update {{ warning.last_success_at }}{% endif %}</p>{% endif %}
  {% if warning.actions %}
  <footer>
    {% for action in warning.actions %}
      {% if action.action_id == 'check_again' %}
      <button type="button" data-pp-warning-action="check_again">{{ action.label }}</button>
      {% else %}
      <a href="{{ action.href }}" data-pp-warning-action="{{ action.action_id }}">{{ action.label }}</a>
      {% endif %}
    {% endfor %}
  </footer>
  {% endif %}
</section>
```

Add the persistent non-modal host immediately after the live partial include in
`people_performance.html`:

```html
<div id="pp-warning-popover" class="pp-warning-popover" role="region"
     aria-label="Warning details" hidden></div>
<span id="pp-action-status" class="sr-only" aria-live="polite" aria-atomic="true"></span>
```

Remove `aria-live="polite"` from the visible `#pp-live-status`; JavaScript will
continue updating its text, while only meaningful manual results are sent to
the dedicated action live region.

- [ ] **Step 7: Render structured warning buttons in the live partial**

Replace each warning span with:

```html
<button type="button" class="pp-warning-trigger"
        data-warning-key="{{ warning.key }}"
        data-warning-kind="{{ warning.kind }}"
        data-warning-summary="{{ warning.summary|e }}"
        aria-expanded="false"
        aria-controls="pp-warning-popover">
  <span aria-hidden="true">!</span>{{ warning.label }}
</button>
```

Keep the `<aside role="status">`, but add `aria-live="off"` so routine polling
does not re-announce unchanged warnings.

- [ ] **Step 8: Run presenter, route, and template tests**

Run: `.venv/bin/pytest tests/test_people_performance_view.py tests/test_people_performance_route.py tests/test_people_performance_template.py -q`

Expected: PASS. The detail partial must contain only actions supplied by the
warning record.

- [ ] **Step 9: Commit the lazy detail endpoint**

```bash
git add src/zira_dashboard/people_performance_view.py src/zira_dashboard/routes/people_performance.py src/zira_dashboard/templates/_people_performance_warning_panel.html src/zira_dashboard/templates/people_performance.html src/zira_dashboard/templates/_people_performance_rows.html tests/test_people_performance_view.py tests/test_people_performance_route.py tests/test_people_performance_template.py
git commit -m "feat: explain People source warnings"
```

### Task 6: Add Count and Warning Browser Interactions

**Files:**
- Modify: `src/zira_dashboard/static/people-performance.js:12-590`
- Modify: `src/zira_dashboard/static/people-performance.css:53-145,522-545`
- Modify: `tests/test_people_performance_static.py`

**Interfaces:**
- Consumes: Task 2 live-root filter attributes and Task 5 warning/detail markup.
- Produces: URL navigation for count buttons and a separate non-modal warning-panel controller inside `createPeoplePerformanceController()`.
- Preserves: existing interval tooltip behavior, row refresh validation, scroll synchronization, focus restoration, and teardown.

- [ ] **Step 1: Extend the Node harness with failing count-filter cases**

Add mock count controls with `data-pp-count-filter`, `data-filter-value`, and
`aria-pressed`. Emit click events and assert exact navigation targets:

```javascript
function makeCountControl(kind, value, pressed) {
  return {
    disabled: false,
    dataset: {ppCountFilter: kind, filterValue: value},
    closest(selector) {
      return selector === '[data-pp-count-filter]' ? this : null;
    },
    getAttribute(name) {
      return name === 'aria-pressed' ? String(pressed) : null;
    },
  };
}
function expectLastNavigation(env, expected) {
  const actual = env.navigations[env.navigations.length - 1];
  if (JSON.stringify(actual) !== JSON.stringify(['assign', expected])) {
    throw new Error('wrong count-filter navigation: ' + JSON.stringify(actual));
  }
}

const filterEnv = makeEnvironment('1');
const filterController = makeController(filterEnv.document, filterEnv.windowObject);
filterController.init();
filterEnv.document.rows.dataset.status = '';
filterEnv.document.emit('click', event(makeCountControl('status', 'working', false)));
expectLastNavigation(filterEnv, '/people-performance?day=2026-08-28&status=working');

filterEnv.document.rows.dataset.status = 'working';
filterEnv.document.rows.dataset.attention = '1';
filterEnv.document.emit('click', event(makeCountControl('status', 'working', true)));
expectLastNavigation(filterEnv, '/people-performance?day=2026-08-28&attention=1');

filterEnv.document.emit('click', event(makeCountControl('attention', '1', true)));
expectLastNavigation(filterEnv, '/people-performance?day=2026-08-28&status=working');
filterController.destroy();
```

Also assert the polling URL contains both active query values and rollover
navigation preserves both.

- [ ] **Step 2: Extend the Node harness with failing warning-panel cases**

Mock `#pp-warning-popover`, warning triggers, marked detail responses, and a
close/check-again button. Cover hover preview, focus preview, click-to-pin,
one-open-at-a-time, outside click, Escape with focus restoration, successful
detail loading, missing/wrong response markers, retry, stale request ordering,
double-check deduplication, issue-cleared behavior, polling trigger replacement,
and `destroy()` aborting both row and warning requests.

Use a detail response helper whose headers return `warning-detail` for
`X-People-Performance-Response`; a missing marker must show the retry state and
must not inject the response body.

- [ ] **Step 3: Run the runtime harness to prove the interactions are absent**

Run: `.venv/bin/pytest tests/test_people_performance_static.py::test_controller_runtime_handles_details_races_navigation_and_teardown -q`

Expected: FAIL on the first missing count navigation or warning panel assertion.

- [ ] **Step 4: Implement URL construction for count controls**

Add a selector and these helpers without changing date auto-submit behavior:

```javascript
function countControlFor(node) {
  return node && node.closest ? node.closest('[data-pp-count-filter]') : null;
}

function peopleUrl(day, status, attention) {
  var params = ['day=' + encodeURIComponent(day)];
  if (status) params.push('status=' + encodeURIComponent(status));
  if (attention) params.push('attention=1');
  return '/people-performance?' + params.join('&');
}

function activateCountFilter(control) {
  var rows = document.getElementById('people-performance-live');
  if (!rows || control.disabled) return;
  var status = rows.dataset.status || '';
  var attention = rows.dataset.attention === '1';
  var pressed = control.getAttribute('aria-pressed') === 'true';
  if (control.dataset.ppCountFilter === 'status') {
    status = pressed ? '' : control.dataset.filterValue;
  } else if (control.dataset.ppCountFilter === 'attention') {
    attention = !pressed;
  }
  windowObject.location.assign(peopleUrl(rows.dataset.day, status, attention));
}
```

In the click handler, process count controls before interval triggers. Native
button keyboard activation supplies Enter/Space clicks; do not add duplicate
keydown navigation.

- [ ] **Step 5: Add separate warning-panel state and safe DOM helpers**

Declare separate warning state:

```javascript
var warningTrigger = null;
var warningKey = null;
var warningPinned = false;
var warningPanel = null;
var warningRequestController = null;
var warningRequestEpoch = 0;
var warningCheckPromise = null;
```

`ensureWarningPanel()` must find the server-rendered host. Build preview/loading/
error/cleared content with `document.createElement()` plus `textContent`; never
concatenate source strings into `innerHTML`. Loaded HTML may be adopted only
after parsing and finding `#pp-warning-panel-content` in a response carrying the
correct marker.

Use these safe helpers:

```javascript
function ensureWarningPanel() {
  if (!warningPanel) warningPanel = document.getElementById('pp-warning-popover');
  return warningPanel;
}

function renderWarningMessage(message, busy) {
  var panel = ensureWarningPanel();
  var content = document.createElement('section');
  var heading = document.createElement('h2');
  var body = document.createElement('p');
  heading.textContent = busy ? 'Checking warning' : 'Warning details';
  body.textContent = message;
  content.appendChild(heading);
  content.appendChild(body);
  panel.replaceChildren(content);
  panel.hidden = false;
  panel.setAttribute('aria-busy', busy ? 'true' : 'false');
}

function renderWarningError() {
  var panel = ensureWarningPanel();
  var content = document.createElement('section');
  var message = document.createElement('p');
  var retry = document.createElement('button');
  message.textContent = 'Details could not be loaded.';
  retry.type = 'button';
  retry.textContent = 'Retry';
  retry.setAttribute('data-pp-warning-action', 'retry');
  content.appendChild(message);
  content.appendChild(retry);
  panel.replaceChildren(content);
  panel.hidden = false;
  panel.setAttribute('aria-busy', 'false');
}

function renderWarningCheckFailure() {
  var panel = ensureWarningPanel();
  var content = document.createElement('section');
  var message = document.createElement('p');
  var retry = document.createElement('button');
  message.textContent = 'The check could not finish.';
  retry.type = 'button';
  retry.textContent = 'Try again';
  retry.setAttribute('data-pp-warning-action', 'check_again');
  content.appendChild(message);
  content.appendChild(retry);
  panel.replaceChildren(content);
  panel.hidden = false;
  panel.setAttribute('aria-busy', 'false');
}
```

- [ ] **Step 6: Implement warning positioning, loading, and close behavior**

Use the interval popover's clamp/flip geometry but position the warning panel
against `.pp-warning-trigger`. The public internal flow is:

```javascript
function previewWarning(trigger) {
  if (warningPinned) return;
  warningTrigger = trigger;
  warningKey = trigger.dataset.warningKey;
  renderWarningMessage(trigger.dataset.warningSummary, false);
  positionWarning(trigger);
}

function pinWarning(trigger) {
  if (warningPinned && warningTrigger === trigger) {
    closeWarning(true);
    return Promise.resolve(false);
  }
  warningPinned = true;
  warningTrigger = trigger;
  warningKey = trigger.dataset.warningKey;
  trigger.setAttribute('aria-expanded', 'true');
  renderWarningMessage('Loading warning details…', true);
  positionWarning(trigger);
  return loadWarningDetail(warningKey, trigger, false);
}
```

`loadWarningDetail()` requests
`/people-performance/warnings/{encoded-key}?day={encoded-day}` with `no-store`,
rejects redirects/auth failures/missing markers, ignores stale epochs, and
shows “Details could not be loaded.” plus a Retry button on ordinary failure.
Its third `announceResult` boolean is false for opening/polling and true only
after Check again. When true, inspect the adopted content's
`data-warning-state`: announce “Issue cleared.” for `cleared`, otherwise
announce “The warning is still active.”
`closeWarning(restoreFocus)` aborts its request, clears expanded state, hides
the panel, and optionally focuses the trigger without scrolling.

- [ ] **Step 7: Implement Check again and polling restoration**

The delegated panel click handler calls this exact single-flight shape:

```javascript
function checkWarningAgain(button) {
  if (warningCheckPromise) return warningCheckPromise;
  button.disabled = true;
  button.textContent = 'Checking…';
  var checkingKey = warningKey;
  warningCheckPromise = refreshRows().then(function (refreshed) {
    if (!refreshed) {
      renderWarningCheckFailure();
      announceAction('The check could not finish.');
      return false;
    }
    var replacement = warningTriggerForKey(checkingKey);
    if (replacement) return pinWarningReplacement(replacement, checkingKey, true);
    return loadWarningDetail(checkingKey, null, true);
  }).finally(function () {
    warningCheckPromise = null;
  });
  return warningCheckPromise;
}

function warningTriggerForKey(key) {
  return document.querySelector(
    '.pp-warning-trigger[data-warning-key="' + escapeSelectorValue(key) + '"]'
  );
}

function pinWarningReplacement(trigger, key, announceResult) {
  warningTrigger = trigger;
  warningKey = key;
  warningPinned = true;
  trigger.setAttribute('aria-expanded', 'true');
  positionWarning(trigger);
  return loadWarningDetail(key, trigger, Boolean(announceResult));
}
```

Extend `captureState()` with `warningKey` only when pinned. After a row swap,
restore the new matching trigger and reload its details. If no trigger remains,
leave the panel visible and request the same key so the server returns the
truthful cleared state. Do not announce unchanged background polls.

Add this helper and call it only for manual check failure, issue-remains, and
issue-cleared results—not for the 30-second timer:

```javascript
function announceAction(message) {
  var status = document.getElementById('pp-action-status');
  if (status && status.textContent !== message) status.textContent = message;
}
```

Build refresh URLs from `data-day`, `data-status`, and `data-attention`. On plant
day rollover, preserve both active filters in the navigation target. A manual
check may supersede an older row poll, but repeated Check again presses share
one promise.

- [ ] **Step 8: Wire pointer, focus, click, Escape, outside-click, and teardown**

Add delegated handling with these rules:

- pointer over or focus previews only when no warning is pinned;
- leaving both trigger and panel closes only an unpinned preview;
- clicking a warning pins it and closes any previously pinned warning;
- clicking `[data-pp-warning-close]`, the active trigger, or outside closes;
- clicking `data-pp-warning-action="retry"` calls
  `loadWarningDetail(warningKey, warningTrigger, false)`, while
  `data-pp-warning-action="check_again"` calls `checkWarningAgain()`;
- Escape closes the warning first and restores its trigger, otherwise preserves
  existing interval-popover Escape behavior;
- panel links navigate normally; and
- `destroy()` aborts warning/detail requests, removes new listeners, clears
  state, and leaves the server-owned warning host in the document.

- [ ] **Step 9: Style actionable warnings and the anchored panel**

Replace `.pp-source-warnings span` with `.pp-warning-trigger`. Give it inherited
font, `min-height:44px`, warning icon spacing, pointer cursor, visible focus,
and an expanded treatment. Add:

```css
.pp-warning-popover {
  position: absolute;
  z-index: 1050;
  width: min(26rem, calc(100vw - 1rem));
  max-height: min(34rem, calc(100vh - 1rem));
  overflow: auto;
  padding: .8rem;
  border: 1px solid #fdba74;
  border-radius: 10px;
  background: var(--panel);
  box-shadow: 0 16px 40px rgba(15, 23, 42, .28);
  color: var(--fg);
}
.pp-warning-popover[hidden] { display: none; }
.pp-warning-popover header,
.pp-warning-popover footer { display:flex;align-items:center;gap:.5rem;flex-wrap:wrap; }
.pp-warning-popover header { justify-content:space-between; }
.pp-warning-popover dl { display:grid;gap:.35rem;margin:.65rem 0; }
.pp-warning-popover dl div { display:grid;grid-template-columns:minmax(8rem,auto) 1fr;gap:.6rem; }
```

Style links and buttons as visible actions, destructive-free, with 44px targets.

- [ ] **Step 10: Run the full static interaction suite**

Run: `.venv/bin/pytest tests/test_people_performance_static.py -q`

Expected: PASS with no Node stderr and no regression to interval details,
request races, focus restoration, or teardown.

- [ ] **Step 11: Commit browser interactions**

```bash
git add src/zira_dashboard/static/people-performance.js src/zira_dashboard/static/people-performance.css tests/test_people_performance_static.py
git commit -m "feat: interact with People status controls"
```

### Task 7: Verify Responsive People Behavior With the Busy Preview

**Files:**
- Modify: `scripts/preview_people_performance.py:500-545`
- Modify: `tests/test_preview_people_performance.py`
- Modify: `src/zira_dashboard/static/people-performance.css`

**Interfaces:**
- Consumes: complete People and Settings markup/CSS/JS from Tasks 2-6.
- Produces: deterministic busy-page coverage at `(1440,900)`, `(1195,768)`, `(1024,768)`, `(768,1024)`, and `(390,844)`.

- [ ] **Step 1: Update the preview fixture to the structured summary shape**

Replace string warnings in `_context()` with three dictionaries matching
`warning_summary_view()`:

```python
"source_warnings": (
    {"key": "111111111111111111111111", "kind": "production_metric_unavailable",
     "label": "Production metric unavailable: Trim Saw 1",
     "summary": "Trim Saw 1 production could not be calculated."},
    {"key": "222222222222222222222222", "kind": "production_metric_unavailable",
     "label": "Production metric unavailable: Hand Build #1",
     "summary": "Hand Build #1 production could not be calculated."},
    {"key": "333333333333333333333333", "kind": "unmatched_forklift_calls",
     "label": "Unmatched forklift calls: 107",
     "summary": "Forklift calls could not be matched to active employees."},
),
```

- [ ] **Step 2: Add failing five-width interaction and geometry checks**

In the preview init script, intercept warning-detail requests and return a
marked HTML partial. At each existing viewport:

1. assert count and warning elements are buttons;
2. focus and click the first warning;
3. wait for `data-warning-state="open"`;
4. assert the panel is within the viewport and does not cover its trigger;
5. assert its actions have at least 44px height;
6. press Escape and verify the panel hides and focus returns;
7. assert selected count state is visible without relying on color; and
8. at widths up to 760px, assert panel facts and actions each stack into one
   column; and
9. retain every existing manager-strip overlap/containment assertion.

Use this deterministic fetch response:

```javascript
window.gpiFetch = async function (url) {
  if (!url.includes('/people-performance/warnings/')) throw new Error('unexpected preview request');
  return new Response(
    '<section id="pp-warning-panel-content" data-warning-state="open" data-warning-key="111111111111111111111111" aria-labelledby="pp-warning-title">' +
    '<header><h2 id="pp-warning-title">Trim Saw 1 production is unavailable</h2><button type="button" data-pp-warning-close aria-label="Close warning details">×</button></header>' +
    '<p>Trim Saw 1 production could not be calculated.</p><p class="pp-warning-impact"><strong>People page impact:</strong> Production is hidden.</p>' +
    '<footer><button type="button" data-pp-warning-action="check_again">Check again</button><a href="/wc/trim-saw-1?day=2026-09-02">Open work center dashboard</a></footer></section>',
    {status: 200, headers: {'X-People-Performance-Response': 'warning-detail'}}
  );
};
```

- [ ] **Step 3: Run the preview tests to expose any layout defects**

Run: `.venv/bin/pytest tests/test_preview_people_performance.py -q`

Expected: FAIL at the 760px-and-narrower assertion because the panel fact/action
rows have not yet received their mobile stacking rule.

- [ ] **Step 4: Add the required narrow-panel stacking rules**

Add these rules inside the existing 760px media query. Do not introduce
page-level horizontal scrolling or fixed manager-strip heights.

```css
.pp-warning-popover dl div {
  grid-template-columns: minmax(0, 1fr);
  gap: .1rem;
}
.pp-warning-popover footer > a,
.pp-warning-popover footer > button {
  flex: 1 1 100%;
  justify-content: center;
}
```

Re-run the two failing mobile parameters:
`.venv/bin/pytest tests/test_preview_people_performance.py -q -k '768 or 390'`.

- [ ] **Step 5: Run preview, template, static, and Settings integration tests**

Run: `.venv/bin/pytest tests/test_preview_people_performance.py tests/test_people_performance_template.py tests/test_people_performance_static.py tests/test_forklift_identity_settings.py tests/test_settings_forklift.py -q`

Expected: PASS at all five viewport sizes with no browser console or page errors.

- [ ] **Step 6: Commit responsive integration coverage**

```bash
git add scripts/preview_people_performance.py tests/test_preview_people_performance.py src/zira_dashboard/static/people-performance.css
git commit -m "test: cover actionable People status layouts"
```

### Task 8: Run Regression Gates and Publish the User-Facing Change

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: all implementation tasks.
- Produces: verified release note and a main-branch-ready implementation series.

- [ ] **Step 1: Run all focused People and forklift identity tests**

Run:

```bash
.venv/bin/pytest \
  tests/test_people_performance_warnings.py \
  tests/test_people_performance_data.py \
  tests/test_people_performance_rows.py \
  tests/test_people_performance_view.py \
  tests/test_people_performance_route.py \
  tests/test_people_performance_template.py \
  tests/test_people_performance_static.py \
  tests/test_preview_people_performance.py \
  tests/test_forklift_identity.py \
  tests/test_forklift_identity_store.py \
  tests/test_forklift_identity_settings.py \
  tests/test_settings_forklift.py -q
```

Expected: PASS; database-only tests may be SKIPPED only when `DATABASE_URL` is
unset.

- [ ] **Step 2: Run lint on every changed Python file**

Run:

```bash
.venv/bin/ruff check \
  src/zira_dashboard/_schema.py \
  src/zira_dashboard/app.py \
  src/zira_dashboard/people_performance_warnings.py \
  src/zira_dashboard/forklift_identity_store.py \
  src/zira_dashboard/forklift_identity_view.py \
  src/zira_dashboard/routes/forklift_identities.py \
  src/zira_dashboard/people_performance.py \
  src/zira_dashboard/people_performance_data.py \
  src/zira_dashboard/people_performance_view.py \
  src/zira_dashboard/routes/people_performance.py \
  src/zira_dashboard/routes/settings.py \
  src/zira_dashboard/forklift_store.py \
  scripts/preview_people_performance.py \
  tests/people_performance_fixtures.py \
  tests/test_people_performance_warnings.py \
  tests/test_people_performance_data.py \
  tests/test_people_performance_rows.py \
  tests/test_people_performance_view.py \
  tests/test_people_performance_route.py \
  tests/test_people_performance_template.py \
  tests/test_people_performance_static.py \
  tests/test_preview_people_performance.py \
  tests/test_forklift_identity_store.py \
  tests/test_forklift_identity.py \
  tests/test_forklift_identity_settings.py \
  tests/test_settings_forklift.py
```

Expected: `All checks passed!`

- [ ] **Step 3: Run the complete test suite**

Run: `.venv/bin/pytest -q`

Expected: PASS with only the repository's documented skips.

- [ ] **Step 4: Perform the final manual acceptance pass**

Open a busy current People day and verify:

- each nonzero count filters and clears as designed;
- working/earlier remain mutually exclusive and attention combines with either;
- a zero count is visible but disabled and the clear-filters link escapes a
  bookmarked empty result;
- each warning previews on hover/focus and pins on click/tap;
- production warnings show only relevant destinations;
- unmatched calls open the focused identity Settings subsection;
- save, change, remove, conflict, and stale-version mapping paths are truthful;
- Check again reports cleared, remains, and failed states accurately;
- polling preserves the selected date, filters, open panel, focus, and scroll;
- keyboard and touch operation work at phone and desktop widths; and
- no manager-strip or panel content overlaps or creates page-level horizontal
  scrolling.

Expected: every item passes before the implementation is called complete.

- [ ] **Step 5: Add the child-readable implementation changelog entry**

At the top of `## 2026-09-02`, add:

```markdown
### Use People page totals and warnings

- **The People page totals can now filter the list.** Warning buttons explain what went wrong and show a safe next step. You can also match unknown forklift drivers to the right active employee in Settings, and every change is recorded.
```

- [ ] **Step 6: Commit the release note**

```bash
git add CHANGELOG.md
git commit -m "docs: explain actionable People status controls"
```

- [ ] **Step 7: Review the final diff and push implementation commits**

Run:

```bash
git status --short
git diff origin/main...HEAD --check
git log --oneline origin/main..HEAD
git push origin main
```

Expected: the diff check is silent, the log contains only this implementation's
reviewed commits, the push succeeds, and unrelated pre-existing worktree files
remain untouched.
