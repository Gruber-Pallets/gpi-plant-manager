# Odoo 2s Feedback Mirror Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Plant Manager feedback local-first, add a local admin lifecycle, and mirror each durable version safely into the shared Odoo `x_2s_improvements` table without affecting other applications or existing Odoo workflows.

**Architecture:** The feedback domain and normalized images live in Postgres and enqueue versioned synchronization intent in the same short transaction as each user action. A dedicated, allowlisted Odoo client and sequential durable worker use namespaced compound identity, immutable manifests, exact readback, and fail-closed quarantine; existing `odoo_client` behavior remains untouched.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, vanilla JavaScript, PostgreSQL/psycopg2, Odoo XML-RPC, Pillow, pytest, Ruff.

## Global Constraints

- Canonical repository is `gpi-plant-manager`; stop if the working repository differs.
- Local feedback is authoritative. Odoo is a one-way reporting mirror and can never fail or roll back a local submission or lifecycle action.
- Odoo Source stored value is exactly `GPI Plant Manager`.
- Source IDs are exactly `GPI-PM-FB-<positive local feedback id>`.
- Every lookup uses both Source and Source ID with `limit=3`; duplicate exact rows fail closed.
- Never create or modify Odoo Studio fields or selection metadata. If Source is missing, report exact stored value `GPI Plant Manager` for Dale to add manually.
- Never delete or archive an Odoo improvement and never emit `Physical`.
- Do not place a local claim token in any Odoo payload, manifest field map, report, or remote field.
- Optional absent values are omitted, never sent as false or null.
- Reporter and completer relations use one exact normalized `hr.employee.work_email` match; zero or multiple matches omit the relation and create a safe local warning.
- Both `ODOO_SHARED_REPORTING_WRITE_ENABLED` and `ODOO_IMPROVEMENTS_WRITE_ENABLED` must equal exact lowercase `true`; do not trim or case-fold them.
- `ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID`, when set, limits claims and mutations to that one positive local ID; invalid values fail closed.
- Ship all code with the improvements gate absent/off. Do not change production credentials, gates, Odoo fields, or Odoo data during implementation.
- Persist an immutable exact manifest before dispatch. Never hold a database transaction or row lock open across an Odoo request.
- Verify every written field with a fresh read before settlement. Request full binary values and compare decoded byte length plus SHA-256.
- Transport timeouts, connection loss, malformed mutation responses, and shutdown after dispatch are ambiguous and quarantine without automatic mutation retry.
- Matching Odoo values alone never clear an ambiguous attempt. Later readback settlement requires locally persisted proof that the mutation RPC returned successfully.
- Definitive failures retry after 1, 2, 4, 8, 16, 32, and 60 minutes, with at most eight mutation attempts.
- Process at most 10 due records sequentially per 60-second worker tick.
- Normalize JPEG/PNG/WebP uploads: 10 MiB input, 8,192 pixels per side, 25 million pixels total, 2,048-pixel output long side, JPEG quality 85, and 5 MiB normalized output.
- Preserve the three existing untracked user files (`.cursorignore`, `.python-version`, `uv.lock`) unless the user separately places them in scope.
- If subagents are chosen for execution, every subagent must inherit the main session model; never use a smaller model.
- Follow TDD for every behavior: add one focused failing test, observe the expected failure, implement the minimum, and rerun the focused and relevant regression suites.
- Every task must leave `main` independently deployable, commit only scoped files, add the exact plain-language What's New note assigned below, and push the commit to `origin/main`.
- No rollout command that reads production Odoo may be executed without Dale's explicit approval. No production Odoo write, gate change, credential change, Studio change, or historical backfill may be performed without its separately required approval.

## Approved Pre-Flight Resolutions

- `Projection.fields` contains nonbinary values only. Base64 image values exist only in the fresh dictionary returned by `Projection.dispatch_fields()` immediately before an RPC.
- Employee-resolution warnings are persisted against the exact immutable projection version being processed, not a later version read from the feedback row.
- Task 11 may add the narrow quarantine-disposition persistence functions to `feedback_sync_store.py`; operator actions belong in the sync store rather than the CLI.
- If a write gate closes after dispatch is marked but before the allowlisted wrapper calls Odoo, record a definitive failure and report `retry_scheduled`; do not classify that state as deferred or ambiguous.
- Task 1 review governs three plan-provided SQL defects: a local-origin row must have a non-null valid status; an active attempt must belong to the same feedback record as its sync row; and idempotence guards must scope constraint-name checks to the intended table.
- `tests/test_feedback_sync_store.py` begins in Task 7, where it receives meaningful state-machine tests. Task 1 does not create a meaningless empty test file.
- Task 4 review governs lifecycle/sync integrity: the locked row must already have `lifecycle_origin = 'local'`; a missing sync row fails and rolls back the entire transition; and advancing desired truth preserves both `in_flight` and `quarantined` states so no overlapping claim can start.
- Task 5 review governs the dedicated client boundary: normalize the accepted URL scheme before transport selection; direct `_execute` calls cannot perform target mutations; create/write wrappers validate their complete payload, require the expected immutable contract, freshly verify target identity and contract, recheck both gates immediately before the executor, and then use an internal mutation authorization that callers cannot omit. Every create contains exact `GPI Plant Manager` Source and the canonical Source ID for its feedback ID. Contract metadata requires both employee fields to have relation exactly `hr.employee`. Sanitized exceptions contain no caller- or remote-controlled values and retain no secret-bearing cause or context. Remote IDs are exact positive non-boolean integers, company many2one values have exact shape, employee results have unique exact IDs and exact normalized emails, wildcard email inputs are rejected, and oversized numeric text fails closed with a domain exception.

## File and Responsibility Map

**Create:**

- `src/zira_dashboard/feedback_image.py` — decode, normalize, hash, and describe one screenshot.
- `src/zira_dashboard/routes/feedback_admin.py` — super-admin feedback list and lifecycle mutation endpoints.
- `src/zira_dashboard/templates/admin_feedback.html` — local feedback triage UI.
- `src/zira_dashboard/static/admin_feedback.css` — admin triage styles.
- `src/zira_dashboard/odoo_improvements.py` — dedicated credentials, gate checks, allowlist, identity/contract reads, compound lookups, create/write/read.
- `src/zira_dashboard/feedback_projection.py` — exact field mapping, employee resolution, HTML escaping, canonical manifest, readback comparison.
- `src/zira_dashboard/feedback_sync_store.py` — claims, immutable attempts, version settlement, retry, and quarantine persistence.
- `src/zira_dashboard/feedback_sync.py` — sequential lookup/adopt/mutate/verify orchestration.
- `src/zira_dashboard/feedback_rollout.py` — read-only preflight/dry-run plus bounded local migration/enqueue/reconciliation operations.
- `scripts/feedback_odoo_rollout.py` — explicit CLI entry point for rollout operations.
- `docs/odoo-2s-feedback-operations.md` — dark deploy, approvals, canary, backfill, reconciliation, and rollback runbook.
- `tests/test_feedback_image.py`
- `tests/test_feedback_admin_routes.py`
- `tests/test_odoo_improvements.py`
- `tests/test_feedback_projection.py`
- `tests/test_feedback_sync_store.py`
- `tests/test_feedback_sync.py`
- `tests/test_feedback_rollout.py`
- `tests/test_feedback_warmer.py`

**Modify:**

- `pyproject.toml` — add Pillow runtime dependency.
- `src/zira_dashboard/_schema.py:1448` — add lifecycle fields and durable image/sync/attempt/backfill tables plus immutable-manifest trigger.
- `src/zira_dashboard/feedback_store.py` — atomic local submission, local lifecycle transitions, image persistence, local status reads, and admin list queries.
- `src/zira_dashboard/routes/feedback.py` — remove remote submission dependency; normalize one before image; preserve legacy status fallback only for unmigrated rows.
- `src/zira_dashboard/templates/_feedback.html` — one optional screenshot and four local status labels.
- `src/zira_dashboard/static/feedback.js` — single screenshot state and Requested/In Progress/Completed/Declined rendering.
- `src/zira_dashboard/app.py` — register admin feedback router and 60-second sync warmer.
- `.env.example` — document names only for dedicated Odoo configuration and closed gates.
- `README.md` — link the operations runbook.
- `CHANGELOG.md` — plain-language user-facing notes, never claiming live Odoo rollout before approval.
- Existing feedback/schema/static tests — update current contracts without weakening unrelated assertions.

## Per-Commit Plain-Language Patch Notes

Every push in this plan must include `CHANGELOG.md`. Tasks 3, 4, and 11 contain
their own exact user-facing sections. For the other tasks, add the following
exact bullet under a current-date `### Safer shared feedback build` section with
`#### Improvements`:

- Task 1: **Feedback now has a safe place to keep its progress.** This storage is ready before anything can be shared with Odoo.
- Task 2: **Feedback pictures are cleaned and resized before they are saved.** This removes hidden picture details and avoids very large files.
- Task 5: **The new Odoo connection has its own locked door.** It cannot change other kinds of Odoo records, and it is still turned off.
- Task 6: **Plant Manager now knows exactly how feedback will fit the shared improvements list.** Each app gets its own name and number so records do not mix.
- Task 7: **Feedback sharing work can safely wait and continue after a restart.** The app remembers each step without putting private pictures in its work notes.
- Task 8: **Shared feedback must be read back and checked after every change.** Unclear results stop for review instead of being sent twice.
- Task 9: **The feedback-sharing helper is installed but starts turned off.** Closed safety switches stop it before it can claim work or call Odoo.
- Task 10: **Old feedback can be checked in small groups before it is shared.** Missing details stay blank instead of being guessed.
- Task 12: **Automatic safety checks now guard the feedback connection.** They prove the connection stays off and separate from other Odoo work until rollout approval.

---

### Task 1: Durable Feedback Lifecycle and Synchronization Schema

**Files:**

- Modify: `src/zira_dashboard/_schema.py:1448`
- Modify: `tests/test_feedback_schema.py`

**Interfaces:**

- Consumes: existing idempotent `SCHEMA_DDL` bootstrapped by `db.bootstrap_schema()`.
- Produces: lifecycle columns and the `feedback_images`, `feedback_odoo_sync`, `feedback_odoo_attempts`, and `feedback_odoo_backfill_state` tables used by every later task.

- [ ] **Step 1: Add failing schema contract tests**

Add these assertions to `tests/test_feedback_schema.py`:

```python
from zira_dashboard._schema import SCHEMA_DDL


def test_feedback_schema_has_local_lifecycle_and_version():
    ddl = " ".join(SCHEMA_DDL.split())
    assert "ADD COLUMN IF NOT EXISTS status TEXT" in ddl
    assert "ADD COLUMN IF NOT EXISTS lifecycle_origin TEXT" in ddl
    assert "ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ" in ddl
    assert "ADD COLUMN IF NOT EXISTS finished_by TEXT" in ddl
    assert "ADD COLUMN IF NOT EXISTS resolution_note TEXT" in ddl
    assert "ADD COLUMN IF NOT EXISTS projection_version BIGINT NOT NULL DEFAULT 1" in ddl
    assert "feedback_local_terminal_fields_check" in ddl


def test_feedback_schema_has_durable_odoo_outbox_and_immutable_manifest():
    ddl = " ".join(SCHEMA_DDL.split())
    assert "CREATE TABLE IF NOT EXISTS feedback_images" in ddl
    assert "PRIMARY KEY (feedback_id, role)" in ddl
    assert "CREATE TABLE IF NOT EXISTS feedback_odoo_sync" in ddl
    assert "state TEXT NOT NULL DEFAULT 'idle'" in ddl
    assert "claim_token UUID" in ddl
    assert "CREATE TABLE IF NOT EXISTS feedback_odoo_attempts" in ddl
    assert "manifest JSONB NOT NULL" in ddl
    assert "manifest_digest TEXT NOT NULL" in ddl
    assert "CREATE TRIGGER feedback_odoo_attempts_immutable_manifest" in ddl
    assert "CREATE TRIGGER feedback_odoo_attempts_reject_delete" in ddl
    assert "CREATE TRIGGER feedback_odoo_attempts_reject_truncate" in ddl
    assert "CREATE TABLE IF NOT EXISTS feedback_odoo_warnings" in ddl
    assert "CREATE TABLE IF NOT EXISTS feedback_odoo_operator_actions" in ddl
    assert "CREATE TABLE IF NOT EXISTS feedback_odoo_backfill_state" in ddl
```

- [ ] **Step 2: Run the schema tests and observe the intended failure**

Run:

```bash
pytest tests/test_feedback_schema.py -v
```

Expected: the new tests fail because the lifecycle columns and outbox tables are absent.

- [ ] **Step 3: Add the idempotent DDL**

Append this schema shape after the existing feedback table migration in `src/zira_dashboard/_schema.py`; use explicit named checks so the tests and production bootstrap remain deterministic:

