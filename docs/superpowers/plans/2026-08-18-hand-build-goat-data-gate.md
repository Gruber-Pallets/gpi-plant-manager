# Hand Build GOAT 30-Day Data Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide every Hand Build GOAT result until the category has 30 distinct positive-production workdays, while leaving Hand Build production rankings, history, and ribbons unchanged.

**Architecture:** Store the minimum data-day policy on `GoatCategory` and expose pure helpers that resolve category aliases and count distinct positive-production dates for that category's work centers. Reuse that readiness decision before the New-Leaderboard computes a GOAT, before global badges apply an override, and before notification finalization creates an alert.

**Tech Stack:** Python 3.11+, FastAPI, PostgreSQL-backed `production_daily`, pytest, Ruff.

## Global Constraints

- Hand Build requires 30 distinct dates with positive attributed production.
- Multiple employees or Hand Build stations on the same date count as one day.
- Zero-production dates do not count.
- The gate covers New-Leaderboard GOAT chips, global GOAT badges, GOAT alerts, and Slack celebrations.
- A manual override cannot bypass the gate.
- Production dashboards, history, family rankings, player cards, and Gold Ribbons remain unchanged.
- Existing GOAT categories retain their current effective one-positive-day behavior.
- No schema migration, manual feature flag, or production-data rewrite is introduced.
- New `CHANGELOG.md` copy must use short, common words a 10-year-old can understand.

---

## File map

- Modify `src/zira_dashboard/goat_categories.py`: category policy, Hand Build group alias, and pure readiness helpers.
- Modify `src/zira_dashboard/routes/new_leaderboard.py`: gate current Hand Build GOAT calculation before overrides.
- Modify `src/zira_dashboard/awards.py`: gate global Hand Build badges before `goat()` and override application.
- Modify `src/zira_dashboard/goat_notifications.py`: gate Hand Build alert creation before record comparison and persistence.
- Modify `tests/test_goat_categories.py`: 29/30-day, duplicate-date, zero-unit, alias, and default-category policy coverage.
- Modify `tests/test_new_leaderboard_routes.py`: current-GOAT chip suppression and activation coverage.
- Modify `tests/test_goat_holders_map.py`: badge and manual-override suppression coverage.
- Modify `tests/test_goat_notifications.py`: no pre-threshold alert coverage.
- Modify `tests/test_hand_build_1_zira_mapping.py`: preserve metering assertions while documenting that GOAT readiness is delayed.
- Modify `CHANGELOG.md`: user-facing explanation of the 30-day wait.

---

### Task 1: Add the category-level readiness policy

**Files:**
- Modify: `tests/test_goat_categories.py`
- Modify: `tests/test_hand_build_1_zira_mapping.py`
- Modify: `src/zira_dashboard/goat_categories.py`

**Interfaces:**
- Produces: `GoatCategory.minimum_data_days: int`
- Produces: `GoatCategory.group_aliases: tuple[str, ...]`
- Produces: `category_for_group_name(group_name: str) -> GoatCategory | None`
- Produces: `positive_data_days(category: GoatCategory, records: list[dict]) -> set[date]`
- Produces: `is_goat_ready(category: GoatCategory, records: list[dict]) -> bool`

- [ ] **Step 1: Write failing category-policy tests**

Add tests that construct 29 positive Hand Build dates, add duplicate employee
rows and a zero-unit date, then append the 30th distinct positive date:

```python
from datetime import date, timedelta


def _hand_build_records(day_count: int) -> list[dict]:
    start = date(2026, 7, 1)
    rows = []
    for offset in range(day_count):
        day = start + timedelta(days=offset)
        rows.extend([
            {"day": day, "person": "Builder A", "wc": "Hand Build #1", "units": 100},
            {"day": day, "person": "Builder B", "wc": "Hand Build #1", "units": 100},
        ])
    return rows


def test_hand_build_goat_waits_for_30_distinct_positive_days(monkeypatch):
    category = goat_categories.category_for_key("hand_build")
    monkeypatch.setattr(
        goat_categories,
        "work_center_names",
        lambda _: {"Hand Build #1", "Big Build #1"},
    )
    records = _hand_build_records(29)
    records.extend([
        {"day": date(2026, 7, 1), "person": "Builder C", "wc": "Big Build #1", "units": 50},
        {"day": date(2026, 8, 15), "person": "Builder A", "wc": "Hand Build #1", "units": 0},
        {"day": date(2026, 8, 15), "person": "Repairer", "wc": "Repair 1", "units": 900},
    ])

    assert category.minimum_data_days == 30
    assert goat_categories.is_goat_ready(category, records) is False

    records.append({
        "day": date(2026, 8, 16),
        "person": "Builder A",
        "wc": "Big Build #1",
        "units": 1,
    })
    assert goat_categories.is_goat_ready(category, records) is True
```

