# Missed Punch-Out Odoo Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Reconcile stale missed-punch Odoo attendance IDs against Odoo's current day records so already-fixed records clear with an explanation and safe replacement records receive the entered correction.

**Architecture:** Add a small Odoo-client read that returns normalized attendances for one employee on one plant-local day. The missed-punch route calls that read only after an Odoo missing-record fault, then either settles the local alert from a changed checkout, writes one safe open replacement, or returns an actionable conflict without changing local state. Both existing correction UIs render the returned success message.

**Tech Stack:** Python 3, FastAPI, PostgreSQL, Odoo XML-RPC, vanilla JavaScript, pytest.

## Global Constraints

- Odoo is the authoritative attendance source; reconciliation uses a fresh Odoo read, not a local cache.
- Only records for the flagged employee and original plant-local check-in day may be considered.
- Do not auto-dismiss an Odoo checkout equal to auto_closed_at; that is the original automatic midnight close and still needs correction.
- Never alter an ambiguous record set or another employee's attendance.
- Return friendly text; raw Odoo XML-RPC fault representations must never reach either UI.
- Preserve the existing normal correction path, time bounds, audit schema, and midnight worker behavior.

---

## File Structure

| File | Responsibility |
| --- | --- |
| src/zira_dashboard/_odoo_attendance.py | Employee/day Odoo attendance query and normalization. |
| src/zira_dashboard/odoo_client.py | Public facade wrapper used by route code. |
| src/zira_dashboard/routes/missed_punch_out.py | Reconcile deleted attendance IDs, resolve/rebind safely, audit outcome. |
| src/zira_dashboard/static/exceptions.js | Show reconciliation success before removing Inbox row. |
| src/zira_dashboard/static/footer.js | Show reconciliation success in the global modal. |
| tests/test_odoo_attendance_for_day.py | Client read tests. |
| tests/test_missed_punch_out_routes.py | Route settlement/rebound/failure tests. |

### Task 1: Add the current Odoo attendance-by-employee/day read

**Files:**

- Modify: src/zira_dashboard/_odoo_attendance.py:164-203
- Modify: src/zira_dashboard/odoo_client.py:396-398
- Test: tests/test_odoo_attendance_for_day.py

**Interfaces:**

- Consumes: execute_fn(model, method, args, kwargs), to_odoo_dt, odoo_dt_to_iso, and SITE_TZ.
- Produces: fetch_employee_attendances_for_day(employee_odoo_id: int, day: date) -> list[dict], with integer id, ISO UTC check_in, and ISO UTC check_out or None.

- [ ] **Step 1: Write the failing tests**

Add these tests:

~~~
from datetime import date

def test_fetch_employee_attendances_for_day_uses_employee_and_day_bounds(monkeypatch):
    calls = []
    monkeypatch.setattr(odoo_client, "execute", lambda *a, **kw: calls.append((a, kw)) or [
        {"id": 34, "check_in": "2026-07-12 17:47:00", "check_out": False},
    ])

    assert odoo_client.fetch_employee_attendances_for_day(6, date(2026, 7, 12)) == [{
        "id": 34, "check_in": "2026-07-12T17:47:00+00:00", "check_out": None,
    }]
    args, kwargs = calls[0]
    assert args[:2] == ("hr.attendance", "search_read")
    assert ("employee_id", "=", 6) in args[2]
    assert kwargs["fields"] == ["id", "check_in", "check_out"]

def test_fetch_employee_attendances_for_day_preserves_closed_checkout(monkeypatch):
    monkeypatch.setattr(odoo_client, "execute", lambda *a, **kw: [
        {"id": 35, "check_in": "2026-07-12 17:47:00", "check_out": "2026-07-12 22:00:00"},
    ])

    assert odoo_client.fetch_employee_attendances_for_day(6, date(2026, 7, 12)) == [{
        "id": 35, "check_in": "2026-07-12T17:47:00+00:00",
        "check_out": "2026-07-12T22:00:00+00:00",
    }]
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: ZIRA_API_KEY=test .venv/bin/python -m pytest -q tests/test_odoo_attendance_for_day.py -k employee_attendances