```sql
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS lifecycle_origin TEXT;
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS finished_by TEXT;
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS resolution_note TEXT;
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS projection_version BIGINT NOT NULL DEFAULT 1;
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS legacy_lifecycle_migrated_at TIMESTAMPTZ;

DO $feedback_checks$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'feedback_status_check'
      AND conrelid = 'feedback'::regclass
  ) THEN
    ALTER TABLE feedback ADD CONSTRAINT feedback_status_check CHECK (
      status IS NULL OR status IN ('requested', 'in_progress', 'completed', 'declined')
    );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'feedback_lifecycle_origin_check'
      AND conrelid = 'feedback'::regclass
  ) THEN
    ALTER TABLE feedback ADD CONSTRAINT feedback_lifecycle_origin_check CHECK (
      lifecycle_origin IS NULL OR lifecycle_origin IN ('local', 'legacy_project_task')
    );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'feedback_local_terminal_fields_check'
      AND conrelid = 'feedback'::regclass
  ) THEN
    ALTER TABLE feedback ADD CONSTRAINT feedback_local_terminal_fields_check CHECK (
      lifecycle_origin IS DISTINCT FROM 'local'
      OR (
        status IS NOT NULL
        AND (
          (
            status IN ('completed', 'declined')
            AND finished_at IS NOT NULL
            AND btrim(COALESCE(finished_by, '')) <> ''
            AND btrim(COALESCE(resolution_note, '')) <> ''
          )
          OR (
            status IN ('requested', 'in_progress')
            AND finished_at IS NULL
            AND finished_by IS NULL
            AND resolution_note IS NULL
          )
        )
      )
    );
  END IF;
END
$feedback_checks$;

CREATE TABLE IF NOT EXISTS feedback_images (
  feedback_id BIGINT NOT NULL REFERENCES feedback(id),
  role TEXT NOT NULL CHECK (role IN ('before', 'after')),
  jpeg_bytes BYTEA NOT NULL,
  sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  byte_length INTEGER NOT NULL CHECK (byte_length > 0 AND byte_length <= 5242880),
  width INTEGER NOT NULL CHECK (width > 0 AND width <= 2048),
  height INTEGER NOT NULL CHECK (height > 0 AND height <= 2048),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (feedback_id, role)
);

CREATE TABLE IF NOT EXISTS feedback_odoo_sync (
  feedback_id BIGINT PRIMARY KEY REFERENCES feedback(id),
  desired_version BIGINT NOT NULL CHECK (desired_version > 0),
  last_synced_version BIGINT NOT NULL DEFAULT 0 CHECK (last_synced_version >= 0),
  odoo_improvement_id INTEGER,
  due_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  state TEXT NOT NULL DEFAULT 'idle' CHECK (state IN ('idle', 'in_flight', 'quarantined')),
  claim_owner TEXT,
  claim_token UUID,
  claim_expires_at TIMESTAMPTZ,
  active_attempt_id UUID,
  last_error_class TEXT,
  last_error_summary TEXT,
  quarantine_reason TEXT,
  quarantined_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feedback_odoo_attempts (
  attempt_id UUID PRIMARY KEY,
  feedback_id BIGINT NOT NULL REFERENCES feedback(id),
  projection_version BIGINT NOT NULL CHECK (projection_version > 0),
  mutation_kind TEXT NOT NULL CHECK (mutation_kind IN ('create', 'update')),
  remote_id INTEGER,
  manifest JSONB NOT NULL,
  manifest_digest TEXT NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
  before_sha256 TEXT,
  before_byte_length INTEGER,
  after_sha256 TEXT,
  after_byte_length INTEGER,
  state TEXT NOT NULL CHECK (state IN (
    'prepared', 'dispatch_marked', 'rpc_succeeded', 'verified',
    'definitive_failed', 'ambiguous'
  )),
  dispatch_marked_at TIMESTAMPTZ,
  rpc_succeeded_at TIMESTAMPTZ,
  readback_at TIMESTAMPTZ,
  settled_at TIMESTAMPTZ,
  outcome_detail TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (feedback_id, projection_version, attempt_id),
  UNIQUE (feedback_id, attempt_id)
);

DO $feedback_sync_fk$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'feedback_odoo_sync_active_attempt_fk'
      AND conrelid = 'feedback_odoo_sync'::regclass
  ) THEN
    ALTER TABLE feedback_odoo_sync
      ADD CONSTRAINT feedback_odoo_sync_active_attempt_fk
      FOREIGN KEY (feedback_id, active_attempt_id)
      REFERENCES feedback_odoo_attempts(feedback_id, attempt_id)
      DEFERRABLE INITIALLY DEFERRED;
  END IF;
END
$feedback_sync_fk$;

CREATE OR REPLACE FUNCTION reject_feedback_attempt_manifest_mutation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
  IF NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
     OR NEW.feedback_id IS DISTINCT FROM OLD.feedback_id
     OR NEW.projection_version IS DISTINCT FROM OLD.projection_version
     OR NEW.mutation_kind IS DISTINCT FROM OLD.mutation_kind
     OR NEW.manifest IS DISTINCT FROM OLD.manifest
     OR NEW.manifest_digest IS DISTINCT FROM OLD.manifest_digest
     OR NEW.before_sha256 IS DISTINCT FROM OLD.before_sha256
     OR NEW.before_byte_length IS DISTINCT FROM OLD.before_byte_length
     OR NEW.after_sha256 IS DISTINCT FROM OLD.after_sha256
     OR NEW.after_byte_length IS DISTINCT FROM OLD.after_byte_length THEN
    RAISE EXCEPTION 'feedback Odoo attempt manifest is immutable';
  END IF;
  RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS feedback_odoo_attempts_immutable_manifest
  ON feedback_odoo_attempts;
CREATE TRIGGER feedback_odoo_attempts_immutable_manifest
BEFORE UPDATE ON feedback_odoo_attempts
FOR EACH ROW EXECUTE FUNCTION reject_feedback_attempt_manifest_mutation();

CREATE OR REPLACE FUNCTION reject_feedback_attempt_removal()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
  RAISE EXCEPTION 'feedback Odoo attempts are append-only';
END
$function$;

DROP TRIGGER IF EXISTS feedback_odoo_attempts_reject_delete
  ON feedback_odoo_attempts;
CREATE TRIGGER feedback_odoo_attempts_reject_delete
BEFORE DELETE ON feedback_odoo_attempts
FOR EACH ROW EXECUTE FUNCTION reject_feedback_attempt_removal();

DROP TRIGGER IF EXISTS feedback_odoo_attempts_reject_truncate
  ON feedback_odoo_attempts;
CREATE TRIGGER feedback_odoo_attempts_reject_truncate
BEFORE TRUNCATE ON feedback_odoo_attempts
FOR EACH STATEMENT EXECUTE FUNCTION reject_feedback_attempt_removal();

CREATE TABLE IF NOT EXISTS feedback_odoo_warnings (
  feedback_id BIGINT NOT NULL REFERENCES feedback(id),
  projection_version BIGINT NOT NULL CHECK (projection_version > 0),
  warning_class TEXT NOT NULL CHECK (
    warning_class IN ('employee_missing', 'employee_ambiguous')
  ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (feedback_id, projection_version, warning_class)
);

CREATE TABLE IF NOT EXISTS feedback_odoo_operator_actions (
  id BIGSERIAL PRIMARY KEY,
  attempt_id UUID NOT NULL REFERENCES feedback_odoo_attempts(attempt_id),
  action TEXT NOT NULL CHECK (
    action IN ('keep', 'release_definitive', 'supersede_and_retry')
  ),
  reviewer TEXT NOT NULL CHECK (btrim(reviewer) <> ''),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feedback_odoo_backfill_state (
  id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  last_feedback_id BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO feedback_odoo_backfill_state (id) VALUES (1)
ON CONFLICT (id) DO NOTHING;
```

- [ ] **Step 4: Run focused schema and bootstrap tests**

Run:

```bash
pytest tests/test_feedback_schema.py tests/test_schema_employee_notifications.py -v
```

Expected: all selected tests pass; no schema string or bootstrap regression.

- [ ] **Step 5: Commit and push the schema foundation**

```bash
git add CHANGELOG.md src/zira_dashboard/_schema.py tests/test_feedback_schema.py docs/superpowers/plans/2026-08-20-odoo-2s-feedback-mirror.md
git commit -m "feat(feedback): add durable lifecycle and sync schema"
git push origin main
```

### Task 2: Safe Before/After Image Pipeline

**Files:**

- Modify: `pyproject.toml`
- Create: `src/zira_dashboard/feedback_image.py`
- Create: `tests/test_feedback_image.py`

**Interfaces:**

- Consumes: raw upload bytes from submission/admin routes.
- Produces: `NormalizedImage(jpeg_bytes, sha256, byte_length, width, height)` and `normalize_image(raw: bytes) -> NormalizedImage`.

- [ ] **Step 1: Write failing normalization tests**

Create `tests/test_feedback_image.py`:

```python
from io import BytesIO

import pytest
from PIL import Image

from zira_dashboard.feedback_image import ImageRejected, normalize_image


def image_bytes(fmt="PNG", size=(3000, 1000), color=(10, 20, 30, 120)):
    output = BytesIO()
    Image.new("RGBA", size, color).save(output, format=fmt)
    return output.getvalue()


def test_normalize_image_strips_metadata_resizes_and_hashes_jpeg():
    normalized = normalize_image(image_bytes())
    reopened = Image.open(BytesIO(normalized.jpeg_bytes))
    assert reopened.format == "JPEG"
    assert reopened.mode == "RGB"
    assert reopened.size == (2048, 683)
    assert reopened.getexif() == {}
    assert normalized.byte_length == len(normalized.jpeg_bytes)
    assert len(normalized.sha256) == 64


@pytest.mark.parametrize("raw", [b"", b"not an image", b"x" * (10 * 1024 * 1024 + 1)])
def test_normalize_image_rejects_empty_invalid_and_oversized_inputs(raw):
    with pytest.raises(ImageRejected):
        normalize_image(raw)


def test_normalize_image_rejects_excessive_dimensions(monkeypatch):
    monkeypatch.setattr("zira_dashboard.feedback_image.MAX_SIDE", 100)
    with pytest.raises(ImageRejected, match="dimensions"):
        normalize_image(image_bytes(size=(101, 10)))
```

- [ ] **Step 2: Run the tests and verify the import failure**

```bash
pytest tests/test_feedback_image.py -v
```

Expected: collection fails because `feedback_image` does not exist.

- [ ] **Step 3: Add Pillow and implement the normalizer**

Add `"Pillow>=10.4,<12",` to runtime dependencies in `pyproject.toml`, then create:

```python
# src/zira_dashboard/feedback_image.py
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 5 * 1024 * 1024
MAX_SIDE = 8192
MAX_PIXELS = 25_000_000
OUTPUT_LONG_SIDE = 2048


class ImageRejected(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedImage:
    jpeg_bytes: bytes
    sha256: str
    byte_length: int
    width: int
    height: int


def normalize_image(raw: bytes) -> NormalizedImage:
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ImageRejected("image must be between 1 byte and 10 MiB")
    try:
        with Image.open(BytesIO(raw)) as source:
            if source.format not in {"JPEG", "PNG", "WEBP"}:
                raise ImageRejected("only JPEG, PNG, and WebP images are supported")
            width, height = source.size
            if width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
                raise ImageRejected("image dimensions exceed the safe limit")
            source.seek(0)
            frame = ImageOps.exif_transpose(source.copy())
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ImageRejected("image could not be decoded safely") from error
    if frame.mode in {"RGBA", "LA"} or "transparency" in frame.info:
        rgba = frame.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        frame = background
    else:
        frame = frame.convert("RGB")
    frame.thumbnail((OUTPUT_LONG_SIDE, OUTPUT_LONG_SIDE), Image.Resampling.LANCZOS)
    output = BytesIO()
    frame.save(output, format="JPEG", quality=85, optimize=True)
    jpeg = output.getvalue()
    if len(jpeg) > MAX_OUTPUT_BYTES:
        raise ImageRejected("normalized image exceeds 5 MiB")
    return NormalizedImage(
        jpeg_bytes=jpeg,
        sha256=hashlib.sha256(jpeg).hexdigest(),
        byte_length=len(jpeg),
        width=frame.width,
        height=frame.height,
    )
```

- [ ] **Step 4: Run image tests and the existing feedback static tests**

```bash
pytest tests/test_feedback_image.py tests/test_whatsnew_panel_static.py tests/test_timeclock_feedback_static.py -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit and push the image pipeline**

```bash
git add CHANGELOG.md pyproject.toml src/zira_dashboard/feedback_image.py tests/test_feedback_image.py
git commit -m "feat(feedback): normalize feedback screenshots"
git push origin main
```

### Task 3: Atomic Local-First Submission and Local Status Reads

**Files:**

- Modify: `src/zira_dashboard/feedback_store.py`
- Modify: `src/zira_dashboard/routes/feedback.py`
- Modify: `src/zira_dashboard/templates/_feedback.html`
- Modify: `src/zira_dashboard/static/feedback.js`
- Modify: `tests/test_feedback_routes.py`
- Modify: `tests/test_feedback_mine_route.py`
- Modify: `tests/test_feedback_store.py`
- Modify: `tests/test_whatsnew_panel_static.py`

**Interfaces:**

- Consumes: `NormalizedImage | None` from Task 2 and schema from Task 1.
- Produces: `feedback_store.create_submission(...) -> int`, local requested status/version/outbox state, and local status output for all new rows.

- [ ] **Step 1: Replace remote-first route expectations with failing local-first tests**

Add/update focused tests in `tests/test_feedback_routes.py`:

```python
from io import BytesIO

from PIL import Image


def valid_png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 8), "green").save(output, format="PNG")
    return output.getvalue()


