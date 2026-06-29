# Inbox Time-Off Coverage Indicator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show, on every Pending Time Off row in the inbox, a peak-day "others off" count (with the requester's own department called out) and a hover/tap tooltip listing who is off each day of the request window.

**Architecture:** A new pure function in `time_off_context.py` computes the per-day breakdown from already-fetched rows; a batched I/O loader fetches approved leaves, other pending requests, and per-person departments in a fixed number of queries plus a cached holiday fetch. `exception_inbox._pending_time_off` attaches the breakdown to each row. A small Jinja partial renders the chip + tooltip, with CSS for styling and a tiny JS tap-toggle for touch.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, psycopg2 (`db.query`), pytest. Front-end is vanilla CSS + a single IIFE in `static/exceptions.js`. Tests run with `ZIRA_API_KEY=test .venv/bin/python -m pytest`.

**Spec:** `docs/superpowers/specs/2026-06-29-inbox-time-off-coverage-design.md`

---

## File Structure

- **Modify** `src/zira_dashboard/time_off_context.py` — add the pure `coverage_breakdown`, its helpers `_timing_label`/`_person_entry`, the batched loader `coverage_breakdowns_for`, and `_departments_by_person`/`_holiday_names`. Leaves existing `coverage_for` untouched.
- **Modify** `src/zira_dashboard/exception_inbox.py` — attach `coverage` to each shaped row in `_pending_time_off`.
- **Create** `src/zira_dashboard/templates/_inbox_coverage.html` — the chip + tooltip partial.
- **Modify** `src/zira_dashboard/templates/exceptions.html` — include the partial in the detail line when `row.coverage` is present.
- **Modify** `src/zira_dashboard/static/exceptions.css` — chip + tooltip styles.
- **Modify** `src/zira_dashboard/static/exceptions.js` — tap-to-toggle + outside-tap-to-close.
- **Modify** `tests/test_time_off_context.py` — unit tests for the pure function + loader.
- **Modify** `tests/test_exception_inbox.py` — builder attaches coverage; keep the existing call-count test green; render + static assertions.

---

## Task 1: Pure coverage breakdown in `time_off_context.py`

**Files:**
- Modify: `src/zira_dashboard/time_off_context.py`
- Test: `tests/test_time_off_context.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_time_off_context.py`:

