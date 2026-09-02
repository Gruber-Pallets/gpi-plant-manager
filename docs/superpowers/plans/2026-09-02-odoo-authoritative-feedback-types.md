# Odoo-Authoritative Feedback Types Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add all four Odoo-authoritative feedback types to Plant Manager, provide exact local start/finish commands, repair the mirror contract, and safely recover feedback 44 from its pre-attempt quarantine.

**Architecture:** A new small `feedback_types` module owns every local label and Odoo stored-value mapping used by Python code. The shared feedback panel renders the same four choices as the reference experience. Two dry-run-first scripts call the existing local lifecycle without writing Odoo, while a new append-only audit table and guarded CLI operation release only contract-mismatch quarantines that never reached an Odoo attempt.

**Tech Stack:** Python 3.13, FastAPI, Jinja2, vanilla JavaScript/CSS, PostgreSQL, Odoo XML-RPC, pytest, Ruff, Railway.

## Global Constraints

- Odoo's `x_2s_improvements.x_studio_type` selection is authoritative.
- The exact mapping is `bug -> Digital`, `feature -> Digital - New Feature`, `floor_issue -> Physical - Issue`, and `floor_suggestion -> Physical - Suggestion`.
- Plant Manager must display Bug, New Feature, Floor Issue, and Floor Suggestion in that order.
- Repair and Maintenance work-order actions are out of scope.
- Unsupported types must be rejected; never coerce them to Bug.
- Database, company, Source, field, relation, status, and exact Type checks remain fail-closed.
- Recovery may never lower versions, invent an Odoo ID, mark a row synchronized, or clear attempt-backed/ambiguous quarantine.
- Do not edit the Odoo mirror directly; feedback 44 must flow through Plant Manager's normal worker.
- Lifecycle commands match only a positive Feedback ID or exact `GPI-PM-FB-<positive id>` Source ID. Never treat an Odoo Project Task ID as a Plant Manager feedback ID.
- Start local work at `in_progress`, including planning. Finish locally only after implementation, validation, and push. The existing mirror remains the only Odoo writer.
- The superseded cross-repository Cursor spec contributes only this Plant Manager lifecycle work. Sales Manager, OS Manager, and copied-prompt changes are deliberately discarded.
- New `CHANGELOG.md` text must use short, common words a 10-year-old can understand.
- Preserve unrelated working-tree changes.

---

### Task 1: Canonical four-type backend contract

**Files:**
- Create: `src/zira_dashboard/feedback_types.py`
- Create: `tests/test_feedback_types.py`
- Modify: `src/zira_dashboard/routes/feedback.py`
- Modify: `src/zira_dashboard/feedback_store.py`
- Modify: `src/zira_dashboard/feedback_projection.py`
- Modify: `src/zira_dashboard/feedback_task_worker.py`
- Modify: `src/zira_dashboard/odoo_improvements.py`
- Modify: `tests/test_feedback_routes.py`
- Modify: `tests/test_feedback_store.py`
- Modify: `tests/test_feedback_projection.py`
- Modify: `tests/test_feedback_task_worker.py`
- Modify: `tests/test_odoo_improvements.py`

**Interfaces:**
- Produces: immutable `FeedbackType`, ordered `FEEDBACK_TYPES`, `feedback_type(value) -> FeedbackType`, and `feedback_type_or_legacy_bug(value) -> FeedbackType`.
- Produces: exact Odoo Type contract `{Digital, Digital - New Feature, Physical - Issue, Physical - Suggestion}`.
- Consumes: existing local `task_type` strings and legacy `None` rows.

- [ ] **Step 1: Write failing canonical-type tests**

Create `tests/test_feedback_types.py`:

```python
import pytest

from zira_dashboard.feedback_types import FEEDBACK_TYPES, feedback_type


def test_feedback_types_match_odoo_reference_order_and_values():
    assert [item.value for item in FEEDBACK_TYPES] == [
        "bug", "feature", "floor_issue", "floor_suggestion"
    ]
    assert [item.label for item in FEEDBACK_TYPES] == [
        "Bug", "New Feature", "Floor Issue", "Floor Suggestion"
    ]
    assert [item.odoo_value for item in FEEDBACK_TYPES] == [
        "Digital",
        "Digital - New Feature",
        "Physical - Issue",
        "Physical - Suggestion",
    ]


@pytest.mark.parametrize("value", [None, "", "other", True])
def test_feedback_type_rejects_unknown_values(value):
    with pytest.raises(ValueError, match="unsupported feedback type"):
        feedback_type(value)
```

