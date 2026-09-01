# Past-Absence PTO Exception Inbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show every open past-absence PTO request in the existing Pending Time Off Inbox section and let a manager approve, deny, or finish payroll review without leaving the Inbox.

**Architecture:** Add a separate, bounded local-mirror reader for `absence_pto_requests`, then compose its count and rows with the ordinary time-off source at the Exception Inbox boundary. Keep a distinct `absence_pto` row/action identity while reusing the existing authenticated past-absence manager endpoints and the Inbox's existing resolution/count-refresh behavior.

**Tech Stack:** Python 3.11, FastAPI, PostgreSQL/psycopg2, Jinja2, browser JavaScript, pytest, Ruff, Node syntax checking.

## Global Constraints

- Keep past-absence PTO storage separate from ordinary `time_off_requests` storage.
- Load the Inbox only from local PostgreSQL mirrors; do not add an Odoo call to Inbox rendering.
- Do not change eligibility, balance, pay-period, conversion, denial, or manual-resolution rules.
- Do not approve, deny, or mark any request handled automatically.
- Keep the existing Time Off approval panel as a backup manager view.
- Use the existing authenticated routes under `/api/exceptions/absence-pto/{id}` for every manager mutation.
- Keep ordinary Time Off Inbox rows and actions behavior-identical.
- Treat source failures as unknown and visible; never turn a failed read into a false all-clear.
- New `CHANGELOG.md` text must use short, common words and must not claim the feature works before verification.
- Preserve unrelated workspace changes in `.superpowers/sdd/task-7-report.md`, `.cursorignore`, `.python-version`, and `uv.lock`.
- Do not press Approve, Deny, or Mark handled during deployment verification.

---

## File map

- `src/zira_dashboard/inbox_keys.py`: canonical stable Inbox identity for a past-absence PTO request.
- `src/zira_dashboard/exception_inbox.py`: bounded local read, row shaping, source isolation, count composition, and Pending Time Off section merge.
- `src/zira_dashboard/inbox_reconcile.py`: classify both row kinds in the shared section and keep each source's failure guard independent.
- `src/zira_dashboard/templates/exceptions.html`: pending and payroll-review controls for `absence_pto` rows.
- `src/zira_dashboard/static/exceptions.js`: call the existing past-absence approval, denial, and handled endpoints and preserve rows on failures.
- `tests/test_exception_inbox.py`: data, summary, snapshot, template, and JavaScript regression coverage.
- `tests/test_exception_inbox_attendance.py`: keep the attendance-focused Inbox fixture isolated from the new source.
- `tests/test_inbox_reconcile.py`: keep reconciler fixtures explicit about the new source and verify the stable item key.
- `CHANGELOG.md`: plain-language shipped behavior after the implementation is verified.

### Task 1: Deliver the complete past-absence PTO Inbox workflow

This is one atomic task because a backend-only or markup-only commit would deploy an incomplete manager action to `main`. Keep all red/green cycles below local, then create one verified implementation commit.

**Files:**
- Modify: `src/zira_dashboard/inbox_keys.py:1-20`
- Modify: `src/zira_dashboard/exception_inbox.py:194-261`
- Modify: `src/zira_dashboard/exception_inbox.py:485-609`
- Modify: `src/zira_dashboard/exception_inbox.py:612-671`
- Modify: `src/zira_dashboard/exception_inbox.py:916-927`
- Modify: `src/zira_dashboard/inbox_reconcile.py:27-115`
- Modify: `src/zira_dashboard/templates/exceptions.html:60-96`
- Modify: `src/zira_dashboard/templates/exceptions.html:170-205`
- Modify: `src/zira_dashboard/static/exceptions.js:840-950`
- Modify: `src/zira_dashboard/static/exceptions.js:1500-1570`
- Modify: `tests/test_exception_inbox.py:300-535`
- Modify: `tests/test_exception_inbox.py:1310-1355`
- Modify: `tests/test_exception_inbox.py:1465-1515`
- Modify: `tests/test_exception_inbox_attendance.py`
- Modify: `tests/test_inbox_reconcile.py`
- Modify: `CHANGELOG.md:12`