```python
def _leave(pid, name, df, dt, *, depts=(), shape="full_day", hf=None, ht=None):
    return {"person_odoo_id": pid, "name": name, "date_from": df, "date_to": dt,
            "depts": set(depts), "shape": shape, "hour_from": hf, "hour_to": ht}


def test_coverage_breakdown_peak_is_worst_single_day():
    approved = [
        _leave(2, "Juan", date(2026, 7, 6), date(2026, 7, 7), depts={"Recycling"}),
        _leave(3, "Sam", date(2026, 7, 7), date(2026, 7, 7), depts={"Recycling"}),
        _leave(4, "Dana", date(2026, 7, 6), date(2026, 7, 7), depts={"Shipping"}),
    ]
    result = ctx.coverage_breakdown(
        approved, [], {}, {"Recycling"},
        date(2026, 7, 6), date(2026, 7, 8), requester_odoo_id=1)

    assert result["peak_count"] == 3            # Jul 7 has Juan, Sam, Dana
    assert result["peak_date"] == date(2026, 7, 7)
    assert result["peak_dept_count"] == 2       # Juan + Sam in Recycling
    assert result["scope"] == "department"
    assert result["dept_label"] == "Recycling"
    assert result["severity"] == "warn"
    # only days with someone off appear (Jul 8 had nobody -> skipped)
    assert [d["date"] for d in result["by_day"]] == [date(2026, 7, 6), date(2026, 7, 7)]


def test_coverage_breakdown_excludes_requester_and_dedupes_per_person():
    approved = [_leave(1, "Me", date(2026, 7, 6), date(2026, 7, 6))]      # requester
    pending = [_leave(2, "Juan", date(2026, 7, 6), date(2026, 7, 6))]
    approved += [_leave(2, "Juan", date(2026, 7, 6), date(2026, 7, 6))]   # same person, approved
    result = ctx.coverage_breakdown(
        approved, pending, {}, set(),
        date(2026, 7, 6), date(2026, 7, 6), requester_odoo_id=1)

    assert result["peak_count"] == 1                       # requester excluded, Juan counted once
    assert result["by_day"][0]["people"][0]["pending"] is False  # approved wins over pending


def test_coverage_breakdown_holiday_is_flag_not_count():
    result = ctx.coverage_breakdown(
        [], [], {date(2026, 7, 6): "Independence Day"}, set(),
        date(2026, 7, 6), date(2026, 7, 6), requester_odoo_id=1)

    assert result["peak_count"] == 0
    assert result["has_holiday"] is True
    assert result["severity"] == "clear"
    assert result["by_day"][0]["holiday"] == "Independence Day"


def test_coverage_breakdown_pending_marked_and_partial_label():
    pending = [_leave(2, "Lee", date(2026, 7, 6), date(2026, 7, 6),
                      shape="late_arrival", hf=8.0, ht=9.0)]
    result = ctx.coverage_breakdown(
        [], pending, {}, set(),
        date(2026, 7, 6), date(2026, 7, 6), requester_odoo_id=1)

    person = result["by_day"][0]["people"][0]
    assert person["pending"] is True
    assert person["label"] == "arrives 9:00am"
    assert result["severity"] == "ok"          # 1 off, no same-dept, below plant threshold


def test_coverage_breakdown_zero_is_clear():
    result = ctx.coverage_breakdown(
        [], [], {}, set(), date(2026, 7, 6), date(2026, 7, 6), requester_odoo_id=1)
    assert result == {
        "severity": "clear", "peak_count": 0, "peak_date": None,
        "peak_dept_count": 0, "scope": "plant", "dept_label": None,
        "has_holiday": False, "by_day": [], "more_days": 0,
    }


def test_coverage_breakdown_caps_long_windows():
    # 14 distinct off-days, each one person
    approved = [_leave(100 + i, f"P{i}", date(2026, 7, 1) + timedelta(days=i),
                       date(2026, 7, 1) + timedelta(days=i)) for i in range(14)]
    result = ctx.coverage_breakdown(
        approved, [], {}, set(),
        date(2026, 7, 1), date(2026, 7, 14), requester_odoo_id=1, max_days=10)

    assert len(result["by_day"]) == 10
    assert result["more_days"] == 4
    assert result["peak_count"] == 1           # peak computed over all days before the cap
```

Add `timedelta` to the import at the top of the test file:

```python
from datetime import date, timedelta
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_time_off_context.py -k coverage_breakdown -v`
Expected: FAIL with `AttributeError: module 'zira_dashboard.time_off_context' has no attribute 'coverage_breakdown'`

- [ ] **Step 3: Write the implementation**

At the top of `src/zira_dashboard/time_off_context.py`, update imports:

```python
from __future__ import annotations

from datetime import date, timedelta

from . import db, time_off_calendar
```

Add these functions (place them after `department_for_person`, before `coverage_for`):

