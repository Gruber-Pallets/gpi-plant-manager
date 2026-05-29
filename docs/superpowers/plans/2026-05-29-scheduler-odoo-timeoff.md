# Plant Scheduler — Odoo Time Off Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the plant scheduler's Time Off panel (and its scheduling-exclusion + partial-day badges) read from the Odoo-backed `time_off_requests` table — showing approved **and** pending requests with partial-day times — replacing the StratusTime source, so kiosk requests drive how we schedule.

**Architecture:** A new focused module `scheduler_time_off.py` produces the scheduler's existing time-off entry shape from `time_off_requests` + `leave_types_cache` + `people` for a given day. `routes/staffing.py` swaps its StratusTime calls (`_safe_time_off_entries`, `_timeoff_names_with_fallback`) for the new builder. The template gains a "pending" indicator. Full-day absences are excluded from the schedulable roster; partial-day people stay on the roster with a badge.

**Tech Stack:** FastAPI, Jinja2, psycopg2 (`db.query`), existing `time_off_requests` / `leave_types_cache` / `people` tables.

---

## Context the implementer needs

**Current entry shape** consumed by `templates/staffing.html` and `routes/staffing.py` (each item in `time_off_entries`):
- `name` (str, roster name) — required
- `hours` (float | None) — `< 8` ⇒ partial; `None`/`>=8` ⇒ full. Drives `partial_hours_by_name`/`partial_range_by_name` and the "partial" tag.
- `pay_type` (str) — shown as meta text (e.g. leave-type name)
- `time_range` (str) — e.g. `"9:00am–2:00pm"`, shown for partials
- `derived` (bool), `manual_absent` (bool) — StratusTime concepts; `derived or manual_absent` ⇒ "absent" CSS class. For Odoo entries both are always `False`.
- `request_id` / `emp_id` — only used by the StratusTime "clear partial" feature. **Odoo entries omit these**, so they won't get a clear (×) button. (Clear-partial for Odoo is explicitly out of scope — see "Out of scope".)

**`time_off_requests` columns** (already exist): `person_odoo_id`, `shape` (`full_day`|`late_arrival`|`early_leave`|`midday_gap`), `date_from`, `date_to`, `hour_from`, `hour_to`, `holiday_status_id`, `state` (`draft`|`confirm`|`validate1`|`validate`|`refuse`|`cancel`|`draft_*`).

**State semantics:** approved ⇒ `state == 'validate'`. Pending ⇒ `state in ('draft','confirm','validate1')`. Exclude `refuse`/`cancel`/`draft_cancel`.

**Partial off-hours** are uniformly `hour_to - hour_from` for all three partial shapes (the kiosk stores the *off* window: late_arrival = shift_start→arrival, early_leave = leave→shift_end, midday_gap = gone→return).

**Name matching is safe:** `people.name` and `roster.json` names both come from `odoo_sync._short_name(emp["name"])`, so joining `time_off_requests → people.name` yields strings that equal roster names.

**Out of scope (flag to user, do NOT build here):**
- Clear-partial (×) for Odoo entries — the existing feature is StratusTime/`late_report`-specific. Odoo partials simply won't show a × button.
- `live_cache` caching for time off — the new builder is a couple of fast local Postgres reads, no Odoo round-trip, so it runs inline without the cache layer.
- Removing StratusTime time-off code paths elsewhere (late report etc.) — leave them; this plan only repoints the **scheduler**.

---

### Task 1: Odoo time-off entry builder module

