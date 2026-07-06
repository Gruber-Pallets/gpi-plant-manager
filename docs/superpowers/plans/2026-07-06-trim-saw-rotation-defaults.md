# Trim Saw Rotation Defaults Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Trim Saw smart defaults that auto-fill a safe two-person pair from pinned defaults, recent rotation history, and Trim Saw skill levels.

**Architecture:** Add a focused `rotation_suggestions` helper module that owns scoring, pair validation, history loading, and smart-default composition. Wire the existing staffing route to call that helper only during default seeding and publish-next-day prefill, then expose a smart defaults map to the existing reset-to-defaults JavaScript.

**Tech Stack:** Python 3.11, FastAPI, Jinja2 templates, existing Postgres access through `zira_dashboard.db`, pytest.

## Global Constraints

- Version 1 only changes `Trim Saw 1`; no generic rotation settings UI.
- Do not auto-reshuffle after manual picker changes.
- Preserve non-Trim Saw default behavior.
- Treat stored Trim Saw defaults as pinned starting points when available.
- Use the most recent 20 saved non-testing schedule rows before the target day.
- Count `published_snapshot` assignments for edited-after-posted days when present.
- Exclude active reserves and full-day time-off people from smart suggestions.
- Leave a slot empty rather than assigning an unsafe Trim Saw pair.
- Keep configuration constants isolated for later generic rotation support.

---

## File Structure

- Create `src/zira_dashboard/rotation_suggestions.py`: Trim Saw constants, candidate scoring, pair validation, bounded history loader, and smart-default map helper.
- Create `tests/test_rotation_suggestions.py`: pure unit tests for pairing, scoring, availability, smart defaults, and history shaping.
- Modify `src/zira_dashboard/routes/staffing.py`: call smart defaults when seeding a brand-new day and when publish pre-fills the next day; pass smart defaults to the template.
- Modify `src/zira_dashboard/templates/staffing.html`: render `window.SMART_DEFAULTS_BY_LOC`.
- Modify `src/zira_dashboard/static/staffing.js`: make reset-to-defaults prefer smart defaults.
- Add route/template tests in `tests/test_staffing_trim_saw_defaults.py`: verify route context and next-day prefill call behavior without hitting Odoo or real Postgres.

---

### Task 1: Pure Trim Saw Pairing And Scoring

**Files:**
- Create: `src/zira_dashboard/rotation_suggestions.py`
- Test: `tests/test_rotation_suggestions.py`

**Interfaces:**
- Consumes: `zira_dashboard.staffing.Person`
- Produces:
  - `TRIM_SAW_WC: str`
  - `TRIM_SAW_SKILL: str`
  - `LOOKBACK_SCHEDULE_COUNT: int`
  - `@dataclass(frozen=True) class TrimSawHistory`
  - `suggest_trim_saw_pair(day: date, roster: Sequence[staffing.Person], pinned_names: Sequence[str], unavailable_names: Iterable[str], history: TrimSawHistory | None = None) -> list[str]`
  - `_valid_trim_saw_pair(level_a: int, level_b: int) -> bool`

- [ ] **Step 1: Write failing unit tests for pairing rules and pinned defaults**

Add this file:

```python
from __future__ import annotations

from datetime import date, time

from zira_dashboard import staffing
from zira_dashboard.rotation_suggestions import (
    TRIM_SAW_SKILL,
    TrimSawHistory,
    _valid_trim_saw_pair,
    suggest_trim_saw_pair,
)


TARGET_DAY = date(2026, 7, 6)


def person(name: str, level: int, *, active: bool = True, reserve: bool = False):
    return staffing.Person(
        name=name,
        active=active,
        reserve=reserve,
        skills={TRIM_SAW_SKILL: level},
    )


def empty_history():
    return TrimSawHistory(appearance_counts={}, most_recent_names=set())


def test_valid_trim_saw_pair_rules():
    assert _valid_trim_saw_pair(3, 1) is True
    assert _valid_trim_saw_pair(3, 0) is True
    assert _valid_trim_saw_pair(2, 2) is True
    assert _valid_trim_saw_pair(2, 1) is False
    assert _valid_trim_saw_pair(1, 1) is False
    assert _valid_trim_saw_pair(0, 2) is False


def test_level_three_default_can_pair_with_level_one():
    roster = [person("Jesus Martinez", 3), person("Rosa", 1), person("Carlos", 2)]

    pair = suggest_trim_saw_pair(
        TARGET_DAY,
        roster,
        pinned_names=["Jesus Martinez"],
        unavailable_names=[],
        history=empty_history(),
    )

    assert pair == ["Jesus Martinez", "Carlos"]
    assert _valid_trim_saw_pair(3, 2)


def test_level_two_default_gets_level_two_or_three_partner():
    roster = [person("Jesus Martinez", 2), person("Luis", 1), person("Rosa", 2)]

    pair = suggest_trim_saw_pair(
        TARGET_DAY,
        roster,
        pinned_names=["Jesus Martinez"],
        unavailable_names=[],
        history=empty_history(),
    )

    assert pair == ["Jesus Martinez", "Rosa"]


def test_level_one_default_requires_level_three_partner():
    roster = [person("Jesus Martinez", 1), person("Luis", 2), person("Rosa", 3)]

    pair = suggest_trim_saw_pair(
        TARGET_DAY,
        roster,
        pinned_names=["Jesus Martinez"],
        unavailable_names=[],
        history=empty_history(),
    )

    assert pair == ["Jesus Martinez", "Rosa"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rotation_suggestions.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'zira_dashboard.rotation_suggestions'`.