```python
def _timing_label(r: dict) -> str:
    """Privacy-safe timing for one off-person ('full day', 'arrives 9:00am',
    'leaves 2:00pm', '10:00am–12:00pm'). Reuses the kiosk label engine; a
    leave with no hour bounds is a full day."""
    if r.get("hour_from") is None or r.get("hour_to") is None:
        return "full day"
    return time_off_calendar.label_for(r)


def _person_entry(r: dict, pending: bool, requester_depts: set[str]) -> dict:
    person_depts = set(r.get("depts") or ())
    return {
        "name": r["name"],
        "dept": (sorted(person_depts)[0] if person_depts else None),
        "label": _timing_label(r),
        "pending": pending,
        "same_dept": bool(person_depts & requester_depts),
    }


def coverage_breakdown(
    approved: list[dict],
    pending: list[dict],
    holiday_names: dict[date, str],
    requester_depts: set[str],
    date_from: date,
    date_to: date,
    requester_odoo_id: int,
    plant_peak_threshold: int = 3,
    max_days: int = 10,
) -> dict:
    """Per-day 'who else is off' breakdown over [date_from, date_to].

    Pure (no I/O). ``approved``/``pending`` rows carry person_odoo_id, name,
    shape, hour_from, hour_to, date_from, date_to, and a ``depts`` set. The
    requester is always excluded; a person counts once per day (approved wins
    over pending). Holidays are surfaced as a per-day flag, never added to the
    people count. Returns the peak day, that day's department count, severity,
    and a capped per-day list (only days with someone off OR a plant closure)."""
    requester_depts = set(requester_depts or ())

    def collect(day: date) -> list[dict]:
        seen: dict[int, dict] = {}
        for r in approved:
            pid = r["person_odoo_id"]
            if pid == requester_odoo_id or pid in seen:
                continue
            if r["date_from"] <= day <= r["date_to"]:
                seen[pid] = _person_entry(r, False, requester_depts)
        for r in pending:
            pid = r["person_odoo_id"]
            if pid == requester_odoo_id or pid in seen:
                continue
            if r["date_from"] <= day <= r["date_to"]:
                seen[pid] = _person_entry(r, True, requester_depts)
        return list(seen.values())

    full: list[dict] = []
    day = date_from
    while day <= date_to:
        people = collect(day)
        holiday = holiday_names.get(day)
        if people or holiday:
            people.sort(key=lambda p: (not p["same_dept"], p["name"].lower()))
            full.append({
                "date": day,
                "count": len(people),
                "dept_count": sum(1 for p in people if p["same_dept"]),
                "holiday": holiday,
                "people": people,
            })
        day = day + timedelta(days=1)

    peak_count, peak_date, peak_dept_count = 0, None, 0
    for entry in full:
        if entry["count"] > peak_count:
            peak_count = entry["count"]
            peak_date = entry["date"]
            peak_dept_count = entry["dept_count"]

    if peak_count == 0:
        severity = "clear"
    elif peak_dept_count > 0 or peak_count >= plant_peak_threshold:
        severity = "warn"
    else:
        severity = "ok"

    return {
        "severity": severity,
        "peak_count": peak_count,
        "peak_date": peak_date,
        "peak_dept_count": peak_dept_count,
        "scope": "department" if requester_depts else "plant",
        "dept_label": sorted(requester_depts)[0] if requester_depts else None,
        "has_holiday": any(e["holiday"] for e in full),
        "by_day": full[:max_days],
        "more_days": max(0, len(full) - max_days),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_time_off_context.py -v`
Expected: PASS (new `coverage_breakdown` tests + the existing `coverage_for`/`balance_for` tests).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/time_off_context.py tests/test_time_off_context.py
git commit -m "feat(inbox): pure per-day time-off coverage breakdown"
```

---

## Task 2: Batched loader `coverage_breakdowns_for`

**Files:**
- Modify: `src/zira_dashboard/time_off_context.py`
- Test: `tests/test_time_off_context.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_time_off_context.py`:

```python
def test_coverage_breakdowns_for_batches_queries(monkeypatch):
    calls = []

    def fake_query(sql, params):
        calls.append(sql)
        if "state = 'validate'" in sql:
            return [{"person_odoo_id": 2, "name": "Juan", "shape": "full_day",
                     "date_from": date(2026, 7, 6), "date_to": date(2026, 7, 6),
                     "hour_from": None, "hour_to": None}]
        if "IN ('draft'" in sql:
            return [{"person_odoo_id": 3, "name": "Lee", "shape": "full_day",
                     "date_from": date(2026, 7, 6), "date_to": date(2026, 7, 6),
                     "hour_from": None, "hour_to": None}]
        # departments lookup
        return [{"person_odoo_id": 2, "department": "Recycling"},
                {"person_odoo_id": 1, "department": "Recycling"}]

    monkeypatch.setattr(ctx.db, "query", fake_query)
    monkeypatch.setattr(ctx, "_holiday_names", lambda s, e: {})

    rows = [{"id": 55, "person_odoo_id": 1,
             "date_from": date(2026, 7, 6), "date_to": date(2026, 7, 6)}]
    out = ctx.coverage_breakdowns_for(rows)

    assert len(calls) == 3                       # approved, pending, departments
    cov = out[55]
    assert cov["peak_count"] == 2                # Juan + Lee
    assert cov["peak_dept_count"] == 1           # Juan shares requester's Recycling
    assert cov["scope"] == "department"