Extend the existing route, store, projection, task-worker, and Odoo contract tests to assert:

```python
@pytest.mark.parametrize(
    ("local_value", "odoo_value"),
    [
        ("bug", "Digital"),
        ("feature", "Digital - New Feature"),
        ("floor_issue", "Physical - Issue"),
        ("floor_suggestion", "Physical - Suggestion"),
    ],
)
def test_projection_uses_authoritative_odoo_type(local_value, odoo_value):
    projected = build_projection(feedback(task_type=local_value), [], employee_lookup=lambda _e: [])
    assert projected.fields["x_studio_type"] == odoo_value
```

Add route cases proving both physical values reach `create_submission`, and `type=other` returns HTTP 400 without opening storage. Add task-name cases expecting `[Floor Issue]` and `[Floor Suggestion]`. Change contract fixtures to the exact four stored values and prove a missing or extra value fails.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_feedback_types.py tests/test_feedback_routes.py tests/test_feedback_store.py tests/test_feedback_projection.py tests/test_feedback_task_worker.py tests/test_odoo_improvements.py
```

Expected: failures because `feedback_types` does not exist, physical values are rejected/coerced, and the old Odoo Type contract expects `Physical`.

- [ ] **Step 3: Implement the minimal canonical registry**

Create `src/zira_dashboard/feedback_types.py` with this public shape:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class FeedbackType:
    value: str
    label: str
    description: str
    odoo_value: str


FEEDBACK_TYPES = (
    FeedbackType("bug", "Bug", "Something in this app is broken", "Digital"),
    FeedbackType("feature", "New Feature", "An idea to make this app better", "Digital - New Feature"),
    FeedbackType("floor_issue", "Floor Issue", "Something wrong out on the floor", "Physical - Issue"),
    FeedbackType("floor_suggestion", "Floor Suggestion", "An idea for the team to consider", "Physical - Suggestion"),
)
_BY_VALUE = {item.value: item for item in FEEDBACK_TYPES}


def feedback_type(value: object) -> FeedbackType:
    if type(value) is not str or value not in _BY_VALUE:
        raise ValueError("unsupported feedback type")
    return _BY_VALUE[value]


def feedback_type_or_legacy_bug(value: object) -> FeedbackType:
    if value is None:
        return _BY_VALUE["bug"]
    return feedback_type(value)
```

Use `feedback_type()` in new-submission route/store validation and task naming. Return `{"ok": False, "error": "Unsupported feedback type."}` with HTTP 400 for invalid route input. Use `feedback_type_or_legacy_bug()` only when reading pre-existing nullable rows. Build `TYPE_VALUES` and the exact Odoo contract from `FEEDBACK_TYPES`, so no Python consumer maintains a second mapping.

- [ ] **Step 4: Run focused tests and Ruff; verify GREEN**

Run:

```bash
.venv/bin/pytest -q tests/test_feedback_types.py tests/test_feedback_routes.py tests/test_feedback_store.py tests/test_feedback_projection.py tests/test_feedback_task_worker.py tests/test_odoo_improvements.py
.venv/bin/ruff check src/zira_dashboard/feedback_types.py src/zira_dashboard/routes/feedback.py src/zira_dashboard/feedback_store.py src/zira_dashboard/feedback_projection.py src/zira_dashboard/feedback_task_worker.py src/zira_dashboard/odoo_improvements.py tests/test_feedback_types.py tests/test_feedback_routes.py tests/test_feedback_store.py tests/test_feedback_projection.py tests/test_feedback_task_worker.py tests/test_odoo_improvements.py
```

Expected: all selected tests pass and Ruff reports no errors.

- [ ] **Step 5: Add the plain-language patch note and commit**

Add under the newest `2026-09-02` CHANGELOG entry:

```markdown
### Keep feedback choices matched with Odoo

- **Plant Manager now understands all four feedback choices used in Odoo.** Bug, New Feature, Floor Issue, and Floor Suggestion keep the same meaning in both places.
```

Commit:

```bash
git add CHANGELOG.md src/zira_dashboard/feedback_types.py src/zira_dashboard/routes/feedback.py src/zira_dashboard/feedback_store.py src/zira_dashboard/feedback_projection.py src/zira_dashboard/feedback_task_worker.py src/zira_dashboard/odoo_improvements.py tests/test_feedback_types.py tests/test_feedback_routes.py tests/test_feedback_store.py tests/test_feedback_projection.py tests/test_feedback_task_worker.py tests/test_odoo_improvements.py
git commit -m "feat: align feedback types with Odoo"
git push origin main
```

---

### Task 2: Four-choice feedback panel and labels

**Files:**
- Modify: `src/zira_dashboard/templates/_feedback.html`
- Modify: `src/zira_dashboard/static/feedback.js`
- Modify: `src/zira_dashboard/static/feedback.css`
- Modify: `src/zira_dashboard/templates/admin_feedback.html`
- Modify: `src/zira_dashboard/feedback_store.py`
- Modify: `src/zira_dashboard/routes/feedback.py`
- Modify: `tests/test_whatsnew_panel_static.py`
- Modify: `tests/test_feedback_routes.py`
- Modify: `tests/test_feedback_admin_routes.py`

**Interfaces:**
- Consumes: Task 1 `feedback_type()` and `feedback_type_or_legacy_bug()`.
- Produces: four-card Step 1 UI, existing description/picture Step 2 UI, and server-provided `type_label` for My Requests/admin.

- [ ] **Step 1: Write failing UI and presentation tests**

Extend `tests/test_whatsnew_panel_static.py` to require the four cards and two steps:

```python
def test_feedback_panel_has_four_reference_types_and_two_steps():
    html = FEEDBACK_TEMPLATE.read_text(encoding="utf-8")
    for value in ("bug", "feature", "floor_issue", "floor_suggestion"):
        assert f'data-type="{value}"' in html
    for label in ("Bug", "New Feature", "Floor Issue", "Floor Suggestion"):
        assert label in html
    assert 'id="fb-type-step"' in html
    assert 'id="fb-detail-step"' in html
    assert 'id="fb-back"' in html
```

Require JS to preserve the exact four-value list, advance after a type card click, return with Back, restore Step 1 on reset, and display `it.type_label` rather than a two-value ternary. Require CSS card, icon, subtitle, selected/focus, and narrow-screen rules. Add route/admin tests asserting `type_label` for all four values and legacy `None` as Bug.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_whatsnew_panel_static.py tests/test_feedback_routes.py tests/test_feedback_admin_routes.py
```

Expected: failures because only two compact buttons exist and responses/templates do not expose the four labels.

- [ ] **Step 3: Implement the four-card two-step flow**

In `_feedback.html`, render Step 1 as four accessible buttons with icon, title, subtitle, and arrow. Render the current description, screenshot, status, and submission controls inside hidden Step 2, with a Back button. Keep all existing IDs used by screenshot/submission behavior.

In `feedback.js`, define only the browser presentation data needed for placeholders:

```javascript
var ALLOWED_TYPES = ['bug', 'feature', 'floor_issue', 'floor_suggestion'];
var PLACEHOLDERS = {
  bug: 'What broke, and what did you expect?',
  feature: 'What would you like to see, and why?',
  floor_issue: 'What is wrong out on the floor?',
  floor_suggestion: 'What should the team improve out on the floor?'
};
```

Make `setType` reject values outside `ALLOWED_TYPES`, select the card, show Step 2, and focus the description. Make Back show Step 1 and focus the chosen card. Make reset select Bug internally but show Step 1. Use `it.type_label || 'Unknown'` for My Requests; the server owns labels.

In `feedback_store.for_admin()` attach `type_label` from the canonical registry before returning rows. In `/api/feedback/mine`, return `type_label` from the same registry. In `admin_feedback.html`, render `item.type_label` directly.

- [ ] **Step 4: Run focused tests and Ruff; verify GREEN**

Run:

```bash
.venv/bin/pytest -q tests/test_whatsnew_panel_static.py tests/test_feedback_routes.py tests/test_feedback_admin_routes.py
.venv/bin/ruff check src/zira_dashboard/feedback_store.py src/zira_dashboard/routes/feedback.py tests/test_feedback_routes.py tests/test_feedback_admin_routes.py tests/test_whatsnew_panel_static.py
```

Expected: all selected tests pass and Ruff reports no errors.

- [ ] **Step 5: Add the plain-language patch note and commit**

Add:

```markdown
### Pick the right kind of feedback

