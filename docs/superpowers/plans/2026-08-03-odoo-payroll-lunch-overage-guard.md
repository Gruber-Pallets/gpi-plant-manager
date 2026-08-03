# Odoo Payroll Lunch-Overage Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a five-minute Plant Manager guard that fixes only Odoo's verified extra 30-minute draft regular-hours defect, records every correction, and creates one Odoo review task for anything unsafe.

**Architecture:** A private Odoo payroll facade owns all `hr.work.entry` and payroll-related `hr.attendance` calls. A pure classifier returns `noop`, `correct`, or `review`; the orchestrator performs fresh-state checks, one narrow write or zero-target deletion, verification, audit persistence, and alert synchronization. The existing app warmer runs the guard every 300 seconds.

**Tech Stack:** Python 3.11+, Odoo 19 XML-RPC through `odoo_client.execute`, PostgreSQL through `zira_dashboard.db`, FastAPI's existing asyncio warmer registry, pytest, Ruff.

## Global Constraints

- Inspect only active Work Entries written in the last 90 days and linked to an Attendance.
- Correct only the exact 0.5-hour regular overage, within a one-minute float tolerance, when approved Attendance overtime and Odoo `OVERTIME` Work Entries agree.
- Require exactly one linked `WORK100` row and require every Work Entry in the employee/day group to remain draft and conflict-free.
- Re-read immediately before mutation and verify immediately afterward.
- A positive target changes only the draft `WORK100.duration`; a target within one minute of zero deletes only that single erroneous draft `WORK100` row because Odoo rejects zero durations.
- Never change punches, check-in/out timestamps, schedules, overtime rules, Overtime Work Entries, validated Work Entries, or payslips, and never regenerate Work Entries.
- `PAYROLL_WORK_ENTRY_GUARD_ENABLED=0`, `false`, or `no` disables the guard before any Odoo call; default is enabled.
- Append an audit row only after Odoo verification succeeds. Never update or delete an audit row.
- Unsafe cases are left untouched and synchronized into one Odoo task named `Payroll work entries need review`.
- Use Odoo 19's verified Work Entry field `date`; do not use removed older-version fields `date_start` or `date_stop`.
- Preserve the user's existing untracked `.cursorignore`, `.python-version`, and `uv.lock` files.
- Before every implementation commit, add a new simple, user-facing `CHANGELOG.md` entry. Push each commit directly to `origin/main` as required by this repository.

---

## File map

- Create `src/zira_dashboard/_odoo_payroll.py`: the only module that knows Odoo payroll model names, fields, domains, normalization, and mutations.
- Modify `src/zira_dashboard/odoo_client.py`: expose narrow facade wrappers without leaking the generic executor to the guard.
- Create `src/zira_dashboard/payroll_work_entry_rules.py`: immutable decision type and pure daily classifier.
- Create `src/zira_dashboard/payroll_work_entry_store.py`: append-only audit writes and singleton alert-state persistence.
- Create `src/zira_dashboard/payroll_work_entry_alert.py`: build and synchronize the singleton Odoo review task.
- Create `src/zira_dashboard/payroll_work_entry_guard.py`: kill switch, 90-day scan, classify-all-first orchestration, mutation, verification, audit, logging, and alert handoff.
- Modify `src/zira_dashboard/_schema.py`: create the audit and monitor tables.
- Modify `src/zira_dashboard/app.py`: register a 300-second guard tick.
- Modify `.env.example`: document the kill switch.
- Create focused tests under `tests/` and extend `tests/test_page_warmer.py`.
- Modify `CHANGELOG.md`: add one plain-language entry before every push.

---

### Task 1: Add the narrow Odoo payroll facade

**Files:**
- Create: `src/zira_dashboard/_odoo_payroll.py`
- Modify: `src/zira_dashboard/odoo_client.py:24-34, 919`
- Create: `tests/test_odoo_payroll.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `execute_fn(model, method, *args, **kwargs)`, `shift_config.SITE_TZ`, and `_odoo_attendance.to_odoo_dt(datetime)`.
- Produces: `fetch_recent_candidates(execute_fn, written_since) -> list[dict]`, `fetch_inputs(execute_fn, employee_ids, start_day, end_day) -> tuple[list[dict], list[dict]]`, `read_work_entry(execute_fn, entry_id) -> dict | None`, `write_duration(execute_fn, entry_id, duration) -> None`, `delete_entry(execute_fn, entry_id) -> None`, and `entry_exists(execute_fn, entry_id) -> bool`.
- Facade wrappers: `odoo_client.fetch_recent_payroll_candidates`, `fetch_payroll_inputs`, `fetch_payroll_work_entry`, `set_payroll_work_entry_duration`, `delete_payroll_work_entry`, and `payroll_work_entry_exists`.

- [ ] **Step 1: Write failing facade tests**

Create `tests/test_odoo_payroll.py` with deterministic fake-executor tests. The important assertions are the verified Odoo 19 field names, local-day normalization, and the narrow write surface:

```python
from datetime import date, datetime, UTC

import pytest

from zira_dashboard import _odoo_payroll as payroll