def test_coverage_breakdowns_for_empty_rows_does_nothing(monkeypatch):
    monkeypatch.setattr(ctx.db, "query",
                        lambda *a: (_ for _ in ()).throw(AssertionError("no query")))
    assert ctx.coverage_breakdowns_for([]) == {}


def test_holiday_names_fan_out_and_fail_soft(monkeypatch):
    from zira_dashboard import odoo_client
    monkeypatch.setattr(odoo_client, "fetch_public_holidays",
                        lambda s, e: [{"name": "July 4", "date_from": "2026-07-03",
                                       "date_to": "2026-07-03"}])
    names = ctx._holiday_names(date(2026, 7, 1), date(2026, 7, 6))
    assert names == {date(2026, 7, 3): "July 4"}

    def boom(s, e):
        raise RuntimeError("odoo down")
    monkeypatch.setattr(odoo_client, "fetch_public_holidays", boom)
    assert ctx._holiday_names(date(2026, 7, 1), date(2026, 7, 6)) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_time_off_context.py -k "breakdowns_for or holiday_names" -v`
Expected: FAIL with `AttributeError: ... has no attribute 'coverage_breakdowns_for'`

- [ ] **Step 3: Write the implementation**

Add to `src/zira_dashboard/time_off_context.py` (after `coverage_breakdown`):

```python
_PENDING_STATES = ("draft", "draft_edit", "confirm", "validate1")

_OVERLAP_SELECT = (
    "SELECT r.person_odoo_id, "
    "COALESCE(p.name, '#' || r.person_odoo_id::text) AS name, "
    "r.shape, r.date_from, r.date_to, r.hour_from, r.hour_to "
    "FROM time_off_requests r "
    "LEFT JOIN people p ON p.odoo_id = r.person_odoo_id "
)


def _departments_by_person(ids: list[int]) -> dict[int, set[str]]:
    """Map each person_odoo_id to the set of departments they default into."""
    if not ids:
        return {}
    rows = db.query(
        "SELECT pe.odoo_id AS person_odoo_id, wc.department AS department "
        "FROM work_center_default_people wcdp "
        "JOIN work_centers wc ON wc.id = wcdp.wc_id "
        "JOIN people pe ON pe.id = wcdp.person_id "
        "WHERE pe.odoo_id = ANY(%s) AND wc.department IS NOT NULL "
        "AND wc.department <> ''",
        (ids,),
    )
    out: dict[int, set[str]] = {}
    for r in rows:
        out.setdefault(r["person_odoo_id"], set()).add(r["department"])
    return out


def _holiday_names(start_d: date, end_d: date) -> dict[date, str]:
    """Public-holiday closures fanned out to {date: name}. Cached via
    odoo_client; a failing fetch degrades to {} so coverage still renders."""
    from . import odoo_client

    try:
        rows = odoo_client.fetch_public_holidays(start_d, end_d)
    except Exception:  # noqa: BLE001 — never let the holiday fetch break the inbox
        return {}
    out: dict[date, str] = {}
    for h in rows:
        hs = time_off_calendar.parse_holiday_date(h.get("date_from"))
        he = time_off_calendar.parse_holiday_date(h.get("date_to"))
        if not hs or not he:
            continue
        cur = max(hs, start_d)
        end = min(he, end_d)
        while cur <= end:
            out.setdefault(cur, h.get("name") or "Plant closed")
            cur = cur + timedelta(days=1)
    return out


