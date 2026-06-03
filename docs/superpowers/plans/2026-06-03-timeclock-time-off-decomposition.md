# timeclock_time_off.py Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Extract the pure wizard validators and the pure "Who's Out" calendar/label math out of the 1295-line `routes/timeclock_time_off.py` into `time_off_wizard.py` + `time_off_calendar.py`, behavior- and UI-identical, and give that currently-untested date/validation logic direct unit tests.

**Architecture:** The validators (`shape_to_hour_bounds` etc.) and the calendar label/grid math are pure (no I/O). Move them to two new leaf modules (deps: only `time_format` + stdlib), mirroring `staffing_view.py`. The route keeps all I/O, token auth, and — critically — keeps a thin `_approved_by_day(start, end)` wrapper (does the `db.query` + safe holiday fetch, delegates to a pure `fan_out_approved`) plus re-imports the moved validators under their existing `_`-prefixed names, so every `timeclock_time_off.<name>` monkeypatch target in the test suite keeps resolving and the existing route tests pass unchanged. **Defer** the Medium-risk `time_off_store`/leave-types-upsert dedup.

**Tech Stack:** Python 3.11, pytest, FastAPI/Jinja (unchanged). New tests are pure (no `DATABASE_URL`).

---

## File Structure

- **Create** `src/zira_dashboard/time_off_wizard.py` — pure `parse_time_to_float`, `shape_to_hour_bounds`, `compute_working_hours_json`. No imports beyond stdlib. Also `VALID_SHAPES` if it helps (optional).
- **Create** `src/zira_dashboard/time_off_calendar.py` — pure `label_for`, `parse_holiday_date`, `month_bounds`, `fan_out_approved`, `build_calendar_grid`. Only dep: `from ..time_format import fmt_decimal_hour` (leaf).
- **Create** `tests/test_time_off_wizard.py`, `tests/test_time_off_calendar.py` — pure characterization tests.
- **Modify** `src/zira_dashboard/routes/timeclock_time_off.py` — re-import the moved names under their `_`-prefixed aliases; keep thin `_approved_by_day` + `_build_calendar_context` wrappers; delete the moved bodies.

**Behavior-preserving constraint (read first):** `tests/test_time_off_routes.py` monkeypatches `timeclock_time_off._shape_to_hour_bounds` (~line 250) and `timeclock_time_off._approved_by_day` (~lines 288, 301). Therefore: (a) re-import moved validators as `from ..time_off_wizard import shape_to_hour_bounds as _shape_to_hour_bounds` etc., and call them via those module-local names; (b) keep `_approved_by_day` as a real function in the route. Do NOT change the route's public/patched surface.

---

## Task 1: Extract pure wizard validators → `time_off_wizard.py`

**Files:**
- Create: `src/zira_dashboard/time_off_wizard.py`
- Create: `tests/test_time_off_wizard.py`
- Modify: `src/zira_dashboard/routes/timeclock_time_off.py`

- [ ] **Step 1: Read** the three functions in `routes/timeclock_time_off.py`: `_parse_time_to_float` (~432-445), `_shape_to_hour_bounds` (~448-496), `_compute_working_hours_json` (~499-528). Confirm pure (no db/odoo/import-of-route). Note exact bodies.

- [ ] **Step 2: Create `time_off_wizard.py`** with module docstring ("Pure validators for the kiosk time-off wizard: parse a HH:MM string, map a leave shape + times to (hour_from, hour_to) bounds with validation, and compute the complementary working-hours windows. No I/O — extracted from routes/timeclock_time_off.py.") + `from __future__ import annotations`, then the three functions moved VERBATIM with public names `parse_time_to_float`, `shape_to_hour_bounds`, `compute_working_hours_json`. Same logic, same error strings, same boundary comparisons.

- [ ] **Step 3: Wire the route.** Delete the three defs; add `from ..time_off_wizard import (parse_time_to_float as _parse_time_to_float, shape_to_hour_bounds as _shape_to_hour_bounds, compute_working_hours_json as _compute_working_hours_json)`. All existing call-sites (`request_submit`, `mine_edit_submit`) keep working unchanged, and the `_shape_to_hour_bounds` monkeypatch target still resolves. Remove any now-unused import in the route (ruff F401).

- [ ] **Step 4: Characterization tests** in `tests/test_time_off_wizard.py` (`from zira_dashboard import time_off_wizard as w`), shift `(6.0, 14.5)`:

```python
def test_parse_time_to_float():
    assert w.parse_time_to_float("09:30") == 9.5
    assert w.parse_time_to_float("14:00") == 14.0
    for bad in (None, "", "bad", "9"):
        assert w.parse_time_to_float(bad) is None


def test_shape_to_hour_bounds_full_day_ignores_times():
    assert w.shape_to_hour_bounds("full_day", "09:00", "10:00", 6.0, 14.5) == (None, None, None)


def test_shape_to_hour_bounds_late_arrival():
    assert w.shape_to_hour_bounds("late_arrival", None, "09:00", 6.0, 14.5) == (6.0, 9.0, None)
    assert w.shape_to_hour_bounds("late_arrival", None, None, 6.0, 14.5)[2]  # error: arrival required
    assert w.shape_to_hour_bounds("late_arrival", None, "06:00", 6.0, 14.5)[2]  # not after shift start
    assert w.shape_to_hour_bounds("late_arrival", None, "15:00", 6.0, 14.5)[2]  # outside shift


def test_shape_to_hour_bounds_early_leave():
    assert w.shape_to_hour_bounds("early_leave", "13:00", None, 6.0, 14.5) == (13.0, 14.5, None)
    assert w.shape_to_hour_bounds("early_leave", "14:30", None, 6.0, 14.5)[2]  # not before shift end


def test_shape_to_hour_bounds_midday_gap():
    assert w.shape_to_hour_bounds("midday_gap", "10:00", "12:00", 6.0, 14.5) == (10.0, 12.0, None)
    assert w.shape_to_hour_bounds("midday_gap", "12:00", "10:00", 6.0, 14.5)[2]  # b<=a
    assert w.shape_to_hour_bounds("nope", "1", "2", 6.0, 14.5)[2].startswith("Unknown")


def test_compute_working_hours_json():
    assert w.compute_working_hours_json("full_day", None, None, 6.0, 14.5) is None
    assert w.compute_working_hours_json("late_arrival", 6.0, 9.0, 6.0, 14.5) == [{"from": 9.0, "to": 14.5}]
    assert w.compute_working_hours_json("early_leave", 13.0, 14.5, 6.0, 14.5) == [{"from": 6.0, "to": 13.0}]
    assert w.compute_working_hours_json("midday_gap", 10.0, 12.0, 6.0, 14.5) == [{"from": 6.0, "to": 10.0}, {"from": 12.0, "to": 14.5}]
```

(Before asserting, the implementer must read the REAL function bodies and adjust the expected error-message checks / exact shapes to match current behavior — these are characterizations of existing code. If any expected value above is wrong vs the real code, fix the TEST to match reality and note it.)

- [ ] **Step 5: Verify.** `pytest tests/test_time_off_wizard.py -v` PASS; `pytest -q` green (561 + new, 207 skipped, 0 failed); `ruff check src/zira_dashboard/time_off_wizard.py src/zira_dashboard/routes/timeclock_time_off.py tests/test_time_off_wizard.py` → "All checks passed!"; `python -c "import sys; sys.path.insert(0,'src'); import zira_dashboard.app; print('OK')"`.

- [ ] **Step 6: Commit (local).** `git add src/zira_dashboard/time_off_wizard.py tests/test_time_off_wizard.py src/zira_dashboard/routes/timeclock_time_off.py && git commit -m "refactor(time-off): extract pure wizard validators to time_off_wizard"`

---

## Task 2: Extract the pure calendar engine → `time_off_calendar.py`

**Files:**
- Create: `src/zira_dashboard/time_off_calendar.py`
- Create: `tests/test_time_off_calendar.py`
- Modify: `src/zira_dashboard/routes/timeclock_time_off.py`

- [ ] **Step 1: Read** `_label_for` (~1087-1105), `_parse_holiday_date` (~1108-1123), `_approved_by_day` (~1126-1186), `_build_calendar_context` (~1189-1247). Identify the I/O in `_approved_by_day` (the `db.query` for leaves + the `try/except`-wrapped `odoo_client.fetch_public_holidays`) vs its pure fan-out (building `{date: [entry,...]}` using `_label_for` + `_parse_holiday_date`). Identify the pure month-grid math in `_build_calendar_context` (month parse + fallback, prev/next anchors incl. the December year-bump, `calendar.Calendar` weeks, Sunday-drop, cell dicts) vs the `range_start`/`range_end` bounds it computes before the I/O.