**Interfaces:**
- Consumes: existing `db.query(sql, params)`, `time_off_context.coverage_breakdowns_for(rows)`, `_capture(errors, source, call, fallback)`, `_queue_from_sections(sections)`, and the authenticated `absence_pto_admin` endpoints.
- Produces: `inbox_keys.absence_pto(request_id) -> str`.
- Produces: `exception_inbox._pending_absence_pto_count() -> int`.
- Produces: `exception_inbox._pending_absence_pto(limit: int = 8) -> tuple[int, list[dict]]`.
- Produces: Inbox action metadata `{"type": "absence_pto", "request_id": int, "state": "pending" | "needs_review"}`.
- Preserves: reconciler identity `absence_pto:<id>` with `item_kind="absence_pto"`, even though the row is rendered inside the shared `time_off` section.

- [ ] **Step 1: Add failing tests for the stable key, count, and bounded row shape**

Add focused tests to `tests/test_exception_inbox.py`:

```python
def test_absence_pto_inbox_key_matches_existing_audit_identity():
    assert inbox_keys.absence_pto(41) == "absence_pto:41"


def test_pending_absence_pto_count_reads_only_open_states(monkeypatch):
    captured = {}

    def fake_query(sql, params=()):
        captured["sql"] = sql
        captured["params"] = params
        return [{"n": 2}]

    monkeypatch.setattr(db, "query", fake_query)

    assert exception_inbox._pending_absence_pto_count() == 2
    assert "absence_pto_requests" in captured["sql"]
    assert "state IN ('pending', 'needs_review')" in captured["sql"]
    assert captured["params"] == ()


def test_pending_absence_pto_shapes_pending_and_review_rows(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params: [
        {
            "id": 41,
            "person_odoo_id": 7,
            "name": "Maria",
            "absence_day": date(2026, 8, 31),
            "state": "pending",
            "leave_type": "Paid Time Off",
            "sync_error": None,
            "total_count": 2,
        },
        {
            "id": 42,
            "person_odoo_id": 8,
            "name": "Eli",
            "absence_day": date(2026, 8, 30),
            "state": "needs_review",
            "leave_type": "Paid Time Off",
            "sync_error": "Payroll review is required.",
            "total_count": 2,
        },
    ])
    monkeypatch.setattr(
        exception_inbox.time_off_context,
        "coverage_breakdowns_for",
        lambda rows: {},
    )

    count, rows = exception_inbox._pending_absence_pto(limit=8)

    assert count == 2
    assert rows[0]["detail"] == "Past absence PTO · 1 PTO day · Waiting for approval"
    assert rows[0]["row_key"] == "absence_pto:41:pending"
    assert rows[0]["item_key"] == "absence_pto:41"
    assert rows[0]["action"] == {
        "type": "absence_pto",
        "request_id": 41,
        "state": "pending",
    }
    assert rows[1]["priority"] == "warn"
    assert rows[1]["badge"] == "Payroll review"
    assert rows[1]["action"]["state"] == "needs_review"
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
env DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest \
  tests/test_exception_inbox.py \
  -k "absence_pto_inbox_key or pending_absence_pto" -v
```

Expected: FAIL because `inbox_keys.absence_pto`, `_pending_absence_pto_count`, and `_pending_absence_pto` do not exist.

- [ ] **Step 3: Implement the canonical key and bounded local-mirror reader**

Add to `src/zira_dashboard/inbox_keys.py` beside `time_off`:

```python
def absence_pto(request_id) -> str:
    return f"absence_pto:{request_id}"
```

Add to `src/zira_dashboard/exception_inbox.py` after `_pending_time_off`:

```python
_PENDING_ABSENCE_PTO_WHERE = "r.state IN ('pending', 'needs_review')"


def _pending_absence_pto_count() -> int:
    from . import db

    rows = db.query(
        "SELECT COUNT(*) AS n FROM absence_pto_requests r "
        f"WHERE {_PENDING_ABSENCE_PTO_WHERE}"
    )
    return int(rows[0]["n"] or 0) if rows else 0


def _pending_absence_pto(limit: int = 8) -> tuple[int, list[dict]]:
    from . import db

    rows = db.query(
        "SELECT r.id, r.person_odoo_id, r.person_name AS name, "
        "r.absence_day, r.state, r.leave_type_name AS leave_type, "
        "r.sync_error, COUNT(*) OVER () AS total_count "
        "FROM absence_pto_requests r "
        f"WHERE {_PENDING_ABSENCE_PTO_WHERE} "
        "ORDER BY r.requested_at, r.id LIMIT %s",
        (limit,),
    )
    shaped = []
    for source in rows:
        day = source["absence_day"]
        needs_review = source["state"] == "needs_review"
        shaped.append({
            "id": source["id"],
            "person_odoo_id": source["person_odoo_id"],
            "date_from": day,
            "date_to": day,
            "name": source["name"],
            "label": day.isoformat(),
            "detail": (
                "Past absence PTO · 1 PTO day · Needs payroll review"
                if needs_review
                else "Past absence PTO · 1 PTO day · Waiting for approval"
            ),
            "state": source["state"],
            "sync_error": source.get("sync_error"),
            "past_due": False,
            "priority": "warn" if needs_review else "info",
            "badge": "Payroll review" if needs_review else "Approval",
            "row_key": _row_key("absence_pto", source["id"], source["state"]),
            "item_key": inbox_keys.absence_pto(source["id"]),
            "action": {
                "type": "absence_pto",
                "request_id": source["id"],
                "state": source["state"],
            },
        })
    coverage = time_off_context.coverage_breakdowns_for(shaped)
    for row in shaped:
        row["coverage"] = coverage.get(row["id"])
    count = int(rows[0].get("total_count") or 0) if rows else 0
    return count, shaped
```

Keep the exact state allowlist and `LIMIT %s`. Do not reuse `absence_pto_store.list_pending()`, because that reads an unbounded list before slicing.

- [ ] **Step 4: Run the focused reader tests and verify GREEN**

Run the Step 2 command again.

Expected: all selected tests PASS.

- [ ] **Step 5: Add failing tests for summary/snapshot composition and source isolation**

Extend the existing summary test so ordinary time off and past-absence PTO are both counted:

```python
monkeypatch.setattr(exception_inbox, "_pending_time_off_counts", lambda today: (4, 2))
monkeypatch.setattr(exception_inbox, "_pending_absence_pto_count", lambda: 1)

assert summary["total"] == 12
assert summary["urgent_total"] == 6
assert summary["sections"]["time_off"] == 5
```

Add a snapshot merge test:

```python
def test_snapshot_merges_past_absence_pto_into_pending_time_off(monkeypatch):
    _empty_inbox_sources(monkeypatch)
    ordinary = {
        "id": 20,
        "name": "Ana",
        "date_from": date(2026, 9, 2),
        "label": "2026-09-02",
        "priority": "info",
        "row_key": "time_off:20:confirm",
        "item_key": "time_off:20",
        "action": {"type": "time_off", "request_id": 20},
    }
    linked = {
        "id": 41,
        "name": "Maria",
        "date_from": date(2026, 8, 31),
        "label": "2026-08-31",
        "priority": "info",
        "row_key": "absence_pto:41:pending",
        "item_key": "absence_pto:41",
        "action": {"type": "absence_pto", "request_id": 41, "state": "pending"},
    }
    monkeypatch.setattr(exception_inbox, "_pending_time_off", lambda today: (1, [ordinary]))
    monkeypatch.setattr(exception_inbox, "_pending_absence_pto", lambda: (1, [linked]))

    snapshot = exception_inbox.build_snapshot()
    section = next(item for item in snapshot["sections"] if item["id"] == "time_off")

    assert section["count"] == 2
    assert [row["item_key"] for row in section["rows"]] == [
        "absence_pto:41",
        "time_off:20",
    ]
    assert snapshot["total"] == 2
```