def coverage_breakdowns_for(rows: list[dict]) -> dict[int, dict]:
    """For each pending inbox row, compute its coverage breakdown.

    Three batched DB queries (approved leaves, other pending requests, and
    departments for everyone involved) over the union date-range of all rows,
    plus one cached holiday fetch — independent of the row count. Returns
    ``{request_id: breakdown}``."""
    if not rows:
        return {}
    window_start = min(r["date_from"] for r in rows)
    window_end = max(r["date_to"] for r in rows)

    approved = db.query(
        _OVERLAP_SELECT + "WHERE r.state = 'validate' "
        "AND r.date_to >= %s AND r.date_from <= %s",
        (window_start, window_end),
    )
    pending = db.query(
        _OVERLAP_SELECT + "WHERE r.state IN ('draft', 'draft_edit', "
        "'confirm', 'validate1') AND r.date_to >= %s AND r.date_from <= %s",
        (window_start, window_end),
    )
    holiday_names = _holiday_names(window_start, window_end)

    ids = {r["person_odoo_id"] for r in rows}
    ids |= {a["person_odoo_id"] for a in approved}
    ids |= {p["person_odoo_id"] for p in pending}
    dept_map = _departments_by_person(list(ids))
    for a in approved:
        a["depts"] = dept_map.get(a["person_odoo_id"], set())
    for p in pending:
        p["depts"] = dept_map.get(p["person_odoo_id"], set())

    out: dict[int, dict] = {}
    for r in rows:
        out[r["id"]] = coverage_breakdown(
            approved, pending, holiday_names,
            dept_map.get(r["person_odoo_id"], set()),
            r["date_from"], r["date_to"], r["person_odoo_id"],
        )
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_time_off_context.py -v`
Expected: PASS (all coverage tests + existing tests).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/time_off_context.py tests/test_time_off_context.py
git commit -m "feat(inbox): batched loader for time-off coverage breakdowns"
```

---

## Task 3: Attach coverage in `exception_inbox._pending_time_off`

**Files:**
- Modify: `src/zira_dashboard/exception_inbox.py`
- Test: `tests/test_exception_inbox.py`

- [ ] **Step 1: Update the existing call-count test and add a new attachment test**

In `tests/test_exception_inbox.py`, the existing `test_pending_time_off_uses_window_count` will now also trigger the coverage loader. Stub it so the DB-call count stays 1. Add this line inside that test, right after `monkeypatch.setattr(db, "query", fake_query)`:

```python
    monkeypatch.setattr(exception_inbox.time_off_context,
                        "coverage_breakdowns_for", lambda rows: {})
```

Then add a new test below it:

```python
def test_pending_time_off_attaches_coverage(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params: [{
        "id": 20, "person_odoo_id": 7, "odoo_leave_id": 99, "name": "Eli",
        "shape": "full_day", "state": "confirm",
        "date_from": date(2026, 6, 20), "date_to": date(2026, 6, 20),
        "hour_from": None, "hour_to": None, "sync_error": None,
        "leave_type": "Vacation", "total_count": 1,
    }])
    monkeypatch.setattr(
        exception_inbox.time_off_context, "coverage_breakdowns_for",
        lambda rows: {20: {"severity": "warn", "peak_count": 3}})

    _count, rows = exception_inbox._pending_time_off(date(2026, 6, 19), limit=8)

    assert rows[0]["coverage"] == {"severity": "warn", "peak_count": 3}
```

- [ ] **Step 2: Run tests to verify the new one fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exception_inbox.py -k "pending_time_off" -v`
Expected: `test_pending_time_off_attaches_coverage` FAILS with `KeyError: 'coverage'`; `test_pending_time_off_uses_window_count` PASSES.

- [ ] **Step 3: Write the implementation**

In `src/zira_dashboard/exception_inbox.py`, add a top-level import so the loader is patchable as `exception_inbox.time_off_context` in tests. The existing import block (lines 13-14) reads:

```python
from . import plant_day, schedule_store, staffing
from . import inbox_keys
```

Add a third line below them:

```python
from . import time_off_context
```

Note on the module contract: this module's docstring says it avoids *fresh* Odoo calls. The coverage loader's only Odoo touch is `_holiday_names → odoo_client.fetch_public_holidays`, which is cached in-process and fail-soft (returns `{}` on error), so it stays within the "cached or local-mirror" contract — no new hard Odoo dependency on render.

Then, in `_pending_time_off`, after the `shaped = [ ... ]` list comprehension and before the `return`, attach coverage:

```python
    coverage = time_off_context.coverage_breakdowns_for(shaped)
    for row in shaped:
        row["coverage"] = coverage.get(row["id"])
    return int(rows[0].get("total_count") or 0) if rows else 0, shaped
```

Note: `shaped` rows carry `id`, `date_from`/`date_to` are NOT in `shaped` — the loader reads `r["date_from"]`/`r["date_to"]`/`r["person_odoo_id"]`. Add those three keys to each shaped dict so the loader has what it needs. Update the `shaped` comprehension to include:

```python
            "person_odoo_id": r["person_odoo_id"],
            "date_from": r["date_from"],
            "date_to": r["date_to"],