Expected: FAIL because fetch_employee_attendances_for_day does not exist.

- [ ] **Step 3: Write minimal implementation**

Add near fetch_attendances_for_day in src/zira_dashboard/_odoo_attendance.py:

~~~
def fetch_employee_attendances_for_day(
    execute_fn: Callable[..., Any], employee_odoo_id: int, day: date
) -> list[dict]:
    start_local = datetime.combine(day, _time.min, tzinfo=shift_config.SITE_TZ)
    end_local = start_local + timedelta(days=1)
    rows = execute_fn(
        "hr.attendance", "search_read",
        [
            ("employee_id", "=", int(employee_odoo_id)),
            ("check_in", ">=", to_odoo_dt(start_local)),
            ("check_in", "<", to_odoo_dt(end_local)),
        ],
        fields=["id", "check_in", "check_out"],
        order="check_in asc, id asc",
    )
    return [
        {
            "id": int(row["id"]),
            "check_in": odoo_dt_to_iso(row.get("check_in")),
            "check_out": odoo_dt_to_iso(row.get("check_out")),
        }
        for row in rows
        if row.get("id") and odoo_dt_to_iso(row.get("check_in"))
    ]
~~~

Add this facade wrapper to src/zira_dashboard/odoo_client.py:

~~~
def fetch_employee_attendances_for_day(employee_odoo_id: int, day) -> list[dict]:
    return _odoo_attendance.fetch_employee_attendances_for_day(
        execute, employee_odoo_id, day
    )
~~~

- [ ] **Step 4: Run tests to verify they pass**

Run: ZIRA_API_KEY=test .venv/bin/python -m pytest -q tests/test_odoo_attendance_for_day.py

Expected: PASS.

- [ ] **Step 5: Commit**

~~~
git add src/zira_dashboard/_odoo_attendance.py src/zira_dashboard/odoo_client.py tests/test_odoo_attendance_for_day.py
git commit -m "feat: read current Odoo attendance for missed punches"
~~~

### Task 2: Reconcile deleted missed-punch records in the route

**Files:**

- Modify: src/zira_dashboard/routes/missed_punch_out.py:1-100
- Test: tests/test_missed_punch_out_routes.py

**Interfaces:**

- Consumes: fetch_employee_attendances_for_day, get_unresolved, correct, and inbox_log.log_event_safe.
- Produces: correction responses with optional message on success; a 409 and a friendly error when deleted-record reconciliation cannot safely finish.

- [ ] **Step 1: Write the failing route tests**

Use a helper unresolved row with attendance_id 3326, employee_odoo_id 6, original check_in, and automatic midnight close. Add these tests plus no-candidate, multiple-open, refresh-failure, and non-deleted-fault coverage:

~~~
def test_deleted_odoo_record_settles_when_current_checkout_changed(monkeypatch):
    row = _unresolved_row()
    _patch_deleted_write(monkeypatch, row)
    monkeypatch.setattr(odoo_client, "fetch_employee_attendances_for_day", lambda *_: [{
        "id": 4001, "check_in": row["check_in"].isoformat(),
        "check_out": "2026-07-13T18:00:00+00:00",
    }])
    corrected = []
    monkeypatch.setattr(mpo, "correct", lambda aid, ts: corrected.append((aid, ts)))

    response = missed_punch_route._correct_sync({"attendance_id": 3326, "time": "13:00"})

    assert response.status_code == 200
    assert _body(response)["message"] == "Odoo already resolved this conflict."
    assert corrected[0][0] == 3326