def test_post_feedback_saves_locally_without_calling_odoo(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        feedback_store,
        "create_submission",
        lambda **values: captured.update(values) or 12,
    )
    monkeypatch.setattr(
        odoo_client,
        "create_feedback_task",
        lambda **values: (_ for _ in ()).throw(AssertionError(values)),
    )
    response = client.post(
        "/feedback",
        data={"type": "bug", "description": "It broke", "page_url": "/recycling"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": 12}
    assert captured["status"] == "requested"
    assert captured["task_type"] == "bug"


def test_post_feedback_still_succeeds_when_odoo_is_unavailable(monkeypatch):
    monkeypatch.setattr(feedback_store, "create_submission", lambda **values: 44)
    monkeypatch.setattr(odoo_client, "authenticate", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    response = client.post("/feedback", data={"type": "feature", "description": "New view"})
    assert response.status_code == 200
    assert response.json()["id"] == 44


def test_post_feedback_normalizes_only_one_optional_image(monkeypatch):
    captured = {}
    monkeypatch.setattr(feedback_store, "create_submission", lambda **values: captured.update(values) or 5)
    response = client.post(
        "/feedback",
        data={"type": "bug", "description": "See shot"},
        files={"screenshot": ("shot.png", valid_png_bytes(), "image/png")},
    )
    assert response.status_code == 200
    assert captured["before_image"].jpeg_bytes.startswith(b"\xff\xd8")
```

Add to `tests/test_feedback_mine_route.py`:

```python
def test_mine_uses_local_status_without_odoo_for_migrated_rows(monkeypatch):
    monkeypatch.setattr(
        feedback_store,
        "for_submitter",
        lambda upn, limit=100: [{
            "id": 1, "message": "Safe", "task_type": "bug",
            "created_at": "2026-08-20", "page_url": None,
            "status": "completed", "odoo_task_id": None,
        }],
    )
    monkeypatch.setattr(
        odoo_client,
        "fetch_task_stage_names",
        lambda ids: (_ for _ in ()).throw(AssertionError(ids)),
    )
    response = client.get("/api/feedback/mine")
    assert response.json()["items"][0]["status"] == "completed"
```

- [ ] **Step 2: Run the focused route/store tests and observe failures**

```bash
pytest tests/test_feedback_routes.py tests/test_feedback_mine_route.py tests/test_feedback_store.py -v
```

Expected: failures show the route still calls Odoo first and `create_submission` is absent.

- [ ] **Step 3: Implement one atomic local transaction**

Add this interface to `feedback_store.py` and use the existing `db.cursor()` transaction:

```python
def create_submission(
    *,
    message: str,
    submitter: str | None,
    page_url: str | None,
    task_type: str,
    status: str = "requested",
    before_image: NormalizedImage | None = None,
) -> int:
    if task_type not in {"bug", "feature"}:
        raise ValueError("unsupported feedback type")
    if status != "requested":
        raise ValueError("new feedback must start requested")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO feedback "
            "(submitter, page_url, task_type, message, status, lifecycle_origin, "
            "projection_version, updated_at) "
            "VALUES (%s, %s, %s, %s, 'requested', 'local', 1, now()) RETURNING id",
            (submitter, page_url, task_type, message),
        )
        feedback_id = int(cur.fetchone()["id"])
        if before_image is not None:
            cur.execute(
                "INSERT INTO feedback_images "
                "(feedback_id, role, jpeg_bytes, sha256, byte_length, width, height) "
                "VALUES (%s, 'before', %s, %s, %s, %s, %s)",
                (
                    feedback_id,
                    before_image.jpeg_bytes,
                    before_image.sha256,
                    before_image.byte_length,
                    before_image.width,
                    before_image.height,
                ),
            )
        cur.execute(
            "INSERT INTO feedback_odoo_sync "
            "(feedback_id, desired_version, last_synced_version, due_at, state) "
            "VALUES (%s, 1, 0, now(), 'idle')",
            (feedback_id,),
        )
        return feedback_id
```

Change `POST /feedback` to accept `screenshot: UploadFile | None`, reject non-image/PDF content through `normalize_image`, call only `create_submission`, and return `{"ok": True, "id": new_id}`. Remove task/project/tag/attachment calls from this route but do not delete the shared legacy helpers.

For `/api/feedback/mine`, use local `status` when non-null. Batch-read Odoo stages only for rows with `status is None` and `odoo_task_id`; map the legacy buckets to `requested`, `completed`, or `declined` for the response without persisting them.

- [ ] **Step 4: Make the modal hold one optional screenshot and local statuses**

Change `_feedback.html` to:

```html
<div class="fb-attachments" id="fb-attachments"></div>
<div class="fb-actions-row">
  <button type="button" id="fb-upload-btn" class="fb-upload">Add screenshot</button>
  <input type="file" id="fb-file-input" class="fb-file-input"
         accept="image/jpeg,image/png,image/webp" hidden>
  <span class="fb-hint">or paste one screenshot</span>
</div>
```

In `feedback.js`, replace the attachment array with `var screenshot = null`, replace the old file only after revoking its URL, append it as form field `screenshot`, and use:

```javascript
function statusLabel(status) {
  return {
    requested: 'Requested',
    in_progress: 'In Progress',
    completed: 'Completed',
    declined: 'Declined'
  }[status] || 'Requested';
}
```

- [ ] **Step 5: Run focused and static regression tests**

```bash
pytest tests/test_feedback_routes.py tests/test_feedback_mine_route.py tests/test_feedback_store.py tests/test_whatsnew_panel_static.py tests/test_timeclock_feedback_static.py -v
```

Expected: all selected tests pass, including the explicit Odoo-unavailable submission case.

- [ ] **Step 6: Add a plain-language patch note, commit, and push**

Add under the current date in `CHANGELOG.md`:

```markdown
### Feedback stays safe

#### Fixes

- **Feedback is now saved before it is shared.** If Odoo is down, your message and screenshot still reach Plant Manager and can be handled later.
```

Then:

```bash
git add CHANGELOG.md src/zira_dashboard/feedback_store.py src/zira_dashboard/routes/feedback.py src/zira_dashboard/templates/_feedback.html src/zira_dashboard/static/feedback.js tests/test_feedback_routes.py tests/test_feedback_mine_route.py tests/test_feedback_store.py tests/test_whatsnew_panel_static.py
git commit -m "feat(feedback): save submissions locally first"
git push origin main
```

### Task 4: Super-Admin Local Feedback Lifecycle

**Files:**

- Create: `src/zira_dashboard/routes/feedback_admin.py`
- Create: `src/zira_dashboard/templates/admin_feedback.html`
- Create: `src/zira_dashboard/static/admin_feedback.css`
- Create: `tests/test_feedback_admin_routes.py`
- Modify: `src/zira_dashboard/feedback_store.py`
- Modify: `src/zira_dashboard/app.py`

**Interfaces:**

- Consumes: `auth.request_is_super_admin`, `normalize_image`, local feedback/image/sync tables.
- Produces: `feedback_store.transition(...)`, super-admin GET/POST routes, authoritative terminal event data, and a newer due projection version.

- [ ] **Step 1: Write failing transition and authorization tests**

Create `tests/test_feedback_admin_routes.py` with direct route tests that set `request.state.user_upn` through a tiny authenticated test app, plus store unit tests:

```python
from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image

from zira_dashboard import feedback_store
from zira_dashboard.routes import feedback_admin


def valid_png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 8), "green").save(output, format="PNG")
    return output.getvalue()


def admin_client(upn: str) -> TestClient:
    test_app = FastAPI()

    @test_app.middleware("http")
    async def set_identity(request: Request, call_next):
        request.state.user_upn = upn
        request.state.user_name = upn
        return await call_next(request)

    test_app.include_router(feedback_admin.router)
    return TestClient(test_app, follow_redirects=False)


class ReturningCursor:
    def __init__(self, row):
        self.row = row
        self.executions = []

    def execute(self, sql, params=None):
        self.executions.append((sql, params))

    def fetchone(self):
        return self.row


@contextmanager
def cursor_returning(row):
    yield ReturningCursor(row)


def test_non_super_admin_cannot_view_or_change_feedback(monkeypatch):
    monkeypatch.setenv("SUPER_ADMIN_UPNS", "dale@gruberpallets.com")
    assert admin_client("person@gruberpallets.com").get("/admin/feedback").status_code == 403
    response = admin_client("person@gruberpallets.com").post(
        "/admin/feedback/7/status",
        data={"status": "in_progress"},
    )
    assert response.status_code == 403


def test_terminal_action_uses_authenticated_admin_and_optional_after_image(
    monkeypatch
):
    captured = {}
    monkeypatch.setattr(
        feedback_store,
        "transition",
        lambda **values: captured.update(values) or values,
    )
    response = admin_client("dale@gruberpallets.com").post(
        "/admin/feedback/7/status",
        data={"status": "completed", "resolution_note": "Fixed safely"},
        files={"after_image": ("after.png", valid_png_bytes(), "image/png")},
    )
    assert response.status_code == 303
    assert captured["feedback_id"] == 7
    assert captured["actor"] == "dale@gruberpallets.com"
    assert captured["status"] == "completed"
    assert captured["resolution_note"] == "Fixed safely"
    assert captured["after_image"].jpeg_bytes.startswith(b"\xff\xd8")


def test_transition_rejects_reopening_terminal_feedback(monkeypatch):
    monkeypatch.setattr(
        feedback_store.db,
        "cursor",
        lambda: cursor_returning({"status": "completed", "projection_version": 2}),
    )
    with pytest.raises(feedback_store.InvalidTransition, match="terminal"):
        feedback_store.transition(
            feedback_id=7,
            status="in_progress",
            actor="dale@gruberpallets.com",
            resolution_note=None,
            after_image=None,
            now=datetime(2026, 8, 20, 18, 0, tzinfo=UTC),
        )
```

- [ ] **Step 2: Run the new tests and observe missing route/store failures**

```bash
pytest tests/test_feedback_admin_routes.py -v
```

Expected: collection or assertions fail because the admin router and transition function do not exist.

- [ ] **Step 3: Implement the state machine in one local transaction**

Add:

```python
class InvalidTransition(ValueError):
    pass


_TRANSITIONS = {
    "requested": {"in_progress", "completed", "declined"},
    "in_progress": {"completed", "declined"},
    "completed": set(),
    "declined": set(),
}