```

(Add them alongside the existing `"id": r["id"]` etc. They are internal fields the template ignores.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exception_inbox.py -k "pending_time_off" -v`
Expected: PASS for both tests.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/exception_inbox.py tests/test_exception_inbox.py
git commit -m "feat(inbox): attach coverage breakdown to pending time-off rows"
```

---

## Task 4: Coverage partial template + include

**Files:**
- Create: `src/zira_dashboard/templates/_inbox_coverage.html`
- Modify: `src/zira_dashboard/templates/exceptions.html:90`
- Test: `tests/test_exception_inbox.py`

- [ ] **Step 1: Write the failing render test**

Add to `tests/test_exception_inbox.py`:

```python
def test_exceptions_page_renders_coverage_chip(monkeypatch):
    time_off_row = {
        "name": "Maria", "label": "Jul 6 – Jul 8", "detail": "Vacation · confirm",
        "priority": "info", "badge": "Approval",
        "row_key": "time_off:55:confirm", "item_key": "time_off:55",
        "action": {"type": "time_off", "request_id": 55},
        "coverage": {
            "severity": "warn", "peak_count": 4, "peak_date": date(2026, 7, 7),
            "peak_dept_count": 2, "scope": "department", "dept_label": "Recycling",
            "has_holiday": False, "more_days": 0,
            "by_day": [{
                "date": date(2026, 7, 7), "count": 4, "dept_count": 2,
                "holiday": None,
                "people": [
                    {"name": "Juan", "dept": "Recycling", "label": "full day",
                     "pending": False, "same_dept": True},
                    {"name": "Lee", "dept": None, "label": "arrives 9:00am",
                     "pending": True, "same_dept": False},
                ],
            }],
        },
    }
    snapshot = {
        "today": "2026-07-01", "generated_at": "7:35 AM", "total": 1,
        "urgent_total": 0, "follow_up_total": 0, "source_errors": [],
        "work_centers": [], "people": [], "sections": [],
        "queue": [{**time_off_row, "section_id": "time_off",
                   "category_label": "Pending Time Off", "tone": "info"}],
    }
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot",
                        lambda: snapshot)
    client = TestClient(app)

    resp = client.get("/exceptions")

    assert resp.status_code == 200
    assert 'class="cov cov-warn"' in resp.text
    assert "4 off peak" in resp.text
    assert "2 in Recycling" in resp.text
    assert 'class="cov-tip"' in resp.text
    assert "Juan" in resp.text and "arrives 9:00am" in resp.text
    assert "pending" in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exception_inbox.py -k "coverage_chip" -v`
Expected: FAIL (chip markup absent).

- [ ] **Step 3: Create the partial**

Create `src/zira_dashboard/templates/_inbox_coverage.html`:

```html
{% set cov = row.coverage %}
{% set hol_only = cov.peak_count == 0 and cov.has_holiday %}
<span class="cov-wrap" data-cov>
  <span class="cov cov-{{ 'hol' if hol_only else cov.severity }}"
        tabindex="0" role="button" aria-label="Coverage during this time off">
    {% if hol_only %}
      Plant closed
    {% elif cov.peak_count == 0 %}
      ✓ no overlap
    {% else %}
      ▲ {{ cov.peak_count }} off peak{% if cov.scope == 'department' and cov.peak_dept_count and cov.dept_label %} · {{ cov.peak_dept_count }} in {{ cov.dept_label }}{% endif %}
    {% endif %}
  </span>
  {% if cov.by_day %}
  <span class="cov-tip" role="tooltip">
    <span class="cov-tip-head">Others off during this window{% if cov.peak_date %} · peak {{ cov.peak_date.strftime('%a %b %-d') }}{% endif %}</span>
    {% for day in cov.by_day %}
      <span class="cov-day">
        <b>{{ day.date.strftime('%a %b %-d') }}{% if day.count %} — {{ day.count }} off{% endif %}</b>
        {% if day.holiday %}<span class="cov-hol-line">Plant closed — {{ day.holiday }}</span>{% endif %}
        {% for p in day.people %}
          <span class="cov-name{% if p.same_dept %} cov-same{% endif %}">{{ p.name }}{% if p.dept %} · {{ p.dept }}{% endif %}{% if p.label and p.label != 'full day' %} ({{ p.label }}){% endif %}{% if p.pending %} · pending{% endif %}</span>
        {% endfor %}
      </span>
    {% endfor %}
    {% if cov.more_days %}<span class="cov-more">+ {{ cov.more_days }} more days</span>{% endif %}
  </span>
  {% endif %}