- [ ] **Step 3: Implement constants, history type, pair validation, candidate scoring, and pair selection**

Create `src/zira_dashboard/rotation_suggestions.py` with:

```python
"""Smart default suggestions for scheduler rotations.

Version 1 is intentionally scoped to Trim Saw. Keep the constants isolated so
other rotating work centers can reuse the shape later without changing the
staffing route again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from itertools import combinations
from typing import Iterable, Sequence

from . import staffing

TRIM_SAW_WC = "Trim Saw 1"
TRIM_SAW_SKILL = "Trim Saw"
LOOKBACK_SCHEDULE_COUNT = 20
TRIM_SAW_MAX_OPERATORS = 2

SKILL_BASE_WEIGHTS = {
    3: 100,
    2: 70,
    1: 25,
    0: 0,
}
APPEARANCE_PENALTY = 12
MOST_RECENT_PENALTY = 8


@dataclass(frozen=True)
class TrimSawHistory:
    appearance_counts: dict[str, int] = field(default_factory=dict)
    most_recent_names: set[str] = field(default_factory=set)


def _trim_saw_level(person: staffing.Person | None) -> int:
    if person is None:
        return 0
    return max(0, min(3, int(person.level(TRIM_SAW_SKILL))))


def _valid_trim_saw_pair(level_a: int, level_b: int) -> bool:
    low = min(int(level_a), int(level_b))
    high = max(int(level_a), int(level_b))
    if low <= 1:
        return high >= 3
    return low >= 2


def _candidate_score(
    person: staffing.Person,
    history: TrimSawHistory,
    *,
    pinned: bool = False,
) -> int:
    level = _trim_saw_level(person)
    base = SKILL_BASE_WEIGHTS.get(level, 0)
    if pinned and level == 0:
        base = 1
    appearances = int(history.appearance_counts.get(person.name, 0))
    score = base - (appearances * APPEARANCE_PENALTY)
    if person.name in history.most_recent_names:
        score -= MOST_RECENT_PENALTY
    return score


def _person_sort_key(
    person: staffing.Person,
    history: TrimSawHistory,
    *,
    pinned: bool = False,
) -> tuple[int, int, int, str]:
    return (
        -_candidate_score(person, history, pinned=pinned),
        -_trim_saw_level(person),
        int(history.appearance_counts.get(person.name, 0)),
        person.name.lower(),
    )


def _best_person(
    people: Sequence[staffing.Person],
    history: TrimSawHistory,
    pinned_names: set[str],
) -> staffing.Person | None:
    if not people:
        return None
    return min(
        people,
        key=lambda p: _person_sort_key(p, history, pinned=p.name in pinned_names),
    )


def _best_compatible_partner(
    anchor: staffing.Person,
    candidates: Sequence[staffing.Person],
    history: TrimSawHistory,
    pinned_names: set[str],
) -> staffing.Person | None:
    anchor_level = _trim_saw_level(anchor)
    compatible = [
        p
        for p in candidates
        if p.name != anchor.name and _valid_trim_saw_pair(anchor_level, _trim_saw_level(p))
    ]
    return _best_person(compatible, history, pinned_names)


def _best_pair(
    candidates: Sequence[staffing.Person],
    history: TrimSawHistory,
    pinned_names: set[str],
) -> list[staffing.Person]:
    valid_pairs: list[tuple[staffing.Person, staffing.Person]] = []
    for left, right in combinations(candidates, 2):
        if _valid_trim_saw_pair(_trim_saw_level(left), _trim_saw_level(right)):
            valid_pairs.append((left, right))
    if valid_pairs:
        best_left, best_right = min(
            valid_pairs,
            key=lambda pair: (
                -(
                    _candidate_score(pair[0], history, pinned=pair[0].name in pinned_names)
                    + _candidate_score(pair[1], history, pinned=pair[1].name in pinned_names)
                ),
                -max(_trim_saw_level(pair[0]), _trim_saw_level(pair[1])),
                -min(_trim_saw_level(pair[0]), _trim_saw_level(pair[1])),
                sum(history.appearance_counts.get(p.name, 0) for p in pair),
                tuple(sorted(p.name.lower() for p in pair)),
            ),
        )
        return sorted(
            [best_left, best_right],
            key=lambda p: _person_sort_key(p, history, pinned=p.name in pinned_names),
        )
    strongest = _best_person(candidates, history, pinned_names)
    return [strongest] if strongest is not None else []


def suggest_trim_saw_pair(
    day: date,
    roster: Sequence[staffing.Person],
    pinned_names: Sequence[str],
    unavailable_names: Iterable[str],
    history: TrimSawHistory | None = None,
) -> list[str]:
    resolved_history = history if history is not None else _load_trim_saw_history(day)
    unavailable = set(unavailable_names or [])
    by_name = {p.name: p for p in roster if p.active and not p.reserve and p.name not in unavailable}
    pinned_set = set(pinned_names or [])
    pinned_people = [by_name[name] for name in pinned_names if name in by_name]
    candidates = list(by_name.values())

    if len(pinned_people) >= TRIM_SAW_MAX_OPERATORS:
        first_two = pinned_people[:TRIM_SAW_MAX_OPERATORS]
        if _valid_trim_saw_pair(_trim_saw_level(first_two[0]), _trim_saw_level(first_two[1])):
            return [p.name for p in first_two]
        anchor = _best_person(first_two, resolved_history, pinned_set)
        if anchor is None:
            return []
        partner = _best_compatible_partner(
            anchor,
            [p for p in candidates if p.name != anchor.name],
            resolved_history,
            pinned_set,
        )
        return [anchor.name] + ([partner.name] if partner else [])

    if len(pinned_people) == 1:
        anchor = pinned_people[0]
        partner = _best_compatible_partner(
            anchor,
            [p for p in candidates if p.name != anchor.name],
            resolved_history,
            pinned_set,
        )
        return [anchor.name] + ([partner.name] if partner else [])

    return [p.name for p in _best_pair(candidates, resolved_history, pinned_set)]


def _load_trim_saw_history(day: date) -> TrimSawHistory:
    return TrimSawHistory()
```