- **The feedback box now shows four clear choices before you write.** You can report an app bug, ask for a new feature, report a floor issue, or share a floor suggestion.
```

Commit:

```bash
git add CHANGELOG.md src/zira_dashboard/templates/_feedback.html src/zira_dashboard/static/feedback.js src/zira_dashboard/static/feedback.css src/zira_dashboard/templates/admin_feedback.html src/zira_dashboard/feedback_store.py src/zira_dashboard/routes/feedback.py tests/test_whatsnew_panel_static.py tests/test_feedback_routes.py tests/test_feedback_admin_routes.py
git commit -m "feat: add four feedback choices"
git push origin main
```

---

### Task 3: Exact local feedback start and finish commands

**Files:**
- Create: `scripts/feedback_lifecycle.py`
- Create: `scripts/process_feedback.py`
- Create: `scripts/resolve_feedback.py`
- Create: `tests/test_feedback_lifecycle_scripts.py`
- Modify: `src/zira_dashboard/feedback_store.py`
- Modify: `AGENTS.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: `feedback_store.lifecycle_state(feedback_id: int) -> Mapping[str, object]` for an exact, read-only local authority check.
- Produces: shared CLI identifier parsing and privacy-safe result formatting in `scripts.feedback_lifecycle`.
- Produces: `python -m scripts.process_feedback (--feedback-id ID | --source-id GPI-PM-FB-ID) [--yes]`.
- Produces: `python -m scripts.resolve_feedback (--feedback-id ID | --source-id GPI-PM-FB-ID) --note NOTE [--by EMAIL] [--yes]`.
- Consumes: existing `feedback_store.transition(feedback_id=..., status=..., actor=..., resolution_note=..., after_image=None, now=...)` and local mirror enqueue behavior.

- [ ] **Step 1: Write failing lifecycle helper and CLI tests**

Create `tests/test_feedback_lifecycle_scripts.py` with isolated parser/store tests that require:

- exactly one of `--feedback-id` or `--source-id`;
- positive signed-64-bit Feedback IDs and canonical `GPI-PM-FB-<positive id>` Source IDs;
- rejection of Task IDs, loose strings, leading-zero aliases, and multiple identifiers;
- read-only preview by default, with JSON reporting the local ID, current status, proposed status, and `applied: false`;
- `--yes` as the only mutation gate;
- start behavior: `requested -> in_progress`, already `in_progress` is a successful no-op, terminal rows stay unchanged;
- finish behavior: only `in_progress -> completed`, a nonblank note is required, `--by` defaults to `dale@gruberpallets.com`, and requested/terminal rows stay unchanged;
- local lifecycle authority only: rows whose `lifecycle_origin` is not `local` fail closed;
- the commands call `feedback_store.transition()` and do not instantiate or import an Odoo client.

