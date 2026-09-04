# Dismiss Test Odoo Work Centers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let managers dismiss unknown Odoo work-center exceptions only when every source work-center label is a system-test label.

**Architecture:** Add one shared, pure label predicate to the inbox composer, use the existing `missing_wc_resolved` table to suppress every attendance record behind an accepted item, and expose a server-validated dismiss endpoint. The template and JavaScript only offer the action for rows marked eligible by the server; the endpoint independently reloads and validates the raw exception before writing.

**Tech Stack:** Python 3, FastAPI, Jinja2, PostgreSQL, browser JavaScript, pytest

## Global Constraints

- Only `attendance_unmapped_location` items with at least one raw label and `test` in every raw label, case-insensitively, may be dismissed.
- Blank, real, and mixed test/non-test labels must remain non-dismissible and retain the mapping action.
- Dismissal must never modify Odoo attendance or Odoo work-center mappings.
- The endpoint must validate the current raw exception snapshot instead of trusting browser-provided labels or attendance IDs.
- A valid dismissal suppresses all attendance IDs atomically and writes one best-effort human audit event.
- The action is not undoable in the inbox UI.
- New What's New text must use short, common words and explain how the change helps the user.

---

## File Structure

- `src/zira_dashboard/exception_inbox.py`: owns the shared test-label predicate, row action metadata, and suppression of resolved unmapped-location issues from both snapshot and summary composition.
- `src/zira_dashboard/missing_wc.py`: adds the atomic multi-attendance suppression write using the existing table.
- `src/zira_dashboard/routes/exceptions.py`: reloads and validates the raw exception item, performs dismissal, and records the audit event.
- `src/zira_dashboard/templates/exceptions.html`: renders Dismiss for eligible rows and Map for other unknown work centers.
- `src/zira_dashboard/static/exceptions.js`: posts the stable item key and resolves the row only after success.
- `tests/test_exception_inbox_attendance.py`: covers label eligibility, row actions, and resolved-item filtering in both inbox representations.
- `tests/test_missing_wc.py`: covers atomic multi-ID suppression.
- `tests/test_test_work_center_dismissal.py`: covers endpoint validation, writes, audit behavior, and rendered controls.
- `tests/test_exception_inbox_attendance_js.py`: covers the browser request contract.
- `CHANGELOG.md`: adds the user-facing shipped feature note.

### Task 1: Eligibility and Inbox Composition

**Files:**
- Modify: `src/zira_dashboard/exception_inbox.py:325-500`
- Modify: `tests/test_exception_inbox_attendance.py:20-465`

**Interfaces:**
- Produces: `is_dismissible_test_work_center(labels: Sequence[object]) -> bool`
- Produces: `_without_resolved_unmapped_issues(snapshot, resolved_ids: set[int]) -> AttendanceExceptionSnapshot`
- Produces: row action `{"type": "attendance_unmapped_location_dismiss"}` for eligible issues
- Consumes: `missing_wc.resolved_ids() -> set[int]`

- [ ] **Step 1: Write failing predicate and row-action tests**

Add tests that construct unmapped issues with explicit labels and assert the exact boundary:

```python
@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (("Test Workcenter",), True),
        (("Night TEST Cell", "test station 2"), True),
        ((), False),
        (("Dismantler 1",), False),
        (("Test Workcenter", "Dismantler 1"), False),
    ],
)
def test_test_work_center_dismissal_requires_every_nonblank_label(labels, expected):
    assert exception_inbox.is_dismissible_test_work_center(labels) is expected


def test_unmapped_test_work_center_row_gets_dismiss_action():
    issue = replace(
        _issue("attendance_unmapped_location", "attendance_unmapped_location:42:901:x"),
        raw_work_center_labels=("Test Workcenter",),
        odoo_work_center_ids=(17,),
    )

    row = exception_inbox._attendance_issue_row(issue)

    assert row["action"] == {"type": "attendance_unmapped_location_dismiss"}
```