</span>
```

- [ ] **Step 4: Include it in the detail line**

In `src/zira_dashboard/templates/exceptions.html`, change the detail line (around line 90) from:

```html
          <div class="exception-detail">{{ row.label }}{% if row.detail %} · {{ row.detail }}{% endif %}</div>
```

to:

```html
          <div class="exception-detail">{{ row.label }}{% if row.detail %} · {{ row.detail }}{% endif %}{% if row.coverage %} · {% include "_inbox_coverage.html" %}{% endif %}</div>
```

- [ ] **Step 5: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exception_inbox.py -k "coverage_chip" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/templates/_inbox_coverage.html src/zira_dashboard/templates/exceptions.html tests/test_exception_inbox.py
git commit -m "feat(inbox): render time-off coverage chip + tooltip"
```

---

## Task 5: Chip + tooltip CSS

**Files:**
- Modify: `src/zira_dashboard/static/exceptions.css`
- Test: `tests/test_exception_inbox.py`

- [ ] **Step 1: Write the failing static test**

Add to `tests/test_exception_inbox.py`:

```python
def test_exceptions_css_has_coverage_chip_styles():
    css = (STATIC_DIR / "exceptions.css").read_text(encoding="utf-8")

    assert ".cov-wrap" in css
    assert ".cov-warn" in css
    assert ".cov-ok" in css
    assert ".cov-clear" in css
    assert ".cov-hol" in css
    assert ".cov-tip" in css
    # tooltip shows on hover and when tapped open
    assert ".cov-wrap:hover .cov-tip" in css
    assert ".cov-wrap.cov-open .cov-tip" in css
    assert ".cov-same" in css
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exception_inbox.py -k "css_has_coverage" -v`
Expected: FAIL.

- [ ] **Step 3: Append the styles**

Append to `src/zira_dashboard/static/exceptions.css`:

```css
/* Time-off coverage chip + hover/tap tooltip (inbox time-off rows) */
.cov-wrap { position: relative; display: inline-block; }
.cov {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 11.5px; font-weight: 650; line-height: 1;
  padding: 2px 9px; border-radius: 999px; cursor: default; white-space: nowrap;
  border: 1px solid transparent;
}
.cov-warn  { background: #fff3e0; color: #b25b00; border-color: #f5d9b0; }
.cov-ok    { background: #eef6ef; color: #2d7a44; border-color: #cfe6d4; }
.cov-clear { background: #f3f6f4; color: #6a7d70; border-color: #e0e7e2; }
.cov-hol   { background: #eef2fb; color: #3a55a8; border-color: #d4ddf3; }

.cov-tip {
  display: none; position: absolute; top: 135%; left: 0; z-index: 30;
  width: 300px; max-width: 78vw; padding: 11px 13px;
  background: #1f2733; color: #e7ecf2; border-radius: 9px;
  font-size: 12.5px; line-height: 1.45; font-weight: 400;
  box-shadow: 0 8px 24px rgba(0, 0, 0, .22); white-space: normal;
}
.cov-tip::before {
  content: ""; position: absolute; top: -6px; left: 18px;
  border: 6px solid transparent; border-top: 0; border-bottom-color: #1f2733;
}
.cov-wrap:hover .cov-tip,
.cov-wrap:focus-within .cov-tip,
.cov-wrap.cov-open .cov-tip { display: block; }

.cov-tip-head {
  display: block; margin-bottom: 6px; font-size: 11px; font-weight: 700;
  letter-spacing: .04em; text-transform: uppercase; color: #9fb0c3;
}
.cov-day { display: block; margin-bottom: 7px; }
.cov-day b { color: #fff; font-weight: 650; }
.cov-name { display: block; padding-left: 10px; color: #cdd8e4; }
.cov-name.cov-same { color: #fff; font-weight: 600; }
.cov-hol-line { display: block; padding-left: 10px; color: #f0b86c; }
.cov-more { display: block; margin-top: 4px; color: #9fb0c3; font-style: italic; }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exception_inbox.py -k "css_has_coverage" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/static/exceptions.css tests/test_exception_inbox.py
git commit -m "feat(inbox): style time-off coverage chip + tooltip"
```