Extend `tests/test_feedback_store.py` to prove `lifecycle_state()` returns one exact local row and rejects missing, nonpositive, or nonlocal rows without changing sync state.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_feedback_lifecycle_scripts.py tests/test_feedback_store.py
```

Expected: failures because the shared CLI helper, entrypoints, and read-only lifecycle query do not exist.

- [ ] **Step 3: Implement the minimal exact lifecycle workflow**

Add `feedback_store.lifecycle_state()` as a read-only exact-ID query returning only the fields needed for the decision: `id`, `status`, `lifecycle_origin`, and `projection_version`. Validate positive IDs and require `lifecycle_origin == "local"`.

In `scripts/feedback_lifecycle.py`, centralize:

- an argparse mutually exclusive required identifier group;
- strict Source ID parsing to one positive signed-64-bit integer;
- database pool initialization and cleanup;
- read-only current-state inspection;
- privacy-safe JSON output; and
- the `--yes` decision before calling `feedback_store.transition()`.

Keep `scripts/process_feedback.py` and `scripts/resolve_feedback.py` as thin entrypoints. Start passes actor `dale@gruberpallets.com`, `resolution_note=None`, `after_image=None`, and the current UTC time; the existing store intentionally saves no finished-by data for a nonterminal transition. Finish passes the required operator note, real closer email, `after_image=None`, and current UTC time. Do not add a generic Odoo client or direct Odoo write.

For terminal or disallowed transitions, print a safe unchanged result and exit nonzero. Treat an already-in-progress start as a successful unchanged result so repeated task startup is safe. Never move `requested` directly to `completed` through the finish command.

- [ ] **Step 4: Update repository lifecycle instructions**

Amend `AGENTS.md` so Plant Manager-owned feedback is moved to local In Progress as soon as work begins, including planning, using the exact start command. Preserve the current completion gate and require the exact finish command only after implementation, validation, and push. State explicitly that:

- `GPI-PM-FB-<id>` or Feedback ID is authoritative, never Task ID;
- start/finish commands default to dry-run and require `--yes`;
- no direct Odoo mirror edit is allowed;
- the normal worker must synchronize and Odoo must be read back before completion is claimed; and
- an unavailable, ambiguous, terminal, or failed lifecycle remains unchanged and is reported to Dale.

- [ ] **Step 5: Run focused tests, Ruff, and instruction checks; verify GREEN**

Run:

```bash
.venv/bin/pytest -q tests/test_feedback_lifecycle_scripts.py tests/test_feedback_store.py
.venv/bin/ruff check scripts/feedback_lifecycle.py scripts/process_feedback.py scripts/resolve_feedback.py src/zira_dashboard/feedback_store.py tests/test_feedback_lifecycle_scripts.py tests/test_feedback_store.py
rg -n "process_feedback|resolve_feedback|GPI-PM-FB|Task ID|origin/main|mirror" AGENTS.md
git diff --check
```

Expected: all selected tests pass, Ruff reports no errors, the instruction check shows every lifecycle guard, and the diff check is clean.

- [ ] **Step 6: Add the plain-language patch note and commit**

Add:

```markdown
### Keep feedback progress up to date

- **Plant Manager now has a safe way to show when feedback work starts and ends.** It checks the exact feedback item and lets the normal shared-list update do its job.
```

Commit:

```bash
git add AGENTS.md CHANGELOG.md scripts/feedback_lifecycle.py scripts/process_feedback.py scripts/resolve_feedback.py src/zira_dashboard/feedback_store.py tests/test_feedback_lifecycle_scripts.py tests/test_feedback_store.py
git commit -m "feat: add local feedback lifecycle commands"
git push origin main
```

---

### Task 4: Audited recovery for pre-attempt contract quarantine

**Files:**
- Modify: `src/zira_dashboard/_schema.py`
- Modify: `src/zira_dashboard/feedback_sync_store.py`
- Modify: `scripts/feedback_odoo_rollout.py`
- Modify: `docs/odoo-2s-feedback-operations.md`
- Modify: `tests/test_feedback_schema.py`
- Modify: `tests/test_feedback_sync_store.py`
- Modify: `tests/test_feedback_rollout.py`
- Modify: `tests/test_feedback_odoo_safety_contract.py`

**Interfaces:**
- Produces: `PreAttemptReleaseResult(feedback_id, desired_version, state)`.
- Produces: `release_pre_attempt_quarantine(feedback_id: int, reviewer: str, now: datetime) -> PreAttemptReleaseResult`.
- Produces: CLI `quarantine-release-pre-attempt --feedback-id ID --reviewer NAME --confirm-read-only --confirm-local-release`.
- Consumes: a fresh successful `feedback_rollout.preflight(ImprovementsClient)` before local release.

- [ ] **Step 1: Write failing schema/store tests**

Require `_schema.py` to create an append-only audit table with this contract:

```sql
CREATE TABLE IF NOT EXISTS feedback_odoo_pre_attempt_releases (
  id BIGSERIAL PRIMARY KEY,
  feedback_id BIGINT NOT NULL REFERENCES feedback(id),
  projection_version BIGINT NOT NULL CHECK (projection_version > 0),
  quarantine_reason TEXT NOT NULL CHECK (
    quarantine_reason = 'target_identity_or_contract_mismatch'
  ),
  quarantined_at TIMESTAMPTZ NOT NULL,
  reviewer TEXT NOT NULL CHECK (btrim(reviewer) <> ''),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (feedback_id, projection_version, quarantined_at)
);
```

Add recording-cursor tests for the exact eligible row and for each refusal: wrong reason, active attempt, saved Odoo ID, nonzero attempt count, synchronized version, mismatched feedback projection, invalid lifecycle, blank reviewer, and repeat release. The happy-path test must assert the transaction inserts audit evidence first and updates only the exact locked quarantine authority to `idle`, with `due_at=now()` and no version/association change.

- [ ] **Step 2: Run schema/store tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_feedback_schema.py tests/test_feedback_sync_store.py
```