- [ ] **Step 4: Run tests and fix the deliberate ranking expectation**

Run: `pytest tests/test_rotation_suggestions.py -v`

Expected: The tests should now PASS. If `test_level_three_default_can_pair_with_level_one` returns the level 2 partner before the level 1 partner, keep that result; the spec says level 3 can pair with level 1, 2, or 3, not that level 1 should be preferred over level 2.

- [ ] **Step 5: Add tests for history penalties, reserves, unavailable people, and unsafe partials**

Append to `tests/test_rotation_suggestions.py`:

```python
def test_recent_history_reduces_candidate_rank():
    roster = [person("Alicia", 3), person("Beatriz", 3), person("Carlos", 2)]
    history = TrimSawHistory(
        appearance_counts={"Alicia": 4, "Beatriz": 0, "Carlos": 0},
        most_recent_names={"Alicia"},
    )

    pair = suggest_trim_saw_pair(
        TARGET_DAY,
        roster,
        pinned_names=[],
        unavailable_names=[],
        history=history,
    )

    assert pair == ["Beatriz", "Carlos"]


def test_level_three_still_outranks_level_two_when_similarly_due():
    roster = [person("Alicia", 3), person("Beatriz", 2), person("Carlos", 2)]

    pair = suggest_trim_saw_pair(
        TARGET_DAY,
        roster,
        pinned_names=[],
        unavailable_names=[],
        history=empty_history(),
    )

    assert pair[0] == "Alicia"
    assert set(pair) == {"Alicia", "Beatriz"}


def test_unavailable_and_reserve_people_are_excluded():
    roster = [
        person("Pinned Off", 3),
        person("Reserve Pro", 3, reserve=True),
        person("Available Pro", 3),
        person("Available Two", 2),
    ]

    pair = suggest_trim_saw_pair(
        TARGET_DAY,
        roster,
        pinned_names=["Pinned Off"],
        unavailable_names=["Pinned Off"],
        history=empty_history(),
    )

    assert pair == ["Available Pro", "Available Two"]


def test_no_safe_pair_returns_partial_assignment():
    roster = [person("Jesus Martinez", 1), person("Luis", 2)]

    pair = suggest_trim_saw_pair(
        TARGET_DAY,
        roster,
        pinned_names=["Jesus Martinez"],
        unavailable_names=[],
        history=empty_history(),
    )

    assert pair == ["Jesus Martinez"]
```

- [ ] **Step 6: Run Task 1 tests**

Run: `pytest tests/test_rotation_suggestions.py -v`

Expected: PASS.

- [ ] **Step 7: Commit Task 1**

```bash
git add src/zira_dashboard/rotation_suggestions.py tests/test_rotation_suggestions.py
git commit -m "feat(staffing): add trim saw pair suggestions"
```

---

### Task 2: Bounded Trim Saw History Loading

**Files:**
- Modify: `src/zira_dashboard/rotation_suggestions.py`
- Test: `tests/test_rotation_suggestions.py`

**Interfaces:**
- Consumes: `TrimSawHistory`
- Produces:
  - `_history_from_schedule_rows(rows: Sequence[dict]) -> TrimSawHistory`
  - `_load_trim_saw_history(day: date) -> TrimSawHistory`

- [ ] **Step 1: Add failing tests for published snapshots and direct DB limit**

Append to `tests/test_rotation_suggestions.py`:

```python
def test_history_uses_published_snapshot_when_present():
    from zira_dashboard.rotation_suggestions import _history_from_schedule_rows

    rows = [
        {
            "day": date(2026, 7, 3),
            "assignments": {"Trim Saw 1": ["Draft Person"]},
            "published_snapshot": {"assignments": {"Trim Saw 1": ["Posted Person"]}},
        },
        {
            "day": date(2026, 7, 2),
            "assignments": {"Trim Saw 1": ["Posted Person", "Other"]},
            "published_snapshot": None,
        },
    ]

    history = _history_from_schedule_rows(rows)

    assert history.appearance_counts == {"Posted Person": 2, "Other": 1}
    assert history.most_recent_names == {"Posted Person"}


def test_load_trim_saw_history_queries_only_recent_non_testing_rows(monkeypatch):
    from zira_dashboard import db
    from zira_dashboard.rotation_suggestions import _load_trim_saw_history

    captured = {}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return [
            {
                "day": date(2026, 7, 3),
                "assignments": {"Trim Saw 1": ["Alicia"]},
                "published_snapshot": None,
            }
        ]

    monkeypatch.setattr(db, "query", fake_query)

    history = _load_trim_saw_history(date(2026, 7, 6))

    assert history.appearance_counts == {"Alicia": 1}
    assert history.most_recent_names == {"Alicia"}
    assert "LIMIT %s" in captured["sql"]
    assert captured["params"] == (date(2026, 7, 6), 20)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rotation_suggestions.py::test_history_uses_published_snapshot_when_present tests/test_rotation_suggestions.py::test_load_trim_saw_history_queries_only_recent_non_testing_rows -v`

Expected: FAIL with missing `_history_from_schedule_rows` or empty history.

- [ ] **Step 3: Implement history shaping and bounded query**

Replace the initial empty `_load_trim_saw_history` implementation in `src/zira_dashboard/rotation_suggestions.py` and add `_history_from_schedule_rows`:

```python
def _names_from_assignments(assignments) -> list[str]:
    if not isinstance(assignments, dict):
        return []
    return [
        str(name)
        for name in (assignments.get(TRIM_SAW_WC) or [])
        if str(name or "").strip()
    ]


def _history_from_schedule_rows(rows: Sequence[dict]) -> TrimSawHistory:
    counts: dict[str, int] = {}
    most_recent_names: set[str] = set()
    for idx, row in enumerate(rows):
        snapshot = row.get("published_snapshot")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("assignments"), dict):
            names = _names_from_assignments(snapshot.get("assignments"))
        else:
            names = _names_from_assignments(row.get("assignments"))
        if idx == 0:
            most_recent_names = set(names)
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    return TrimSawHistory(appearance_counts=counts, most_recent_names=most_recent_names)


def _load_trim_saw_history(day: date) -> TrimSawHistory:
    from . import db

    rows = db.query(
        "SELECT s.day, s.published_snapshot, "
        "       COALESCE(jsonb_object_agg(wc.name, names.people) "
        "                FILTER (WHERE wc.name IS NOT NULL), '{}'::jsonb) AS assignments "
        "FROM ("
        "  SELECT day, published_snapshot "
        "  FROM schedules "
        "  WHERE day < %s AND testing_day = FALSE "
        "  ORDER BY day DESC "
        "  LIMIT %s"
        ") s "
        "LEFT JOIN LATERAL ("
        "  SELECT sa.day, sa.wc_id, jsonb_agg(pe.name ORDER BY sa.sort_order) AS people "
        "  FROM schedule_assignments sa "
        "  JOIN people pe ON pe.id = sa.person_id "
        "  WHERE sa.day = s.day "
        "  GROUP BY sa.day, sa.wc_id"
        ") names ON TRUE "
        "LEFT JOIN work_centers wc ON wc.id = names.wc_id "
        "GROUP BY s.day, s.published_snapshot "
        "ORDER BY s.day DESC",
        (day, LOOKBACK_SCHEDULE_COUNT),
    )
    return _history_from_schedule_rows(rows)
```

- [ ] **Step 4: Make history failure graceful in `suggest_trim_saw_pair`**

In `suggest_trim_saw_pair`, replace:

```python
resolved_history = history if history is not None else _load_trim_saw_history(day)
```

with:

```python
if history is not None:
    resolved_history = history
else:
    try:
        resolved_history = _load_trim_saw_history(day)
    except Exception:
        resolved_history = TrimSawHistory()
```

- [ ] **Step 5: Run Task 2 tests**