def transition(
    *,
    feedback_id: int,
    status: str,
    actor: str,
    resolution_note: str | None,
    after_image: NormalizedImage | None,
    now: datetime,
) -> int:
    clean_actor = actor.strip().lower()
    clean_note = (resolution_note or "").strip()
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, lifecycle_origin, projection_version "
            "FROM feedback WHERE id = %s FOR UPDATE",
            (feedback_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(feedback_id)
        if row["lifecycle_origin"] != "local":
            raise InvalidTransition("feedback is not locally managed")
        current = row["status"]
        if status not in _TRANSITIONS.get(current, set()):
            raise InvalidTransition("feedback is terminal or transition is invalid")
        terminal = status in {"completed", "declined"}
        if terminal and (not clean_actor or not clean_note):
            raise InvalidTransition("terminal feedback requires an actor and resolution note")
        version = int(row["projection_version"]) + 1
        cur.execute(
            "UPDATE feedback SET status = %s, lifecycle_origin = 'local', "
            "finished_at = %s, finished_by = %s, resolution_note = %s, "
            "projection_version = %s, updated_at = %s WHERE id = %s",
            (
                status,
                now if terminal else None,
                clean_actor if terminal else None,
                clean_note if terminal else None,
                version,
                now,
                feedback_id,
            ),
        )
        if after_image is not None:
            if not terminal:
                raise InvalidTransition("after image is allowed only for terminal feedback")
            cur.execute(
                "INSERT INTO feedback_images "
                "(feedback_id, role, jpeg_bytes, sha256, byte_length, width, height) "
                "VALUES (%s, 'after', %s, %s, %s, %s, %s) "
                "ON CONFLICT (feedback_id, role) DO UPDATE SET "
                "jpeg_bytes = EXCLUDED.jpeg_bytes, sha256 = EXCLUDED.sha256, "
                "byte_length = EXCLUDED.byte_length, width = EXCLUDED.width, "
                "height = EXCLUDED.height, created_at = now()",
                (
                    feedback_id,
                    after_image.jpeg_bytes,
                    after_image.sha256,
                    after_image.byte_length,
                    after_image.width,
                    after_image.height,
                ),
            )
        cur.execute(
            "UPDATE feedback_odoo_sync SET desired_version = %s, due_at = %s, "
            "state = CASE WHEN state IN ('in_flight', 'quarantined') "
            "THEN state ELSE 'idle' END, "
            "updated_at = %s WHERE feedback_id = %s RETURNING feedback_id",
            (version, now, now, feedback_id),
        )
        if cur.fetchone() is None:
            raise InvalidTransition("feedback sync state is missing")
        return version
```

Do not clear a quarantine during a lifecycle change; the newer truth remains desired but still requires explicit disposition.

- [ ] **Step 4: Add the super-admin router and focused template**

Create GET `/admin/feedback` and POST `/admin/feedback/{feedback_id}/status`. Both call `auth.request_is_super_admin(request)` and return 403 before any store or image work. The POST normalizes `after_image`, calls `transition`, maps `KeyError` to 404 and `InvalidTransition`/`ImageRejected` to 422, then redirects with 303.

The template must render local state and sync state, an In Progress action for Requested rows, terminal forms with required `resolution_note`, optional `after_image`, and no mutation form for Completed/Declined rows. Use escaped Jinja text, not `|safe`.

Register `feedback_admin.router` in `app.py` and add a Settings/admin link visible only where existing super-admin links are shown.

- [ ] **Step 5: Run lifecycle, auth, feedback, and template tests**

```bash
pytest tests/test_feedback_admin_routes.py tests/test_auth_session.py tests/test_feedback_routes.py tests/test_feedback_mine_route.py -v
```

Expected: all selected tests pass.

- [ ] **Step 6: Add a plain-language patch note, commit, and push**

Add:

```markdown
### Feedback progress

#### Features

- **Feedback can now be worked on and finished inside Plant Manager.** The person handling it can mark progress, explain the result, and add an after picture.
```

Then:

```bash
git add CHANGELOG.md src/zira_dashboard/feedback_store.py src/zira_dashboard/routes/feedback_admin.py src/zira_dashboard/templates/admin_feedback.html src/zira_dashboard/static/admin_feedback.css src/zira_dashboard/app.py tests/test_feedback_admin_routes.py
git commit -m "feat(feedback): add local admin lifecycle"
git push origin main
```

### Task 5: Dedicated Allowlisted Odoo Improvements Client

**Files:**

- Create: `src/zira_dashboard/odoo_improvements.py`
- Create: `tests/test_odoo_improvements.py`
- Modify: `tests/test_odoo_facade_contract.py`

**Interfaces:**

- Consumes: dedicated environment variables only; no imports or state from the generic `odoo_client` facade.
- Produces: `ImprovementsClient`, `ImprovementContract`, `GateClosed`, `TargetIdentityError`, `ContractError`, exact compound lookup/read/create/write, employee lookup, and legacy task-stage read.

- [ ] **Step 1: Write failing configuration, gate, and allowlist tests**

Create `tests/test_odoo_improvements.py`:

```python
import pytest

from zira_dashboard.odoo_improvements import (
    ContractError,
    GateClosed,
    ImprovementsClient,
    ImprovementsConfig,
)


ENV = {
    "ODOO_IMPROVEMENTS_URL": "https://odoo.invalid",
    "ODOO_IMPROVEMENTS_DB": "database",
    "ODOO_IMPROVEMENTS_LOGIN": "service@example.invalid",
    "ODOO_IMPROVEMENTS_API_KEY": "secret-key",
    "ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID": "uuid-expected",
    "ODOO_IMPROVEMENTS_EXPECTED_COMPANY": "Gruber Pallets, Inc.",
}


def contract_fields(source_selection):
    fields = {
        "x_name": {"type": "char", "readonly": False},
        "x_studio_source_id": {"type": "char", "readonly": False},
        "x_studio_date_start": {"type": "date", "readonly": False},
        "x_studio_submitted_by": {"type": "many2one", "readonly": False},
        "x_studio_date_stop": {"type": "date", "readonly": False},
        "x_studio_completed_by": {"type": "many2one", "readonly": False},
        "x_studio_notes": {"type": "html", "readonly": False},
        "x_studio_image": {"type": "binary", "readonly": False},
        "x_studio_after_image": {"type": "binary", "readonly": False},
        "x_studio_source": {
            "type": "selection", "readonly": False, "selection": source_selection,
        },
        "x_studio_status": {
            "type": "selection", "readonly": False,
            "selection": [["Requested", "Requested"], ["In-Progress", "In-Progress"],
                          ["Completed", "Completed"], ["Declined", "Declined"]],
        },
        "x_studio_type": {
            "type": "selection", "readonly": False,
            "selection": [["Digital", "Digital - Bug"],
                          ["Digital - New Feature", "Digital - New Feature"],
                          ["Physical", "Physical"]],
        },
    }
    return fields


def set_config(monkeypatch):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("value", [None, "", "TRUE", " true", "true ", "1"])
def test_write_gate_requires_exact_lowercase_true(monkeypatch, value):
    set_config(monkeypatch)
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "true")
    if value is None:
        monkeypatch.delenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", value)
    with pytest.raises(GateClosed):
        ImprovementsClient.from_env().assert_mutation_allowed(17)


def test_canary_fence_allows_only_exact_positive_feedback_id(monkeypatch):
    set_config(monkeypatch)
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "true")
    monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", "true")
    monkeypatch.setenv("ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID", "17")
    client = ImprovementsClient.from_env()
    client.assert_mutation_allowed(17)
    with pytest.raises(GateClosed, match="canary"):
        client.assert_mutation_allowed(18)


@pytest.mark.parametrize(
    ("model", "method"),
    [
        ("x_2s_improvements", "unlink"),
        ("x_2s_improvements", "action_archive"),
        ("ir.model.fields", "write"),
        ("project.task", "write"),
        ("hr.employee", "create"),
    ],
)
def test_allowlist_rejects_destructive_and_unrelated_calls(monkeypatch, model, method):
    set_config(monkeypatch)
    client = ImprovementsClient.from_env(executor=lambda *args, **kwargs: None)
    with pytest.raises(ContractError, match="not allowlisted"):
        client._execute(model, method, [])


def test_target_write_rejects_unknown_active_and_sync_token_fields(monkeypatch):
    set_config(monkeypatch)
    client = ImprovementsClient.from_env(executor=lambda *args, **kwargs: True)
    for fields in ({"active": False}, {"sync_token": "x"}, {"x_unknown": "x"}):
        with pytest.raises(ContractError):
            client.write_improvement(9, fields, feedback_id=17)
```

- [ ] **Step 2: Run the client tests and observe the missing-module failure**

```bash
pytest tests/test_odoo_improvements.py -v
```

Expected: collection fails because `odoo_improvements` does not exist.

- [ ] **Step 3: Implement secret-safe configuration and exact gates**

Create the client with frozen configuration whose `repr` masks login/key/URL values:

```python
@dataclass(frozen=True, repr=False)
class ImprovementsConfig:
    url: str
    database: str
    login: str
    api_key: str
    expected_database_uuid: str
    expected_company: str

    def __repr__(self) -> str:
        return "ImprovementsConfig(<redacted>)"

    @classmethod
    def from_env(cls) -> "ImprovementsConfig":
        names = {
            "url": "ODOO_IMPROVEMENTS_URL",
            "database": "ODOO_IMPROVEMENTS_DB",
            "login": "ODOO_IMPROVEMENTS_LOGIN",
            "api_key": "ODOO_IMPROVEMENTS_API_KEY",
            "expected_database_uuid": "ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID",
            "expected_company": "ODOO_IMPROVEMENTS_EXPECTED_COMPANY",
        }
        values = {field: os.environ.get(name, "") for field, name in names.items()}
        missing = [names[field] for field, value in values.items() if not value]
        if missing:
            raise ImprovementsConfigError("missing dedicated Odoo settings: " + ", ".join(missing))
        values["url"] = values["url"].rstrip("/")
        return cls(**values)


def _exact_true(name: str) -> bool:
    return os.environ.get(name) == "true"


def _canary_id() -> int | None:
    raw = os.environ.get("ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID")
    if raw is None or raw == "":
        return None
    if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
        raise GateClosed("invalid improvements canary feedback id")
    return int(raw)


def assert_mutation_allowed(self, feedback_id: int) -> None:
    if not _exact_true("ODOO_SHARED_REPORTING_WRITE_ENABLED"):
        raise GateClosed("shared reporting write gate is closed")
    if not _exact_true("ODOO_IMPROVEMENTS_WRITE_ENABLED"):
        raise GateClosed("improvements write gate is closed")
    canary = _canary_id()
    if canary is not None and canary != feedback_id:
        raise GateClosed("feedback is outside the canary fence")
```

`ImprovementsClient` exposes `default_executor` as a class-level callable that
builds the dedicated timeout-enabled XML-RPC proxies. `from_env(executor=None,
uid=None)` uses an injected executor/UID in unit tests and otherwise authenticates
with the dedicated configuration. Do not include configuration values in
exception strings or object representations.

- [ ] **Step 4: Implement the permanent method/field allowlist and narrow wrappers**

Use this allowlist and validate wrapper arguments before `_execute`:

```python
TARGET_MODEL = "x_2s_improvements"
SOURCE_VALUE = "GPI Plant Manager"
TARGET_FIELDS = frozenset({
    "x_name",
    "x_studio_source_id",
    "x_studio_date_start",
    "x_studio_submitted_by",
    "x_studio_date_stop",
    "x_studio_completed_by",
    "x_studio_notes",
    "x_studio_type",
    "x_studio_image",
    "x_studio_after_image",
    "x_studio_status",
    "x_studio_source",
})
ALLOWED = frozenset({
    ("ir.config_parameter", "get_param"),
    ("res.users", "read"),
    ("res.company", "read"),
    (TARGET_MODEL, "fields_get"),
    (TARGET_MODEL, "search_read"),
    (TARGET_MODEL, "read"),
    (TARGET_MODEL, "create"),
    (TARGET_MODEL, "write"),
    ("hr.employee", "search_read"),
    ("project.task", "read"),
})


def _execute(self, model: str, method: str, *args, **kwargs):
    if (model, method) not in ALLOWED:
        raise ContractError(f"{model}.{method} is not allowlisted")
    return self._executor(model, method, *args, **kwargs)


def find_exact(self, source_id: str) -> list[dict]:
    return self._execute(
        TARGET_MODEL,
        "search_read",
        [("x_studio_source", "=", SOURCE_VALUE), ("x_studio_source_id", "=", source_id)],
        fields=["id", "x_studio_source", "x_studio_source_id"],
        limit=3,
    ) or []


def create_improvement(
    self,
    fields: dict,
    *,
    feedback_id: int,
    expected_contract: ImprovementContract,
) -> int:
    self._validate_target_fields(fields, feedback_id=feedback_id, require_identity=True)
    authorization = self._authorize_mutation(feedback_id, expected_contract)
    self.assert_mutation_allowed(feedback_id)
    result = self._execute(TARGET_MODEL, "create", fields, authorization=authorization)
    if type(result) is not int or result <= 0:
        raise MalformedMutationResponse("create response was not a positive integer")
    return result


def write_improvement(
    self,
    remote_id: int,
    fields: dict,
    *,
    feedback_id: int,
    expected_contract: ImprovementContract,
) -> None:
    self._validate_target_fields(fields, feedback_id=feedback_id)
    authorization = self._authorize_mutation(feedback_id, expected_contract)
    self.assert_mutation_allowed(feedback_id)
    result = self._execute(
        TARGET_MODEL, "write", [remote_id], fields, authorization=authorization
    )
    if result is not True:
        raise MalformedMutationResponse("write response was not exactly true")
```

`_validate_target_fields` requires a nonempty subset of `TARGET_FIELDS`, rejects `active`, and rejects any key containing `token`. Create additionally requires exact Source and canonical matching Source ID. `_execute` rejects target `create`/`write` without the internal authorization minted only after complete validation, fresh identity/contract equality, and an open-gate check; both wrappers re-read the gates immediately before the executor call. `read_improvement` accepts only exact positive non-boolean integer IDs and an explicit subset of `{"id"} | TARGET_FIELDS`; when binary verification is requested, pass context `{"bin_size": False}`. `read_legacy_task_stages` accepts only a nonempty list of exact positive IDs sourced by the rollout layer and always fixes fields to `["id", "stage_id"]`.

- [ ] **Step 5: Add contract and fresh identity verification tests**

Extend the test fake executor with ordered responses and assert `verify_target_identity()` freshly calls:

```python
def test_verify_target_identity_checks_uuid_company_and_source_every_time(monkeypatch):
    set_config(monkeypatch)
    calls = []
    responses = iter([
        "uuid-expected",
        [{"id": 4, "company_id": [8, "Gruber Pallets, Inc."]}],
        [{"id": 8, "name": "Gruber Pallets, Inc."}],
        contract_fields(source_selection=[["GPI Plant Manager", "GPI Plant Manager"]]),
    ] * 2)
    client = ImprovementsClient.from_env(
        executor=lambda model, method, *args, **kwargs: calls.append((model, method)) or next(responses),
        uid=4,
    )
    client.verify_target_identity()
    client.verify_target_identity()
    assert calls.count(("ir.config_parameter", "get_param")) == 2
    assert calls.count(("x_2s_improvements", "fields_get")) == 2
```

Implement `verify_target_identity` with exact string equality for UUID/company and `fields_get` checks for every required field, writable state, date/date-time types, and exact status/type/source stored selections. Cache nothing. Return an immutable `ImprovementContract` carrying the discovered start/stop date field types.

Also expose `read_contract() -> ImprovementContract` as the same uncached
`fields_get` validation without UUID/company reads. The worker uses it to build
the immutable manifest, then passes that contract to `create_improvement` or
`write_improvement`. The wrapper freshly calls `verify_target_identity()` and
requires its return value to equal the supplied immutable contract before the
mutation executor can be reached.

- [ ] **Step 6: Run client and existing facade tests**

```bash
pytest tests/test_odoo_improvements.py tests/test_odoo_client.py tests/test_odoo_facade_contract.py tests/test_feedback_odoo.py -v
```

Expected: all selected tests pass and the existing facade contract remains unchanged.

- [ ] **Step 7: Commit and push the isolated client**

```bash
git add CHANGELOG.md src/zira_dashboard/odoo_improvements.py tests/test_odoo_improvements.py tests/test_odoo_facade_contract.py
git commit -m "feat(feedback): add isolated Odoo improvements client"
git push origin main
```

### Task 6: Exact Projection, Employee Resolution, and Manifest

**Files:**

- Create: `src/zira_dashboard/feedback_projection.py`
- Create: `tests/test_feedback_projection.py`
- Modify: `src/zira_dashboard/feedback_store.py`

**Interfaces:**

- Consumes: committed local feedback/image snapshots and `ImprovementContract` from Task 5.
- Produces: `Projection(fields, manifest, manifest_digest, binaries)`, `source_id_for`, `resolve_employee_id`, `build_projection`, and `verify_readback`.

- [ ] **Step 1: Write failing exact-mapping and omission tests**

Create `tests/test_feedback_projection.py`:

```python
import hashlib
from datetime import UTC, datetime