Expected: failures because the audit table, result type, and guarded release function do not exist.

- [ ] **Step 3: Implement the minimal guarded local release**

Add the table exactly as tested. Add immutable `PreAttemptReleaseResult` validation. Implement `release_pre_attempt_quarantine()` as one transaction that:

1. locks `feedback_odoo_sync` joined to `feedback` by the positive feedback ID;
2. validates the exact eligible state and authoritative projection/lifecycle;
3. inserts the prior reason/time and reviewer into the audit table;
4. updates the same locked row to `idle`, clears only claim/quarantine/error fields, keeps `desired_version`, `last_synced_version`, and null associations unchanged, and makes it due immediately;
5. validates the returned row before commit.

No Odoo/network calls belong in `feedback_sync_store.py`.

- [ ] **Step 4: Run schema/store tests and verify GREEN**

Run:

```bash
.venv/bin/pytest -q tests/test_feedback_schema.py tests/test_feedback_sync_store.py
.venv/bin/ruff check src/zira_dashboard/_schema.py src/zira_dashboard/feedback_sync_store.py tests/test_feedback_schema.py tests/test_feedback_sync_store.py
```

Expected: all selected tests pass and Ruff reports no errors.

- [ ] **Step 5: Write failing CLI safety tests**

Add parser/payload tests proving the command refuses to run without both confirmation flags, does not release when preflight reports any false/mismatch diagnostic, and calls the store once only after a fully green preflight:

```python
args = parser.parse_args([
    "quarantine-release-pre-attempt",
    "--feedback-id", "44",
    "--reviewer", "Dale Gruber",
    "--confirm-read-only",
    "--confirm-local-release",
])
```

Require the JSON serializer to allow only `PreAttemptReleaseResult`; ensure errors still collapse to `feedback rollout command failed safely` without echoing reviewer, metadata, or credentials.

- [ ] **Step 6: Run CLI tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_feedback_rollout.py tests/test_feedback_odoo_safety_contract.py
```

Expected: failures because the new command and report allowlist do not exist.

- [ ] **Step 7: Implement the CLI gate and operations documentation**

Add the parser command and require both flags. In `_command_payload`, construct the dedicated client, run `rollout.preflight(client)`, require database/company/fields/Source success, and only then call `release_pre_attempt_quarantine()`. Never call an Odoo mutation from this command.

Add a runbook section stating that each production preflight and local release needs Dale's separate approval and giving only this placeholder-safe command:

```bash
.venv/bin/python -m scripts.feedback_odoo_rollout quarantine-release-pre-attempt --feedback-id "$APPROVED_FEEDBACK_ID" --reviewer "$REVIEWER_NAME" --confirm-read-only --confirm-local-release
```

State that the normal worker performs the later Odoo mutation and that a repeated/wrong-state release fails safely.

- [ ] **Step 8: Run CLI tests, Ruff, and diff checks; verify GREEN**

Run:

```bash
.venv/bin/pytest -q tests/test_feedback_schema.py tests/test_feedback_sync_store.py tests/test_feedback_rollout.py tests/test_feedback_odoo_safety_contract.py
.venv/bin/ruff check src/zira_dashboard/feedback_sync_store.py scripts/feedback_odoo_rollout.py tests/test_feedback_sync_store.py tests/test_feedback_rollout.py tests/test_feedback_odoo_safety_contract.py
git diff --check
```

Expected: all selected tests pass, Ruff reports no errors, and the diff check is clean.

- [ ] **Step 9: Add the plain-language patch note and commit**

Add:

```markdown
### Safely retry feedback that stopped early