Run: `pytest tests/test_rotation_suggestions.py -v`

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/zira_dashboard/rotation_suggestions.py tests/test_rotation_suggestions.py
git commit -m "feat(staffing): load trim saw rotation history"
```

---

### Task 3: Smart Default Assignment Composition

**Files:**
- Modify: `src/zira_dashboard/rotation_suggestions.py`
- Test: `tests/test_rotation_suggestions.py`

**Interfaces:**
- Consumes: `suggest_trim_saw_pair(...)`
- Produces:
  - `smart_defaults_for_day(day: date, roster: Sequence[staffing.Person], base_assignments: dict[str, list[str]], time_off_entries: Sequence[dict]) -> dict[str, list[str]]`

- [ ] **Step 1: Add failing tests for base assignment copying, time off exclusion, and non-Trim Saw preservation**

Append to `tests/test_rotation_suggestions.py`:

```python
def test_smart_defaults_replaces_only_trim_saw_and_excludes_full_day_time_off(monkeypatch):
    from zira_dashboard import rotation_suggestions

    roster = [
        person("Jesus Martinez", 3),
        person("Off Person", 3),
        person("Rotation Two", 2),
        person("Repair Default", 2),
    ]
    base = {
        "Trim Saw 1": ["Jesus Martinez", "Off Person"],
        "Repair 1": ["Repair Default"],
    }

    monkeypatch.setattr(
        rotation_suggestions,
        "_load_trim_saw_history",
        lambda day: empty_history(),
    )

    smart = rotation_suggestions.smart_defaults_for_day(
        TARGET_DAY,
        roster,
        base,
        time_off_entries=[{"name": "Off Person", "hours": None}],
    )

    assert smart["Trim Saw 1"] == ["Jesus Martinez", "Rotation Two"]
    assert smart["Repair 1"] == ["Repair Default"]
    assert base["Trim Saw 1"] == ["Jesus Martinez", "Off Person"]