Also assert a mixed-label row has `action is None`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `pytest -q tests/test_exception_inbox_attendance.py -k 'test_work_center_dismissal or unmapped_test_work_center_row'`

Expected: FAIL because `is_dismissible_test_work_center` does not exist and unmapped rows have no dismiss action.

- [ ] **Step 3: Implement the pure predicate and row metadata**

Add the public predicate near `_ATTENDANCE_SECTION_META`:

```python
def is_dismissible_test_work_center(labels: Sequence[object]) -> bool:
    normalized = [str(label).strip() for label in labels]
    return bool(normalized) and all(label and "test" in label.casefold() for label in normalized)
```

In `_attendance_issue_row`, replace the unconditional `"action": None` with:

```python
"action": (
    {"type": "attendance_unmapped_location_dismiss"}
    if issue.kind == "attendance_unmapped_location"
    and is_dismissible_test_work_center(raw_labels)
    else None
),
```

- [ ] **Step 4: Add failing suppression-composition tests**

Test the pure filter directly with an unmapped issue carrying `(901, 902)`:

```python
def test_resolved_unmapped_issue_is_hidden_only_when_all_attendance_ids_are_resolved():
    issue = replace(
        _issue("attendance_unmapped_location", "attendance_unmapped_location:42:901,902:x"),
        attendance_ids=(901, 902),
        raw_work_center_labels=("Test Workcenter",),
    )
    snapshot = _attendance_snapshot(mode="shadow", issues=(issue,))

    partial = exception_inbox._without_resolved_unmapped_issues(snapshot, {901})
    complete = exception_inbox._without_resolved_unmapped_issues(snapshot, {901, 902})

    assert partial.issues == (issue,)
    assert complete.issues == ()
```

Extend `test_summary_and_snapshot_count_the_same_attendance_items` by monkeypatching `missing_wc.resolved_ids` to include the test issue's attendance ID, then assert the unmapped count is zero in both results while the unrelated duplicate issue remains.

- [ ] **Step 5: Run the suppression tests and verify RED**

Run: `pytest -q tests/test_exception_inbox_attendance.py -k 'resolved_unmapped or summary_and_snapshot'`

Expected: FAIL because the filter does not exist and resolved attendance IDs do not affect timeline exceptions.

- [ ] **Step 6: Implement suppression in the shared composer**

Import `replace` from `dataclasses`. Add:

```python
def _without_resolved_unmapped_issues(snapshot, resolved_ids: set[int]):
    issues = tuple(
        issue
        for issue in snapshot.issues
        if not (
            issue.kind == "attendance_unmapped_location"
            and issue.attendance_ids
            and set(issue.attendance_ids).issubset(resolved_ids)
        )
    )
    return snapshot if issues == snapshot.issues else replace(snapshot, issues=issues)
```

At the end of `_attendance_snapshot`, read `missing_wc.resolved_ids()` inside a `try/except`. On read failure, log the failure and keep the raw snapshot visible. On success, pass the snapshot through `_without_resolved_unmapped_issues` before returning it. This single composition point ensures `build_snapshot()` and `build_summary()` see the same filtered issues.

- [ ] **Step 7: Run the complete inbox attendance test file**

Run: `pytest -q tests/test_exception_inbox_attendance.py`

Expected: PASS.

- [ ] **Step 8: Commit Task 1**

```bash
git add src/zira_dashboard/exception_inbox.py tests/test_exception_inbox_attendance.py
git commit -m "feat: identify dismissible test work centers"
```

### Task 2: Atomic Suppression and Validated Endpoint

**Files:**
- Modify: `src/zira_dashboard/missing_wc.py:63-80`
- Modify: `src/zira_dashboard/routes/exceptions.py:17-70`
- Modify: `tests/test_missing_wc.py:195-225`
- Create: `tests/test_test_work_center_dismissal.py`