def fake_execute(responses, calls):
    def execute(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        value = responses[(model, method)]
        return value(*args, **kwargs) if callable(value) else value
    return execute


def test_recent_candidates_use_write_date_date_and_linked_work100():
    calls = []
    execute = fake_execute({
        ("hr.work.entry.type", "search_read"): [
            {"id": 1, "code": "WORK100"}, {"id": 2, "code": "OVERTIME"},
        ],
        ("hr.work.entry", "search_read"): [{
            "id": 8508, "employee_id": [6, "Caleb Asmussen"],
            "date": "2026-07-24", "duration": 0.5, "state": "draft",
            "conflict": False, "active": True, "work_entry_type_id": [1, "Attendance"],
            "attendance_id": [3804, "12:06"], "write_date": "2026-07-30 18:14:48",
        }],
    }, calls)

    rows = payroll.fetch_recent_candidates(
        execute, datetime(2026, 5, 5, tzinfo=UTC)
    )

    assert rows[0] == {
        "id": 8508, "employee_id": 6, "employee_name": "Caleb Asmussen",
        "date": date(2026, 7, 24), "duration": 0.5, "state": "draft",
        "conflict": False, "active": True, "type_code": "WORK100",
        "attendance_id": 3804, "write_date": "2026-07-30 18:14:48",
    }
    domain = calls[1][2][0]
    assert ("active", "=", True) in domain
    assert ("attendance_id", "!=", False) in domain
    assert ("work_entry_type_id", "=", 1) in domain
    assert any(term[0] == "write_date" for term in domain)
    assert all(term[0] not in {"date_start", "date_stop"} for term in domain)


def test_fetch_inputs_maps_utc_attendance_to_central_work_date():
    calls = []
    execute = fake_execute({
        ("hr.work.entry.type", "search_read"): [
            {"id": 1, "code": "WORK100"}, {"id": 2, "code": "OVERTIME"},
        ],
        ("hr.work.entry", "search_read"): [],
        ("hr.attendance", "search_read"): [{
            "id": 3996, "employee_id": [9, "Darren Donahue"],
            "check_in": "2026-07-31 10:45:00", "worked_hours": 10.548333333,
            "overtime_hours": 10.5483, "validated_overtime_hours": 10.5483,
            "overtime_status": "approved", "expected_hours": 0.000033333,
        }],
    }, calls)

    work, attendance = payroll.fetch_inputs(
        execute, [9], date(2026, 7, 31), date(2026, 7, 31)
    )

    assert work == []
    assert attendance[0]["date"] == date(2026, 7, 31)
    assert attendance[0]["employee_id"] == 9
    assert attendance[0]["overtime_status"] == "approved"


def test_write_duration_rejects_zero_before_xmlrpc():
    calls = []
    execute = fake_execute({}, calls)
    with pytest.raises(ValueError, match="positive"):
        payroll.write_duration(execute, 8508, 0.0)
    assert calls == []


def test_mutation_helpers_touch_only_one_work_entry():
    calls = []
    execute = fake_execute({
        ("hr.work.entry", "write"): True,
        ("hr.work.entry", "unlink"): True,
        ("hr.work.entry", "search_count"): 0,
    }, calls)
    payroll.write_duration(execute, 8502, 3.121355556)
    payroll.delete_entry(execute, 8508)
    assert payroll.entry_exists(execute, 8508) is False
    assert calls == [
        ("hr.work.entry", "write", ([8502], {"duration": 3.121355556}), {}),
        ("hr.work.entry", "unlink", ([8508],), {}),
        ("hr.work.entry", "search_count", ([("id", "=", 8508)],), {}),
    ]


def test_read_work_entry_returns_fresh_normalized_row():
    calls = []
    execute = fake_execute({
        ("hr.work.entry.type", "search_read"): [
            {"id": 1, "code": "WORK100"}, {"id": 2, "code": "OVERTIME"},
        ],
        ("hr.work.entry", "read"): [{
            "id": 8502, "employee_id": [19, "Isidro Moctezuma Aviles"],
            "date": "2026-07-24", "duration": 3.6214, "state": "draft",
            "conflict": False, "active": True, "work_entry_type_id": [1, "Attendance"],
            "attendance_id": [3811, "08:26"], "write_date": "2026-08-03 20:00:00",
        }],
    }, calls)

    row = payroll.read_work_entry(execute, 8502)

    assert row["id"] == 8502
    assert row["type_code"] == "WORK100"
    assert row["attendance_id"] == 3811
    assert calls[1][2] == ([8502],)


def test_public_odoo_client_wrappers_delegate(monkeypatch):
    from zira_dashboard import odoo_client

    recent = MagicMock(return_value=[{"id": 1}])
    monkeypatch.setattr(odoo_client._odoo_payroll, "fetch_recent_candidates", recent)
    since = datetime(2026, 5, 5, tzinfo=UTC)
    assert odoo_client.fetch_recent_payroll_candidates(since) == [{"id": 1}]
    recent.assert_called_once_with(odoo_client.execute, since)
```

- [ ] **Step 2: Run the tests and confirm the red state**

Run:

```bash
.venv/bin/python -m pytest tests/test_odoo_payroll.py -q
```

Expected: collection fails because `zira_dashboard._odoo_payroll` does not exist.

- [ ] **Step 3: Implement the private facade**

Create `src/zira_dashboard/_odoo_payroll.py`. Use these constants and normalization rules exactly:

```python
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from . import _odoo_attendance, shift_config

_TYPE_CODES = ("WORK100", "OVERTIME")
_WORK_FIELDS = [
    "id", "employee_id", "date", "duration", "state", "conflict", "active",
    "work_entry_type_id", "attendance_id", "write_date",
]
_ATTENDANCE_FIELDS = [
    "id", "employee_id", "check_in", "worked_hours", "overtime_hours",
    "validated_overtime_hours", "overtime_status", "expected_hours",
]


def _m2o_id(value: Any) -> int | None:
    if isinstance(value, (list, tuple)) and value:
        return int(value[0])
    return int(value) if value else None


def _m2o_name(value: Any) -> str:
    return str(value[1]) if isinstance(value, (list, tuple)) and len(value) > 1 else ""


def _type_maps(execute_fn: Callable[..., Any]) -> tuple[dict[str, int], dict[int, str]]:
    rows = execute_fn(
        "hr.work.entry.type", "search_read", [("code", "in", list(_TYPE_CODES))],
        fields=["id", "code"],
    )
    by_code = {str(row["code"]): int(row["id"]) for row in rows}
    missing = set(_TYPE_CODES) - set(by_code)
    if missing:
        raise RuntimeError(f"Missing Odoo Work Entry type code(s): {', '.join(sorted(missing))}")
    return by_code, {entry_id: code for code, entry_id in by_code.items()}


def _normalize_work(row: dict, codes_by_id: dict[int, str]) -> dict:
    employee = row.get("employee_id")
    type_id = _m2o_id(row.get("work_entry_type_id"))
    return {
        "id": int(row["id"]),
        "employee_id": _m2o_id(employee),
        "employee_name": _m2o_name(employee),
        "date": date.fromisoformat(str(row["date"])),
        "duration": float(row.get("duration") or 0),
        "state": str(row.get("state") or ""),
        "conflict": bool(row.get("conflict")),
        "active": bool(row.get("active")),
        "type_code": codes_by_id.get(type_id, ""),
        "attendance_id": _m2o_id(row.get("attendance_id")),
        "write_date": row.get("write_date"),
    }


def _normalize_attendance(row: dict) -> dict:
    employee = row.get("employee_id")
    check_in = datetime.strptime(row["check_in"], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=_odoo_attendance.UTC
    )
    return {
        "id": int(row["id"]),
        "employee_id": _m2o_id(employee),
        "employee_name": _m2o_name(employee),
        "date": check_in.astimezone(shift_config.SITE_TZ).date(),
        "worked_hours": float(row.get("worked_hours") or 0),
        "overtime_hours": float(row.get("overtime_hours") or 0),
        "validated_overtime_hours": float(row.get("validated_overtime_hours") or 0),
        "overtime_status": row.get("overtime_status") or "",
        "expected_hours": float(row.get("expected_hours") or 0),
    }
```

Then implement the six public functions using these exact domains:

```python
def fetch_recent_candidates(execute_fn, written_since: datetime) -> list[dict]:
    by_code, codes_by_id = _type_maps(execute_fn)
    rows = execute_fn(
        "hr.work.entry", "search_read",
        [
            ("active", "=", True),
            ("attendance_id", "!=", False),
            ("work_entry_type_id", "=", by_code["WORK100"]),
            ("write_date", ">=", _odoo_attendance.to_odoo_dt(written_since)),
        ],
        fields=_WORK_FIELDS,
        order="employee_id,date,id",
    )
    return [_normalize_work(row, codes_by_id) for row in rows]


def fetch_inputs(execute_fn, employee_ids, start_day: date, end_day: date):
    ids = sorted({int(value) for value in employee_ids})
    if not ids:
        return [], []
    _by_code, codes_by_id = _type_maps(execute_fn)
    work_rows = execute_fn(
        "hr.work.entry", "search_read",
        [("active", "=", True), ("employee_id", "in", ids),
         ("date", ">=", start_day.isoformat()), ("date", "<=", end_day.isoformat())],
        fields=_WORK_FIELDS,
        order="employee_id,date,id",
    )
    local_start = datetime.combine(start_day, time.min, tzinfo=shift_config.SITE_TZ)
    local_stop = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=shift_config.SITE_TZ)
    attendance_rows = execute_fn(
        "hr.attendance", "search_read",
        [("employee_id", "in", ids),
         ("check_in", ">=", _odoo_attendance.to_odoo_dt(local_start)),
         ("check_in", "<", _odoo_attendance.to_odoo_dt(local_stop))],
        fields=_ATTENDANCE_FIELDS,
        order="employee_id,check_in,id",
    )
    work = [_normalize_work(row, codes_by_id) for row in work_rows]
    attendance = [_normalize_attendance(row) for row in attendance_rows]
    attendance = [row for row in attendance if start_day <= row["date"] <= end_day]
    return work, attendance


def read_work_entry(execute_fn, entry_id: int) -> dict | None:
    _by_code, codes_by_id = _type_maps(execute_fn)
    rows = execute_fn("hr.work.entry", "read", [int(entry_id)], fields=_WORK_FIELDS)
    return _normalize_work(rows[0], codes_by_id) if rows else None


def write_duration(execute_fn, entry_id: int, duration: float) -> None:
    if duration <= 0:
        raise ValueError("Odoo Work Entry duration must be positive")
    execute_fn("hr.work.entry", "write", [int(entry_id)], {"duration": float(duration)})


def delete_entry(execute_fn, entry_id: int) -> None:
    execute_fn("hr.work.entry", "unlink", [int(entry_id)])


def entry_exists(execute_fn, entry_id: int) -> bool:
    return bool(execute_fn("hr.work.entry", "search_count", [("id", "=", int(entry_id))]))
```

- [ ] **Step 4: Add narrow wrappers to `odoo_client.py`**

Import `_odoo_payroll` beside the other private facade modules, then append:

```python
def fetch_recent_payroll_candidates(written_since: datetime) -> list[dict]:
    return _odoo_payroll.fetch_recent_candidates(execute, written_since)


def fetch_payroll_inputs(employee_ids, start_day, end_day):
    return _odoo_payroll.fetch_inputs(execute, employee_ids, start_day, end_day)


def fetch_payroll_work_entry(entry_id: int) -> dict | None:
    return _odoo_payroll.read_work_entry(execute, entry_id)


def set_payroll_work_entry_duration(entry_id: int, duration: float) -> None:
    _odoo_payroll.write_duration(execute, entry_id, duration)


def delete_payroll_work_entry(entry_id: int) -> None:
    _odoo_payroll.delete_entry(execute, entry_id)


def payroll_work_entry_exists(entry_id: int) -> bool:
    return _odoo_payroll.entry_exists(execute, entry_id)
```

- [ ] **Step 5: Run focused tests and lint**

Run:

```bash
.venv/bin/python -m pytest tests/test_odoo_payroll.py tests/test_odoo_client.py -q
.venv/bin/ruff check src/zira_dashboard/_odoo_payroll.py src/zira_dashboard/odoo_client.py tests/test_odoo_payroll.py
```

Expected: all tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 6: Add the Task 1 changelog entry, commit, and push**

Run `date '+%I:%M %p'` immediately before editing. Add a new subsection at
the top of `## 2026-08-03` whose heading is that exact printed time followed
by ` - Payroll safety reader`, without changing older notes. Add this bullet:

```markdown
- **Plant Manager can now read the Odoo payroll details needed for the new safety check.** The automatic check is not turned on yet.
```

Then run:

```bash
git add CHANGELOG.md src/zira_dashboard/_odoo_payroll.py src/zira_dashboard/odoo_client.py tests/test_odoo_payroll.py
git commit -m "feat: add Odoo payroll facade"
git push origin main
```

Expected: commit and push succeed; the new module is unused, so production behavior remains unchanged.

---

### Task 2: Build the pure exact-defect classifier

**Files:**
- Create: `src/zira_dashboard/payroll_work_entry_rules.py`
- Create: `tests/test_payroll_work_entry_rules.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: normalized work-entry and attendance dictionaries from Task 1.
- Produces: `Decision`, `classify_day(employee_id, employee_name, work_date, work_entries, attendances) -> Decision`, `TOLERANCE_HOURS = 1 / 60`, and `EXPECTED_EXCESS_HOURS = 0.5`.

- [ ] **Step 1: Write the failing classifier tests**

Create `tests/test_payroll_work_entry_rules.py`. Define compact row factories and cover every branch with exact assertions:

```python
from datetime import date

from zira_dashboard.payroll_work_entry_rules import classify_day

DAY = date(2026, 7, 24)


def work(entry_id, code, duration, *, state="draft", attendance_id=None, conflict=False):
    return {
        "id": entry_id, "employee_id": 9, "employee_name": "Darren Donahue",
        "date": DAY, "duration": duration, "state": state, "active": True,
        "conflict": conflict, "type_code": code, "attendance_id": attendance_id,
    }


def attendance(expected=0.0, overtime=8.5228, *, status="approved", raw=None):
    return {
        "id": 3803, "employee_id": 9, "employee_name": "Darren Donahue",
        "date": DAY, "expected_hours": expected, "worked_hours": expected + overtime,
        "overtime_hours": overtime if raw is None else raw,
        "validated_overtime_hours": overtime, "overtime_status": status,
    }


def classify(work_rows, attendance_rows):
    return classify_day(9, "Darren Donahue", DAY, work_rows, attendance_rows)


def test_exact_positive_defect_is_duration_update():
    result = classify(
        [work(8512, "WORK100", 3.6214, attendance_id=3803),
         work(8513, "OVERTIME", 5.3092)],
        [attendance(expected=3.1214, overtime=5.3092)],
    )
    assert result.kind == "correct"
    assert result.action == "duration_update"
    assert result.work_entry_id == 8512
    assert abs(result.after_duration - 3.1214) < 1e-9


def test_exact_zero_target_deletes_only_regular_row():
    result = classify(
        [work(8512, "WORK100", 0.5, attendance_id=3803),
         work(8513, "OVERTIME", 8.522777778)],
        [attendance(expected=-0.000022222, overtime=8.5228)],
    )
    assert result.kind == "correct"
    assert result.action == "delete_zero_regular"
    assert result.after_duration == 0.0


def test_corrected_values_are_noop():
    result = classify(
        [work(8512, "WORK100", 3.1214, attendance_id=3803),
         work(8513, "OVERTIME", 5.3092)],
        [attendance(expected=3.1214, overtime=5.3092)],
    )
    assert result.kind == "noop"


def test_regular_mismatch_without_any_overtime_is_noop():
    result = classify(
        [work(6349, "WORK100", 8.8647, attendance_id=3000)],
        [attendance(expected=8.3647, overtime=0.0, raw=0.0)],
    )
    assert result.kind == "noop"


def test_unsafe_variants_are_review():
    base_work = [work(1, "WORK100", 3.6214, attendance_id=3803),
                 work(2, "OVERTIME", 5.3092)]
    cases = [
        (base_work, [attendance(expected=3.1214, overtime=5.3092, status="to_approve")], "unapproved_overtime"),
        (base_work, [attendance(expected=3.1214, overtime=5.3092, raw=5.0)], "attendance_overtime_mismatch"),
        ([work(1, "WORK100", 3.6214, attendance_id=3803), work(2, "OVERTIME", 4.0)],
         [attendance(expected=3.1214, overtime=5.3092)], "payroll_overtime_mismatch"),
        ([work(1, "WORK100", 3.8214, attendance_id=3803), work(2, "OVERTIME", 5.3092)],
         [attendance(expected=3.1214, overtime=5.3092)], "regular_excess_not_half_hour"),
        ([work(1, "WORK100", 3.6214, state="validated", attendance_id=3803),
          work(2, "OVERTIME", 5.3092)], [attendance(expected=3.1214, overtime=5.3092)], "non_draft_work_entry"),
        ([work(1, "WORK100", 1.8, attendance_id=3803),
          work(3, "WORK100", 1.8214, attendance_id=3803), work(2, "OVERTIME", 5.3092)],
         [attendance(expected=3.1214, overtime=5.3092)], "ambiguous_regular_entries"),
        ([work(1, "WORK100", 3.6214, attendance_id=3803, conflict=True),
          work(2, "OVERTIME", 5.3092)], [attendance(expected=3.1214, overtime=5.3092)], "conflicting_work_entry"),
    ]
    for work_rows, attendance_rows, reason in cases:
        result = classify(work_rows, attendance_rows)
        assert result.kind == "review"
        assert reason in result.reason_codes


def test_missing_attendance_link_is_review():
    result = classify(
        [work(1, "WORK100", 3.6214), work(2, "OVERTIME", 5.3092)],
        [attendance(expected=3.1214, overtime=5.3092)],
    )
    assert result.kind == "review"
    assert "regular_not_attendance_linked" in result.reason_codes


def test_target_below_negative_tolerance_is_review():
    result = classify(
        [work(1, "WORK100", 0.48, attendance_id=3803),
         work(2, "OVERTIME", 5.3092)],
        [attendance(expected=-0.02, overtime=5.3092)],
    )
    assert result.kind == "review"
    assert "negative_target" in result.reason_codes


def test_overtime_mismatch_reviews_even_when_regular_matches():
    result = classify(
        [work(1, "WORK100", 3.1214, attendance_id=3803),
         work(2, "OVERTIME", 4.0)],
        [attendance(expected=3.1214, overtime=5.3092)],
    )
    assert result.kind == "review"
    assert "payroll_overtime_mismatch" in result.reason_codes


def test_regular_excess_more_than_one_minute_from_half_hour_is_review():
    expected = 3.1214
    result = classify(
        [work(1, "WORK100", expected + 0.5 + 61 / 3600, attendance_id=3803),
         work(2, "OVERTIME", 5.3092)],
        [attendance(expected=expected, overtime=5.3092)],
    )
    assert result.kind == "review"
    assert "regular_excess_not_half_hour" in result.reason_codes
```

- [ ] **Step 2: Run the classifier tests and confirm the red state**

Run:

```bash
.venv/bin/python -m pytest tests/test_payroll_work_entry_rules.py -q
```

Expected: collection fails because `payroll_work_entry_rules` does not exist.

- [ ] **Step 3: Implement the immutable decision and classifier**

Create `src/zira_dashboard/payroll_work_entry_rules.py` with this public shape:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

TOLERANCE_HOURS = 1.0 / 60.0
EXPECTED_EXCESS_HOURS = 0.5


@dataclass(frozen=True)
class Decision:
    kind: Literal["noop", "correct", "review"]
    employee_id: int
    employee_name: str
    work_date: date
    reason_codes: tuple[str, ...]
    action: Literal["duration_update", "delete_zero_regular"] | None
    work_entry_id: int | None
    attendance_id: int | None
    before_duration: float
    after_duration: float | None
    attendance_regular: float
    attendance_overtime: float
    work_regular: float
    work_overtime: float

    @property
    def issue_key(self) -> str:
        return f"{self.employee_id}:{self.work_date.isoformat()}:{','.join(self.reason_codes)}"
```

Implement `classify_day` as a pure function with this order:

1. Sum Attendance `expected_hours`, raw `overtime_hours`, and `validated_overtime_hours`.
2. Sum `WORK100` and `OVERTIME` durations.
3. Return `noop` when both payroll regular and payroll overtime agree within one minute.
4. Return `noop` for a regular-only mismatch when neither Attendance nor Payroll has any overtime signal.
5. For an overtime-related mismatch, collect deterministic reason codes in this order: `ambiguous_regular_entries`, `regular_not_attendance_linked`, `attendance_overtime_not_positive`, `unapproved_overtime`, `attendance_overtime_mismatch`, `payroll_overtime_mismatch`, `regular_excess_not_half_hour`, `non_draft_work_entry`, `conflicting_work_entry`, `negative_target`.
6. Return `review` when any reason exists.
7. Otherwise return `correct`. Set `after_duration = before_duration - measured_excess`; normalize `abs(after_duration) <= TOLERANCE_HOURS` to `0.0`; use `delete_zero_regular` for zero and `duration_update` otherwise.