from zira_dashboard.feedback_image import NormalizedImage
from zira_dashboard.feedback_projection import build_projection, source_id_for


def feedback(**changes):
    row = {
        "id": 12345,
        "message": "Problem <script>alert(1)</script>",
        "task_type": "bug",
        "created_at": datetime(2026, 8, 20, 15, 30, tzinfo=UTC),
        "submitter": " Person@Example.com ",
        "status": "requested",
        "finished_at": None,
        "finished_by": None,
        "resolution_note": None,
        "projection_version": 1,
    }
    row.update(changes)
    return row


def normalized(raw: bytes) -> NormalizedImage:
    return NormalizedImage(
        jpeg_bytes=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw),
        width=8,
        height=8,
    )


def projection_with_before(raw: bytes):
    return build_projection(
        feedback(),
        images={"before": normalized(raw)},
        employee_lookup=lambda email: None,
        start_type="date",
        stop_type="date",
    )


def test_source_id_and_requested_bug_mapping_are_exact():
    projection = build_projection(
        feedback(), images={}, employee_lookup=lambda email: None,
        start_type="date", stop_type="date",
    )
    assert source_id_for(12345) == "GPI-PM-FB-12345"
    assert projection.fields == {
        "x_name": "Problem <script>alert(1)</script>",
        "x_studio_source_id": "GPI-PM-FB-12345",
        "x_studio_date_start": "2026-08-20",
        "x_studio_type": "Digital",
        "x_studio_status": "Requested",
        "x_studio_source": "GPI Plant Manager",
    }


def test_completed_feature_maps_terminal_fields_and_escapes_note():
    row = feedback(
        task_type="feature",
        status="completed",
        finished_at=datetime(2026, 8, 21, 2, 0, tzinfo=UTC),
        finished_by="admin@example.com",
        resolution_note="Fixed <b>safely</b> & checked",
        projection_version=3,
    )
    employees = {"person@example.com": 7, "admin@example.com": 8}
    projection = build_projection(
        row, images={}, employee_lookup=employees.get,
        start_type="datetime", stop_type="datetime",
    )
    assert projection.fields["x_studio_type"] == "Digital - New Feature"
    assert projection.fields["x_studio_status"] == "Completed"
    assert projection.fields["x_studio_submitted_by"] == 7
    assert projection.fields["x_studio_completed_by"] == 8
    assert projection.fields["x_studio_date_stop"] == "2026-08-21 02:00:00"
    assert projection.fields["x_studio_notes"] == "<p>Fixed &lt;b&gt;safely&lt;/b&gt; &amp; checked</p>"


def test_missing_optional_values_and_remote_sync_tokens_are_never_emitted():
    projection = build_projection(
        feedback(task_type=None), images={}, employee_lookup=lambda email: None,
        start_type="date", stop_type="date",
    )
    assert projection.fields["x_studio_type"] == "Digital"
    assert not {"x_studio_submitted_by", "x_studio_image", "x_studio_after_image"} & projection.fields.keys()
    assert all("token" not in key.lower() for key in projection.fields)
    assert all("token" not in key.lower() for key in projection.manifest)
```

- [ ] **Step 2: Run the projection tests and observe the missing-module failure**

```bash
pytest tests/test_feedback_projection.py -v
```

Expected: collection fails because `feedback_projection` does not exist.

- [ ] **Step 3: Implement deterministic mapping and canonical manifest digest**

Create frozen `BinaryEvidence` and `Projection` dataclasses. `Projection.fields`
contains nonbinary values only; `dispatch_fields()` creates a fresh dict and
adds base64 only in memory immediately before RPC:

```python
@dataclass(frozen=True)
class BinaryEvidence:
    jpeg_bytes: bytes
    sha256: str
    byte_length: int


@dataclass(frozen=True)
class Projection:
    source_id: str
    fields: dict[str, object]
    binaries: dict[str, BinaryEvidence]
    manifest: dict[str, object]
    manifest_digest: str

    def dispatch_fields(self) -> dict[str, object]:
        values = dict(self.fields)
        for field_name, evidence in self.binaries.items():
            values[field_name] = base64.b64encode(evidence.jpeg_bytes).decode("ascii")
        return values
```

Use these exact mappings:

```python
STATUS_VALUES = {
    "requested": "Requested",
    "in_progress": "In-Progress",
    "completed": "Completed",
    "declined": "Declined",
}
TYPE_VALUES = {"bug": "Digital", "feature": "Digital - New Feature", None: "Digital"}


def source_id_for(feedback_id: int) -> str:
    if type(feedback_id) is not int or feedback_id <= 0:
        raise ValueError("feedback id must be a positive integer")
    return f"GPI-PM-FB-{feedback_id}"


def _odoo_time(value: datetime, field_type: str) -> str:
    aware = value.astimezone(UTC)
    if field_type == "datetime":
        return aware.strftime("%Y-%m-%d %H:%M:%S")
    if field_type == "date":
        return value.astimezone(ZoneInfo("America/Chicago")).date().isoformat()
    raise ValueError("Odoo date field must be date or datetime")