**Files:**
- Create: `src/zira_dashboard/scheduler_time_off.py`
- Test: `tests/test_scheduler_time_off.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler_time_off.py
from datetime import date
from zira_dashboard import scheduler_time_off as sto


def _fake_db(monkeypatch, rows):
    monkeypatch.setattr(sto.db, "query", lambda sql, params=None: rows)


def test_full_day_entry_is_not_partial(monkeypatch):
    _fake_db(monkeypatch, [{
        "name": "Adrian Aragon", "shape": "full_day",
        "hour_from": None, "hour_to": None, "state": "validate",
        "pay_type": "Paid Time Off",
    }])
    out = sto.time_off_entries_for_day(date(2026, 6, 1))
    assert out[0]["name"] == "Adrian Aragon"
    assert out[0]["hours"] is None          # full day -> not partial
    assert out[0]["pending"] is False
    assert out[0]["pay_type"] == "Paid Time Off"
    assert "request_id" not in out[0]       # no clear button for Odoo


def test_late_arrival_is_partial_with_time_range(monkeypatch):
    _fake_db(monkeypatch, [{
        "name": "Pascual Moreno", "shape": "late_arrival",
        "hour_from": 6.0, "hour_to": 9.0, "state": "validate",
        "pay_type": "Unpaid Time Off",
    }])
    out = sto.time_off_entries_for_day(date(2026, 6, 1))
    assert out[0]["hours"] == 3.0           # 9 - 6
    assert out[0]["time_range"] == "6:00am–9:00am"
    assert out[0]["pending"] is False


def test_pending_state_flagged(monkeypatch):
    _fake_db(monkeypatch, [{
        "name": "Juan Delgado", "shape": "full_day",
        "hour_from": None, "hour_to": None, "state": "confirm",
        "pay_type": "Paid Time Off",
    }])
    out = sto.time_off_entries_for_day(date(2026, 6, 1))
    assert out[0]["pending"] is True


def test_full_day_off_names_only_full(monkeypatch):
    _fake_db(monkeypatch, [
        {"name": "Full Person", "shape": "full_day", "hour_from": None,
         "hour_to": None, "state": "validate", "pay_type": "PTO"},
        {"name": "Partial Person", "shape": "early_leave", "hour_from": 12.0,
         "hour_to": 14.5, "state": "validate", "pay_type": "PTO"},
    ])
    full = sto.full_day_off_names(date(2026, 6, 1))
    assert full == {"Full Person"}          # partial people stay schedulable
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_scheduler_time_off.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'zira_dashboard.scheduler_time_off'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/zira_dashboard/scheduler_time_off.py
"""Scheduler-facing time-off entries sourced from the Odoo-backed
``time_off_requests`` mirror (replacing the StratusTime feed).

Returns the same dict shape ``routes/staffing.py`` already consumes:
``{name, hours, pay_type, time_range, derived, manual_absent, pending}``.
Full-day requests use ``hours=None`` (not partial); partial shapes use the
off-window span (``hour_to - hour_from``). ``pending`` flags requests not yet
approved in Odoo (``state != 'validate'``) so the template can style them.
"""
from __future__ import annotations

from datetime import date as _date

from . import db

# Requests in these states count as "happening" on the scheduler. 'validate'
# is approved; the rest are pending. Refused/cancelled/draft-cancel excluded.
_APPROVED = "validate"
_PENDING = ("draft", "confirm", "validate1")
_VISIBLE_STATES = (_APPROVED,) + _PENDING


def _fmt_hf(h: float) -> str:
    """Decimal-hour float -> 12-hour clock, e.g. 6.5 -> '6:30am'."""
    hh = int(h)
    mm = int(round((h - hh) * 60))
    suffix = "am" if hh < 12 else "pm"
    disp = hh if hh <= 12 else hh - 12
    if disp == 0:
        disp = 12
    return f"{disp}:{mm:02d}{suffix}"


def _rows_for_day(day: _date) -> list[dict]:
    return db.query(
        "SELECT p.name AS name, r.shape AS shape, "
        "r.hour_from AS hour_from, r.hour_to AS hour_to, r.state AS state, "
        "COALESCE(lt.name, 'Time Off') AS pay_type "
        "FROM time_off_requests r "
        "JOIN people p ON p.odoo_id = r.person_odoo_id "
        "LEFT JOIN leave_types_cache lt "
        "  ON lt.holiday_status_id = r.holiday_status_id "
        "WHERE r.state = ANY(%s) "
        "AND r.date_from <= %s AND r.date_to >= %s "
        "ORDER BY p.name",
        (list(_VISIBLE_STATES), day, day),
    )


def time_off_entries_for_day(day: _date) -> list[dict]:
    """List of scheduler time-off entries for ``day`` (approved + pending)."""
    out: list[dict] = []
    for r in _rows_for_day(day):
        is_full = r["shape"] == "full_day"
        if is_full:
            hours = None
            time_range = ""
        else:
            hf = float(r["hour_from"] or 0)
            ht = float(r["hour_to"] or 0)
            hours = round(ht - hf, 2)
            time_range = f"{_fmt_hf(hf)}–{_fmt_hf(ht)}"
        out.append({
            "name": r["name"],
            "hours": hours,
            "pay_type": r["pay_type"],
            "time_range": time_range,
            "derived": False,
            "manual_absent": False,
            "pending": r["state"] != _APPROVED,
        })
    return out


def full_day_off_names(day: _date) -> set[str]:
    """Names of people who are off the WHOLE day (full_day shape). Partial-day
    people are intentionally excluded so they stay on the schedulable roster
    with a badge instead of disappearing."""
    return {
        r["name"] for r in _rows_for_day(day) if r["shape"] == "full_day"
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_scheduler_time_off.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/scheduler_time_off.py tests/test_scheduler_time_off.py
git commit -m "feat(scheduler): Odoo-backed time-off entry builder"
```