Also assert alias resolution and unchanged defaults:

```python
def test_goat_category_group_aliases_and_default_minimums():
    hand_build = goat_categories.category_for_key("hand_build")
    assert goat_categories.category_for_group_name("Hand Build") == hand_build
    assert goat_categories.category_for_group_name("Hand Builds") == hand_build
    assert goat_categories.category_for_group_name("not-a-goat-group") is None
    assert goat_categories.category_for_key("repairs").minimum_data_days == 1
```

Update the Hand Build integration test to assert the meter remains active while
the category policy requires 30 days:

```python
category = goat_categories.category_for_key("hand_build")
assert goat_categories.has_metered_source(category) is True
assert category.minimum_data_days == 30
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest \
  tests/test_goat_categories.py \
  tests/test_hand_build_1_zira_mapping.py -q
```

Expected: FAIL because `minimum_data_days`, `group_aliases`,
`category_for_group_name`, and `is_goat_ready` do not exist.

- [ ] **Step 3: Implement the minimal category policy**

Extend the immutable category definition without breaking existing positional
constructors:

```python
@dataclass(frozen=True)
class GoatCategory:
    key: str
    label: str
    leaderboard_label: str
    group_name: str | None = None
    skill: str | None = None
    minimum_data_days: int = 1
    group_aliases: tuple[str, ...] = ()
```

Configure Hand Build explicitly:

```python
GoatCategory(
    "hand_build",
    "Hand Build",
    "Hand Build GOAT",
    skill="Hand Build",
    minimum_data_days=30,
    group_aliases=("Hand Builds",),
),
```

Add pure lookup/count helpers:

```python
from datetime import date


def category_for_group_name(group_name: str) -> GoatCategory | None:
    for category in _CATEGORIES:
        names = {category.label, *category.group_aliases}
        if category.group_name:
            names.add(category.group_name)
        if group_name in names:
            return category
    return None


def positive_data_days(category: GoatCategory, records: list[dict]) -> set[date]:
    names = work_center_names(category)
    return {
        record["day"]
        for record in records
        if record.get("wc") in names and float(record.get("units") or 0) > 0
    }


def is_goat_ready(category: GoatCategory, records: list[dict]) -> bool:
    return len(positive_data_days(category, records)) >= category.minimum_data_days
```

- [ ] **Step 4: Run tests and verify GREEN**

Run the Task 1 command again.

Expected: all selected tests pass.

---

### Task 2: Gate New-Leaderboard GOAT chips before overrides

**Files:**
- Modify: `tests/test_new_leaderboard_routes.py`
- Modify: `src/zira_dashboard/routes/new_leaderboard.py`

**Interfaces:**
- Consumes: `goat_categories.category_for_group_name()`
- Consumes: `goat_categories.is_goat_ready()`
- Preserves: `_leaderboard_payload(today: date) -> dict`

- [ ] **Step 1: Write failing route tests**

Create a payload fixture whose only active family is Hand Build. Stub the
normalized records with 29 distinct positive dates and assert that the GOAT
calculator is not called and `current_goats` stays empty. Then supply 30 dates
and assert the existing GOAT result appears:

```python
def test_hand_build_goat_chip_waits_for_30_positive_days(monkeypatch):
    from datetime import timedelta
    from zira_dashboard.routes import new_leaderboard

    payload = fake_payload()
    payload["active_families"] = ["Hand Build"]
    payload["current_goats"] = []
    payload["families"]["Hand Build"] = payload["families"]["Juniors"]
    records = [
        {
            "day": date(2026, 7, 1) + timedelta(days=offset),
            "person": "Builder",
            "wc": "Hand Build #1",
            "units": 100,
            "hours": 7,
        }
        for offset in range(29)
    ]
    calls = []
    monkeypatch.setattr(new_leaderboard.production_history, "normalized_daily_records", lambda *_: records)
    monkeypatch.setattr(new_leaderboard.production_metrics, "build_family_leaderboard", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(new_leaderboard.awards, "load_overrides", lambda: [])
    monkeypatch.setattr(new_leaderboard.awards, "goat_for_wc_names", lambda *_args, **_kwargs: calls.append(True))

    data = new_leaderboard._leaderboard_payload(date(2026, 8, 18))

    assert data["current_goats"] == []
    assert calls == []
```

Add a second test with 30 dates and a concrete winner to prove the boundary
activates the chip:

```python
def test_hand_build_goat_chip_activates_on_day_30(monkeypatch):
    from datetime import timedelta
    from zira_dashboard.routes import new_leaderboard

    payload = fake_payload()
    payload["active_families"] = ["Hand Build"]
    payload["current_goats"] = []
    payload["families"]["Hand Build"] = payload["families"]["Juniors"]
    records = [
        {
            "day": date(2026, 7, 1) + timedelta(days=offset),
            "person": "Builder",
            "wc": "Hand Build #1",
            "units": 100 + offset,
            "hours": 7,
        }
        for offset in range(30)
    ]
    monkeypatch.setattr(new_leaderboard.production_history, "normalized_daily_records", lambda *_: records)
    monkeypatch.setattr(new_leaderboard.production_metrics, "build_family_leaderboard", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(new_leaderboard.awards, "load_overrides", lambda: [])
    monkeypatch.setattr(
        new_leaderboard.awards,
        "goat_for_wc_names",
        lambda *_args, **_kwargs: {
            "name": "Builder",
            "units": 129,
            "day": records[-1]["day"],
        },
    )

    data = new_leaderboard._leaderboard_payload(date(2026, 8, 18))

    assert data["current_goats"] == [{
        "label": "Hand Build GOAT",
        "group": "Hand Build",
        "name": "Builder",
        "units": 129,
        "day": records[-1]["day"],
    }]
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest \
  tests/test_new_leaderboard_routes.py::test_hand_build_goat_chip_waits_for_30_positive_days -q
```

Expected: FAIL because the current route calls `goat_for_wc_names` immediately.

- [ ] **Step 3: Add the route gate**

Before calling `awards.goat_for_wc_names`, resolve the active family's
category and skip GOAT calculation when it is not ready:

```python
category = goat_categories.category_for_group_name(family)
if category is not None and not goat_categories.is_goat_ready(category, records):
    continue
```

Keep this check before `goat_for_wc_names`, which also keeps a manual override
from bypassing the waiting period.

- [ ] **Step 4: Run route tests and verify GREEN**

Run:

```bash
DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest tests/test_new_leaderboard_routes.py -q
```

Expected: all New-Leaderboard route tests pass.

---

### Task 3: Gate global Hand Build GOAT badges

**Files:**
- Modify: `tests/test_goat_holders_map.py`
- Modify: `src/zira_dashboard/awards.py`

**Interfaces:**
- Consumes: `goat_categories.category_for_group_name()`
- Consumes: `goat_categories.is_goat_ready()`
- Preserves: `goat_holders_map() -> dict[str, list[str]]`

- [ ] **Step 1: Write a failing override-suppression test**

Stub the registered group as `Hand Builds`, return a replacement override, and
provide only 29 positive Hand Build dates. Assert there is no holder and that
`goat()` is never called:

```python
def test_hand_build_badge_and_override_wait_for_30_days(monkeypatch):
    from datetime import date, timedelta
    from zira_dashboard import awards, goat_categories, production_history, work_centers_store

    awards._GOAT_HOLDERS_CACHE.clear()
    goat_calls = []
    monkeypatch.setattr(work_centers_store, "registered_groups", lambda: ["Hand Builds"])
    monkeypatch.setattr(goat_categories, "work_center_names", lambda _: {"Hand Build #1"})
    monkeypatch.setattr(
        production_history,
        "daily_records",
        lambda *_: [
            {
                "day": date(2026, 7, 1) + timedelta(days=offset),
                "person": "Builder",
                "wc": "Hand Build #1",
                "units": 100,
                "hours": 7,
            }
            for offset in range(29)
        ],
    )
    monkeypatch.setattr(awards, "goat", lambda group: goat_calls.append(group) or None)
    monkeypatch.setattr(
        awards,
        "apply_overrides_single",
        lambda *_args, **_kwargs: {"name": "Manual Builder"},
    )
    monkeypatch.setattr(awards, "_people_name_rows", lambda: [])

    assert awards.goat_holders_map() == {}
    assert goat_calls == []
```

- [ ] **Step 2: Run the badge test and verify RED**

Run:

```bash
DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest \
  tests/test_goat_holders_map.py::test_hand_build_badge_and_override_wait_for_30_days -q
```

Expected: FAIL because the override currently creates a Hand Build badge.

- [ ] **Step 3: Add a lazy readiness check to `goat_holders_map`**

Load full positive daily records only when a registered group maps to a
category whose minimum exceeds one day. Cache that list within the single map
build, fail closed for that gated group, and continue before calling `goat()`
or applying overrides:

```python
readiness_records = None
for group_name in work_centers_store.registered_groups():
    category = goat_categories.category_for_group_name(group_name)
    if category is not None and category.minimum_data_days > 1:
        try:
            if readiness_records is None:
                from datetime import datetime
                from . import production_history
                today = datetime.now(UTC).date()
                readiness_records = production_history.daily_records(
                    AWARDS_DATA_FLOOR,
                    today,
                )
            if not goat_categories.is_goat_ready(category, readiness_records):
                continue
        except Exception:
            continue
    # Existing goat() and override behavior follows unchanged.
```

Use the existing `UTC` import and add a local `goat_categories` import to avoid
new module-level cycles.

- [ ] **Step 4: Run badge tests and verify GREEN**

Run:

```bash
DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest tests/test_goat_holders_map.py -q
```

Expected: all holder-map tests pass.

---

### Task 4: Gate Hand Build alerts and Slack celebrations

**Files:**
- Modify: `tests/test_goat_notifications.py`
- Modify: `src/zira_dashboard/goat_notifications.py`

**Interfaces:**
- Consumes: `goat_categories.is_goat_ready()`
- Preserves: `_eligible_categories() -> tuple[GoatCategory, ...]`
- Preserves: `finalize_day(day: date, client) -> list[dict]`

- [ ] **Step 1: Write a failing notification test**

Use the real Hand Build category with 29 distinct positive dates. Stub the
precompute and record loader, force `_eligible_categories()` to return Hand
Build, and assert neither winner calculation nor alert persistence runs:

```python
def test_hand_build_notifications_wait_for_30_positive_days(monkeypatch):
    from datetime import timedelta

    category = goat_categories.category_for_key("hand_build")
    records = [
        {
            "day": date(2026, 7, 1) + timedelta(days=offset),
            "person": "Builder",
            "wc": "Hand Build #1",
            "units": 100 + offset,
            "hours": 7,
        }
        for offset in range(29)
    ]
    winner_calls = []
    inserted = []
    monkeypatch.setattr(goat_categories, "work_center_names", lambda _: {"Hand Build #1"})
    monkeypatch.setattr(goat_notifications, "_eligible_categories", lambda: (category,))
    monkeypatch.setattr(goat_notifications.precompute, "precompute_day", lambda *_: None)
    monkeypatch.setattr(goat_notifications, "_records_through", lambda _: records)
    monkeypatch.setattr(goat_notifications, "winner_for_day", lambda *_: winner_calls.append(True))
    monkeypatch.setattr(goat_notifications.store, "insert_alert_and_delivery", inserted.append)

    assert goat_notifications.finalize_day(records[-1]["day"], client=object()) == []
    assert winner_calls == []
    assert inserted == []
```

- [ ] **Step 2: Run the notification test and verify RED**

Run:

```bash
DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest \
  tests/test_goat_notifications.py::test_hand_build_notifications_wait_for_30_positive_days -q
```