The result must carry all numeric totals for the audit and review task. It must never mutate either input list.

- [ ] **Step 4: Run focused tests and lint**

Run:

```bash
.venv/bin/python -m pytest tests/test_payroll_work_entry_rules.py -q
.venv/bin/ruff check src/zira_dashboard/payroll_work_entry_rules.py tests/test_payroll_work_entry_rules.py
```

Expected: all classifier tests pass and Ruff reports no errors.

- [ ] **Step 5: Add the Task 2 changelog entry, commit, and push**

Run `date '+%I:%M %p'` and add a heading using that exact time followed by
` - Exact payroll mistake check`, then add:

```markdown
- **Plant Manager can now tell the known extra-lunch mistake from other payroll differences.** It still does not change payroll on its own yet.
```

Then run:

```bash
git add CHANGELOG.md src/zira_dashboard/payroll_work_entry_rules.py tests/test_payroll_work_entry_rules.py
git commit -m "feat: classify payroll lunch overages"
git push origin main
```

Expected: commit and push succeed; the classifier remains unused in production.

---

### Task 3: Add append-only correction audit and singleton monitor state

**Files:**
- Modify: `src/zira_dashboard/_schema.py:1529`
- Create: `src/zira_dashboard/payroll_work_entry_store.py`
- Create: `tests/test_payroll_work_entry_store.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `Decision` from Task 2 and `zira_dashboard.db`.
- Produces: `append_correction(decision, verification_detail, corrected_at) -> None`, `load_monitor_state() -> dict`, and `save_monitor_state(odoo_task_id, reported_issue_keys, updated_at) -> None`.

- [ ] **Step 1: Write failing schema and store tests**

Create `tests/test_payroll_work_entry_store.py`:

```python
from datetime import date, datetime, UTC
from unittest.mock import MagicMock

from zira_dashboard._schema import SCHEMA_DDL
from zira_dashboard.payroll_work_entry_rules import Decision
import zira_dashboard.payroll_work_entry_store as store


def correction_decision():
    return Decision(
        kind="correct", employee_id=19, employee_name="Isidro Moctezuma Aviles",
        work_date=date(2026, 7, 24), reason_codes=(), action="duration_update",
        work_entry_id=8502, attendance_id=3811, before_duration=3.621388889,
        after_duration=3.121355556, attendance_regular=3.121355556,
        attendance_overtime=5.3092, work_regular=3.621388889,
        work_overtime=5.309166667,
    )


def test_schema_defines_append_only_audit_and_singleton_monitor():
    assert "CREATE TABLE IF NOT EXISTS payroll_work_entry_corrections" in SCHEMA_DDL
    assert "action TEXT NOT NULL CHECK (action IN ('duration_update', 'delete_zero_regular'))" in SCHEMA_DDL
    assert "CREATE TABLE IF NOT EXISTS payroll_work_entry_guard_monitor" in SCHEMA_DDL
    assert "reported_issue_keys TEXT[] NOT NULL DEFAULT '{}'" in SCHEMA_DDL


def test_append_correction_inserts_every_audit_value(monkeypatch):
    execute = MagicMock()
    monkeypatch.setattr(store.db, "execute", execute)
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    store.append_correction(correction_decision(), "duration reread matched", now)
    sql, params = execute.call_args.args
    assert "INSERT INTO payroll_work_entry_corrections" in sql
    assert params[0] == 8502
    assert params[1] == "duration_update"
    assert params[-2] == "duration reread matched"
    assert params[-1] == now


def test_monitor_state_defaults_and_round_trips(monkeypatch):
    monkeypatch.setattr(store.db, "query", lambda *_: [])
    assert store.load_monitor_state() == {"odoo_task_id": None, "reported_issue_keys": []}
    execute = MagicMock()
    monkeypatch.setattr(store.db, "execute", execute)
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    store.save_monitor_state(44, ["9:2026-07-24:z", "9:2026-07-24:a"], now)
    assert execute.call_args.args[1] == (44, ["9:2026-07-24:a", "9:2026-07-24:z"], now)
