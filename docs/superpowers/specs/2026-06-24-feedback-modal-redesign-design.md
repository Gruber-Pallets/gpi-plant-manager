# Feedback modal redesign → Odoo tasks

**Date:** 2026-06-24
**Status:** Approved design, pending implementation plan

## Summary

Replace the existing inline "Send feedback" form (a category dropdown + textarea
that POSTs to a local-only `feedback` table) with a redesigned, green modal that
files every submission as a **task in Odoo assigned to the site owner**. Add a
**"View Feedback"** button so a submitter can see their own submissions and each
one's status (Open / Done / Rejected).

Odoo becomes the system of record for triage. The local `feedback` table is kept
only as a per-user index that links a submitter to the Odoo task they created.

## Goals

- "Send feedback" opens a polished modal styled with the app's green accent.
- The modal captures: **type** (Bug or Feature request), **description**, and
  **attachments** (file picker + paste-a-screenshot directly into the box).
- Submitting auto-creates an Odoo `project.task`:
  - in a single project named **"Plant Manager"** (find-or-created by name),
  - **assigned to the authenticated Odoo user** (the app's `ODOO_LOGIN` user — i.e. the owner),
  - **due today**,
  - tagged `Bug` or `Feature request`,
  - carrying the description, submitter, and originating page URL,
  - with every attachment/screenshot pushed onto the task.
- "View Feedback" shows the signed-in user *their own* submissions with a
  collapsed status of **Open / Done / Rejected**.
- Remove the legacy `/admin/feedback` page and its `Feedback` nav tab in Settings.

## Non-goals

- No per-submitter routing/assignment — every task goes to the owner.
- No editing/closing tasks from the app — triage happens in Odoo.
- No local file storage — attachments live only in Odoo.
- No email/Slack notification beyond the Odoo task itself (out of scope for now).

## Current state (what changes)

| Piece | Today | After |
|---|---|---|
| `_footer.html` | Inline feedback form (select + textarea) inside the What's New modal | New green **Send feedback** modal + **View Feedback** panel; two buttons in the panel header |
| `footer.js` | `toggleFeedback` / `submitFeedback` POST JSON to `/feedback` | Build/teardown the new modal; multipart submit with files; paste-image capture; render "View Feedback" list |
| `footer.css` | `.changelog-feedback*` styles | New modal + segmented toggle + attachment-chip + status-pill styles (green accent) |
| `routes/feedback.py` | `POST /feedback` (JSON → DB) + `GET /admin/feedback` (HTML table) | `POST /feedback` (multipart → Odoo task + DB index); `GET /api/feedback/mine` (this user's items + live status); **`/admin/feedback` removed** |
| `feedback_store.py` | `insert()` / `recent()` | `insert()` (now also stores `task_type`, `odoo_task_id`); `for_submitter()` replaces `recent()` |
| `_schema.py` `feedback` table | `submitter, page_url, category, message` | add `task_type TEXT`, `odoo_task_id BIGINT`; `category` retained (unused, harmless) |
| `odoo_client.py` | reads + hr.attendance/hr.leave writes | add feedback-task helpers (project, task, attachment, stage lookup) |
| `settings.html` | `<a href="/admin/feedback">Feedback</a>` nav tab | **removed** |
| `admin_feedback.html` | admin table template | **deleted** |
| `tests/` | (current feedback tests, if any) | new route + helper + status-bucketing tests with Odoo mocked |

## Architecture

### UI — the modal and buttons (`_footer.html`, `footer.css`, `footer.js`)

The What's New panel header gains two buttons next to Close:
`[Send feedback] [View Feedback] [Close]`.

**Send feedback modal** (own dialog overlay, green accent — see approved mockup):
- Header: "Send feedback" + Close.
- Segmented toggle: `Bug` | `Feature request`. Active segment uses the green
  accent. Default = Bug.
- Description label + textarea. Placeholder swaps with type:
  - Bug → "What broke, and what did you expect?"
  - Feature request → "What would you like to see, and why?"
- Attachments:
  - "Upload files" button → file picker (images + PDF, multiple).
  - Paste handler on the textarea: a pasted image blob is captured and added to
    the attachment list (same list as the picker), shown as a removable thumbnail/chip.
- Footer: Cancel + green **Send feedback** submit button.
- Client-side validation: description required; submit disabled while empty/in-flight.
- Per-file guard: client rejects files over the size cap and non-allowed types
  before upload (server re-checks).

**View Feedback panel**: fetches `/api/feedback/mine` and lists the user's items
newest-first — each row shows type, the task title/first line, submitted date, and
a status pill (Open = neutral/green, Done = green, Rejected = muted/red). Empty
state: "You haven't sent any feedback yet."

### Submit flow — `POST /feedback` (multipart)

1. Parse multipart: `type` (`bug`|`feature`), `description` (required), `page_url`,
   and `files[]`.
2. Resolve the Odoo project: `ensure_feedback_project()` →
   find `project.project` named "Plant Manager", create it (with the four stages)
   if absent. Cache the id in-process.
3. Create the task: `create_feedback_task(...)`:
   - `name` = `[Bug] <first line, truncated>` / `[Feature] <…>`,
   - `project_id`,
   - assignee = authenticated uid (`authenticate()`),
   - `date_deadline` = today (server local date),
   - tag (`Bug` / `Feature request`) via `tag_ids`, tag find-or-created,
   - `description` = HTML body with the full text + "Submitted by <name/upn>" + page URL.
   - Assignee field is version-robust: prefer `user_ids` (Odoo 17+), fall back to
     `user_id` if the create rejects the field.
4. For each file: `add_task_attachment(task_id, filename, mimetype, bytes)` →
   create `ir.attachment` with `res_model='project.task'`, `res_id=task_id`,
   base64 `datas`. Attachment failures are logged but do not fail the submission
   (the task is already created).
5. Insert the local index row (`submitter`, `task_type`, `message`, `page_url`,
   `odoo_task_id`).
6. Return `{ok: true, id, task_id}`.

Error handling: if project resolution or task create fails (Odoo down / config
error), return `{ok: false, error}` with a 502-style status and a user-facing
"Couldn't reach Odoo — try again" message; nothing is written locally so there
are no orphan rows.

### Status read — `GET /api/feedback/mine`

1. `feedback_store.for_submitter(upn)` → this user's rows (incl. `odoo_task_id`).
2. Batch-read the tasks' stages from Odoo: `fetch_task_stage_names([task_ids])`
   → `{task_id: stage_name}`.
3. Collapse each stage name to a bucket:
   - matches the Done stage → `done`
   - matches the Rejected stage → `rejected`
   - anything else (incl. missing/deleted task) → `open`
4. Return rows with `{type, title, created_at, status}`.

Status is read **live** from Odoo on each open of the panel (chosen over a cached
copy: simpler, always accurate, and the page already tolerates Odoo latency
elsewhere; the volume here is tiny). If Odoo is unreachable, items still render
with status `open` and a small "status unavailable" note.

### Odoo helpers (`odoo_client.py`)

All built on the existing generic `execute(model, method, ...)`:
- `ensure_feedback_project() -> int` — find-or-create the "Plant Manager" project
  and its stages `New / In Progress / Done / Rejected`; returns project id.
- `ensure_feedback_tag(name) -> int` — find-or-create a `project.tags` row.
- `create_feedback_task(project_id, name, description_html, assignee_uid, tag_id, deadline) -> int`.
- `add_task_attachment(task_id, filename, mimetype, raw_bytes) -> int`.
- `fetch_task_stage_names(task_ids) -> dict[int, str]`.
- Module-level constants for the project name, stage names, and the
  Done/Rejected stage identifiers used for bucketing.

### Data model (`_schema.py`, `feedback_store.py`)

`feedback` table gains:
- `task_type TEXT` — `bug` | `feature`
- `odoo_task_id BIGINT` — the created Odoo task id

`feedback_store`:
- `insert(message, submitter, page_url, task_type, odoo_task_id)`.
- `for_submitter(submitter, limit=100)` — newest-first rows for one user.
- `recent()` is removed (only used by the deleted admin page).

## Testing

- `POST /feedback`: with `odoo_client` mocked, asserts project resolved, task
  created with correct name/assignee/deadline/tag, attachments uploaded, and the
  local row written with `task_type` + `odoo_task_id`. Missing description → 400.
  Odoo failure → error response and no local row.
- `ensure_feedback_project`: creates when absent, reuses when present (mocked
  search/create call counts).
- Status bucketing: Done/Rejected/other stage names → correct buckets; missing
  task id → `open`.
- `GET /api/feedback/mine`: returns only the calling user's rows; merges live
  stages; tolerates an Odoo read failure.
- These run under the existing `ZIRA_API_KEY=test` pytest setup with Odoo mocked
  (no live Odoo needed), matching how attendance/leave write paths are tested.

## Open implementation details (decide during build, not blocking)

- Exact attachment size cap and accepted MIME list (proposed: 10 MB/file,
  images + PDF).
- Whether to also store `submitter` display name in the task body vs only UPN
  (proposed: both — "Submitted by <name> (<upn>)").
- Truncation length for the task title's first line (proposed: ~70 chars).