- **Plant Manager can now safely retry feedback that stopped before it reached Odoo.** It checks the shared list first and keeps a note of who approved the retry.
```

Commit:

```bash
git add CHANGELOG.md src/zira_dashboard/_schema.py src/zira_dashboard/feedback_sync_store.py scripts/feedback_odoo_rollout.py docs/odoo-2s-feedback-operations.md tests/test_feedback_schema.py tests/test_feedback_sync_store.py tests/test_feedback_rollout.py tests/test_feedback_odoo_safety_contract.py
git commit -m "feat: recover pre-attempt feedback quarantine"
git push origin main
```

---

### Task 5: Full verification, deploy, and feedback 44 recovery

**Files:**
- Verify only; no source edits unless a failing test exposes a scoped defect.

**Interfaces:**
- Consumes: Tasks 1-4 and Railway production deployment.
- Produces: green local verification, green production preflight, one verified Odoo mirror row for `GPI-PM-FB-44`, and completed task handoff.

- [ ] **Step 1: Run the complete affected regression suite**

Run:

```bash
.venv/bin/pytest -q tests/test_feedback_schema.py tests/test_feedback_image.py tests/test_feedback_routes.py tests/test_feedback_mine_route.py tests/test_feedback_store.py tests/test_feedback_admin_routes.py tests/test_feedback_lifecycle_scripts.py tests/test_odoo_improvements.py tests/test_feedback_projection.py tests/test_feedback_sync_store.py tests/test_feedback_sync.py tests/test_feedback_rollout.py tests/test_feedback_warmer.py tests/test_feedback_odoo_safety_contract.py tests/test_feedback_task_delivery.py tests/test_feedback_task_worker.py tests/test_feedback_odoo.py tests/test_whatsnew_panel_static.py tests/test_timeclock_feedback_static.py
.venv/bin/ruff check src/zira_dashboard/feedback_types.py src/zira_dashboard/routes/feedback.py src/zira_dashboard/feedback_store.py src/zira_dashboard/feedback_projection.py src/zira_dashboard/feedback_task_worker.py src/zira_dashboard/odoo_improvements.py src/zira_dashboard/feedback_sync_store.py scripts/feedback_lifecycle.py scripts/process_feedback.py scripts/resolve_feedback.py scripts/feedback_odoo_rollout.py
rg -n "process_feedback|resolve_feedback|GPI-PM-FB|Task ID|origin/main|mirror" AGENTS.md
git diff --check
```

Expected: all tests pass, Ruff reports no errors, the repository rules retain
every exact-ID and lifecycle guard, and the diff check is clean.

- [ ] **Step 2: Verify repository and deployment state**

Run:

```bash
git status --short
git rev-parse HEAD
git rev-parse origin/main
railway status
```

Expected: HEAD equals `origin/main`, only the user's pre-existing unrelated files remain dirty, and Railway reports the `web` service Online on the new deployment.

- [ ] **Step 3: Run the separately approved production preflight**

After Dale approves this exact live read, run:

```bash
railway ssh -s web python -m scripts.feedback_odoo_rollout preflight --confirm-read-only
```

Expected: database/company match, fields are OK, Source is present, and no missing/wrong Type selections remain.

- [ ] **Step 4: Run the separately approved local release for feedback 44**

After Dale approves this exact production-local mutation, run:

```bash
railway ssh -s web python -m scripts.feedback_odoo_rollout quarantine-release-pre-attempt --feedback-id 44 --reviewer "Dale Gruber" --confirm-read-only --confirm-local-release
```

Expected: feedback 44 remains desired version 2, changes from `quarantined` to `idle`, and is not marked synchronized by the command.

- [ ] **Step 5: Wait for the normal worker and verify local synchronization**

Poll the privacy-safe commands without changing state:

```bash
railway ssh -s web python -m scripts.feedback_odoo_rollout reconcile
railway ssh -s web python -m scripts.feedback_odoo_rollout quarantine-list
```

Expected: feedback 44 is absent from quarantine, version lag returns to its prior baseline, and the Plant Manager admin page shows `shared 2`.

- [ ] **Step 6: Verify the authoritative Odoo row through the signed-in UI**

Filter 2s Improvements by exact Source ID `GPI-PM-FB-44`, then require exactly one row with:

```text
Source: GPI Plant Manager
Source ID: GPI-PM-FB-44
Type: Digital - New Feature
Status: Completed
Completion Notes: Printed schedules and Slack schedule PDFs now show full-day absences near the top, while partial-day times stay beside each person's name.
```

Do not create or edit the row manually. If zero/multiple rows or any readback mismatch occurs, leave the task active and report the blocker.

- [ ] **Step 7: Final handoff**

Report commits, test counts, Railway deployment state, Plant Manager `shared 2` readback, and exact Odoo Completed readback. Do not mark complete until every preceding check passes.