def test_deleted_odoo_record_corrects_one_open_current_record(monkeypatch):
    row = _unresolved_row()
    calls = []
    monkeypatch.setattr(mpo, "get_unresolved", lambda _id: row)
    monkeypatch.setattr(odoo_client, "clock_out",
                        lambda aid, ts, **kw: calls.append((aid, ts, kw)) if aid == 4001 else _raise_deleted_fault())
    monkeypatch.setattr(odoo_client, "fetch_employee_attendances_for_day", lambda *_: [{
        "id": 4001, "check_in": row["check_in"].isoformat(), "check_out": None,
    }])
    monkeypatch.setattr(mpo, "correct", lambda *args: None)

    response = missed_punch_route._correct_sync({"attendance_id": 3326, "time": "13:00"})

    assert response.status_code == 200
    assert calls[0][0] == 4001
    assert _body(response)["message"] == "Updated the current Odoo attendance."

def test_original_automatic_midnight_record_is_not_auto_dismissed(monkeypatch):
    row = _unresolved_row()
    _patch_deleted_write(monkeypatch, row)
    monkeypatch.setattr(odoo_client, "fetch_employee_attendances_for_day", lambda *_: [{
        "id": 4001, "check_in": row["check_in"].isoformat(),
        "check_out": row["auto_closed_at"].astimezone(timezone.utc).isoformat(),
    }])

    response = missed_punch_route._correct_sync({"attendance_id": 3326, "time": "13:00"})

    assert response.status_code == 409
    assert "Verify" in _body(response)["error"]
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: ZIRA_API_KEY=test .venv/bin/python -m pytest -q tests/test_missed_punch_out_routes.py

Expected: FAIL because the current route returns raw deleted-record fault text with 500.

- [ ] **Step 3: Implement narrowly scoped reconciliation**

Import xmlrpc.client. Add these helpers before _correct_sync:

~~~
def _is_missing_odoo_attendance(error: Exception) -> bool:
    return (
        isinstance(error, xmlrpc.client.Fault)
        and "record does not exist or has been deleted" in error.faultString.lower()
    )

def _site_datetime(value):
    return datetime.fromisoformat(value).astimezone(SITE_TZ) if value else None

def _reconcile_deleted_attendance(row: dict):
    try:
        current = odoo_client.fetch_employee_attendances_for_day(
            int(row["employee_odoo_id"]),
            row["check_in"].astimezone(SITE_TZ).date(),
        )
    except Exception:
        return None, None, "Unable to refresh this attendance from Odoo. Verify it in Odoo and try again."

    auto_closed = row["auto_closed_at"].astimezone(SITE_TZ)
    settled = [_site_datetime(item.get("check_out")) for item in current]
    settled = [checkout for checkout in settled if checkout and checkout != auto_closed]
    if settled:
        return None, max(settled), None

    open_rows = [item for item in current if not item.get("check_out")]
    if len(open_rows) == 1:
        return int(open_rows[0]["id"]), None, None
    if not current:
        return None, None, "Odoo has no attendance for this employee on that day. Verify it in Odoo, then try again."
    return None, None, "Odoo has multiple current attendances for this employee on that day. Verify the correct record in Odoo, then try again."
~~~

Inside the existing clock_out exception branch:

1. Retain the friendly 500 when _is_missing_odoo_attendance is false.
2. Call _reconcile_deleted_attendance for a missing-record fault.
3. With settled_checkout: call correct(att_id, settled_checkout), log action dismiss and outcome Odoo already resolved this conflict., and return ok true with that message.
4. With replacement_id: write corrected to that replacement using mode manual, call correct(att_id, corrected), log action correct and outcome Updated the current Odoo attendance., and return ok true with that message.
5. With error: return it as a 409 without resolving or logging success.

Extract the existing inbox log call into one helper taking outcome, before value, and after value so normal, settled, and rebound corrections retain identical actor/item metadata.

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: ZIRA_API_KEY=test .venv/bin/python -m pytest -q tests/test_missed_punch_out_routes.py