def _manifest(fields: dict, binaries: dict[str, BinaryEvidence]) -> tuple[dict, str]:
    value = {
        "fields": fields,
        "binary_evidence": {
            name: {"sha256": item.sha256, "byte_length": item.byte_length}
            for name, item in sorted(binaries.items())
        },
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return value, hashlib.sha256(encoded).hexdigest()
```

`build_projection` escapes the note with `html.escape(note, quote=True)` and wraps it in one `<p>`. Its `fields` contain nonbinary values only; `Projection.dispatch_fields()` adds base64 image strings to a fresh in-memory dictionary only for dispatch. Manifest `fields` excludes the two binary strings and `binary_evidence` carries only hash/length, so no stored object accidentally serializes raw image/base64 into JSON.

- [ ] **Step 4: Implement exact normalized employee resolution**

Add:

```python
def normalize_email(value: str | None) -> str | None:
    cleaned = (value or "").strip().casefold()
    return cleaned or None


def resolve_employee_id(client, email: str | None, *, feedback_id: int, warn) -> int | None:
    normalized = normalize_email(email)
    if normalized is None:
        return None
    rows = client.find_employees_by_email(normalized, limit=3)
    exact = [row for row in rows if normalize_email(row.get("work_email")) == normalized]
    if len(exact) == 1:
        return int(exact[0]["id"])
    warn(feedback_id, "employee_missing" if not exact else "employee_ambiguous")
    return None
```

The client wrapper uses `context={"active_test": False}`, fields `id,work_email`, a case-insensitive equality domain, and `limit=3`. Warning persistence/logging contains only local feedback ID and warning class.

- [ ] **Step 5: Add full readback and binary comparison tests**

Add tests that Odoo many2one `[id, label]` compares by ID, scalar/date/HTML/selection strings compare exactly, full binary base64 decodes with `validate=True`, and length/hash mismatches fail verification:

```python
def test_verify_readback_compares_full_binary_hash_and_length():
    projection = projection_with_before(b"safe-jpeg")
    remote = dict(projection.fields)
    remote["x_studio_image"] = base64.b64encode(b"safe-jpeg").decode("ascii")
    verify_readback(projection, remote)
    remote["x_studio_image"] = base64.b64encode(b"other").decode("ascii")
    with pytest.raises(ReadbackMismatch, match="x_studio_image"):
        verify_readback(projection, remote)
```

- [ ] **Step 6: Run projection tests**

```bash
pytest tests/test_feedback_projection.py tests/test_feedback_image.py -v
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit and push projection logic**

```bash
git add CHANGELOG.md src/zira_dashboard/feedback_projection.py src/zira_dashboard/feedback_store.py tests/test_feedback_projection.py
git commit -m "feat(feedback): map exact Odoo improvement projections"
git push origin main
```

### Task 7: Durable Claims, Immutable Attempts, Retry, and Settlement

**Files:**

- Create: `src/zira_dashboard/feedback_sync_store.py`
- Modify: `tests/test_feedback_sync_store.py`

**Interfaces:**

- Consumes: Task 1 tables and Task 6 manifest/evidence values.
- Produces: `Claim`, `Attempt`, `claim_due`, `prepare_attempt`, `defer_prepared_for_closed_gate`, `mark_dispatch`, `mark_rpc_succeeded`, `schedule_readback`, `settle_verified`, `record_definitive_failure`, `quarantine`, and `recover_expired_claims`.

- [ ] **Step 1: Write failing SQL-shape and state-transition tests**

Add tests with mocked cursors that assert:

```python
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import UUID

from zira_dashboard import feedback_sync_store as store


class RecordingCursor(AbstractContextManager):
    def __init__(self, *, fetchone=None, fetchall=None):
        self._fetchone = fetchone
        self._fetchall = [] if fetchall is None else fetchall
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall


def recording_cursor(**values):
    return RecordingCursor(**values)


def aware_now():
    return datetime(2026, 8, 20, 18, 0, tzinfo=UTC)


def claim(version=1, remote_id=None):
    return store.Claim(
        feedback_id=17,
        desired_version=version,
        last_synced_version=0,
        odoo_improvement_id=remote_id,
        claim_owner="worker-a",
        claim_token=UUID("11111111-1111-1111-1111-111111111111"),
        claim_expires_at=aware_now() + timedelta(minutes=5),
        active_attempt_id=None,
        attempt_count=0,
    )


def attempt_row(**changes):
    row = {
        "attempt_id": UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        "feedback_id": 17,
        "projection_version": 1,
        "mutation_kind": "update",
        "remote_id": 77,
        "manifest": {"fields": {"x_name": "Safe"}, "binary_evidence": {}},
        "manifest_digest": "a" * 64,
        "before_sha256": None,
        "before_byte_length": None,
        "after_sha256": None,
        "after_byte_length": None,
        "state": "prepared",
        "created_at": aware_now(),
        "updated_at": aware_now(),
    }
    row.update(changes)
    return row


def test_claim_due_uses_skip_locked_and_canary_filter(monkeypatch):
    cursor = recording_cursor(fetchall=[])
    monkeypatch.setattr(store.db, "cursor", lambda: cursor)
    assert store.claim_due(
        now=aware_now(), worker_id="worker-a", limit=10, canary_feedback_id=17
    ) == []
    sql = " ".join(cursor.executions[0][0].split())
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "s.feedback_id = %s" in sql
    assert "LIMIT %s" in sql


def test_prepare_attempt_persists_exact_manifest_before_dispatch(monkeypatch):
    cursor = recording_cursor(fetchone=attempt_row(state="prepared"))
    monkeypatch.setattr(store.db, "cursor", lambda: cursor)
    attempt = store.prepare_attempt(
        claim=claim(version=3),
        attempt_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        mutation_kind="update",
        remote_id=77,
        manifest={"fields": {"x_name": "Safe"}, "binary_evidence": {}},
        manifest_digest="a" * 64,
        binaries={},
        now=aware_now(),
    )
    insert_sql = " ".join(cursor.executions[0][0].split())
    assert "INSERT INTO feedback_odoo_attempts" in insert_sql
    assert attempt.state == "prepared"


def test_settlement_cannot_overwrite_newer_desired_version(monkeypatch):
    execute = MagicMock(return_value=[])
    monkeypatch.setattr(store.db, "query", execute)
    assert store.settle_verified(claim=claim(version=2), remote_id=77, now=aware_now()) is False
    sql = " ".join(execute.call_args.args[0].split())
    assert "desired_version = %s" in sql
    assert "claim_token = %s" in sql
    assert "active_attempt_id = %s" in sql
```

- [ ] **Step 2: Run store tests and observe missing-interface failures**

```bash
pytest tests/test_feedback_sync_store.py -v
```

Expected: failures identify each missing dataclass/function.

- [ ] **Step 3: Implement bounded short-transaction claiming**

Define frozen `Claim` and `Attempt` dataclasses and implement `claim_due` in one transaction. Generate claim UUIDs in Python; do not add or assume a PostgreSQL extension:

```sql
SELECT s.feedback_id, s.desired_version, s.last_synced_version,
       s.odoo_improvement_id, s.active_attempt_id, s.attempt_count
  FROM feedback_odoo_sync s
  WHERE s.state = 'idle'
    AND s.due_at <= %s
    AND s.last_synced_version < s.desired_version
    AND (%s::bigint IS NULL OR s.feedback_id = %s)
  ORDER BY s.due_at, s.feedback_id
  FOR UPDATE SKIP LOCKED
  LIMIT %s;
```

For each selected row, create `claim_token = uuid4()` and execute this guarded update before leaving the same transaction:

```sql
UPDATE feedback_odoo_sync
SET state = 'in_flight', claim_owner = %s, claim_token = %s,
    claim_expires_at = %s, updated_at = %s
WHERE feedback_id = %s AND state = 'idle'
RETURNING feedback_id, desired_version, last_synced_version,
          odoo_improvement_id, claim_owner, claim_token,
          claim_expires_at, active_attempt_id, attempt_count;
```

- [ ] **Step 4: Implement attempt progression and safe recovery**

Use guarded updates with all of `feedback_id`, `claim_token`, `active_attempt_id`, and expected state. Required behavior:

```python
RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=2),
    timedelta(minutes=4),
    timedelta(minutes=8),
    timedelta(minutes=16),
    timedelta(minutes=32),
    timedelta(minutes=60),
)
MAX_MUTATION_ATTEMPTS = 8


def retry_due(now: datetime, attempt_count: int) -> datetime:
    if attempt_count <= 0:
        raise ValueError("attempt count must be positive")
    index = min(attempt_count - 1, len(RETRY_DELAYS) - 1)
    return now + RETRY_DELAYS[index]
```

- `prepare_attempt` inserts the immutable row and sets `active_attempt_id` while still `in_flight`.
- `defer_prepared_for_closed_gate` releases the claim to `idle`, retains the prepared active attempt, leaves `attempt_count` unchanged, and makes no Odoo call.
- `mark_dispatch` commits `dispatch_marked` before any RPC.
- `mark_rpc_succeeded` requires a valid positive remote ID for create, records `rpc_succeeded_at`, and persists the adopted/created ID.
- `schedule_readback` releases the claim to `idle` while retaining the same `active_attempt_id` in `rpc_succeeded`; its next worker action is readback only.
- `settle_verified` marks the attempt `verified`, advances `last_synced_version`, and either leaves the sync row idle/due immediately when desired is newer or clears claim fields when current.
- `record_definitive_failure` increments mutation attempts and applies the exact delay; attempt eight quarantines with `retry_exhausted`.
- `quarantine` sets both attempt and sync state without deleting evidence.
- `recover_expired_claims` releases only claims with no attempt or a `prepared` attempt; any expired `dispatch_marked` attempt becomes ambiguous/quarantined; `rpc_succeeded` becomes idle for readback-only recovery.

- [ ] **Step 5: Add safe local-Postgres concurrency tests**

Following the guarded pattern in `tests/test_payroll_work_entry_store.py`, run only when `DATABASE_URL` targets loopback, the database name ends in `_test`, and `FEEDBACK_SYNC_TEST_DATABASE=1`. Start two threads, have both call `claim_due(limit=1)`, and assert exactly one receives the row. Then advance `desired_version` while the first claim is active and assert settling version 1 does not mark version 2 synchronized.

- [ ] **Step 6: Run unit and opted-in DB tests**

```bash
pytest tests/test_feedback_sync_store.py -v
```

Expected locally without the explicit test database: unit tests pass and DB concurrency tests skip with a clear reason. In an approved local `_test` database, rerun with `FEEDBACK_SYNC_TEST_DATABASE=1` and expect both concurrency tests to pass.

- [ ] **Step 7: Commit and push the durable sync store**

```bash
git add CHANGELOG.md src/zira_dashboard/feedback_sync_store.py tests/test_feedback_sync_store.py
git commit -m "feat(feedback): add durable Odoo sync claims"
git push origin main
```

### Task 8: Sequential Lookup, Adoption, Mutation, and Exact Readback Worker

**Files:**

- Create: `src/zira_dashboard/feedback_sync.py`
- Create: `tests/test_feedback_sync.py`
- Modify: `src/zira_dashboard/feedback_store.py`
- Modify: `src/zira_dashboard/odoo_improvements.py`

**Interfaces:**

- Consumes: `ImprovementsClient`, `Projection`, and sync-store claims/attempts.
- Produces: `run_batch(now, worker_id, limit=10) -> BatchResult` and `process_claim(...)` with zero/one/duplicate adoption, exact verification, definitive retry, and ambiguity quarantine.

- [ ] **Step 1: Write failing behavior tests for lookup and adoption**

Create these deterministic helpers at the top of `tests/test_feedback_sync.py`,
then use monkeypatch to replace sync-store functions with `MagicMock` instances
inside each test:

```python
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from zira_dashboard import feedback_sync_store as sync_store
from zira_dashboard.odoo_improvements import MalformedMutationResponse


def aware_now():
    return datetime(2026, 8, 20, 18, 0, tzinfo=UTC)


def claim(remote_id=None, version=1):
    return sync_store.Claim(
        feedback_id=17,
        desired_version=version,
        last_synced_version=0,
        odoo_improvement_id=remote_id,
        claim_owner="worker-a",
        claim_token=UUID("11111111-1111-1111-1111-111111111111"),
        claim_expires_at=aware_now() + timedelta(minutes=5),
        active_attempt_id=None,
        attempt_count=0,
    )


def attempt(state):
    return sync_store.Attempt(
        attempt_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        feedback_id=17,
        projection_version=1,
        mutation_kind="create",
        remote_id=901 if state == "rpc_succeeded" else None,
        manifest={"fields": {"x_name": "Safe"}, "binary_evidence": {}},
        manifest_digest="a" * 64,
        binaries={},
        state=state,
    )


def dispatch_marked_attempt():
    return attempt("dispatch_marked")


def rpc_succeeded_attempt():
    return attempt("rpc_succeeded")


@dataclass(frozen=True)
class ReadCall:
    fields: set[str]


class FakeClient:
    def __init__(
        self,
        *,
        exact_rows=None,
        create_result=901,
        create_error=None,
        remote_matches=True,
    ):
        self.exact_rows = [] if exact_rows is None else exact_rows
        self.create_result = create_result
        self.create_error = create_error
        self.remote_matches = remote_matches
        self.calls = []
        self.mutation_calls = []
        self.create_fields = {}
        self.write_fields = {}
        self.write_id = None
        self.read_calls = []
        self.read_count = 0

    def find_exact(self, source_id):
        self.calls.append(("find_exact", source_id))
        return self.exact_rows

    def create_improvement(self, fields, *, feedback_id, expected_contract):
        self.mutation_calls.append("create")
        self.create_fields = fields
        if self.create_error is not None:
            raise self.create_error
        return self.create_result

    def write_improvement(self, remote_id, fields, *, feedback_id, expected_contract):
        self.mutation_calls.append("write")
        self.write_id = remote_id
        self.write_fields = fields

    def read_improvement(self, remote_id, fields, *, full_binary):
        self.read_count += 1
        self.read_calls.append(ReadCall(set(fields)))
        return {"id": remote_id, "x_name": "Safe"} if self.remote_matches else {"id": remote_id}
```

The per-test fixture must stub `feedback_store.projection_snapshot`,
`build_projection_from_snapshot`, `sync_store.load_active_attempt`,
`prepare_attempt`, `mark_dispatch`, `mark_rpc_succeeded`, `settle_verified`,
`schedule_readback`, `record_definitive_failure`, and `quarantine` with exact
return objects/call recorders needed by that test. Keep the real control flow in
`process_claim`; never mock the method under test.

Then add:

```python
def test_zero_match_creates_then_reads_back_every_written_field():
    client = FakeClient(exact_rows=[], create_result=901)
    result = process_claim(claim(), client=client, now=aware_now())
    assert result == "verified"
    assert client.calls[0] == ("find_exact", "GPI-PM-FB-17")
    assert client.create_fields["x_studio_source"] == "GPI Plant Manager"
    assert client.read_calls[-1].fields == set(client.create_fields)


def test_one_match_adopts_and_updates_without_clearing_absent_optionals():
    client = FakeClient(exact_rows=[{"id": 901, "x_studio_source": "GPI Plant Manager", "x_studio_source_id": "GPI-PM-FB-17"}])
    process_claim(claim(remote_id=None), client=client, now=aware_now())
    assert client.write_id == 901
    assert "x_studio_image" not in client.write_fields
    assert "x_studio_submitted_by" not in client.write_fields


def test_duplicate_exact_matches_quarantine_without_mutation():
    client = FakeClient(exact_rows=[{"id": 1}, {"id": 2}])
    result = process_claim(claim(), client=client, now=aware_now())
    assert result == "quarantined"
    assert client.mutation_calls == []


def test_saved_id_missing_from_compound_lookup_quarantines_ownership_conflict():
    client = FakeClient(exact_rows=[])
    result = process_claim(claim(remote_id=901), client=client, now=aware_now())
    assert result == "quarantined"
    assert client.mutation_calls == []
```

- [ ] **Step 2: Add failing ambiguity and recovery tests**

```python
@pytest.mark.parametrize("error", [TimeoutError(), ConnectionError(), MalformedMutationResponse("bad")])
def test_ambiguous_mutation_quarantines_without_retry(error, monkeypatch):
    retry = MagicMock()
    quarantine = MagicMock()
    monkeypatch.setattr(sync_store, "record_definitive_failure", retry)
    monkeypatch.setattr(sync_store, "quarantine", quarantine)
    client = FakeClient(exact_rows=[], create_error=error)
    result = process_claim(claim(), client=client, now=aware_now())
    assert result == "quarantined"
    retry.assert_not_called()
    quarantine.assert_called_once()


def test_matching_values_do_not_clear_dispatch_marked_ambiguity():
    client = FakeClient(remote_matches=True)
    result = _recover_active(
        claim(), dispatch_marked_attempt(), client=client, now=aware_now()
    )
    assert result == "quarantined"
    assert client.read_count == 0


def test_rpc_succeeded_attempt_may_settle_from_fresh_matching_readback():
    client = FakeClient(remote_matches=True)
    result = _recover_active(
        claim(), rpc_succeeded_attempt(), client=client, now=aware_now()
    )
    assert result == "verified"
    assert client.read_count == 1
    assert client.mutation_calls == []
```

- [ ] **Step 3: Run worker tests and observe missing worker failures**

```bash
pytest tests/test_feedback_sync.py -v
```

Expected: collection fails because `feedback_sync` does not exist.

- [ ] **Step 4: Implement one sequential claim processor**

Add `feedback_store.projection_snapshot(feedback_id: int,
projection_version: int) -> ProjectionSnapshot`. It performs one short local
transaction, reads the feedback row plus its two image rows, requires the stored
version to equal the claimed version, copies all bytes into a frozen snapshot,
and closes the transaction. Add this adapter in `feedback_sync.py`:

```python
def build_projection_from_snapshot(snapshot, *, client, contract):
    def employee_lookup(email):
        return resolve_employee_id(
            client,
            email,
            feedback_id=snapshot.feedback["id"],
            warn=lambda feedback_id, warning_class: feedback_store.record_sync_warning(
                feedback_id,
                snapshot.feedback["projection_version"],
                warning_class,
            ),
        )

    return build_projection(
        snapshot.feedback,
        images=snapshot.images,
        employee_lookup=employee_lookup,
        start_type=contract.start_type,
        stop_type=contract.stop_type,
    )
```

`record_sync_warning(feedback_id, projection_version, warning_class)` stores only
those three safe values; it never stores or logs an email address. The version
must come from the immutable snapshot, not a fresh read of the mutable feedback
row. Then use this control flow;
every store function opens and closes its own short transaction:

```python
def process_claim(claim: Claim, *, client: ImprovementsClient, now: datetime) -> str:
    active = sync_store.load_active_attempt(claim)
    if active is not None:
        return _recover_active(claim, active, client=client, now=now)

    snapshot = feedback_store.projection_snapshot(claim.feedback_id, claim.desired_version)
    contract = client.read_contract()
    projection = build_projection_from_snapshot(snapshot, client=client, contract=contract)
    rows = client.find_exact(projection.source_id)
    if len(rows) > 1:
        sync_store.quarantine(claim, "duplicate_compound_identity", now)
        return "quarantined"
    if claim.odoo_improvement_id is not None:
        if not rows or int(rows[0]["id"]) != claim.odoo_improvement_id:
            sync_store.quarantine(claim, "saved_id_ownership_conflict", now)
            return "quarantined"
        remote_id = claim.odoo_improvement_id
        mutation_kind = "update"
    elif rows:
        remote_id = int(rows[0]["id"])
        mutation_kind = "update"
    else:
        remote_id = None
        mutation_kind = "create"

    attempt = sync_store.prepare_attempt(
        claim=claim,
        attempt_id=uuid4(),
        mutation_kind=mutation_kind,
        remote_id=remote_id,
        manifest=projection.manifest,
        manifest_digest=projection.manifest_digest,
        binaries=projection.binaries,
        now=now,
    )
    try:
        client.assert_mutation_allowed(claim.feedback_id)
    except GateClosed:
        sync_store.defer_prepared_for_closed_gate(claim, attempt, now)
        return "deferred"
    sync_store.mark_dispatch(claim, attempt, now)
    try:
        if mutation_kind == "create":
            remote_id = client.create_improvement(
                projection.dispatch_fields(),
                feedback_id=claim.feedback_id,
                expected_contract=contract,
            )
        else:
            client.write_improvement(
                remote_id,
                projection.dispatch_fields(),
                feedback_id=claim.feedback_id,
                expected_contract=contract,
            )
        sync_store.mark_rpc_succeeded(claim, attempt, remote_id, now)
    except GateClosed:
        sync_store.record_definitive_failure(
            claim, attempt, "gate_closed_before_rpc", "mutation was not called", now
        )
        return "retry_scheduled"
    except (TargetIdentityError, ContractError):
        sync_store.quarantine(
            claim, "target_identity_or_contract_mismatch", now, attempt=attempt
        )
        return "quarantined"
    except xmlrpc.client.Fault as error:
        sync_store.record_definitive_failure(claim, attempt, "odoo_fault", safe_fault(error), now)
        return "retry_scheduled"
    except (TimeoutError, ConnectionError, OSError, MalformedMutationResponse) as error:
        sync_store.quarantine(claim, "ambiguous_mutation", safe_class(error), now, attempt=attempt)
        return "quarantined"
    return _verify_rpc_succeeded(claim, attempt, projection, remote_id, client=client, now=now)
```

If persisting successful-RPC evidence raises, attempt a best-effort local quarantine and re-raise; the durable `dispatch_marked` state ensures restart recovery quarantines rather than repeats. Never log the exception string when it could contain remote payload data.

- [ ] **Step 5: Implement exact readback-only recovery**

`_verify_rpc_succeeded` requests exactly every written field, including full binary values with `bin_size=False`, calls `verify_readback`, then `settle_verified`. A read transport failure calls `schedule_readback` without another mutation. A readback mismatch quarantines. `_recover_active` rules are:

```python
if attempt.state == "prepared":
    return _dispatch_prepared_once(claim, attempt, client=client, now=now)
if attempt.state == "dispatch_marked":
    sync_store.quarantine(claim, "ambiguous_stale_dispatch", now, attempt=attempt)
    return "quarantined"
if attempt.state == "rpc_succeeded":
    return _verify_saved_projection_only(claim, attempt, client=client, now=now)
raise RuntimeError("active attempt state is not recoverable")
```

The saved manifest is the authority for recovery; do not rebuild from newer feedback. Reconstruct binary dispatch/readback evidence from the immutable local image whose saved hash/length must still match the attempt. If it does not, quarantine `local_binary_evidence_changed`.

- [ ] **Step 6: Implement bounded sequential batches**

```python
@dataclass(frozen=True)
class BatchResult:
    attempted: int = 0
    verified: int = 0
    deferred: int = 0
    retry_scheduled: int = 0
    quarantined: int = 0
    skipped: str | None = None

    @classmethod
    def from_outcomes(cls, outcomes: list[str]) -> "BatchResult":
        return cls(
            attempted=len(outcomes),
            verified=outcomes.count("verified"),
            deferred=outcomes.count("deferred"),
            retry_scheduled=outcomes.count("retry_scheduled"),
            quarantined=outcomes.count("quarantined"),
        )


def run_batch(now: datetime | None = None, worker_id: str | None = None, limit: int = 10) -> BatchResult:
    current = now or datetime.now(UTC)
    identity = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    capped = max(1, min(int(limit), 10))
    client = ImprovementsClient.from_env()
    client.assert_worker_enabled()
    sync_store.recover_expired_claims(current)
    claims = sync_store.claim_due(
        now=current,
        worker_id=identity,
        limit=capped,
        canary_feedback_id=client.canary_feedback_id(),
    )
    outcomes = [process_claim(item, client=client, now=current) for item in claims]
    return BatchResult.from_outcomes(outcomes)
```

`assert_worker_enabled` checks gates without authenticating or making any RPC.

- [ ] **Step 7: Run full worker/projection/client tests**

```bash
pytest tests/test_feedback_sync.py tests/test_feedback_sync_store.py tests/test_feedback_projection.py tests/test_odoo_improvements.py -v
```

Expected: all selected tests pass, including ambiguity and no-mutation recovery assertions.

- [ ] **Step 8: Commit and push the dark worker core**

```bash
git add CHANGELOG.md src/zira_dashboard/feedback_sync.py src/zira_dashboard/feedback_store.py src/zira_dashboard/odoo_improvements.py tests/test_feedback_sync.py
git commit -m "feat(feedback): add verified Odoo mirror worker"
git push origin main
```

### Task 9: Dark 60-Second Worker Wiring and Closed-Gate Proof

**Files:**

- Modify: `src/zira_dashboard/app.py`
- Create: `tests/test_feedback_warmer.py`
- Modify: `tests/test_feedback_sync.py`

**Interfaces:**

- Consumes: `feedback_sync.run_batch` from Task 8 and the existing `_WARMERS` registry.
- Produces: one 60-second dark worker tick that performs zero claims and zero Odoo calls with either gate closed.

- [ ] **Step 1: Write failing closed-gate and warmer registration tests**

Create `tests/test_feedback_warmer.py`:

```python
import asyncio
from unittest.mock import MagicMock

import pytest

from zira_dashboard import app as app_module
from zira_dashboard import feedback_sync


def test_feedback_warmer_is_registered_once_at_sixty_seconds():
    matches = [item for item in app_module._WARMERS if item[0] == "feedback Odoo mirror"]
    assert len(matches) == 1
    assert matches[0][2] == 60


def test_feedback_tick_runs_batch_off_event_loop(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr(feedback_sync, "run_batch", run)
    asyncio.run(app_module._tick_feedback_sync())
    run.assert_called_once_with()
```

Add to `tests/test_feedback_sync.py`:

```python
def set_or_delete(monkeypatch, name, value):
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize(
    ("master", "improvements"),
    [(None, None), ("true", None), (None, "true"), ("TRUE", "true")],
)
def test_closed_gates_prevent_recovery_claims_and_odoo_calls(
    monkeypatch, master, improvements
):
    set_or_delete(monkeypatch, "ODOO_SHARED_REPORTING_WRITE_ENABLED", master)
    set_or_delete(monkeypatch, "ODOO_IMPROVEMENTS_WRITE_ENABLED", improvements)
    claim = MagicMock(side_effect=AssertionError("must not claim"))
    recover = MagicMock(side_effect=AssertionError("must not recover"))
    executor = MagicMock(side_effect=AssertionError("must not call Odoo"))
    monkeypatch.setattr(sync_store, "claim_due", claim)
    monkeypatch.setattr(sync_store, "recover_expired_claims", recover)
    monkeypatch.setattr(ImprovementsClient, "default_executor", executor)
    result = run_batch()
    assert result.skipped == "write_gates_closed"
```

- [ ] **Step 2: Run the tests and observe registration/closed-gate failures**

```bash
pytest tests/test_feedback_warmer.py tests/test_feedback_sync.py -v
```

Expected: the warmer is absent and/or `run_batch` does work before its gate check.

- [ ] **Step 3: Put the gate check before client configuration, recovery, and claim work**

At the start of `run_batch`, use an environment-only helper:

```python
def worker_write_enabled() -> bool:
    return (
        os.environ.get("ODOO_SHARED_REPORTING_WRITE_ENABLED") == "true"
        and os.environ.get("ODOO_IMPROVEMENTS_WRITE_ENABLED") == "true"
    )


def run_batch(now: datetime | None = None, worker_id: str | None = None, limit: int = 10) -> BatchResult:
    if not worker_write_enabled():
        return BatchResult(skipped="write_gates_closed")
    current = now or datetime.now(UTC)
    identity = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    client = ImprovementsClient.from_env()
    canary = client.canary_feedback_id()
    sync_store.recover_expired_claims(current)
    claims = sync_store.claim_due(
        now=current,
        worker_id=identity,
        limit=max(1, min(int(limit), 10)),
        canary_feedback_id=canary,
    )
    outcomes = [process_claim(item, client=client, now=current) for item in claims]
    return BatchResult.from_outcomes(outcomes)
```

This order is required: no config parsing, database claim/recovery, authentication, or Odoo call occurs while either gate is closed.

- [ ] **Step 4: Register the app warmer**

Add to `app.py`:

```python
async def _tick_feedback_sync():
    """Mirror due local feedback versions to Odoo when both exact gates are open."""
    from . import feedback_sync

    await asyncio.to_thread(feedback_sync.run_batch)
```

Add exactly `("feedback Odoo mirror", _tick_feedback_sync, 60)` to `_WARMERS`. Do not add a startup preflight, backfill, or direct Odoo call.

- [ ] **Step 5: Run warmer, app, and worker regressions**

```bash
pytest tests/test_feedback_warmer.py tests/test_feedback_sync.py tests/test_goat_notification_warmer.py tests/test_machine_breakdown_warmer.py -v
pytest --collect-only -q
```

Expected: all existing modules pass; collection succeeds; closed gates prove zero claims/calls.

- [ ] **Step 6: Commit and push the dark wiring**

```bash
git add CHANGELOG.md src/zira_dashboard/app.py src/zira_dashboard/feedback_sync.py tests/test_feedback_warmer.py tests/test_feedback_sync.py
git commit -m "feat(feedback): wire dark Odoo mirror worker"
git push origin main
```

### Task 10: Legacy Lifecycle Provenance and Bounded Dry-Run/Backfill Engine

**Files:**

- Create: `src/zira_dashboard/feedback_rollout.py`
- Create: `tests/test_feedback_rollout.py`
- Modify: `src/zira_dashboard/feedback_store.py`
- Modify: `src/zira_dashboard/odoo_improvements.py`
- Modify: `tests/test_feedback_mine_route.py`

**Interfaces:**

- Consumes: local legacy `odoo_task_id`, restricted `project.task.read`, exact projection builder, compound lookup, and outbox tables.
- Produces: read-only `preflight`, bounded `dry_run_batch`, `propose_legacy_status`, explicit `apply_legacy_batch`, bounded `enqueue_history_batch`, and local `reconciliation_counts`.

- [ ] **Step 1: Write failing legacy mapping and omission tests**

Create `tests/test_feedback_rollout.py`:

```python
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from zira_dashboard import feedback_store
from zira_dashboard.feedback_rollout import (
    EnqueueReport,
    apply_legacy_batch,
    dry_run_batch,
    enqueue_history_batch,
    propose_legacy_status,
    reconciliation_counts,
)


def aware_now():
    return datetime(2026, 8, 20, 18, 0, tzinfo=UTC)


class FakeClient:
    def __init__(self):
        self.create_calls = []
        self.write_calls = []


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("New", "requested"),
        ("Waiting", "requested"),
        ("In Progress", "in_progress"),
        ("Done", "completed"),
        ("Rejected", "declined"),
    ],
)
def test_legacy_stage_mapping(stage, expected):
    assert propose_legacy_status(stage) == expected


def test_legacy_terminal_migration_does_not_invent_terminal_details(monkeypatch):
    saved = {}
    monkeypatch.setattr(
        feedback_store,
        "apply_legacy_status",
        lambda **values: saved.update(values),
    )
    apply_legacy_batch(
        rows=[{"id": 7, "odoo_task_id": 90}],
        stages={90: "Done"},
        now=aware_now(),
    )
    assert saved["status"] == "completed"
    assert saved["lifecycle_origin"] == "legacy_project_task"
    assert saved["finished_at"] is None
    assert saved["finished_by"] is None
    assert saved["resolution_note"] is None
```

- [ ] **Step 2: Write failing bounded and restart-safe tests**

```python
def test_dry_run_is_bounded_and_never_mutates(monkeypatch):
    client = FakeClient()
    local_mutation = MagicMock(side_effect=AssertionError("local mutation attempted"))
    monkeypatch.setattr(feedback_store, "apply_legacy_status", local_mutation)
    monkeypatch.setattr(feedback_store, "feedback_after", lambda after_id, limit: [])
    report = dry_run_batch(after_id=100, batch_size=100, client=client)
    assert report.requested_batch_size == 100
    assert report.next_after_id >= 100
    assert client.create_calls == []
    assert client.write_calls == []
    local_mutation.assert_not_called()


def test_historical_enqueue_advances_cursor_idempotently(monkeypatch):
    responses = iter([
        EnqueueReport(feedback_ids=(1, 2), next_cursor=2),
        EnqueueReport(feedback_ids=(3, 4), next_cursor=4),
    ])
    monkeypatch.setattr(
        feedback_store,
        "enqueue_history_batch",
        lambda batch_size, now: next(responses),
    )
    first = enqueue_history_batch(batch_size=100, now=aware_now())
    second = enqueue_history_batch(batch_size=100, now=aware_now())
    assert set(first.feedback_ids).isdisjoint(second.feedback_ids)
    assert first.next_cursor < second.next_cursor


def test_reconciliation_reports_all_required_buckets(monkeypatch):
    monkeypatch.setattr(
        feedback_store,
        "reconciliation_counts",
        lambda gates_open: {
            "synchronized": 3,
            "due": 2,
            "deferred": 0,
            "in_flight": 1,
            "quarantined": 1,
            "version_lag": 4,
        },
    )
    counts = reconciliation_counts()
    assert set(counts) == {
        "synchronized", "due", "deferred", "in_flight", "quarantined", "version_lag"
    }
```

- [ ] **Step 3: Run rollout tests and observe missing interfaces**

```bash
pytest tests/test_feedback_rollout.py -v
```

Expected: collection fails because rollout functions do not exist.

- [ ] **Step 4: Implement read-only preflight and dry-run reports**

Define frozen report dataclasses with counts and local feedback IDs only. `preflight(client)` calls authentication, fresh target identity/contract validation, and returns:

```python
@dataclass(frozen=True)
class PreflightReport:
    database_uuid_matches: bool
    company_matches: bool
    fields_ok: bool
    missing_fields: tuple[str, ...]
    wrong_types: tuple[str, ...]
    missing_selections: tuple[str, ...]
    source_value_present: bool
    required_source_value: str = "GPI Plant Manager"
```

Add `feedback_store.feedback_after(after_id: int, limit: int) -> list[dict]`
with exact SQL `SELECT ... FROM feedback WHERE id > %s ORDER BY id LIMIT %s`,
where the selected columns are every projection/lifecycle field plus the legacy
task ID. `dry_run_batch(after_id, batch_size, client)` clamps batch size to
1..100, calls that function, optionally batch-reads locally referenced legacy
task stages, builds projections, performs exact compound read-only lookups, and
classifies create/adopt/update/duplicate/ownership-conflict/employee/image
counts. It never calls create/write, changes local lifecycle, inserts sync rows,
or advances a cursor.

- [ ] **Step 5: Implement explicit legacy status persistence**

`feedback_store.apply_legacy_status` uses `WHERE id = %s AND status IS NULL` and sets only:

```sql
status = %s,
lifecycle_origin = 'legacy_project_task',
legacy_lifecycle_migrated_at = %s,
projection_version = projection_version + 1,
updated_at = %s
```

It leaves `finished_at`, `finished_by`, and `resolution_note` null. In the same transaction, insert/update `feedback_odoo_sync` to the new desired version without clearing quarantine. `apply_legacy_batch` accepts at most 100 rows and is idempotent because already-migrated rows do not match the guarded update.

- [ ] **Step 6: Implement restart-safe historical enqueue and counts**

Inside one transaction, lock singleton `feedback_odoo_backfill_state`, select the next 1..100 migrated/local-authority feedback IDs after its cursor, upsert sync rows without lowering `desired_version` or clearing quarantine, then advance the cursor to the largest selected ID. No Odoo call occurs in this function.

`reconciliation_counts` is local-only SQL. Derive `deferred` as all unsynchronized nonquarantined rows when either exact gate is closed; otherwise those rows remain `due` based on `due_at`. Include total positive `desired_version - last_synced_version` as `version_lag`.

- [ ] **Step 7: Preserve legacy live status only until migration**

Keep `/api/feedback/mine` behavior from Task 3: only rows with `status is None` may use live legacy task stages. Add a test that after `status='completed'` is persisted, the route does not call `fetch_task_stage_names` even when `odoo_task_id` remains present.

- [ ] **Step 8: Run rollout, route, store, projection, and client tests**

```bash
pytest tests/test_feedback_rollout.py tests/test_feedback_mine_route.py tests/test_feedback_store.py tests/test_feedback_projection.py tests/test_odoo_improvements.py -v
```

Expected: all selected tests pass; dry-run fakes record no mutations.

- [ ] **Step 9: Commit and push rollout logic without invoking it**

```bash
git add CHANGELOG.md src/zira_dashboard/feedback_rollout.py src/zira_dashboard/feedback_store.py src/zira_dashboard/odoo_improvements.py tests/test_feedback_rollout.py tests/test_feedback_mine_route.py
git commit -m "feat(feedback): add bounded Odoo rollout analysis"
git push origin main
```

### Task 11: Explicit Rollout CLI and Operations Documentation

**Files:**

- Create: `scripts/feedback_odoo_rollout.py`
- Create: `docs/odoo-2s-feedback-operations.md`
- Modify: `src/zira_dashboard/feedback_sync_store.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `tests/test_feedback_rollout.py`

**Interfaces:**

- Consumes: rollout functions from Task 10 and sync-store quarantine evidence.
- Produces: explicit inert-by-default operator commands and the complete approval/runbook contract.

- [ ] **Step 1: Write failing CLI safety tests**

Add subprocess-free parser/main tests:

```python
from scripts import feedback_odoo_rollout as cli


def test_preflight_and_dry_run_require_explicit_read_only_ack(monkeypatch):
    for argv in (["preflight"], ["dry-run", "--batch-size", "10"]):
        with pytest.raises(SystemExit):
            cli.main(argv)


def test_enqueue_requires_explicit_local_backfill_ack(monkeypatch):
    with pytest.raises(SystemExit):
        cli.main(["enqueue-history", "--batch-size", "10"])


def test_cli_never_accepts_credentials_or_gate_values_as_arguments():
    help_text = cli.build_parser().format_help()
    assert "api-key" not in help_text.lower()
    assert "password" not in help_text.lower()
    assert "write-enabled" not in help_text.lower()


def test_quarantine_cannot_auto_clear_from_matching_values(monkeypatch):
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["quarantine", "clear-if-matching", "--attempt-id", str(uuid4())])
```

- [ ] **Step 2: Run CLI tests and observe missing parser failures**

```bash
pytest tests/test_feedback_rollout.py -v
```

Expected: the new CLI tests fail because the script/parser does not exist.

- [ ] **Step 3: Implement the explicit command surface**

Create an `argparse` parser with only these subcommands:

```text
preflight --confirm-read-only
dry-run --confirm-read-only --after-id N --batch-size 1..100
migrate-legacy --confirm-read-only --confirm-local-migration --after-id N --batch-size 1..100
enqueue-history --confirm-local-backfill --batch-size 1..100
reconcile
canary-report --confirm-read-only --feedback-id N
quarantine-list
quarantine-disposition --attempt-id UUID --disposition keep|release-definitive|supersede-and-retry --reviewer NAME --confirm-human-review
```

The parser must not accept URL, database, login, API key, expected identity, gate, or Source overrides. Those come only from environment configuration.

Implement the acknowledgment guard exactly:

```python
def require_flag(value: bool, message: str) -> None:
    if value is not True:
        raise SystemExit(message)