Add two source-isolation assertions: an exception from `_pending_time_off` must still show past-absence rows with `{"source": "Pending Time Off"}`, and an exception from `_pending_absence_pto` must still show ordinary rows with `{"source": "Past Absence PTO"}`.

Add reconciler tests in `tests/test_inbox_reconcile.py` for the shared section:

```python
def test_open_now_keeps_absence_pto_kind_inside_time_off_section():
    snapshot = {
        "queue": [{
            "section_id": "time_off",
            "item_key": "absence_pto:41",
            "name": "Maria",
            "action": {"type": "absence_pto", "request_id": 41},
        }],
    }

    assert inbox_reconcile._open_now_from_snapshot(snapshot)["absence_pto:41"][
        "item_kind"
    ] == "absence_pto"


def test_complete_kinds_protects_only_failed_source_in_shared_time_off_section():
    snapshot = {
        "source_errors": [{"source": "Past Absence PTO"}],
        "sections": [{"id": "time_off", "count": 1, "rows": [{}]}],
    }

    complete = inbox_reconcile._complete_kinds(snapshot)

    assert "time_off" in complete
    assert "absence_pto" not in complete
```

Also assert that a healthy, untruncated shared section makes both kinds complete, while a truncated shared section makes neither kind complete. This conservative rule prevents the mirror from auto-resolving either kind when the combined display cap hid a row.

- [ ] **Step 6: Run the composition tests and verify RED**

Run:

```bash
env DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest \
  tests/test_exception_inbox.py tests/test_inbox_reconcile.py \
  -k "summary_counts_open or snapshot_merges_past_absence or time_off_source_failure or absence_pto_kind or failed_source_in_shared_time_off or shared_time_off_section" -v
```

Expected: FAIL because `build_summary()` and `build_snapshot()` do not call or merge the new source, and the reconciler does not yet distinguish the second kind inside the shared section.

- [ ] **Step 7: Compose counts and rows without coupling source failures**

In `build_summary()`, capture the new count separately and add it only at the composition boundary:

```python
pending_count, pending_urgent_count = _capture(
    source_errors,
    "Pending Time Off",
    lambda: _pending_time_off_counts(today),
    (0, 0),
)
absence_pto_count = _capture(
    source_errors,
    "Past Absence PTO",
    _pending_absence_pto_count,
    0,
)
pending_count += absence_pto_count
```

In `build_snapshot()`, capture and merge the bounded rows separately:

```python
pending_count, pending_rows = _capture(
    source_errors,
    "Pending Time Off",
    lambda: _pending_time_off(today),
    (0, []),
)
absence_pto_count, absence_pto_rows = _capture(
    source_errors,
    "Past Absence PTO",
    _pending_absence_pto,
    (0, []),
)
pending_count += absence_pto_count
pending_rows = sorted(
    [*pending_rows, *absence_pto_rows],
    key=lambda row: (
        row.get("date_from") or date.max,
        str(row.get("name") or "").lower(),
        str((row.get("action") or {}).get("type") or ""),
        int(row.get("id") or 0),
    ),
)
```

Update `_empty_inbox_sources()` and direct `build_summary()` / `build_snapshot()` fixtures in:

```python
monkeypatch.setattr(exception_inbox, "_pending_absence_pto_count", lambda: 0)
monkeypatch.setattr(exception_inbox, "_pending_absence_pto", lambda: (0, []))
```

Add those explicit zero-source patches in `tests/test_exception_inbox_attendance.py` and `tests/test_inbox_reconcile.py` anywhere their fixture calls the real snapshot/summary composer. Do not add a production fallback that hides a missing test patch.

In `src/zira_dashboard/inbox_reconcile.py`, let a section advertise more than one independently guarded kind:

```python
_SECTION_KINDS = {
    **{section: (kind,) for section, kind in _SECTION_KIND.items()},
    "time_off": ("time_off", "absence_pto"),
}

_KIND_SOURCE = {
    # existing entries remain unchanged
    "time_off": "Pending Time Off",
    "absence_pto": "Past Absence PTO",
}
```

Update `_complete_kinds()` so it first rejects a whole section when `complete` is false or `len(rows) < count`, then adds each kind from `_SECTION_KINDS[section_id]` only when that kind's own source label is absent from `source_errors`. Keep the existing `attendance_cutover_blocked` behavior.

Update `_open_now_from_snapshot()` so a row whose server-owned action type is `absence_pto` is stored with `item_kind="absence_pto"`; all other rows keep their existing section mapping. Do not infer the kind from visible labels. Together, these changes ensure a failed Past Absence PTO read cannot auto-resolve an existing past-PTO mirror row, while an unrelated ordinary time-off failure does not freeze a healthy past-PTO row forever.

- [ ] **Step 8: Run Inbox data tests and verify GREEN**

Run:

```bash
env DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest \
  tests/test_exception_inbox.py \
  tests/test_exception_inbox_attendance.py \
  tests/test_inbox_reconcile.py -q
```

Expected: all tests PASS with no production database connection.

- [ ] **Step 9: Add failing rendered-markup tests for pending and payroll-review rows**

Add a TestClient rendering test with two `absence_pto` queue rows and assert:

```python
assert 'data-action-type="absence_pto"' in response.text
assert 'data-request-id="41"' in response.text
assert 'class="row-btn primary js-time-off-approve"' in response.text
assert 'class="row-btn danger js-time-off-refuse"' in response.text
assert 'aria-label="Reason to deny past PTO"' in response.text
assert 'aria-label="How this past PTO request was handled"' in response.text
assert 'class="row-btn primary js-absence-pto-handled"' in response.text
```

The pending row action is `{"type": "absence_pto", "request_id": 41, "state": "pending"}`. The review row uses request ID 42 and state `needs_review`.

- [ ] **Step 10: Run the rendered-markup test and verify RED**

Run:

```bash
env DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest tests/test_exception_inbox.py \
  -k "renders_past_absence_pto_controls" -v
```

Expected: FAIL because the template does not recognize `absence_pto` action metadata.

- [ ] **Step 11: Render the correct controls from server-owned state**

In `exceptions.html`, attach the request ID for both action types:

```jinja2
{% elif action and action.type in ('time_off', 'absence_pto') %}
  data-request-id="{{ action.request_id or '' }}"
```

Before the existing ordinary time-off action branch, add the payroll-review state:

```jinja2
{% elif action and action.type == 'absence_pto' and action.state == 'needs_review' %}
  <input type="text"
         class="inline-input js-absence-pto-note"
         aria-label="How this past PTO request was handled"
         placeholder="How was this handled?"
         hidden>
  <button type="button" class="row-btn primary js-absence-pto-handled">
    Mark handled
  </button>
{% elif action and action.type in ('time_off', 'absence_pto') %}
  <button type="button" class="row-btn primary js-time-off-approve">Approve</button>
  <input type="text"
         class="inline-input js-time-off-reason"
         aria-label="{{ 'Reason to deny past PTO' if action.type == 'absence_pto' else 'Reason to deny time off' }}"
         placeholder="Reason to deny"
         hidden>
  <button type="button" class="row-btn danger js-time-off-refuse">Deny</button>
```

Server-owned `action.state` selects the control set; do not infer payroll-review state from visible copy.

- [ ] **Step 12: Run the rendered-markup test and verify GREEN**

Run the Step 10 command again.

Expected: PASS.

- [ ] **Step 13: Add failing JavaScript contract tests for all three past-absence actions**

Extend the static JavaScript test in `tests/test_exception_inbox.py` with exact contracts:

```python
assert "row.dataset.actionType === 'absence_pto'" in js
assert "'/api/exceptions/absence-pto/'" in js
assert "resp.status === 'needs_review'" in js
assert "resp.status === 'pending'" in js
assert "js-absence-pto-note" in js
assert "js-absence-pto-handled" in js
assert "base + '/approve'" in js
assert "base + '/deny'" in js
assert "base + '/handled'" in js
assert "note: handledNote" in js
```

Also assert Enter submits both `.js-time-off-reason` and `.js-absence-pto-note` through `submitRowInput`.

- [ ] **Step 14: Run the JavaScript contract test and verify RED**

Run:

```bash
env DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest tests/test_exception_inbox.py \
  -k "js_requires_time_off_deny_reason" -v
```

Expected: FAIL because the Inbox JavaScript always calls ordinary time-off endpoints and has no handled action.

- [ ] **Step 15: Route browser actions by the server action type**

In the click handler, calculate the base once after reading the row:

```javascript
var isAbsencePto = row.dataset.actionType === 'absence_pto';
var base = isAbsencePto
  ? '/api/exceptions/absence-pto/' + encodeURIComponent(row.dataset.requestId)
  : '/api/exceptions/time-off/' + encodeURIComponent(row.dataset.requestId);
```

Use `base + '/approve'` for approval. Handle past-absence responses before the ordinary response branches:

```javascript
if (isAbsencePto && resp && resp.status === 'needs_review') {
  rowStatus(row, resp.warning || 'This needs payroll review.', false);
  setTimeout(function () { window.location.reload(); }, 600);
} else if (isAbsencePto && resp && resp.status === 'pending') {
  failRow(row, resp.warning || resp.error || 'Approval is still pending.');
} else if (resp && resp.ok && resp.approved === false) {
  rowStatus(row, 'Moved forward; refreshing...', false);
  setTimeout(function () { window.location.reload(); }, 600);
} else if (resp && resp.ok) {
  resolveRow(row, resp.message || 'Approved');
} else {
  failRow(row, (resp && (resp.warning || resp.error)) || 'Approval failed.');
}
```

Use `base + (isAbsencePto ? '/deny' : '/refuse')` for denial. Keep the required-reason interaction and send `source: 'inbox'`.

Add the handled branch:

```javascript
if (rowBtn.classList.contains('js-absence-pto-handled')) {
  var noteInput = row.querySelector('.js-absence-pto-note');
  if (noteInput && noteInput.hidden) {
    noteInput.hidden = false;
    noteInput.focus();
    rowStatus(row, 'Add a note, then Mark handled again.', false);
    return;
  }
  var handledNote = noteInput ? noteInput.value.trim() : '';
  if (!handledNote) {
    if (noteInput) noteInput.focus();
    failRow(row, 'A note is required to mark this handled.');
    return;
  }
  setBusy(row, true);
  rowStatus(row, 'Saving...', false);
  postJson(base + '/handled', {note: handledNote, source: 'inbox'})
    .then(function (resp) {
      if (resp && resp.ok) resolveRow(row, resp.message || 'Marked handled');
      else failRow(row, (resp && (resp.warning || resp.error)) || 'Could not mark handled.');
    })
    .catch(function () { failRow(row, 'Network error.'); });
  return;
}
```

Extend the keydown handler:

```javascript
input = event.target.closest('.js-absence-pto-note');
if (submitRowInput(input, '.js-absence-pto-handled')) event.preventDefault();
```

Never remove a row when the response is not successful.

- [ ] **Step 16: Run static UI tests and syntax validation**

Run:

```bash
env DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest tests/test_exception_inbox.py \
  -k "past_absence_pto or inline_time_off or js_requires_time_off" -v
node --check src/zira_dashboard/static/exceptions.js
```

Expected: selected tests PASS and Node exits 0 with no output.

- [ ] **Step 17: Run existing past-absence action regressions**

The Inbox must use, not rewrite, the already-tested server actions:

```bash
env DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest \
  tests/test_absence_pto_admin_routes.py \
  tests/test_absence_pto_conversion.py \
  tests/test_absence_pto_review.py -q
```