Expected: PASS.

- [ ] **Step 5: Commit**

~~~
git add src/zira_dashboard/routes/missed_punch_out.py tests/test_missed_punch_out_routes.py
git commit -m "fix: reconcile stale missed-punch Odoo records"
~~~

### Task 3: Explain reconciliation outcomes in both correction UIs

**Files:**

- Modify: src/zira_dashboard/static/exceptions.js:745-760
- Modify: src/zira_dashboard/static/footer.js:1017-1035
- Test: tests/test_exception_inbox.py

**Interfaces:**

- Consumes: correction response with ok, optional message, and optional error.
- Produces: both clients show message before removing a successful row; errors leave the row visible.

- [ ] **Step 1: Write the failing static contract test**

~~~
def test_missed_punch_clients_show_odoo_reconciliation_message():
    exceptions_js = (STATIC_DIR / "exceptions.js").read_text()
    footer_js = (STATIC_DIR / "footer.js").read_text()

    assert "resolveRow(row, (resp && resp.message) || 'Corrected')" in exceptions_js
    assert "finishMpoRow(li, (res && res.message) || 'Corrected ✓', true, api)" in footer_js
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: ZIRA_API_KEY=test .venv/bin/python -m pytest -q tests/test_exception_inbox.py -k reconciliation_message

Expected: FAIL because both scripts hard-code Corrected text.

- [ ] **Step 3: Implement the minimal client changes**

Replace the Inbox success branch with:

~~~
if (resp && resp.ok) resolveRow(row, (resp && resp.message) || 'Corrected');
~~~

Replace the modal success branch with:

~~~
finishMpoRow(li, (res && res.message) || 'Corrected ✓', true, api);
~~~

Do not alter error branches.

- [ ] **Step 4: Run focused regression tests**

Run: ZIRA_API_KEY=test .venv/bin/python -m pytest -q tests/test_exception_inbox.py -k reconciliation_message tests/test_missed_punch_out_routes.py

Expected: PASS.

- [ ] **Step 5: Commit**

~~~
git add src/zira_dashboard/static/exceptions.js src/zira_dashboard/static/footer.js tests/test_exception_inbox.py
git commit -m "fix: explain resolved missed-punch Odoo conflicts"
~~~

### Task 4: Verify and document the behavior

**Files:**

- Modify: CHANGELOG.md
- Verify: focused tests, full pytest suite, Ruff, and git diff check.

- [ ] **Step 1: Add the changelog entry**

Under the current date heading, add:

~~~
- **Missed Punch Out now reconciles Odoo edits safely.** If a manager already fixed or replaced an auto-closed attendance in Odoo, Plant Manager refreshes that employee's current attendance for the original day. A changed Odoo checkout clears the alert with an explanation; a single open replacement receives the entered correction; missing or ambiguous Odoo data remains open with a clear verification instruction instead of a raw Odoo fault.
~~~

- [ ] **Step 2: Run complete verification**

Run:

~~~
ZIRA_API_KEY=test .venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
git diff --check
~~~

Expected: pytest exits 0, database-dependent tests may skip, Ruff exits 0, and git diff check prints nothing.

- [ ] **Step 3: Commit**

~~~
git add CHANGELOG.md
git commit -m "docs: record missed-punch Odoo reconciliation"
~~~

## Plan Self-Review

- **Spec coverage:** Task 1 creates the fresh Odoo read; Task 2 covers changed checkout settlement, one-open-record rebound, automatic-midnight protection, safe errors, audit, and raw-fault suppression; Task 3 shows outcome text in both surfaces; Task 4 documents and verifies the result.
- **Plan completeness:** Every test, implementation, and verification step has concrete content.
- **Type consistency:** Task 1 defines fetch_employee_attendances_for_day; Task 2 consumes it and emits message; Task 3 consumes message. The original attendance ID remains the local alert key; a replacement ID is used only for the Odoo write.