```

`preflight`, `dry-run`, `migrate-legacy`, and `canary-report` require `--confirm-read-only` before constructing an Odoo client. `migrate-legacy` additionally requires `--confirm-local-migration`. `enqueue-history` requires `--confirm-local-backfill`. `reconcile` and quarantine listing read only local state.

Add narrow quarantine-listing and disposition functions to
`feedback_sync_store.py`. The CLI validates arguments and formats reports, but
all row locking, state validation, append-only operator-audit insertion, and
desired-version changes remain in short sync-store transactions.

Normalize `--reviewer` by trimming it and reject blank values. Store it only in
the local operator-action audit; do not print it in reports. Quarantine
dispositions enforce:

- `keep`: annotate reviewed-at/reviewer locally; state remains quarantined.
- `release-definitive`: only an attempt in `prepared` or `definitive_failed`; never an ambiguous/dispatch-marked attempt.
- `supersede-and-retry`: only after `--confirm-human-review`; retain the old ambiguous attempt forever, append an operator audit row, create a newer local desired version, and leave the row idle/due. Print a duplicate-risk warning. Do not make an Odoo call in the command.

No disposition may mark synchronized based only on current remote values.

- [ ] **Step 4: Write the complete operations runbook**

`docs/odoo-2s-feedback-operations.md` must include these sections with copy-paste commands that never contain values/secrets:

1. Architecture and shared-table namespace.
2. Environment variable names and exact gate semantics.
3. Permanent allowlist and denied operations.
4. Dark deployment with improvements gate off.
5. Approval checkpoint for `preflight --confirm-read-only`.
6. Missing Source response: Dale adds exact stored value `GPI Plant Manager`; the app never adds it.
7. Approval checkpoint for bounded `dry-run` batches.
8. Dry-run report interpretation: fields, selections, duplicates, ownership, employee, images, projected counts.
9. Approval checkpoint and exact setup for one `ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID`.
10. Opening both gates for the named canary, exact readback evidence, closing the improvements gate immediately after.
11. Separate approval to remove the canary fence and enable live writes.
12. Separate approval for legacy migration and historical enqueue batches.
13. Reconciliation counts and version lag.
14. Quarantine rules and why matching values cannot auto-clear ambiguity.
15. Rollback by closing either gate; no Odoo deletion/archive.
16. Explicit list of unproven production behavior until preflight/canary.

- [ ] **Step 5: Document environment names without values and link the runbook**

Add to `.env.example`:

```dotenv
# Dedicated one-way Odoo 2s Improvements mirror (writes are closed by default)
ODOO_IMPROVEMENTS_URL=
ODOO_IMPROVEMENTS_DB=
ODOO_IMPROVEMENTS_LOGIN=
ODOO_IMPROVEMENTS_API_KEY=
ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID=
ODOO_IMPROVEMENTS_EXPECTED_COMPANY=
ODOO_SHARED_REPORTING_WRITE_ENABLED=
ODOO_IMPROVEMENTS_WRITE_ENABLED=
ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID=
```

Add a short README link to `docs/odoo-2s-feedback-operations.md`. Do not add real values, examples that resemble credentials, or instructions to enable gates without the approval sequence.

- [ ] **Step 6: Add a plain-language dark-rollout patch note**

Add:

```markdown
### Shared improvements connection is safely off