Expected: all tests PASS. Any database-gated cases skip with their existing reason.

- [ ] **Step 18: Add the shipped What's New note**

Add a new entry above the existing plan note under `## 2026-09-01` in `CHANGELOG.md`. Do not edit the historical design or plan entries:

```markdown
### Past PTO requests now show in the Inbox

- **Past-absence PTO requests now appear with the other daily work in the Exception Inbox.** Managers can approve or deny a waiting request there. Requests that need payroll help stay visible until a manager adds a note and marks them handled.
```

- [ ] **Step 19: Run focused and full repository validation**

Run:

```bash
env DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest \
  tests/test_exception_inbox.py \
  tests/test_exception_inbox_attendance.py \
  tests/test_inbox_reconcile.py \
  tests/test_absence_pto_admin_routes.py -q

env DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest -q

.venv/bin/ruff check \
  src/zira_dashboard/exception_inbox.py \
  src/zira_dashboard/inbox_reconcile.py \
  src/zira_dashboard/inbox_keys.py \
  tests/test_exception_inbox.py \
  tests/test_exception_inbox_attendance.py \
  tests/test_inbox_reconcile.py

node --check src/zira_dashboard/static/exceptions.js
git diff --check
```

Expected: focused tests PASS; the full suite PASS with only documented skips; Ruff reports `All checks passed!`; Node and `git diff --check` exit 0 with no output.

- [ ] **Step 20: Review the final diff and commit only intentional files**

Run:

```bash
git status --short
git diff -- \
  CHANGELOG.md \
  src/zira_dashboard/exception_inbox.py \
  src/zira_dashboard/inbox_reconcile.py \
  src/zira_dashboard/inbox_keys.py \
  src/zira_dashboard/templates/exceptions.html \
  src/zira_dashboard/static/exceptions.js \
  tests/test_exception_inbox.py \
  tests/test_exception_inbox_attendance.py \
  tests/test_inbox_reconcile.py
```

Confirm the diff does not contain the pre-existing unrelated files named in Global Constraints. Then:

```bash
git add \
  CHANGELOG.md \
  src/zira_dashboard/exception_inbox.py \
  src/zira_dashboard/inbox_reconcile.py \
  src/zira_dashboard/inbox_keys.py \
  src/zira_dashboard/templates/exceptions.html \
  src/zira_dashboard/static/exceptions.js \
  tests/test_exception_inbox.py \
  tests/test_exception_inbox_attendance.py \
  tests/test_inbox_reconcile.py
git commit -m "feat: show past PTO requests in inbox"
git push origin main
```

Expected: one implementation commit lands on `origin/main`; unrelated workspace changes remain unstaged.

- [ ] **Step 21: Verify the deployment and live read-only Inbox shape**

Wait for Railway's `web` deployment for the implementation commit to report `SUCCESS`, then run:

```bash
curl -fsS https://gpiplantmanager.com/healthz

railway ssh -s web python -c "from zira_dashboard import exception_inbox; s=exception_inbox.build_snapshot(); print([{'item_key': r.get('item_key'), 'date': str(r.get('label')), 'action_type': (r.get('action') or {}).get('type'), 'state': (r.get('action') or {}).get('state')} for r in s['queue'] if (r.get('action') or {}).get('type') == 'absence_pto'])"
```

Expected: health returns success, and the deployed read-only snapshot includes the August 31 row with `item_key` `absence_pto:1`, action type `absence_pto`, and state `pending`. Do not invoke any action endpoint during this check.

If authentication prevents browser automation, report that limitation plainly; the TestClient-rendered controls, JavaScript contracts, deployed code snapshot, and health check remain the required evidence.

---

## Completion boundary

The task is complete only when the implementation commit is pushed to `origin/main`, the Railway deployment is healthy, the live read-only Inbox snapshot contains the open past-absence request, and every required validation above passes. A committed or pushed plan alone is not completion, and the manager remains responsible for the actual approval decision.