Expected: FAIL because `finalize_day` currently evaluates Hand Build
immediately.

- [ ] **Step 3: Add the finalization gate**

Inside the existing category loop, skip before `winner_for_day`:

```python
for category in _eligible_categories():
    if not goat_categories.is_goat_ready(category, records):
        continue
    winner = winner_for_day(category, day, records)
```

Because the notification-day store already finalizes each completed day once,
the first 29 days remain finalized and cannot replay after day 30.

- [ ] **Step 4: Run notification tests and verify GREEN**

Run:

```bash
DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest tests/test_goat_notifications.py -q
```

Expected: all notification tests pass.

---

### Task 5: Add release copy and validate the complete change

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- No new runtime interface.

- [ ] **Step 1: Add a child-friendly release note**

Add this entry at the top of `2026-08-18`:

```markdown
### Hand Build GOAT timing

#### Improvements

- **Hand Build GOATs now wait for enough history.** The app waits for 30 workdays with pallet counts before naming a Hand Build GOAT. Regular production scores still show right away.
```

- [ ] **Step 2: Run the focused feature suite**

Run:

```bash
DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest \
  tests/test_goat_categories.py \
  tests/test_new_leaderboard_routes.py \
  tests/test_goat_holders_map.py \
  tests/test_goat_notifications.py \
  tests/test_hand_build_1_zira_mapping.py -q
```

Expected: all selected tests pass with no failures.

- [ ] **Step 3: Run lint and whitespace checks**

Run:

```bash
.venv/bin/python -m ruff check \
  src/zira_dashboard/goat_categories.py \
  src/zira_dashboard/routes/new_leaderboard.py \
  src/zira_dashboard/awards.py \
  src/zira_dashboard/goat_notifications.py \
  tests/test_goat_categories.py \
  tests/test_new_leaderboard_routes.py \
  tests/test_goat_holders_map.py \
  tests/test_goat_notifications.py \
  tests/test_hand_build_1_zira_mapping.py
git diff --check
```

Expected: Ruff reports `All checks passed!`; `git diff --check` is silent.

- [ ] **Step 4: Run the broad safe suite**

Run the broad local suite with production connections disabled and the seven
known unrelated environment/baseline failures deselected:

```bash
DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest -q \
  --deselect=tests/test_inbox_event_wiring.py::test_late_declare_absent_records_inbox_event \
  --deselect=tests/test_odoo_facade_contract.py::test_facade_department_helper_is_resolved_at_call_time \
  --deselect=tests/test_preview_new_leaderboard.py::test_preview_three_family_tv_ribbon_geometry_fits_target_viewports \
  --deselect=tests/test_settings_api_keys.py::test_api_settings_page_route_renders \
  --deselect=tests/test_settings_api_keys.py::test_super_admin_session_can_render_api_settings \
  --deselect=tests/test_time_off_approvals.py::test_approvals_url_redirects_to_merged_time_off_page \
  --deselect=tests/test_time_off_approvals.py::test_approvals_page_renders_pending_context_and_recent_decisions
```

Expected: no failures; only documented skips and the seven explicit
deselections.

- [ ] **Step 5: Review the final diff against the design**

Confirm:

```text
- 29 positive Hand Build dates: no chip, badge, alert, or Slack delivery.
- 30 positive Hand Build dates: GOAT calculation is eligible.
- Duplicate people/stations on a date count once.
- Zero units do not count.
- Other GOAT categories behave unchanged.
- No schema or production-data mutation was added.
```

- [ ] **Step 6: Commit and push the complete implementation**

```bash
git add CHANGELOG.md \
  src/zira_dashboard/goat_categories.py \
  src/zira_dashboard/routes/new_leaderboard.py \
  src/zira_dashboard/awards.py \
  src/zira_dashboard/goat_notifications.py \
  tests/test_goat_categories.py \
  tests/test_new_leaderboard_routes.py \
  tests/test_goat_holders_map.py \
  tests/test_goat_notifications.py \
  tests/test_hand_build_1_zira_mapping.py
git commit -m "fix: wait for Hand Build GOAT history"
git push origin main
```

Expected: the implementation commit reaches `origin/main`; unrelated untracked
workspace files remain untouched.