def test_smart_defaults_excludes_people_already_defaulted_elsewhere(monkeypatch):
    from zira_dashboard import rotation_suggestions

    roster = [
        person("Jesus Martinez", 3),
        person("Repair Default", 3),
        person("Rotation Two", 2),
    ]
    base = {
        "Trim Saw 1": ["Jesus Martinez"],
        "Repair 1": ["Repair Default"],
    }

    monkeypatch.setattr(
        rotation_suggestions,
        "_load_trim_saw_history",
        lambda day: empty_history(),
    )

    smart = rotation_suggestions.smart_defaults_for_day(TARGET_DAY, roster, base, [])

    assert smart["Trim Saw 1"] == ["Jesus Martinez", "Rotation Two"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rotation_suggestions.py::test_smart_defaults_replaces_only_trim_saw_and_excludes_full_day_time_off tests/test_rotation_suggestions.py::test_smart_defaults_excludes_people_already_defaulted_elsewhere -v`

Expected: FAIL with `AttributeError: module 'zira_dashboard.rotation_suggestions' has no attribute 'smart_defaults_for_day'`.

- [ ] **Step 3: Implement smart defaults helper**

Add to `src/zira_dashboard/rotation_suggestions.py`:

```python
def _full_day_time_off_names(time_off_entries: Sequence[dict]) -> set[str]:
    return {
        str(entry.get("name") or "")
        for entry in (time_off_entries or [])
        if entry.get("hours") is None and str(entry.get("name") or "").strip()
    }


def smart_defaults_for_day(
    day: date,
    roster: Sequence[staffing.Person],
    base_assignments: dict[str, list[str]],
    time_off_entries: Sequence[dict],
) -> dict[str, list[str]]:
    smart = {wc: list(names or []) for wc, names in (base_assignments or {}).items()}
    pinned = list(smart.get(TRIM_SAW_WC, []))
    unavailable = _full_day_time_off_names(time_off_entries)
    for wc_name, names in smart.items():
        if wc_name == TRIM_SAW_WC:
            continue
        unavailable.update(names or [])
    pair = suggest_trim_saw_pair(day, roster, pinned, unavailable)
    if pair:
        smart[TRIM_SAW_WC] = pair[:TRIM_SAW_MAX_OPERATORS]
    else:
        smart.pop(TRIM_SAW_WC, None)
    return smart
```

- [ ] **Step 4: Run Task 3 tests**

Run: `pytest tests/test_rotation_suggestions.py -v`

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/zira_dashboard/rotation_suggestions.py tests/test_rotation_suggestions.py
git commit -m "feat(staffing): compose trim saw smart defaults"
```

---

### Task 4: Staffing Route And Template Integration

**Files:**
- Modify: `src/zira_dashboard/routes/staffing.py`
- Modify: `src/zira_dashboard/templates/staffing.html`
- Test: `tests/test_staffing_trim_saw_defaults.py`

**Interfaces:**
- Consumes: `rotation_suggestions.smart_defaults_for_day(...)`
- Produces:
  - Template context key `smart_defaults_by_loc: dict[str, list[str]]`
  - JavaScript global `window.SMART_DEFAULTS_BY_LOC`

- [ ] **Step 1: Add failing template test for `SMART_DEFAULTS_BY_LOC`**

Create `tests/test_staffing_trim_saw_defaults.py`:

```python
from __future__ import annotations

from pathlib import Path

from zira_dashboard.deps import templates


def _globals_script() -> str:
    html = Path("src/zira_dashboard/templates/staffing.html").read_text()
    marker = "window.DEFAULTS_BY_LOC"
    start = html.rindex("<script>", 0, html.index(marker))
    end = html.index("</script>", html.index(marker)) + len("</script>")
    return html[start:end]


def test_staffing_template_exposes_smart_defaults():
    rendered = templates.env.from_string(_globals_script()).render(
        person_certs={},
        cert_icon_data=lambda: {},
        day="2026-07-06",
        today="2026-07-06",
        view_mode="draft",
        published=False,
        viewing_posted=False,
        defaults_by_loc={"Trim Saw 1": ["Stored"]},
        smart_defaults_by_loc={"Trim Saw 1": ["Smart"]},
        people_meta={},
        partial_range_by_name={},
        partial_hours_by_name={},
        time_off_names=[],
        forklift_live_model={"available": False},
    )

    assert "window.SMART_DEFAULTS_BY_LOC" in rendered
    assert '"Smart"' in rendered
```

- [ ] **Step 2: Run template test to verify it fails**

Run: `pytest tests/test_staffing_trim_saw_defaults.py::test_staffing_template_exposes_smart_defaults -v`

Expected: FAIL because `window.SMART_DEFAULTS_BY_LOC` is not rendered.

- [ ] **Step 3: Add route helper imports and safe smart defaults function**

In `src/zira_dashboard/routes/staffing.py`, update the import:

```python
from .. import _http_cache, attendance, rotation_suggestions, schedule_store, shift_config, staffing, staffing_view, time_format, work_centers_store
```

Add this helper near `_forklift_scheduled_counts`:

```python
def _smart_defaults_for_day(d: date, roster, defaults: dict[str, list[str]], time_off_entries):
    try:
        return rotation_suggestions.smart_defaults_for_day(d, roster, defaults, time_off_entries)
    except Exception:
        return {k: list(v) for k, v in (defaults or {}).items()}
```

- [ ] **Step 4: Resolve time off before default seeding**

In `staffing_page`, the route currently waits to read `f_time_off_entries.result()` until after default seeding. Move that read up so smart defaults can exclude full-day absences. After this block:

```python
        person_certs = f_certs.result()
        roster = f_roster.result()
        sched = f_sched.result()
```

add:

```python
        time_off_entries = f_time_off_entries.result()
```

Then in the later `with _Phase(phases, "attendance"):` block, remove the duplicate assignment:

```python
        time_off_entries = f_time_off_entries.result()
```

Keep:

```python
        attendance_pkg = f_attendance.result()
        attendance_by_name = attendance_pkg.get("by_name") or {}
```

- [ ] **Step 5: Use smart defaults during GET seeding and context building**

In `staffing_page`, replace the current default seeding block:

```python
    if not sched.assignments:
        seeded: dict[str, list[str]] = {}
        for loc in staffing.LOCATIONS:
            dp = work_centers_store.default_people(loc)
            if dp:
                seeded[loc.name] = list(dp)
        if not seeded:  # fallback for first-run: legacy CSV defaults
            seeded = staffing.default_assignments()
        sched.assignments = seeded
```

with:

```python
    seeded_from_defaults = False
    if not sched.assignments:
        seeded: dict[str, list[str]] = {}
        for loc in staffing.LOCATIONS:
            dp = work_centers_store.default_people(loc)
            if dp:
                seeded[loc.name] = list(dp)
        if not seeded:  # fallback for first-run: legacy CSV defaults
            seeded = staffing.default_assignments()
        sched.assignments = _smart_defaults_for_day(d, roster, seeded, time_off_entries)
        seeded_from_defaults = True
```

Then before `TemplateResponse`, after `bay_model = ...`, add:

```python
    raw_defaults_by_loc = bay_model.get("defaults_by_loc") or {}
    if seeded_from_defaults:
        smart_defaults_by_loc = {k: list(v) for k, v in sched.assignments.items()}
        for loc_name, names in raw_defaults_by_loc.items():
            smart_defaults_by_loc.setdefault(loc_name, list(names))
    else:
        smart_defaults_by_loc = _smart_defaults_for_day(
            d,
            roster,
            {k: list(v) for k, v in raw_defaults_by_loc.items()},
            time_off_entries,
        )
```

Add `"smart_defaults_by_loc": smart_defaults_by_loc,` to the template context next to the existing bay model keys.

- [ ] **Step 6: Use smart defaults when publish pre-fills the next day**

In `_staffing_save_work`, replace:

```python
            if defaults:
                staffing.save_schedule(staffing.Schedule(day=next_day, published=False, assignments=defaults))
```

with:

```python
            if defaults:
                try:
                    next_roster = staffing.load_roster()
                    next_time_off = _safe_time_off_entries(next_day)
                    smart_defaults = _smart_defaults_for_day(
                        next_day,
                        next_roster,
                        defaults,
                        next_time_off,
                    )
                except Exception:
                    smart_defaults = {k: list(v) for k, v in defaults.items()}
                staffing.save_schedule(staffing.Schedule(
                    day=next_day,
                    published=False,
                    assignments=smart_defaults,
                ))
```

- [ ] **Step 7: Render `window.SMART_DEFAULTS_BY_LOC`**

In `src/zira_dashboard/templates/staffing.html`, after:

```html
  window.DEFAULTS_BY_LOC = {{ defaults_by_loc|tojson }};
```

add:

```html
  window.SMART_DEFAULTS_BY_LOC = {{ smart_defaults_by_loc|tojson }};
```

- [ ] **Step 8: Run template test**

Run: `pytest tests/test_staffing_trim_saw_defaults.py::test_staffing_template_exposes_smart_defaults -v`

Expected: PASS.

- [ ] **Step 9: Add route tests for new-day seeding, publish prefill, and fallback**

Append to `tests/test_staffing_trim_saw_defaults.py`:

```python
from datetime import date


def test_staffing_page_seeds_empty_day_with_smart_defaults(monkeypatch):
    from zira_dashboard import cert_lookup
    from zira_dashboard import staffing as staffing_mod, staffing_view
    from zira_dashboard.routes import staffing as staffing_routes

    target_day = date(2026, 7, 7)
    captured = {}

    monkeypatch.setattr(staffing_routes, "plant_today", lambda: date(2026, 7, 6))
    monkeypatch.setattr(staffing_routes, "_next_working_day", lambda today: target_day)
    monkeypatch.setattr(staffing_routes._http_cache, "get_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(staffing_routes._http_cache, "set_cache_headers", lambda *a, **k: None)
    monkeypatch.setattr(staffing_routes._http_cache, "store_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(cert_lookup, "load_person_certs", lambda: {})
    monkeypatch.setattr(staffing_mod, "load_roster", lambda: [])
    monkeypatch.setattr(
        staffing_mod,
        "load_schedule",
        lambda d: staffing_mod.Schedule(day=d, published=False, assignments={}),
    )
    monkeypatch.setattr(staffing_routes, "_safe_time_off_entries", lambda d: [])
    monkeypatch.setattr(
        staffing_routes,
        "_safe_attendance",
        lambda d, sched, today: {"by_name": {}, "name_to_id": {}},
    )
    monkeypatch.setattr(staffing_routes, "_late_emp_ids", lambda d, today, pkg: set())
    monkeypatch.setattr(staffing_routes.attendance, "person_id_to_name", lambda name_to_id: {})
    monkeypatch.setattr(staffing_routes.shift_config, "configured_shift_start_for", lambda d: time(7, 0))
    monkeypatch.setattr(staffing_routes.shift_config, "configured_shift_end_for", lambda d: time(15, 30))
    monkeypatch.setattr(staffing_routes.shift_config, "configured_breaks_for", lambda d: [])
    monkeypatch.setattr(staffing_routes.shift_config, "scheduler_hours_source", lambda d, custom: "weekday_default")
    monkeypatch.setattr(
        staffing_routes.work_centers_store,
        "default_people",
        lambda loc: ["Stored"] if loc.name == "Trim Saw 1" else [],
    )
    monkeypatch.setattr(
        staffing_routes,
        "_smart_defaults_for_day",
        lambda d, roster, defaults, time_off: {"Trim Saw 1": ["Smart"]},
    )

    def fake_build_staffing_bays(roster, sched, time_off_entries, publish_blocked):
        captured["assignments"] = dict(sched.assignments)
        return {
            "bays": [],
            "publish_block_reasons": [],
            "defaults_by_loc": {"Trim Saw 1": ["Stored"]},
            "unassigned": [],
            "reserves": [],
            "time_off_names": [],
            "time_off_entries": [],
            "partial_hours_by_name": {},
            "partial_range_by_name": {},
            "partial_clear_by_name": {},
            "people_meta": {},
            "all_active_people": [],
        }

    monkeypatch.setattr(staffing_view, "build_staffing_bays", fake_build_staffing_bays)

    class FakeResponse:
        def __init__(self, context):
            self.context = context
            self.headers = {}

    class FakeTemplates:
        def TemplateResponse(self, request, template, context):
            captured["context"] = context
            return FakeResponse(context)

    monkeypatch.setattr(staffing_routes, "templates", FakeTemplates())

    staffing_routes.staffing_page(request=object(), day=None, publish_blocked=0, view="draft")

    assert captured["assignments"] == {"Trim Saw 1": ["Smart"]}
    assert captured["context"]["smart_defaults_by_loc"]["Trim Saw 1"] == ["Smart"]


def test_publish_prefills_next_day_with_smart_defaults(monkeypatch):
    from zira_dashboard import staffing as staffing_mod
    from zira_dashboard.routes import staffing as staffing_routes

    current_day = date(2026, 7, 6)
    next_day = date(2026, 7, 7)
    saved = []
    smart_calls = []

    class FakeForm:
        def getlist(self, key):
            return []

        def get(self, key, default=None):
            if key == "action":
                return "publish"
            return default

    class FakeRequest:
        headers = {}

    monkeypatch.setattr(staffing_routes, "_next_working_day", lambda d: next_day)
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(
        staffing_mod,
        "load_schedule",
        lambda d: staffing_mod.Schedule(day=d, published=False, assignments={}),
    )
    monkeypatch.setattr(staffing_mod, "save_schedule", lambda sched: saved.append(sched))
    monkeypatch.setattr(staffing_mod, "load_roster", lambda: [])
    monkeypatch.setattr(staffing_routes, "_safe_time_off_entries", lambda d: [])
    monkeypatch.setattr(staffing_routes.work_centers_store, "min_ops", lambda loc: loc.min_ops)
    monkeypatch.setattr(
        staffing_routes.work_centers_store,
        "default_people",
        lambda loc: ["Stored"] if loc.name == "Trim Saw 1" else [],
    )

    def fake_smart(d, roster, defaults, time_off):
        smart_calls.append((d, defaults))
        return {"Trim Saw 1": ["Smart"]}

    monkeypatch.setattr(staffing_routes, "_smart_defaults_for_day", fake_smart)

    response = staffing_routes._staffing_save_work(FakeRequest(), current_day, 0, FakeForm())

    assert response.status_code == 303
    assert smart_calls == [(next_day, {"Trim Saw 1": ["Stored"]})]
    assert saved[-1].day == next_day
    assert saved[-1].assignments == {"Trim Saw 1": ["Smart"]}


def test_route_smart_defaults_falls_back_to_raw_defaults(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_routes
    from zira_dashboard import rotation_suggestions

    def boom(*args, **kwargs):
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(rotation_suggestions, "smart_defaults_for_day", boom)

    out = staffing_routes._smart_defaults_for_day(
        date(2026, 7, 6),
        roster=[],
        defaults={"Trim Saw 1": ["Stored"]},
        time_off_entries=[],
    )

    assert out == {"Trim Saw 1": ["Stored"]}
```

- [ ] **Step 10: Run Task 4 tests**

Run: `pytest tests/test_staffing_trim_saw_defaults.py -v`

Expected: PASS.

- [ ] **Step 11: Commit Task 4**

```bash
git add src/zira_dashboard/routes/staffing.py src/zira_dashboard/templates/staffing.html tests/test_staffing_trim_saw_defaults.py
git commit -m "feat(staffing): wire trim saw smart defaults"
```

---

### Task 5: Reset-To-Defaults JavaScript Uses Smart Defaults

**Files:**
- Modify: `src/zira_dashboard/static/staffing.js`
- Test: manual or existing static checks

**Interfaces:**
- Consumes: `window.SMART_DEFAULTS_BY_LOC`
- Produces: reset-to-defaults behavior using smart map when available.

- [ ] **Step 1: Change reset source map**

In `src/zira_dashboard/static/staffing.js`, replace:

```javascript
  const __defaultsByLoc = window.DEFAULTS_BY_LOC;
```

with:

```javascript
  const __defaultsByLoc = window.SMART_DEFAULTS_BY_LOC || window.DEFAULTS_BY_LOC;
```

- [ ] **Step 2: Run existing Python tests that inspect static/templates**

Run: `pytest tests/test_staffing_trim_saw_defaults.py tests/test_timeclock_time_off_static.py -v`

Expected: PASS. `tests/test_timeclock_time_off_static.py` is included because this repo has static-template smoke coverage patterns and it is fast.

- [ ] **Step 3: Commit Task 5**

```bash
git add src/zira_dashboard/static/staffing.js
git commit -m "feat(staffing): reset to trim saw smart defaults"
```

---

### Task 6: End-To-End Verification And Polish

**Files:**
- Modify if needed: files touched in Tasks 1-5

**Interfaces:**
- Consumes all previous task outputs.
- Produces verified implementation ready for review.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
pytest tests/test_rotation_suggestions.py tests/test_staffing_trim_saw_defaults.py -v
```

Expected: PASS.

- [ ] **Step 2: Run lint for touched Python files**

Run:

```bash
ruff check src/zira_dashboard/rotation_suggestions.py src/zira_dashboard/routes/staffing.py tests/test_rotation_suggestions.py tests/test_staffing_trim_saw_defaults.py
```

Expected: PASS.

- [ ] **Step 3: Inspect final diff**

Run:

```bash
git diff --stat HEAD
git diff HEAD -- src/zira_dashboard/rotation_suggestions.py src/zira_dashboard/routes/staffing.py src/zira_dashboard/templates/staffing.html src/zira_dashboard/static/staffing.js tests/test_rotation_suggestions.py tests/test_staffing_trim_saw_defaults.py
```

Expected: Diff only contains Trim Saw smart-default implementation and tests.

- [ ] **Step 4: Manual smoke test if a dev server is available**

Run the app using the repository's normal local command:

```bash
python -m zira_dashboard.app
```

Open `/staffing`, select a future unsaved work day, and confirm:

- `Trim Saw 1` is prefilled with a compatible pair.
- `Reset to defaults` restores the smart Trim Saw pair.
- Manually changing Trim Saw autosaves and remains after reload.

If the app cannot run because environment variables or Postgres are unavailable, record that in the final handoff and rely on the focused tests.

- [ ] **Step 5: Final commit if verification required fixes**

If Step 1-4 required any fixes after Task 5, commit them:

```bash
git add src/zira_dashboard/rotation_suggestions.py src/zira_dashboard/routes/staffing.py src/zira_dashboard/templates/staffing.html src/zira_dashboard/static/staffing.js tests/test_rotation_suggestions.py tests/test_staffing_trim_saw_defaults.py
git commit -m "fix(staffing): polish trim saw smart defaults"
```