**Interfaces:**
- Consumes: `exception_inbox.is_dismissible_test_work_center(labels)`
- Produces: `missing_wc.resolve_many(attendance_ids: Sequence[int], action: str, name: str | None = None, wc_name: str | None = None) -> None`
- Produces: `POST /api/exceptions/attendance-unmapped-location/dismiss` with JSON `{"item_key": str}` and response `{"ok": true}` or an error status

- [ ] **Step 1: Write a failing atomic multi-ID store test**

Use two reserved attendance IDs and clean both before and after the test:

```python
@pg
def test_resolve_many_suppresses_every_attendance_id():
    from zira_dashboard import db

    ids = (999012, 999013)
    db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = ANY(%s)", (list(ids),))
    try:
        missing_wc.resolve_many(ids, "dismissed", name="Luke")
        assert ids[0] in missing_wc.resolved_ids()
        assert ids[1] in missing_wc.resolved_ids()
    finally:
        db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = ANY(%s)", (list(ids),))
```

- [ ] **Step 2: Run the store test and verify RED**

Run: `pytest -q tests/test_missing_wc.py::test_resolve_many_suppresses_every_attendance_id`

Expected: FAIL because `resolve_many` does not exist.

- [ ] **Step 3: Implement one-transaction multi-ID suppression**

Add a sequence import and implement:

```python
def resolve_many(
    attendance_ids: Sequence[int],
    action: str,
    name: str | None = None,
    wc_name: str | None = None,
) -> None:
    ids = tuple(dict.fromkeys(int(value) for value in attendance_ids))
    if not ids:
        raise ValueError("at least one attendance id is required")
    from . import db

    with db.cursor() as cur:
        cur.executemany(
            "INSERT INTO missing_wc_resolved (attendance_id, action, name, wc_name) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (attendance_id) DO UPDATE SET action = EXCLUDED.action, "
            "name = EXCLUDED.name, wc_name = EXCLUDED.wc_name, resolved_at = now()",
            [(attendance_id, action, name, wc_name) for attendance_id in ids],
        )
```

- [ ] **Step 4: Run the store test and verify GREEN**

Run: `pytest -q tests/test_missing_wc.py::test_resolve_many_suppresses_every_attendance_id`

Expected: PASS when PostgreSQL tests are enabled; otherwise one explicit skip.

- [ ] **Step 5: Write failing endpoint contract tests**

In the new test file, construct `AttendanceException` values and monkeypatch `attendance_exceptions.build_snapshot`, `missing_wc.resolve_many`, and `inbox_log.log_event_safe`. Cover:

```python
def test_dismiss_current_test_item_suppresses_all_ids_and_audits(monkeypatch):
    issue = _unmapped_issue(labels=("Test Workcenter",), attendance_ids=(901, 902))
    resolved = MagicMock()
    logged = MagicMock(return_value=77)
    _install_snapshot(monkeypatch, issues=(issue,))
    monkeypatch.setattr(missing_wc, "resolve_many", resolved)
    monkeypatch.setattr(inbox_log, "log_event_safe", logged)

    response = client.post(
        "/api/exceptions/attendance-unmapped-location/dismiss",
        json={"item_key": issue.item_key},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    resolved.assert_called_once_with((901, 902), "dismissed", name=issue.employee_name)
    assert logged.call_args.kwargs["item_key"] == issue.item_key
    assert logged.call_args.kwargs["reversible"] is False
```

Add separate tests asserting no write and status `400`, `404`, or `409` for a missing item key, stale/no match, duplicate matches, blank labels, real labels, mixed labels, and empty attendance IDs.

- [ ] **Step 6: Run endpoint tests and verify RED**

Run: `pytest -q tests/test_test_work_center_dismissal.py -k dismiss`

Expected: FAIL with 404 because the endpoint does not exist.

- [ ] **Step 7: Implement server-side reload, validation, write, and audit**

Add a synchronous worker helper and async route. The helper must:

```python
def _dismiss_test_work_center_sync(body: dict, actor_upn=None, actor_name=None):
    item_key = str(body.get("item_key") or "").strip()
    if not item_key:
        return JSONResponse({"ok": False, "error": "Missing inbox item."}, status_code=400)

    snapshot = attendance_exceptions.build_snapshot(
        plant_day.today(), now_utc=exception_inbox._now_utc()
    )
    matches = [
        issue for issue in snapshot.issues
        if issue.kind == "attendance_unmapped_location" and issue.item_key == item_key
    ]
    if not matches:
        return JSONResponse({"ok": False, "error": "That inbox item is no longer open."}, status_code=404)
    if len(matches) != 1:
        return JSONResponse({"ok": False, "error": "That inbox item is ambiguous."}, status_code=409)

    issue = matches[0]
    if not exception_inbox.is_dismissible_test_work_center(issue.raw_work_center_labels):
        return JSONResponse({"ok": False, "error": "Only test work centers can be dismissed."}, status_code=409)
    if not issue.attendance_ids:
        return JSONResponse({"ok": False, "error": "This item has no attendance records."}, status_code=409)

    missing_wc.resolve_many(issue.attendance_ids, "dismissed", name=issue.employee_name)
    inbox_log.log_event_safe(
        item_kind="attendance_unmapped_location",
        item_key=issue.item_key,
        person_name=issue.employee_name,
        category_label="Unknown Odoo Work Center",
        action="dismiss",
        outcome="Dismissed test work center",
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        reversible=False,
        detail={"raw_work_center_labels": list(issue.raw_work_center_labels)},
    )
    return JSONResponse({"ok": True})
```

Import `attendance_exceptions` and `missing_wc` at the route module boundary. The async endpoint reads JSON, obtains the actor through `inbox_log.actor_from(request)`, and calls the helper through `asyncio.to_thread`. Catch source or write failures around the helper boundary, log them, and return a plain `500` error without claiming dismissal.

- [ ] **Step 8: Run endpoint and store tests**

Run: `pytest -q tests/test_test_work_center_dismissal.py tests/test_missing_wc.py`

Expected: PASS, with only documented PostgreSQL skips when `DATABASE_URL` is absent.

- [ ] **Step 9: Commit Task 2**

```bash
git add src/zira_dashboard/missing_wc.py src/zira_dashboard/routes/exceptions.py tests/test_missing_wc.py tests/test_test_work_center_dismissal.py
git commit -m "feat: dismiss test work center exceptions"
```

### Task 3: Render and Submit the Dismiss Action

**Files:**
- Modify: `src/zira_dashboard/templates/exceptions.html:62-165`
- Modify: `src/zira_dashboard/static/exceptions.js:1290-1450`
- Modify: `tests/test_test_work_center_dismissal.py`
- Modify: `tests/test_exception_inbox_attendance_js.py`
- Modify: `CHANGELOG.md:1-20`

**Interfaces:**
- Consumes: row action type `attendance_unmapped_location_dismiss`
- Consumes: `POST /api/exceptions/attendance-unmapped-location/dismiss`
- Sends: `{"item_key": row.dataset.itemKey}`

- [ ] **Step 1: Write failing rendered-control tests**

Render `/exceptions` with one eligible test row and assert:

```python
assert 'class="row-btn js-test-work-center-dismiss"' in response.text
assert "Map this Odoo work center" not in response.text
assert f'data-item-key="{issue.item_key}"' in response.text
```

Render a real unknown row and a mixed-label row and assert each contains `Map this Odoo work center` and does not contain `js-test-work-center-dismiss`.

- [ ] **Step 2: Run rendered-control tests and verify RED**

Run: `pytest -q tests/test_test_work_center_dismissal.py -k render`

Expected: FAIL because eligible rows still render the mapping link.

- [ ] **Step 3: Render mutually exclusive Dismiss and Map controls**

Before the existing unmapped-location mapping branch, add:

```jinja2
{% elif action and action.type == 'attendance_unmapped_location_dismiss' %}
  <button type="button" class="row-btn js-test-work-center-dismiss">Dismiss</button>
{% elif row.get('kind') == 'attendance_unmapped_location' and can_manage_work_centers and row.get('odoo_work_center_ids') and row.get('raw_work_center_labels') %}
```

Do not add browser-supplied label or attendance-ID data attributes.

- [ ] **Step 4: Write a failing JavaScript contract test**

Add exact source assertions:

```python
def test_test_work_center_dismiss_posts_only_stable_item_key():
    js = _js()
    assert "rowBtn.classList.contains('js-test-work-center-dismiss')" in js
    assert "'/api/exceptions/attendance-unmapped-location/dismiss'" in js
    assert "item_key: row.dataset.itemKey" in js
    assert "resolveRow(row, 'Dismissed')" in js
```

- [ ] **Step 5: Run the JavaScript test and verify RED**

Run: `pytest -q tests/test_exception_inbox_attendance_js.py::test_test_work_center_dismiss_posts_only_stable_item_key`

Expected: FAIL because the click branch does not exist.

- [ ] **Step 6: Implement the browser action**

Add a branch before the legacy missing-work-center dismiss branch:

```javascript
if (rowBtn.classList.contains('js-test-work-center-dismiss')) {
  if (!row.dataset.itemKey) {
    failRow(row, 'Missing inbox item.');
    return;
  }
  setBusy(row, true);
  rowStatus(row, 'Dismissing...', false);
  postJson('/api/exceptions/attendance-unmapped-location/dismiss', {
    item_key: row.dataset.itemKey,
  }).then(function (resp) {
    if (resp && resp.ok) resolveRow(row, 'Dismissed');
    else failRow(row, (resp && resp.error) || 'Dismiss failed.');
  }).catch(function () { failRow(row, 'Network error.'); });
  return;
}
```

Omitting the audit event ID from `resolveRow` deliberately prevents the Undo control.

- [ ] **Step 7: Add the shipped What's New note**

Add a new September 4 entry above the plan note without changing older text:

```markdown
### Dismiss system test work centers

- **Unknown work centers used for system tests now have a Dismiss button.** Real unknown work centers still ask you to match them, so setup problems stay easy to spot.
```

- [ ] **Step 8: Run all focused tests and static checks**

Run: `pytest -q tests/test_exception_inbox_attendance.py tests/test_test_work_center_dismissal.py tests/test_exception_inbox_attendance_js.py tests/test_missing_wc.py`

Expected: PASS, with only documented PostgreSQL skips when `DATABASE_URL` is absent.

Run: `ruff check src/zira_dashboard/exception_inbox.py src/zira_dashboard/missing_wc.py src/zira_dashboard/routes/exceptions.py tests/test_exception_inbox_attendance.py tests/test_test_work_center_dismissal.py tests/test_missing_wc.py`

Expected: `All checks passed!`

- [ ] **Step 9: Run the full regression suite**

Run: `pytest -q`

Expected: PASS with zero failures.

- [ ] **Step 10: Review the final diff against the design**

Run: `git diff --check && git diff --stat && git status --short`

Expected: no whitespace errors; only the planned source, test, changelog, design, and plan files are changed. Preserve unrelated `.cursorignore`, `.python-version`, and `uv.lock` files.

- [ ] **Step 11: Commit and push the implementation**

```bash
git add CHANGELOG.md src/zira_dashboard/exception_inbox.py src/zira_dashboard/missing_wc.py src/zira_dashboard/routes/exceptions.py src/zira_dashboard/templates/exceptions.html src/zira_dashboard/static/exceptions.js tests/test_exception_inbox_attendance.py tests/test_missing_wc.py tests/test_test_work_center_dismissal.py tests/test_exception_inbox_attendance_js.py
git commit -m "feat: dismiss system test work centers"
git push origin main
```