```

- [ ] **Step 2: Run the tests and confirm the red state**

Run:

```bash
.venv/bin/python -m pytest tests/test_payroll_work_entry_store.py -q
```

Expected: collection fails because the store module and schema tables do not exist.

- [ ] **Step 3: Add the idempotent schema**

Insert after `calendar_conflict_monitor` in `src/zira_dashboard/_schema.py`:

```sql
-- 2026-08-03: append-only Odoo payroll guard audit and singleton alert state.
CREATE TABLE IF NOT EXISTS payroll_work_entry_corrections (
  id                       BIGSERIAL PRIMARY KEY,
  odoo_work_entry_id       INTEGER NOT NULL,
  action                   TEXT NOT NULL CHECK (action IN ('duration_update', 'delete_zero_regular')),
  employee_odoo_id         INTEGER NOT NULL,
  employee_name            TEXT NOT NULL,
  work_date                DATE NOT NULL,
  before_duration          DOUBLE PRECISION NOT NULL,
  after_duration           DOUBLE PRECISION NOT NULL,
  attendance_regular       DOUBLE PRECISION NOT NULL,
  attendance_overtime      DOUBLE PRECISION NOT NULL,
  work_regular_before      DOUBLE PRECISION NOT NULL,
  work_overtime            DOUBLE PRECISION NOT NULL,
  verification_detail      TEXT NOT NULL,
  corrected_at             TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS payroll_work_entry_corrections_entry_idx
  ON payroll_work_entry_corrections (odoo_work_entry_id, corrected_at DESC);

CREATE TABLE IF NOT EXISTS payroll_work_entry_guard_monitor (
  id                    INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  odoo_task_id          INTEGER,
  reported_issue_keys   TEXT[] NOT NULL DEFAULT '{}',
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- [ ] **Step 4: Implement the store**

Create `src/zira_dashboard/payroll_work_entry_store.py`. `append_correction` must
reject any decision that is not `kind == "correct"`, lacks an action, lacks a
Work Entry id, or has `after_duration is None` (zero is valid for a verified
deletion). Use one `INSERT` with all Decision values. `load_monitor_state` and
`save_monitor_state` should follow
`calendar_conflict_monitor._load_state/_save_state`, but use sorted `TEXT[]`
keys and do not add a throttle timestamp.

Use this state upsert:

```python
db.execute(
    "INSERT INTO payroll_work_entry_guard_monitor "
    "(id, odoo_task_id, reported_issue_keys, updated_at) VALUES (1, %s, %s, %s) "
    "ON CONFLICT (id) DO UPDATE SET "
    "odoo_task_id = EXCLUDED.odoo_task_id, "
    "reported_issue_keys = EXCLUDED.reported_issue_keys, "
    "updated_at = EXCLUDED.updated_at",
    (odoo_task_id, sorted(set(reported_issue_keys)), updated_at),
)
```

- [ ] **Step 5: Run focused tests, a real-schema test when available, and lint**

Run:

```bash
.venv/bin/python -m pytest tests/test_payroll_work_entry_store.py -q
.venv/bin/ruff check src/zira_dashboard/_schema.py src/zira_dashboard/payroll_work_entry_store.py tests/test_payroll_work_entry_store.py
```

If `DATABASE_URL` is available, also run:

```bash
.venv/bin/python -m pytest tests/test_db.py::test_bootstrap_schema_idempotent -q
```

Expected: focused tests pass; the DB test passes or is skipped by the environment.

- [ ] **Step 6: Add the Task 3 changelog entry, commit, and push**

Run `date '+%I:%M %p'` and add a heading using that exact time followed by
` - Payroll fix history`, then add:

```markdown
- **Plant Manager is ready to keep a permanent record of every safe payroll correction.** The automatic check is not turned on yet.
```

Then run:

```bash
git add CHANGELOG.md src/zira_dashboard/_schema.py src/zira_dashboard/payroll_work_entry_store.py tests/test_payroll_work_entry_store.py
git commit -m "feat: store payroll guard audit history"
git push origin main
```

Expected: schema bootstraps idempotently on Railway; no audit rows are written because the guard is not wired yet.

---

### Task 4: Add the singleton Odoo review-task synchronizer

**Files:**
- Create: `src/zira_dashboard/payroll_work_entry_alert.py`
- Create: `tests/test_payroll_work_entry_alert.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: review `Decision` objects, `payroll_work_entry_store.load_monitor_state/save_monitor_state`, and existing Odoo task helpers.
- Produces: `sync_review_task(issues: list[Decision], now: datetime | None = None) -> dict`.

- [ ] **Step 1: Write failing alert lifecycle tests**

Create `tests/test_payroll_work_entry_alert.py` with these lifecycle tests:

```python
from datetime import date, datetime, UTC
from unittest.mock import MagicMock

import zira_dashboard.payroll_work_entry_alert as alert
from zira_dashboard.payroll_work_entry_rules import Decision

NOW = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)


def issue(employee_name="Darren Donahue", reason="payroll_overtime_mismatch"):
    return Decision(
        kind="review", employee_id=9, employee_name=employee_name,
        work_date=date(2026, 7, 24), reason_codes=(reason,), action=None,
        work_entry_id=8512, attendance_id=3803, before_duration=0.5, after_duration=None,
        attendance_regular=0.0, attendance_overtime=8.5228,
        work_regular=0.5, work_overtime=8.0,
    )


def patch_state(monkeypatch, task_id=None, keys=()):
    monkeypatch.setattr(
        alert.store, "load_monitor_state",
        lambda: {"odoo_task_id": task_id, "reported_issue_keys": list(keys)},
    )
    save = MagicMock()
    monkeypatch.setattr(alert.store, "save_monitor_state", save)
    return save


def patch_odoo(monkeypatch, *, created_id=222, update_error=None, task_stages=None):
    monkeypatch.setattr(alert.odoo_client, "ensure_feedback_project", MagicMock(return_value=3))
    monkeypatch.setattr(alert.odoo_client, "authenticate", MagicMock(return_value=9))
    create = MagicMock(return_value=created_id)
    update = MagicMock(side_effect=update_error)
    comment = MagicMock()
    monkeypatch.setattr(alert.odoo_client, "create_feedback_task", create)
    monkeypatch.setattr(alert.odoo_client, "update_task", update)
    monkeypatch.setattr(alert.odoo_client, "post_task_message", comment)
    monkeypatch.setattr(
        alert.odoo_client, "fetch_task_stage_names",
        MagicMock(return_value={111: "New"} if task_stages is None else task_stages),
    )
    return create, update, comment


def test_first_issue_creates_one_task_and_saves_keys(monkeypatch):
    current = issue()
    save = patch_state(monkeypatch)
    create, update, comment = patch_odoo(monkeypatch)

    result = alert.sync_review_task([current], NOW)

    create.assert_called_once()
    update.assert_not_called()
    comment.assert_called_once()
    save.assert_called_once_with(222, [current.issue_key], NOW)
    assert result == {"changed": True, "task_id": 222, "count": 1}


def test_same_issue_set_is_silent(monkeypatch):
    current = issue()
    save = patch_state(monkeypatch, task_id=111, keys=[current.issue_key])
    create, update, comment = patch_odoo(monkeypatch)

    result = alert.sync_review_task([current], NOW)

    create.assert_not_called()
    update.assert_not_called()
    comment.assert_not_called()
    save.assert_not_called()
    assert result == {"changed": False, "task_id": 111, "count": 1}


def test_changed_issue_set_updates_existing_task(monkeypatch):
    current = issue(reason="non_draft_work_entry")
    save = patch_state(monkeypatch, task_id=111, keys=[issue().issue_key])
    create, update, comment = patch_odoo(monkeypatch)

    alert.sync_review_task([current], NOW)

    create.assert_not_called()
    update.assert_called_once()
    assert update.call_args.args[0] == 111
    assert "description" in update.call_args.kwargs
    comment.assert_called_once()
    save.assert_called_once_with(111, [current.issue_key], NOW)


def test_deleted_stored_task_is_recreated(monkeypatch):
    current = issue(reason="non_draft_work_entry")
    save = patch_state(monkeypatch, task_id=111, keys=[issue().issue_key])
    create, update, comment = patch_odoo(
        monkeypatch, created_id=333, update_error=RuntimeError("task deleted")
    )

    alert.sync_review_task([current], NOW)

    update.assert_called_once()
    create.assert_called_once()
    comment.assert_called_once()
    save.assert_called_once_with(333, [current.issue_key], NOW)


def test_unchanged_issue_recreates_missing_stored_task(monkeypatch):
    current = issue()
    save = patch_state(monkeypatch, task_id=111, keys=[current.issue_key])
    create, update, comment = patch_odoo(
        monkeypatch, created_id=444, task_stages={}
    )

    alert.sync_review_task([current], NOW)

    update.assert_not_called()
    create.assert_called_once()
    comment.assert_called_once()
    save.assert_called_once_with(444, [current.issue_key], NOW)


def test_empty_issue_set_archives_existing_task(monkeypatch):
    previous = issue()
    save = patch_state(monkeypatch, task_id=111, keys=[previous.issue_key])
    create, update, comment = patch_odoo(monkeypatch)

    result = alert.sync_review_task([], NOW)

    create.assert_not_called()
    comment.assert_called_once_with(111, "✅ All payroll Work Entry review items resolved.")
    update.assert_called_once_with(111, active=False)
    save.assert_called_once_with(None, [], NOW)
    assert result == {"changed": True, "task_id": None, "count": 0}


def test_task_body_escapes_employee_and_lists_totals():
    body = alert._build_task_body([issue(employee_name="<Dale & Co>")])
    assert "&lt;Dale &amp; Co&gt;" in body
    assert "2026-07-24" in body
    assert "0.5000" in body
    assert "8.5228" in body
    assert "Payroll and Attendance overtime disagree" in body
```

Use a review `Decision` with `reason_codes=("payroll_overtime_mismatch",)` and verify the description includes employee, date, regular totals, overtime totals, and a plain-language explanation for the reason code.

- [ ] **Step 2: Run the tests and confirm the red state**

Run:

```bash
.venv/bin/python -m pytest tests/test_payroll_work_entry_alert.py -q
```

Expected: collection fails because `payroll_work_entry_alert` does not exist.

- [ ] **Step 3: Implement alert synchronization**

Create `src/zira_dashboard/payroll_work_entry_alert.py` with:

```python
_TASK_NAME = "Payroll work entries need review"
_REASON_TEXT = {
    "ambiguous_regular_entries": "more than one regular Work Entry",
    "regular_not_attendance_linked": "regular Work Entry is not linked to Attendance",
    "attendance_overtime_not_positive": "approved Attendance overtime is not positive",
    "unapproved_overtime": "Attendance overtime is not approved",
    "attendance_overtime_mismatch": "Attendance worked and approved overtime disagree",
    "payroll_overtime_mismatch": "Payroll and Attendance overtime disagree",
    "regular_excess_not_half_hour": "regular difference is not the known 30-minute defect",
    "non_draft_work_entry": "one or more Work Entries are no longer draft",
    "conflicting_work_entry": "Odoo marks a Work Entry as conflicting",
    "negative_target": "the safe correction would be negative",
    "fresh_state_changed": "the Work Entry changed before correction",
    "write_failed": "Odoo refused the correction",
    "verification_failed": "Odoo did not retain the correction",
    "audit_failed": "the correction succeeded but its local audit failed",
    "missing_candidate_group": "the recent candidate was absent from the batch reread",
    "fresh_read_failed": "Plant Manager could not reread the Work Entry",
}
```

Build escaped HTML sorted by `(employee_name.lower(), work_date, issue_key)`.
Follow the calendar-conflict monitor lifecycle: update the existing task,
recreate it if update fails, post one change summary, archive and clear the
stored task when issues become empty. When the issue keys are unchanged and a
task id is stored, call `fetch_task_stage_names([task_id])`; return silently if
the id exists, or recreate the task if Odoo returns no record. Use a seven-day
deadline for a new task. Save state only after the matching Odoo action
succeeds.

- [ ] **Step 4: Run alert tests and lint**

Run:

```bash
.venv/bin/python -m pytest tests/test_payroll_work_entry_alert.py -q
.venv/bin/ruff check src/zira_dashboard/payroll_work_entry_alert.py tests/test_payroll_work_entry_alert.py
```

Expected: all tests pass and Ruff reports no errors.

- [ ] **Step 5: Add the Task 4 changelog entry, commit, and push**

Run `date '+%I:%M %p'` and add a heading using that exact time followed by
` - One place for payroll questions`, then add:

```markdown
- **Plant Manager is ready to make one Odoo task when a payroll item needs a person to review it.** The automatic payroll check is not turned on yet.
```

Then run:

```bash
git add CHANGELOG.md src/zira_dashboard/payroll_work_entry_alert.py tests/test_payroll_work_entry_alert.py
git commit -m "feat: add payroll review task monitor"
git push origin main
```

Expected: commit and push succeed; no task is created because the guard is not wired yet.

---

### Task 5: Implement classify-all-first correction, verification, and audit

**Files:**
- Create: `src/zira_dashboard/payroll_work_entry_guard.py`
- Create: `tests/test_payroll_work_entry_guard.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: every interface from Tasks 1-4.
- Produces: `enabled() -> bool` and `run_once(now: datetime | None = None) -> dict`.

- [ ] **Step 1: Write failing orchestrator tests**

Create `tests/test_payroll_work_entry_guard.py`. Monkeypatch the facade, store,
alert, and classifier so the file never makes a real Odoo or database call:

```python
import logging
from datetime import date, datetime, UTC
from unittest.mock import MagicMock

import pytest

import zira_dashboard.payroll_work_entry_guard as guard
from zira_dashboard.payroll_work_entry_rules import Decision

NOW = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
DAY = date(2026, 7, 24)


def correct_decision(
    employee_id=19, entry_id=8502, attendance_id=3811,
    action="duration_update", before=3.6214, after=3.1214,
):
    return Decision(
        kind="correct", employee_id=employee_id,
        employee_name=f"Employee {employee_id}", work_date=DAY,
        reason_codes=(), action=action, work_entry_id=entry_id,
        attendance_id=attendance_id, before_duration=before,
        after_duration=after, attendance_regular=after,
        attendance_overtime=5.3092, work_regular=before,
        work_overtime=5.3092,
    )


def candidate(decision):
    return {
        "id": decision.work_entry_id, "employee_id": decision.employee_id,
        "employee_name": decision.employee_name, "date": decision.work_date,
    }


def fresh(decision, *, duration=None, state="draft", conflict=False):
    return {
        "id": decision.work_entry_id, "employee_id": decision.employee_id,
        "employee_name": decision.employee_name, "date": decision.work_date,
        "duration": decision.before_duration if duration is None else duration,
        "state": state, "active": True, "conflict": conflict,
        "type_code": "WORK100", "attendance_id": decision.attendance_id,
    }


def wire_batch(monkeypatch, decisions, events=None):
    events = events if events is not None else []
    monkeypatch.setenv("PAYROLL_WORK_ENTRY_GUARD_ENABLED", "1")
    monkeypatch.setattr(
        guard.odoo_client, "fetch_recent_payroll_candidates",
        lambda _since: [candidate(item) for item in decisions],
    )
    grouped_work = [
        {"employee_id": item.employee_id, "employee_name": item.employee_name,
         "date": item.work_date, "id": item.work_entry_id}
        for item in decisions
    ]
    monkeypatch.setattr(
        guard.odoo_client, "fetch_payroll_inputs",
        lambda _ids, _start, _end: (grouped_work, []),
    )
    by_employee = {item.employee_id: item for item in decisions}

    def classify(employee_id, _name, _day, _work, _attendance):
        events.append(f"classify {employee_id}")
        return by_employee[employee_id]

    monkeypatch.setattr(guard, "classify_day", classify)
    monkeypatch.setattr(guard.alert, "sync_review_task", MagicMock())
    monkeypatch.setattr(guard.store, "append_correction", MagicMock())
    monkeypatch.setattr(guard.odoo_client, "set_payroll_work_entry_duration", MagicMock())
    monkeypatch.setattr(guard.odoo_client, "delete_payroll_work_entry", MagicMock())
    monkeypatch.setattr(guard.odoo_client, "payroll_work_entry_exists", MagicMock(return_value=False))
    return events


@pytest.mark.parametrize("value", ["0", "FALSE", "no"])
def test_kill_switch_makes_zero_odoo_db_or_alert_calls(monkeypatch, value):
    monkeypatch.setenv("PAYROLL_WORK_ENTRY_GUARD_ENABLED", value)
    fetch = MagicMock()
    monkeypatch.setattr(guard.odoo_client, "fetch_recent_payroll_candidates", fetch)
    monkeypatch.setattr(guard.alert, "sync_review_task", MagicMock())
    monkeypatch.setattr(guard.store, "append_correction", MagicMock())
    assert guard.run_once(NOW) == {"skipped": "disabled"}
    fetch.assert_not_called()
    guard.alert.sync_review_task.assert_not_called()
    guard.store.append_correction.assert_not_called()


def test_default_is_enabled(monkeypatch):
    monkeypatch.delenv("PAYROLL_WORK_ENTRY_GUARD_ENABLED", raising=False)
    monkeypatch.setattr(guard.odoo_client, "fetch_recent_payroll_candidates", lambda _since: [])
    monkeypatch.setattr(guard.alert, "sync_review_task", MagicMock())
    assert guard.run_once(NOW) == {"corrected": 0, "review": 0, "noop": 0}


def test_positive_target_writes_rereads_then_audits(monkeypatch):
    item = correct_decision()
    events = wire_batch(monkeypatch, [item])
    reads = iter([fresh(item), fresh(item, duration=item.after_duration)])
    monkeypatch.setattr(
        guard.odoo_client, "fetch_payroll_work_entry",
        lambda _entry_id: events.append("read") or next(reads),
    )
    monkeypatch.setattr(
        guard.odoo_client, "set_payroll_work_entry_duration",
        lambda _entry_id, _duration: events.append("write"),
    )
    monkeypatch.setattr(
        guard.store, "append_correction",
        lambda _decision, _detail, _now: events.append("audit"),
    )

    result = guard.run_once(NOW)

    assert events == ["classify 19", "read", "write", "read", "audit"]
    assert result == {"corrected": 1, "review": 0, "noop": 0}
    guard.alert.sync_review_task.assert_called_once_with([], NOW)


def test_zero_target_deletes_only_regular_row_then_audits(monkeypatch):
    item = correct_decision(action="delete_zero_regular", before=0.5, after=0.0)
    wire_batch(monkeypatch, [item])
    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_work_entry", lambda _id: fresh(item))
    delete = MagicMock()
    write = MagicMock()
    monkeypatch.setattr(guard.odoo_client, "delete_payroll_work_entry", delete)
    monkeypatch.setattr(guard.odoo_client, "set_payroll_work_entry_duration", write)
    monkeypatch.setattr(guard.odoo_client, "payroll_work_entry_exists", lambda _id: False)

    result = guard.run_once(NOW)

    delete.assert_called_once_with(item.work_entry_id)
    write.assert_not_called()
    guard.store.append_correction.assert_called_once()
    assert result["corrected"] == 1


def test_changed_fresh_state_refuses_write_and_creates_review_issue(monkeypatch):
    item = correct_decision()
    wire_batch(monkeypatch, [item])
    monkeypatch.setattr(
        guard.odoo_client, "fetch_payroll_work_entry",
        lambda _id: fresh(item, state="validated"),
    )

    result = guard.run_once(NOW)

    guard.odoo_client.set_payroll_work_entry_duration.assert_not_called()
    guard.store.append_correction.assert_not_called()
    issues = guard.alert.sync_review_task.call_args.args[0]
    assert issues[0].reason_codes == ("fresh_state_changed",)
    assert result["review"] == 1


def test_write_failure_does_not_audit_and_other_group_still_corrects(monkeypatch):
    first = correct_decision(employee_id=19, entry_id=8502)
    second = correct_decision(employee_id=22, entry_id=8483, attendance_id=3805)
    wire_batch(monkeypatch, [first, second])
    read_counts = {8502: 0, 8483: 0}

    def read(entry_id):
        read_counts[entry_id] += 1
        item = first if entry_id == 8502 else second
        duration = item.before_duration if read_counts[entry_id] == 1 else item.after_duration
        return fresh(item, duration=duration)

    def write(entry_id, _duration):
        if entry_id == 8502:
            raise RuntimeError("Odoo refused")

    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_work_entry", read)
    monkeypatch.setattr(guard.odoo_client, "set_payroll_work_entry_duration", write)

    result = guard.run_once(NOW)

    assert result == {"corrected": 1, "review": 1, "noop": 0}
    audited = guard.store.append_correction.call_args.args[0]
    assert audited.work_entry_id == 8483
    issues = guard.alert.sync_review_task.call_args.args[0]
    assert issues[0].reason_codes == ("write_failed",)


def test_failed_verification_does_not_audit(monkeypatch):
    item = correct_decision()
    wire_batch(monkeypatch, [item])
    reads = iter([fresh(item), fresh(item, duration=item.after_duration + 1.0)])
    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_work_entry", lambda _id: next(reads))

    result = guard.run_once(NOW)

    guard.store.append_correction.assert_not_called()
    issues = guard.alert.sync_review_task.call_args.args[0]
    assert issues[0].reason_codes == ("verification_failed",)
    assert result["review"] == 1


def test_delete_verification_that_finds_id_does_not_audit(monkeypatch):
    item = correct_decision(action="delete_zero_regular", before=0.5, after=0.0)
    wire_batch(monkeypatch, [item])
    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_work_entry", lambda _id: fresh(item))
    monkeypatch.setattr(guard.odoo_client, "payroll_work_entry_exists", lambda _id: True)

    result = guard.run_once(NOW)

    guard.store.append_correction.assert_not_called()
    assert guard.alert.sync_review_task.call_args.args[0][0].reason_codes == ("verification_failed",)
    assert result["review"] == 1


def test_audit_failure_becomes_review_without_second_odoo_write(monkeypatch):
    item = correct_decision()
    wire_batch(monkeypatch, [item])
    reads = iter([fresh(item), fresh(item, duration=item.after_duration)])
    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_work_entry", lambda _id: next(reads))
    monkeypatch.setattr(guard.store, "append_correction", MagicMock(side_effect=RuntimeError("db down")))

    result = guard.run_once(NOW)

    guard.odoo_client.set_payroll_work_entry_duration.assert_called_once()
    assert guard.alert.sync_review_task.call_args.args[0][0].reason_codes == ("audit_failed",)
    assert result["corrected"] == 1
    assert result["review"] == 1


def test_every_candidate_group_is_classified_before_first_write(monkeypatch):
    first = correct_decision(employee_id=19, entry_id=8502)
    second = correct_decision(employee_id=22, entry_id=8483, attendance_id=3805)
    events = wire_batch(monkeypatch, [first, second])
    read_counts = {8502: 0, 8483: 0}

    def read(entry_id):
        read_counts[entry_id] += 1
        item = first if entry_id == 8502 else second
        duration = item.before_duration if read_counts[entry_id] == 1 else item.after_duration
        return fresh(item, duration=duration)

    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_work_entry", read)
    monkeypatch.setattr(
        guard.odoo_client, "set_payroll_work_entry_duration",
        lambda entry_id, _duration: events.append(f"write {entry_id}"),
    )

    guard.run_once(NOW)

    assert events[:3] == ["classify 19", "classify 22", "write 8502"]


def test_no_candidates_closes_existing_review_task_without_input_fetch(monkeypatch):
    monkeypatch.setenv("PAYROLL_WORK_ENTRY_GUARD_ENABLED", "1")
    monkeypatch.setattr(guard.odoo_client, "fetch_recent_payroll_candidates", lambda _since: [])
    fetch_inputs = MagicMock()
    sync = MagicMock()
    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_inputs", fetch_inputs)
    monkeypatch.setattr(guard.alert, "sync_review_task", sync)

    result = guard.run_once(NOW)

    fetch_inputs.assert_not_called()
    sync.assert_called_once_with([], NOW)
    assert result == {"corrected": 0, "review": 0, "noop": 0}


def test_missing_candidate_group_is_review(monkeypatch):
    item = correct_decision()
    wire_batch(monkeypatch, [item])
    monkeypatch.setattr(
        guard.odoo_client, "fetch_payroll_inputs", lambda _ids, _start, _end: ([], [])
    )

    result = guard.run_once(NOW)

    issues = guard.alert.sync_review_task.call_args.args[0]
    assert issues[0].reason_codes == ("missing_candidate_group",)
    assert result == {"corrected": 0, "review": 1, "noop": 0}


def test_alert_failure_is_logged_without_changing_counts(monkeypatch, caplog):
    item = correct_decision()
    wire_batch(monkeypatch, [item])
    reads = iter([fresh(item), fresh(item, duration=item.after_duration)])
    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_work_entry", lambda _id: next(reads))
    monkeypatch.setattr(
        guard.alert, "sync_review_task", MagicMock(side_effect=RuntimeError("task API down"))
    )

    with caplog.at_level(logging.WARNING, logger="zira_dashboard.payroll_work_entry_guard"):
        result = guard.run_once(NOW)

    assert result == {"corrected": 1, "review": 0, "noop": 0}
    assert "could not sync review task" in caplog.text
```

- [ ] **Step 2: Run orchestrator tests and confirm the red state**

Run:

```bash
.venv/bin/python -m pytest tests/test_payroll_work_entry_guard.py -q
```

Expected: collection fails because `payroll_work_entry_guard` does not exist.

- [ ] **Step 3: Implement the guard skeleton and kill switch**

Create `src/zira_dashboard/payroll_work_entry_guard.py` with:

```python
from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, UTC

from . import odoo_client
from . import payroll_work_entry_alert as alert
from . import payroll_work_entry_store as store
from .payroll_work_entry_rules import Decision, TOLERANCE_HOURS, classify_day

_log = logging.getLogger(__name__)
LOOKBACK = timedelta(days=90)
_DISABLED_VALUES = {"0", "false", "no"}


def enabled() -> bool:
    return os.environ.get("PAYROLL_WORK_ENTRY_GUARD_ENABLED", "1").strip().lower() not in _DISABLED_VALUES
```

- [ ] **Step 4: Implement batch classification before all mutations**

In `run_once`, perform this sequence exactly:

```python
def run_once(now: datetime | None = None) -> dict:
    if not enabled():
        return {"skipped": "disabled"}
    now = now or datetime.now(UTC)
    candidates = odoo_client.fetch_recent_payroll_candidates(now - LOOKBACK)
    if not candidates:
        try:
            alert.sync_review_task([], now)
        except Exception:
            _log.warning("payroll guard: could not clear review task", exc_info=True)
        return {"corrected": 0, "review": 0, "noop": 0}

    keys = sorted({(row["employee_id"], row["date"]) for row in candidates})
    employee_ids = sorted({key[0] for key in keys})
    work_rows, attendance_rows = odoo_client.fetch_payroll_inputs(
        employee_ids, min(key[1] for key in keys), max(key[1] for key in keys)
    )
    work_by_key = defaultdict(list)
    attendance_by_key = defaultdict(list)
    for row in work_rows:
        work_by_key[(row["employee_id"], row["date"])].append(row)
    for row in attendance_rows:
        attendance_by_key[(row["employee_id"], row["date"])].append(row)

    names = {(row["employee_id"], row["date"]): row["employee_name"] for row in candidates}
    decisions = []
    for employee_id, work_date in keys:
        grouped_work = work_by_key.get((employee_id, work_date), [])
        if not grouped_work:
            decisions.append(Decision(
                kind="review", employee_id=employee_id,
                employee_name=names[(employee_id, work_date)], work_date=work_date,
                reason_codes=("missing_candidate_group",), action=None,
                work_entry_id=None, attendance_id=None,
                before_duration=0, after_duration=None,
                attendance_regular=0, attendance_overtime=0,
                work_regular=0, work_overtime=0,
            ))
            continue
        decisions.append(classify_day(
            employee_id, names[(employee_id, work_date)], work_date,
            grouped_work, attendance_by_key.get((employee_id, work_date), []),
        ))
```

Do not place a mutation inside this classification loop.

- [ ] **Step 5: Add fresh-state validation, mutation, verification, and audit**

For every `correct` Decision, reread its Work Entry and require all of these snapshot facts before mutating: same id, `active is True`, `state == "draft"`, `conflict is False`, `type_code == "WORK100"`, same `attendance_id`, and duration within one minute of `before_duration`. Convert failure to a review Decision with `reason_codes=("fresh_state_changed",)`.

For `duration_update`, call `set_payroll_work_entry_duration`, reread, and require the draft state and target duration within one minute. For `delete_zero_regular`, call `delete_payroll_work_entry` and require `payroll_work_entry_exists` to be false. Only after verification call:

```python
def _as_review(decision: Decision, reason: str) -> Decision:
    return replace(
        decision, kind="review", reason_codes=(reason,), action=None
    )


def _same_identity(row: dict | None, decision: Decision) -> bool:
    return bool(
        row
        and row["id"] == decision.work_entry_id
        and row["employee_id"] == decision.employee_id
        and row["date"] == decision.work_date
        and row["active"] is True
        and row["state"] == "draft"
        and row["conflict"] is False
        and row["type_code"] == "WORK100"
        and row["attendance_id"] == decision.attendance_id
    )


def _duration_matches(row: dict | None, decision: Decision, expected: float) -> bool:
    return _same_identity(row, decision) and abs(float(row["duration"]) - expected) <= TOLERANCE_HOURS
```

Continue `run_once` after the classification loop with this exact per-group
isolation. Never attempt a compensating write:

```python
    review_issues = [item for item in decisions if item.kind == "review"]
    noop_count = sum(item.kind == "noop" for item in decisions)
    corrected_count = 0

    for decision in [item for item in decisions if item.kind == "correct"]:
        try:
            fresh = odoo_client.fetch_payroll_work_entry(decision.work_entry_id)
        except Exception:
            _log.warning(
                "payroll guard: fresh read failed for entry %s",
                decision.work_entry_id, exc_info=True,
            )
            review_issues.append(_as_review(decision, "fresh_read_failed"))
            continue
        if not _duration_matches(fresh, decision, decision.before_duration):
            review_issues.append(_as_review(decision, "fresh_state_changed"))
            continue

        try:
            if decision.action == "duration_update":
                odoo_client.set_payroll_work_entry_duration(
                    decision.work_entry_id, decision.after_duration
                )
            elif decision.action == "delete_zero_regular":
                odoo_client.delete_payroll_work_entry(decision.work_entry_id)
            else:
                raise RuntimeError(f"unsupported correction action {decision.action!r}")
        except Exception:
            _log.warning(
                "payroll guard: mutation failed for entry %s",
                decision.work_entry_id, exc_info=True,
            )
            review_issues.append(_as_review(decision, "write_failed"))
            continue

        try:
            if decision.action == "duration_update":
                verified = odoo_client.fetch_payroll_work_entry(decision.work_entry_id)
                verification_ok = _duration_matches(
                    verified, decision, decision.after_duration
                )
                detail = "duration reread matched"
            else:
                verification_ok = not odoo_client.payroll_work_entry_exists(
                    decision.work_entry_id
                )
                detail = "zero-target draft regular row absent"
        except Exception:
            verification_ok = False
            _log.warning(
                "payroll guard: verification read failed for entry %s",
                decision.work_entry_id, exc_info=True,
            )
        if not verification_ok:
            review_issues.append(_as_review(decision, "verification_failed"))
            continue

        corrected_count += 1
        try:
            store.append_correction(decision, detail, now)
        except Exception:
            _log.warning(
                "payroll guard: audit failed for corrected entry %s",
                decision.work_entry_id, exc_info=True,
            )
            review_issues.append(_as_review(decision, "audit_failed"))

    try:
        alert.sync_review_task(review_issues, now)
    except Exception:
        _log.warning("payroll guard: could not sync review task", exc_info=True)

    _log.warning(
        "payroll guard: corrected=%d review=%d noop=%d candidates=%d",
        corrected_count, len(review_issues), noop_count, len(keys),
    )
    return {
        "corrected": corrected_count,
        "review": len(review_issues),
        "noop": noop_count,
    }
```

An audit failure returns `corrected == 1` and `review == 1`, because the Odoo
correction was verified even though its local audit needs review.

- [ ] **Step 6: Run all guard-layer tests and lint**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_odoo_payroll.py \
  tests/test_payroll_work_entry_rules.py \
  tests/test_payroll_work_entry_store.py \
  tests/test_payroll_work_entry_alert.py \
  tests/test_payroll_work_entry_guard.py -q
.venv/bin/ruff check \
  src/zira_dashboard/_odoo_payroll.py \
  src/zira_dashboard/payroll_work_entry_rules.py \
  src/zira_dashboard/payroll_work_entry_store.py \
  src/zira_dashboard/payroll_work_entry_alert.py \
  src/zira_dashboard/payroll_work_entry_guard.py
```

Expected: all focused tests pass; Ruff reports no errors.

- [ ] **Step 7: Add the Task 5 changelog entry, commit, and push**

Run `date '+%I:%M %p'` and add a heading using that exact time followed by
` - Payroll safety check built`, then add:

```markdown
- **The payroll safety check is built and tested.** It checks fresh Odoo details before any change and keeps unclear items for a person. It is not running on its own yet.
```

Then run:

```bash
git add CHANGELOG.md src/zira_dashboard/payroll_work_entry_guard.py tests/test_payroll_work_entry_guard.py
git commit -m "feat: reconcile Odoo payroll lunch overages"
git push origin main
```

Expected: commit and push succeed; the guard is callable but not scheduled.

---

### Task 6: Schedule, document, deploy, and verify the permanent guard

**Files:**
- Modify: `src/zira_dashboard/app.py:335-396`
- Modify: `tests/test_page_warmer.py`
- Modify: `.env.example`
- Modify: `docs/superpowers/specs/2026-08-03-odoo-payroll-lunch-overage-guard-design.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `payroll_work_entry_guard.run_once()`.
- Produces: `_tick_payroll_work_entry_guard()` and `_WARMERS` entry `("payroll work-entry guard", _tick_payroll_work_entry_guard, 300)`.

- [ ] **Step 1: Write the failing warmer tests**

Append to `tests/test_page_warmer.py`:

```python
def test_payroll_work_entry_guard_registered_every_five_minutes():
    from zira_dashboard import app as app_module
    assert asyncio.iscoroutinefunction(app_module._tick_payroll_work_entry_guard)
    entry = next(
        (item for item in app_module._WARMERS
         if item[1] is app_module._tick_payroll_work_entry_guard),
        None,
    )
    assert entry == (
        "payroll work-entry guard",
        app_module._tick_payroll_work_entry_guard,
        300,
    )


def test_payroll_guard_tick_runs_blocking_work_off_event_loop(monkeypatch):
    from zira_dashboard import app as app_module, payroll_work_entry_guard
    calls = []

    async def fake_to_thread(fn, *args):
        calls.append((fn, args))
        return fn(*args)

    monkeypatch.setattr(app_module.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(payroll_work_entry_guard, "run_once", lambda: {"corrected": 0})
    asyncio.run(app_module._tick_payroll_work_entry_guard())
    assert calls == [(payroll_work_entry_guard.run_once, ())]
```

- [ ] **Step 2: Run the warmer tests and confirm the red state**

Run:

```bash
.venv/bin/python -m pytest tests/test_page_warmer.py::test_payroll_work_entry_guard_registered_every_five_minutes tests/test_page_warmer.py::test_payroll_guard_tick_runs_blocking_work_off_event_loop -q
```

Expected: fail because the tick is not defined or registered.

- [ ] **Step 3: Wire the five-minute tick**

Add near `_tick_calendar_conflicts` in `app.py`:

```python
async def _tick_payroll_work_entry_guard():
    """Repair only the verified Odoo draft payroll lunch-overage defect."""
    from . import payroll_work_entry_guard

    await asyncio.to_thread(payroll_work_entry_guard.run_once)
```

Add this exact registry row:

```python
("payroll work-entry guard", _tick_payroll_work_entry_guard, 300),
```

The existing `_run_warmer` exception boundary remains the outer safety net.

- [ ] **Step 4: Document the kill switch**

Add under the Odoo block in `.env.example`:

```dotenv
PAYROLL_WORK_ENTRY_GUARD_ENABLED=1  # set to 0 for an immediate no-write stop
```

- [ ] **Step 5: Run focused, full-suite, compile, and lint verification**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_odoo_payroll.py \
  tests/test_payroll_work_entry_rules.py \
  tests/test_payroll_work_entry_store.py \
  tests/test_payroll_work_entry_alert.py \
  tests/test_payroll_work_entry_guard.py \
  tests/test_page_warmer.py -q
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src/zira_dashboard
.venv/bin/ruff check src/zira_dashboard tests
git diff --check
```

Expected: focused tests pass; the full suite passes with only documented skips; compileall and Ruff succeed; `git diff --check` prints nothing.

- [ ] **Step 6: Update design status and add the final user-facing changelog entry**

Add an implementation status below the design date:

```markdown
**Status:** Implemented and production-verified.
```

Run `date '+%I:%M %p'` and add a new top changelog heading using that exact
time followed by ` - Automatic payroll lunch protection`, then add:

```markdown
#### Fixes

- **Plant Manager now checks Odoo payroll every five minutes after overtime is approved.** It removes only the known extra 30-minute lunch from draft regular hours, keeps a record, and asks for help instead of changing anything unclear.
```

- [ ] **Step 7: Commit and push the activation**

Run:

```bash
git add .env.example CHANGELOG.md docs/superpowers/specs/2026-08-03-odoo-payroll-lunch-overage-guard-design.md src/zira_dashboard/app.py tests/test_page_warmer.py
git commit -m "feat: activate payroll lunch overage guard"
git push origin main
```

Expected: Railway starts a deployment from `main`.

- [ ] **Step 8: Verify Railway and Odoo production behavior**

Run:

```bash
railway status
railway logs --service web
```

Wait for the deployment to become Online. Confirm:

- schema bootstrap has no error;
- the warmer logs a summary and continues ticking;
- no `payroll guard` exception repeats;
- the previously repaired W27, W30, and W31 records remain at `40:00` regular;
- the fresh scan reports `corrected=0` unless Odoo regenerated a known draft defect during deployment;
- any review task lists only overtime-related mismatches that failed a safety rule;
- running `payroll_work_entry_guard.run_once()` a second time is idempotent and reports no repeated correction for the same current state.

If anything unexpected occurs, set `PAYROLL_WORK_ENTRY_GUARD_ENABLED=0` in Railway immediately, redeploy, and report the exact log and affected record ids. Do not regenerate Work Entries and do not manually widen the classifier.

---

## Completion gate

The permanent fix is complete only when all six tasks are checked, every implementation commit is pushed to `origin/main`, the full test and Ruff gates pass, Railway is Online, the five-minute warmer has run successfully, the live repaired weeks still show `40:00`, and a second pass is idempotent. A committed plan or a local-only implementation is not completion.