- [ ] **Step 2: Create `time_off_calendar.py`** (docstring: pure Who's-Out calendar engine; only dep `from ..time_format import fmt_decimal_hour`) with:
  - `label_for(r)` — VERBATIM body of `_label_for`.
  - `parse_holiday_date(s)` — VERBATIM body of `_parse_holiday_date`.
  - `month_bounds(month)` — the pure month parse/fallback + `(first_of_month, range_start, range_end, next_month_str, prev_month_str)` computation lifted from `_build_calendar_context` (so the route can get bounds before doing I/O). Preserve the malformed-month fallback and the Dec→Jan year bump EXACTLY.
  - `fan_out_approved(leave_rows, holiday_rows, start_d, end_d)` — the pure fan-out half of `_approved_by_day`: per-day expansion of each leave row (clipped to `[start_d, end_d]`, `full`/`label_for` per shape) + holiday rows (`{name, label, source:"holiday"}`). Returns `{date: [entry,...]}`.
  - `build_calendar_grid(month, off_map)` — the pure grid assembly (weeks, Sunday-drop, `outside`/`is_today`/`is_current_month`, heading, prev/next) using `month_bounds`.

- [ ] **Step 3: Wire the route, preserving monkeypatch targets.**
  - Keep `_approved_by_day(start_d, end_d)` in the route as a THIN wrapper: run the leaves `db.query`, do the `try/except` holiday fetch (degrade to `[]`), then `return time_off_calendar.fan_out_approved(leaves, holidays, start_d, end_d)`. (The test monkeypatches THIS — keep it.)
  - Keep `_build_calendar_context(month)` in the route as a thin orchestrator: `first, range_start, range_end, nxt, prv = time_off_calendar.month_bounds(month)` (or compute bounds via month_bounds), `off_map = _approved_by_day(range_start, range_end)`, `return time_off_calendar.build_calendar_grid(month, off_map)`. (Adjust to the real return contract `_build_calendar_context` had — same dict keys the template uses.)
  - Add `from .. import time_off_calendar`. Delete the moved bodies (`_label_for`, `_parse_holiday_date`, and the pure halves). Remove now-unused imports (ruff F401) — note `calendar`/`fmt_decimal_hour` may no longer be used in the route after the move; drop them if so.

- [ ] **Step 4: Characterization tests** in `tests/test_time_off_calendar.py` (`from zira_dashboard import time_off_calendar as c`). After extraction, read the real outputs and pin:
  - `label_for`: `{"shape":"full_day"}` → "full day"; `late_arrival` w/ `hour_to=9.0` → "arrives 9:00am" (no leave-type name — privacy); `early_leave` w/ `hour_from=14.0` → "leaves 2:00pm"; `midday_gap` `hf=10,ht=12` → "10:00am–12:00pm"; `hour_from=None` coerced to 0 without crashing.
  - `parse_holiday_date`: `"2026-07-04 00:00:00"` → `date(2026,7,4)`; a `date` in → same date; `None`/`""`/"garbage" → `None`.
  - `month_bounds`: `"2026-06"` → next "2026-07", prev "2026-05"; `"2026-12"` → next "2027-01"; `None`/"nonsense"/"2026-13" → current-month fallback, no raise.
  - `build_calendar_grid("2026-06", {})`: heading "June 2026"; every week has 6 cells (no Sunday); `is_current_month` correct; spill-over days flagged `outside`.
  - `fan_out_approved`: a multi-day full-day leave fans to one `full=True` entry per in-range day; a `midday_gap` row → `full=False` + the label string; a holiday row → `{label:"Plant Closed"/real, source:"holiday"}` per day; clipping at range edges. (Match the REAL entry dict keys you find.)

- [ ] **Step 5: Verify.** `pytest tests/test_time_off_calendar.py -v` PASS; **`pytest tests/test_time_off_routes.py -v` PASS unchanged** (the `_approved_by_day` monkeypatch still works); `pytest -q` green, 0 failed; `ruff check` on the 3 files → clean; app import OK.

- [ ] **Step 6: Commit (local).** `git add src/zira_dashboard/time_off_calendar.py tests/test_time_off_calendar.py src/zira_dashboard/routes/timeclock_time_off.py && git commit -m "refactor(time-off): extract pure Who's-Out calendar engine to time_off_calendar"`

---

## Deferred (NOT in this plan)

`time_off_store.py` and the `leave_types_cache` upsert dedup vs `time_off_sync.py` — Medium risk, low value. The count/warning twins (`_pending_count`/`_sync_error_warning`) are *intentionally* duplicated (the file's docstrings say so); the leave-types upsert dedup must preserve per-row `try/except` isolation AND the asymmetric `odoo_client._leave_types_cache=None` bust (present in the route's fallback, absent in the poller). Tackle separately, behind its own `poll_odoo_leaves` characterization tests.

## Notes / guardrails

- **Both new modules are pure leaves** — no `db`/`odoo_client`/FastAPI/template/`routes` imports; only stdlib + (calendar module) `time_format`. No import cycles.
- **Preserve every monkeypatch target** under its `timeclock_time_off.<name>` path via re-import aliases; keep `_approved_by_day` + `_build_calendar_context` as real route functions.
- **Preserve the holiday-fetch `try/except`** in the route's `_approved_by_day` wrapper (degrade to `[]`, not 500).
- **Don't touch** the token-auth imports from `.timeclock`, the `odoo_client._leave_types_cache` poke, or any data-access/store function — all out of scope.
- Move bodies VERBATIM; these are characterizations of existing behavior, not rewrites.

## CHANGELOG
After both tasks ship + final review, add one `### TIME` entry under today's date summarizing the timeclock_time_off.py decomposition (behavior-identical; wizard + calendar math now unit-tested).