---

### Task 2: Repoint the scheduler route to the Odoo builder

**Files:**
- Modify: `src/zira_dashboard/routes/staffing.py` (`_safe_time_off_entries` ~81-90, `_timeoff_names_with_fallback` ~114-121, call site ~163)

- [ ] **Step 1: Replace `_safe_time_off_entries` body**

Find (~81-90):

```python
def _safe_time_off_entries(d):
    """Wrap stratustime_client.time_off_entries_for_day so a StratusTime
    ...
        if d == today:
            return _timeoff_entries_with_fallback(d)
        return stratustime_client.time_off_entries_for_day(d)
```

Replace the body with (keep the function name + signature so callers don't change):

```python
def _safe_time_off_entries(d):
    """Time-off entries for the scheduler, sourced from the Odoo-backed
    time_off_requests mirror (approved + pending). Never raises — a query
    failure degrades to an empty panel rather than a 500."""
    from .. import scheduler_time_off
    try:
        return scheduler_time_off.time_off_entries_for_day(d)
    except Exception:  # noqa: BLE001 — empty panel beats a broken scheduler
        return []
```

- [ ] **Step 2: Replace `_timeoff_names_with_fallback` body**

Find (~114-121) and replace its body so the schedulable-exclusion set uses **full-day** Odoo absences only:

```python
def _timeoff_names_with_fallback(day):
    """Names off the WHOLE day — removed from the schedulable roster.
    Partial-day people stay on the roster (badged) so they can still be
    assigned around their partial. Sourced from time_off_requests."""
    from .. import scheduler_time_off
    try:
        return scheduler_time_off.full_day_off_names(day)
    except Exception:  # noqa: BLE001
        return set()
```

- [ ] **Step 3: Simplify the call site at ~163**

Find:

```python
            time_off_today = _timeoff_names_with_fallback(d)
```

It already calls the rewritten helper — no change needed, but confirm there is no remaining `if d == today` branch that bypasses it. If `_timeoff_names_with_fallback` is only called inside an `if d == today:` guard, move the call so it runs for **every** day `d` (the scheduler supports viewing other days). Verify by reading lines 160-185.

- [ ] **Step 4: Verify compile**

Run: `python3 -m py_compile src/zira_dashboard/routes/staffing.py`
Expected: no output (success)

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/routes/staffing.py
git commit -m "feat(scheduler): read time off from Odoo, exclude only full-day absences"
```

---

### Task 3: Pending indicator in the template

**Files:**
- Modify: `src/zira_dashboard/templates/staffing.html` (Time Off panel ~85-92)

- [ ] **Step 1: Add a "pending" tag + muted styling to the time-off row**

Find (~89-91):

```jinja
<li class="time-off-row {% if is_absent %}absent{% elif is_partial %}partial{% else %}full{% endif %}{% if is_clearable %} clearable{% endif %}"...>
  <span class="name">...{% if is_partial %} <span class="partial-tag">partial</span>{% endif %}</span>
  <span class="ts-meta">{{ e.pay_type }}{% if is_partial %} · {% if e.time_range %}off {{ e.time_range }}{% else %}{{ e.hours }}h{% endif %}{% endif %}</span>
```

Add `{% if e.pending %} pending{% endif %}` to the `<li>` class list, and a pending chip after the partial tag:

```jinja
<li class="time-off-row {% if is_absent %}absent{% elif is_partial %}partial{% else %}full{% endif %}{% if is_clearable %} clearable{% endif %}{% if e.pending %} pending{% endif %}"...>
  <span class="name">...{% if is_partial %} <span class="partial-tag">partial</span>{% endif %}{% if e.pending %} <span class="pending-tag" title="Not yet approved in Odoo">pending</span>{% endif %}</span>
  <span class="ts-meta">{{ e.pay_type }}{% if is_partial %} · {% if e.time_range %}off {{ e.time_range }}{% else %}{{ e.hours }}h{% endif %}{% endif %}</span>
```

- [ ] **Step 2: Add CSS for `.pending-tag` and `.time-off-row.pending`**

Find the existing `.partial-tag` rule in the `<style>` block (search `partial-tag`) and add nearby:

```css
.time-off-row.pending { opacity: 0.7; }
.pending-tag {
  font-size: 0.7rem; font-weight: 600; color: #92400e;
  background: #fef3c7; border-radius: 0.25rem; padding: 0 0.3rem;
  margin-left: 0.25rem;
}
```

- [ ] **Step 3: Guard the clear (×) button so Odoo entries don't show it**

`is_clearable = is_partial` (~88) currently makes every partial clearable, but Odoo entries have no `request_id`/`emp_id` backend to clear. Change to only mark clearable when a clear key exists:

Find (~88): `{% set is_clearable = is_partial %}`
Replace: `{% set is_clearable = is_partial and (partial_clear_by_name and e.name in partial_clear_by_name) %}`

(`partial_clear_by_name` only includes entries with `request_id`/`emp_id`; Task 1 omits those, so Odoo partials render without a × button — correct for now.)

- [ ] **Step 4: Manual verification**

Run the app, visit `/staffing`, confirm: full-day Odoo leaves show in the panel; partial-day leaves show with their time range; a pending request shows the muted "pending" chip; no JS error from a missing clear handler.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/staffing.html
git commit -m "feat(scheduler): show pending Odoo time off, hide clear for Odoo partials"
```

---

### Task 4: Route smoke test (approved + pending render)

**Files:**
- Test: `tests/test_scheduler_time_off.py` (append)

- [ ] **Step 1: Write a test that the builder feeds the route shape**

```python
def test_entries_have_keys_the_template_reads(monkeypatch):
    _fake_db(monkeypatch, [{
        "name": "X", "shape": "midday_gap", "hour_from": 10.0,
        "hour_to": 12.0, "state": "validate", "pay_type": "PTO",
    }])
    e = sto.time_off_entries_for_day(date(2026, 6, 1))[0]
    for key in ("name", "hours", "pay_type", "time_range",
                "derived", "manual_absent", "pending"):
        assert key in e
    assert e["hours"] == 2.0
    assert e["time_range"] == "10:00am–12:00pm"
```

- [ ] **Step 2: Run + verify pass**

Run: `pytest tests/test_scheduler_time_off.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_scheduler_time_off.py
git commit -m "test(scheduler): assert Odoo time-off entry shape"
```

---

## Self-Review

- **Spec coverage:** Replace StratusTime → Odoo (Tasks 1-2 ✓). Approved + pending (Task 1 `_VISIBLE_STATES`, Task 3 pending chip ✓). Partial-day times shown (Task 1 `time_range`, already-rendered by template ✓). Schedule around partials — full-day excluded, partial kept (Task 1 `full_day_off_names`, Task 2 ✓).
- **Placeholders:** none — every step has concrete code.
- **Type consistency:** entry dict keys (`name/hours/pay_type/time_range/derived/manual_absent/pending`) match across Task 1 producer, Task 3 template, Task 4 test.
- **Risks flagged:** clear-partial for Odoo (out of scope, × hidden via Task 3 Step 3); `live_cache` bypassed (acceptable — local reads); verify the `d == today` guard in Task 2 Step 3 so other-day views also exclude full-day absences.