---

## Task 6: Tap-to-toggle JS for touch

**Files:**
- Modify: `src/zira_dashboard/static/exceptions.js`
- Test: `tests/test_exception_inbox.py`

- [ ] **Step 1: Write the failing static test**

Add to `tests/test_exception_inbox.py`:

```python
def test_exceptions_js_toggles_coverage_tooltip_on_tap():
    js = (STATIC_DIR / "exceptions.js").read_text(encoding="utf-8")

    assert "closest('[data-cov]')" in js
    assert "classList.toggle('cov-open')" in js
    # tapping outside closes any open coverage tooltip
    assert "cov-open" in js
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exception_inbox.py -k "toggles_coverage" -v`
Expected: FAIL.

- [ ] **Step 3: Add the handler**

In `src/zira_dashboard/static/exceptions.js`, inside the existing IIFE, just before the final initialization block (the line `try { currentFocus = sessionStorage.getItem('exceptions_focus') ...`), add:

```javascript
  // Coverage chip: hover shows the tooltip on desktop (CSS); on touch, a tap
  // toggles it open and a tap elsewhere closes it.
  document.addEventListener('click', function (event) {
    var wrap = event.target.closest('[data-cov]');
    document.querySelectorAll('[data-cov].cov-open').forEach(function (open) {
      if (open !== wrap) open.classList.remove('cov-open');
    });
    if (wrap) {
      event.stopPropagation();
      wrap.classList.toggle('cov-open');
    }
  });
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exception_inbox.py -k "toggles_coverage" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/static/exceptions.js tests/test_exception_inbox.py
git commit -m "feat(inbox): tap-to-toggle coverage tooltip on touch"
```

---

## Task 7: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest`
Expected: PASS (DATABASE_URL/Odoo-gated tests skip, as documented). Confirm no regressions in `test_exception_inbox.py`, `test_time_off_context.py`, or the JS/CSS static tests.

- [ ] **Step 2: Manual smoke (optional but recommended)**

Run the app locally and open `/exceptions` with at least one pending time-off request whose window overlaps an approved leave. Confirm:
- the chip appears in the detail line with the peak count and (if the requester has a department) the "N in <dept>" suffix;
- hovering the chip on desktop shows the per-day tooltip; tapping it on a touch device toggles it;
- a request with no overlap shows the quiet `✓ no overlap` chip;
- a window covering a plant holiday shows `Plant closed` and a holiday line in the tooltip.

- [ ] **Step 3: Final commit (if any verification fixes were needed)**

```bash
git add -A
git commit -m "test(inbox): verify time-off coverage indicator end-to-end"
```

---

## Self-Review Notes

- **Spec coverage:** plant-total-with-dept-emphasis (Task 1 `peak_dept_count` + `dept_label`; Task 4 chip), approved+pending+holidays (Task 1/2), peak-day headline + per-day hover (Task 1 `by_day`/`peak_*`; Task 4 tooltip), severity colors (Task 1 `severity`; Task 5 CSS), zero-case quiet chip (Task 4 `✓ no overlap`), holiday-as-flag + display precedence (Task 1 `has_holiday`; Task 4 `hol_only`), privacy timing labels (`_timing_label`), long-window cap (Task 1 `max_days`/`more_days`), fail-soft holidays (Task 2 `_holiday_names`), batched queries (Task 2), touch fallback (Task 6).
- **No placeholders:** every code step shows complete code; every run step shows the exact command + expected result.
- **Type consistency:** the breakdown dict shape returned by `coverage_breakdown` (Task 1) is the exact shape consumed by the template (Task 4) and asserted in the render test; `coverage_breakdowns_for` returns `{request_id: breakdown}` consumed by `_pending_time_off` (Task 3).
```