#### Improvements

- **Plant Manager is ready to share feedback with the improvements list, but the connection starts off.** It will stay off until its safety checks and one test item are approved.
```

- [ ] **Step 7: Run CLI, secret-scan, and docs checks**

```bash
pytest tests/test_feedback_rollout.py -v
rg -n "ODOO_IMPROVEMENTS_(API_KEY|LOGIN)=.+" .env.example docs README.md
git diff --check
```

Expected: pytest passes; `rg` returns no matches because no values are committed; diff check exits 0.

- [ ] **Step 8: Commit and push the inert rollout tooling**

```bash
git add scripts/feedback_odoo_rollout.py docs/odoo-2s-feedback-operations.md .env.example README.md CHANGELOG.md tests/test_feedback_rollout.py
git commit -m "docs(feedback): add guarded Odoo rollout tools"
git push origin main
```

### Task 12: Cross-Cutting Safety Contract and Complete Verification

**Files:**

- Create: `tests/test_feedback_odoo_safety_contract.py`
- Modify: `CHANGELOG.md`
- Modify only if a failing test proves a scoped defect: files owned by Tasks 1-11

**Interfaces:**

- Consumes: every public feedback/Odoo mirror interface implemented above.
- Produces: one high-level executable safety contract and fresh full-suite evidence before implementation is reported complete.

- [ ] **Step 1: Write the cross-cutting safety tests before final verification**

Create `tests/test_feedback_odoo_safety_contract.py`:

```python
from unittest.mock import MagicMock

from zira_dashboard import feedback_sync
from zira_dashboard.feedback_projection import TYPE_VALUES
from zira_dashboard.odoo_improvements import SOURCE_VALUE, TARGET_FIELDS, TARGET_MODEL


def test_shared_identity_and_target_are_fixed():
    assert SOURCE_VALUE == "GPI Plant Manager"
    assert TARGET_MODEL == "x_2s_improvements"


def test_closed_improvements_gate_blocks_claim_and_remote_call(monkeypatch):
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "true")
    monkeypatch.delenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", raising=False)
    claim = MagicMock(side_effect=AssertionError("claim attempted"))
    remote = MagicMock(side_effect=AssertionError("remote call attempted"))
    monkeypatch.setattr(feedback_sync.sync_store, "claim_due", claim)
    monkeypatch.setattr(feedback_sync.ImprovementsClient, "default_executor", remote)
    result = feedback_sync.run_batch()
    assert result.skipped == "write_gates_closed"
    claim.assert_not_called()
    remote.assert_not_called()


def test_target_payload_contract_has_no_token_archive_delete_or_physical_values():
    assert not any("token" in item.lower() for item in TARGET_FIELDS)
    assert "active" not in TARGET_FIELDS
    assert "Physical" not in TYPE_VALUES.values()
```

Keep this module independent of a database or live Odoo.

- [ ] **Step 2: Run the new safety contract and observe any integration mismatch**

```bash
pytest tests/test_feedback_odoo_safety_contract.py -v
```

Expected: all tests pass if Tasks 1-11 expose consistent constants; otherwise fix the owning module without weakening the assertions.

- [ ] **Step 3: Run focused feedback verification**

```bash
pytest tests/test_feedback_schema.py tests/test_feedback_image.py tests/test_feedback_routes.py tests/test_feedback_mine_route.py tests/test_feedback_store.py tests/test_feedback_admin_routes.py tests/test_odoo_improvements.py tests/test_feedback_projection.py tests/test_feedback_sync_store.py tests/test_feedback_sync.py tests/test_feedback_rollout.py tests/test_feedback_warmer.py tests/test_feedback_odoo_safety_contract.py tests/test_whatsnew_panel_static.py tests/test_timeclock_feedback_static.py -v
```

Expected: zero failures and zero errors. Database-only concurrency tests may skip only under their explicit safe-local-database guard.

- [ ] **Step 4: Run existing Odoo workflow regressions**

```bash
pytest tests/test_odoo_client.py tests/test_odoo_facade_contract.py tests/test_feedback_odoo.py tests/test_odoo_client_leaves.py tests/test_odoo_attendance_for_day.py tests/test_odoo_payroll.py tests/test_time_off_sync.py tests/test_timeclock_sync_dedup.py -v
```

Expected: zero failures, proving the dedicated integration did not change existing attendance, time-off, payroll, project-task, or generic facade contracts.

- [ ] **Step 5: Run the complete project verification suite**

```bash
ruff check src tests scripts
pytest -v
git diff --check
```

Expected: Ruff exits 0; pytest reports zero failures/errors with only documented environment/known-debt skips; diff check exits 0.

- [ ] **Step 6: Verify no production rollout action occurred**

Run local static checks only:

```bash
git status --short
rg -n "ODOO_SHARED_REPORTING_WRITE_ENABLED=true|ODOO_IMPROVEMENTS_WRITE_ENABLED=true|ODOO_IMPROVEMENTS_API_KEY=.+" .env.example docs README.md
```

Expected: the three pre-existing untracked files remain untouched and no new
unexpected file is present; the secret/gate scan returns no matches. Review the
Task 1-11 commit file lists with `git log --stat -12` and require every file to
appear in the File and Responsibility Map. Do not run preflight, dry-run,
canary, migration, enqueue, or any other rollout command in this task.

- [ ] **Step 7: Commit and push the final safety contract**

```bash
git add CHANGELOG.md tests/test_feedback_odoo_safety_contract.py
git commit -m "test(feedback): enforce Odoo mirror safety contract"
git push origin main
```

- [ ] **Step 8: Report verified versus unproven behavior precisely**

The implementation handoff must state:

- exact commands run and their pass/fail/skip counts,
- that local submission/lifecycle and mocked Odoo synchronization were tested,
- that closed gates prevented claims and calls,
- that no production Odoo read/write, Studio change, credential change, gate change, or backfill was performed,
- that real field types/selections, database/company identity, service-user permissions, binary round-trip behavior, live concurrency, and historical counts remain unproven until their approved rollout stages.
