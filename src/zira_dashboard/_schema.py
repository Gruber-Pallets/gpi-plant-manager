"""Postgres schema DDL, extracted verbatim from db.py.

One idempotent script: every CREATE uses IF NOT EXISTS and the historical
migrations are guarded DO-blocks, so db.bootstrap_schema() runs it on every
boot. Kept as a Python constant (not a .sql file) so it always ships in the
wheel/Railway build with zero packaging config.
"""

SCHEMA_DDL = """
-- 2026-05-29 migration: the "kiosk" app was renamed to "timeclock". Rename
-- the existing prod tables + indexes IN PLACE (preserving punch history and
-- schedule-variance data) BEFORE the CREATE TABLE IF NOT EXISTS statements
-- below, so the app doesn't silently start writing to fresh empty tables.
-- Guarded so fresh installs skip it and it's idempotent on every boot.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = current_schema() AND table_name = 'kiosk_punches_log')
     AND NOT EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = current_schema() AND table_name = 'timeclock_punches_log') THEN
    ALTER TABLE kiosk_punches_log RENAME TO timeclock_punches_log;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = current_schema() AND table_name = 'kiosk_schedule_variances')
     AND NOT EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = current_schema() AND table_name = 'timeclock_schedule_variances') THEN
    ALTER TABLE kiosk_schedule_variances RENAME TO timeclock_schedule_variances;
  END IF;
  ALTER INDEX IF EXISTS idx_kiosk_punches_log_unsynced
    RENAME TO idx_timeclock_punches_log_unsynced;
  ALTER INDEX IF EXISTS idx_kiosk_punches_log_person
    RENAME TO idx_timeclock_punches_log_person;
  ALTER INDEX IF EXISTS idx_kiosk_schedule_variances_day
    RENAME TO idx_timeclock_schedule_variances_day;
END $$;

-- 2026-05-26 migration: legacy "value stream" identifiers were renamed
-- to "department" everywhere. This DO block does the one-time table +
-- column rename on existing installs; fresh installs skip it (the
-- CREATE TABLE statements below already use the new names). The IF
-- EXISTS / NOT EXISTS guards make it idempotent and safe on every
-- boot. Must run BEFORE the CREATE TABLE block so the old `value_streams`
-- table doesn't coexist with a freshly-created `departments` table.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = current_schema() AND table_name = 'value_streams')
     AND NOT EXISTS (SELECT 1 FROM information_schema.tables
                     WHERE table_schema = current_schema() AND table_name = 'departments') THEN
    ALTER TABLE value_streams RENAME TO departments;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'work_centers' AND column_name = 'value_stream')
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                     WHERE table_schema = current_schema() AND table_name = 'work_centers' AND column_name = 'department') THEN
    ALTER TABLE work_centers RENAME COLUMN value_stream TO department;
  END IF;
END $$;

-- 2026-06-24 migration: the Shift Handoff feature was removed. Drop its
-- table (and its indexes, which go with it) on existing installs. Idempotent:
-- drops once in prod, then a no-op on every boot; fresh installs never had it.
DROP TABLE IF EXISTS plant_shift_handoffs;

-- HR-mastered entities (mirrored from Odoo via TTL sync) ----------------

CREATE TABLE IF NOT EXISTS people (
  id              SERIAL PRIMARY KEY,
  odoo_id         INTEGER UNIQUE,
  name            TEXT NOT NULL UNIQUE,
  active          BOOLEAN NOT NULL DEFAULT TRUE,
  reserve         BOOLEAN NOT NULL DEFAULT FALSE,
  last_pulled_at  TIMESTAMPTZ,
  last_pushed_at  TIMESTAMPTZ,
  local_dirty     BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS people_active_idx ON people (active);
ALTER TABLE people ADD COLUMN IF NOT EXISTS excluded BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE people ADD COLUMN IF NOT EXISTS wage_type TEXT;
ALTER TABLE people ADD COLUMN IF NOT EXISTS spanish_speaker BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE people ADD COLUMN IF NOT EXISTS resource_calendar_id INTEGER;

CREATE TABLE IF NOT EXISTS skills (
  id              SERIAL PRIMARY KEY,
  odoo_id         INTEGER UNIQUE,
  name            TEXT NOT NULL UNIQUE,
  skill_type      TEXT NOT NULL,
  sort_order      INTEGER NOT NULL DEFAULT 0,
  last_pulled_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS person_skills (
  person_id       INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  skill_id        INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  level           SMALLINT NOT NULL DEFAULT 0,
  last_pulled_at  TIMESTAMPTZ,
  last_pushed_at  TIMESTAMPTZ,
  local_dirty     BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY (person_id, skill_id)
);

-- Work centers ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS work_centers (
  id              SERIAL PRIMARY KEY,
  odoo_id         INTEGER UNIQUE,
  name            TEXT NOT NULL UNIQUE,
  meter_id        TEXT,
  category        TEXT NOT NULL,
  cell            TEXT,
  department      TEXT,
  min_ops         INTEGER NOT NULL DEFAULT 1,
  max_ops         INTEGER,
  goal_per_day_override INTEGER,
  group_name      TEXT,
  note            TEXT,
  last_pulled_at  TIMESTAMPTZ,
  last_pushed_at  TIMESTAMPTZ,
  local_dirty     BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS work_center_required_skills (
  wc_id           INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE CASCADE,
  skill_id        INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  PRIMARY KEY (wc_id, skill_id)
);

CREATE TABLE IF NOT EXISTS work_center_default_people (
  wc_id           INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE CASCADE,
  person_id       INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  sort_order      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (wc_id, person_id)
);

CREATE TABLE IF NOT EXISTS groups (
  name            TEXT PRIMARY KEY,
  goal_per_day_override INTEGER
);

CREATE TABLE IF NOT EXISTS departments (
  name            TEXT PRIMARY KEY,
  goal_per_day_override INTEGER
);

-- App-specific (not mirrored anywhere) ---------------------------------

CREATE TABLE IF NOT EXISTS schedules (
  day                 DATE PRIMARY KEY,
  published           BOOLEAN NOT NULL DEFAULT FALSE,
  testing_day         BOOLEAN NOT NULL DEFAULT FALSE,
  notes               TEXT NOT NULL DEFAULT '',
  custom_hours        JSONB,
  published_snapshot  JSONB,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schedule_assignments (
  day             DATE NOT NULL REFERENCES schedules(day) ON DELETE CASCADE,
  wc_id           INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE CASCADE,
  person_id       INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  sort_order      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, wc_id, person_id)
);
CREATE INDEX IF NOT EXISTS schedule_assignments_day_idx ON schedule_assignments(day);

-- schedule_time_off: removed (sub-project #2 — time-off now sourced live
-- from StratusTime, not stored locally). Drop the orphan table on bootstrap.
DROP TABLE IF EXISTS schedule_time_off;

CREATE TABLE IF NOT EXISTS schedule_wc_notes (
  day             DATE NOT NULL REFERENCES schedules(day) ON DELETE CASCADE,
  wc_id           INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE CASCADE,
  note            TEXT NOT NULL,
  PRIMARY KEY (day, wc_id)
);

-- Recycled smart rotations ---------------------------------------------

ALTER TABLE schedules ADD COLUMN IF NOT EXISTS recycled_rotation_mode TEXT NOT NULL DEFAULT 'normal';
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS assignment_sources JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS person_rotation_preferences (
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  rotation_group TEXT NOT NULL CHECK (rotation_group IN ('Dismantler', 'Repair', 'Trim Saw')),
  preference TEXT NOT NULL CHECK (preference IN ('primary', 'regular', 'occasional', 'never')),
  PRIMARY KEY (person_id, rotation_group)
);

CREATE TABLE IF NOT EXISTS rotation_training_blocks (
  id BIGSERIAL PRIMARY KEY,
  trainee_id INTEGER NOT NULL REFERENCES people(id),
  trainer_id INTEGER NOT NULL REFERENCES people(id),
  skill_id INTEGER NOT NULL REFERENCES skills(id),
  start_day DATE NOT NULL,
  planned_attended_days SMALLINT NOT NULL CHECK (planned_attended_days > 0),
  status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'completed', 'ended')),
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rotation_training_block_days (
  block_id BIGINT NOT NULL REFERENCES rotation_training_blocks(id) ON DELETE CASCADE,
  day DATE NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('attended', 'absent', 'conflict')),
  PRIMARY KEY (block_id, day)
);

-- Retro time-windowed WC attributions: when a metered WC produced units but
-- had no one scheduled there, the user can attribute the production to the
-- person who actually worked it. Used by attribute_for_day so leaderboards
-- and dashboards pick up the credit. No FK on day -- attribution can predate
-- the schedule entry.
CREATE TABLE IF NOT EXISTS wc_time_attributions (
  id              BIGSERIAL PRIMARY KEY,
  day             DATE NOT NULL,
  wc_name         TEXT NOT NULL,
  person_name     TEXT NOT NULL,
  start_utc       TIMESTAMPTZ NOT NULL,
  end_utc         TIMESTAMPTZ,            -- NULL = open assignment (still running)
  source          TEXT NOT NULL DEFAULT 'manual',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS wc_time_attributions_day_idx ON wc_time_attributions(day);
CREATE INDEX IF NOT EXISTS wc_time_attributions_day_wc_idx ON wc_time_attributions(day, wc_name);
-- Migrate pre-existing deployments where end_utc was created NOT NULL.
ALTER TABLE wc_time_attributions ALTER COLUMN end_utc DROP NOT NULL;

-- Late / absence overrides for the Late/Absence Report ----------------
-- manual_absences: marks a scheduled person as Absent for a single day
-- (manager-declared via the Late/Absence Report). Layered into the
-- StratusTime time-off list so they drop out of Unscheduled + picker.
-- cleared_time_off: per-day, per-request opt-out for StratusTime
-- partial-day off entries. When a StratusTime PTO/Early-Leave request
-- is filed but the person actually worked through it (Jose Luis case),
-- the user can clear that request for the day. Doesn't touch StratusTime.
CREATE TABLE IF NOT EXISTS cleared_time_off (
  day            DATE NOT NULL,
  request_id     BIGINT NOT NULL,
  declared_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, request_id)
);
CREATE INDEX IF NOT EXISTS cleared_time_off_day_idx ON cleared_time_off(day);

-- cleared_non_work_shifts: per-day, per-emp opt-out for StratusTime
-- non-work-shift entries (manager-entered Unpaid Time, etc.) that don't
-- have a request_id. Same idea as cleared_time_off but keyed by emp_id
-- because the V1 punch endpoint doesn't expose a stable id per entry.
CREATE TABLE IF NOT EXISTS cleared_non_work_shifts (
  day            DATE NOT NULL,
  emp_id         TEXT NOT NULL,
  declared_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, emp_id)
);
CREATE INDEX IF NOT EXISTS cleared_non_work_shifts_day_idx ON cleared_non_work_shifts(day);

-- cleared_partials_by_name: clear key on (day, name). Catch-all for any
-- partial entry the user wants to suppress, regardless of source —
-- works even when the underlying StratusTime entry has neither a
-- request_id nor a usable emp_id (which is why the previous (day,
-- request_id) and (day, emp_id) approaches missed Jose Luis's case).
-- Names align with the scheduler's roster names.
CREATE TABLE IF NOT EXISTS cleared_partials_by_name (
  day            DATE NOT NULL,
  name           TEXT NOT NULL,
  declared_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, name)
);
CREATE INDEX IF NOT EXISTS cleared_partials_by_name_day_idx ON cleared_partials_by_name(day);

CREATE TABLE IF NOT EXISTS manual_absences (
  day            DATE NOT NULL,
  emp_id         TEXT NOT NULL,
  name           TEXT NOT NULL,
  declared_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, emp_id)
);
CREATE INDEX IF NOT EXISTS manual_absences_day_idx ON manual_absences(day);

ALTER TABLE manual_absences ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE manual_absences ADD COLUMN IF NOT EXISTS odoo_leave_id INTEGER;

CREATE TABLE IF NOT EXISTS late_arrivals (
  day            DATE NOT NULL,
  emp_id         TEXT NOT NULL,
  name           TEXT NOT NULL,
  reason         TEXT,
  declared_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, emp_id)
);
CREATE INDEX IF NOT EXISTS late_arrivals_day_idx ON late_arrivals(day);

-- late_snoozes: silences a person from the Late/Absence Report until
-- `until_utc`. After expiry the report re-checks them automatically.
CREATE TABLE IF NOT EXISTS late_snoozes (
  day            DATE NOT NULL,
  emp_id         TEXT NOT NULL,
  name           TEXT NOT NULL,
  until_utc      TIMESTAMPTZ NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, emp_id)
);
CREATE INDEX IF NOT EXISTS late_snoozes_day_idx ON late_snoozes(day);

CREATE TABLE IF NOT EXISTS global_schedule (
  id              INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  shift_start     TIME NOT NULL,
  shift_end       TIME NOT NULL,
  work_weekdays   INTEGER[] NOT NULL,
  breaks          JSONB NOT NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS saturday_schedule (
  id              INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  shift_start     TIME NOT NULL,
  shift_end       TIME NOT NULL,
  breaks          JSONB NOT NULL DEFAULT '[]'::jsonb,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS widget_layouts (
  page            TEXT PRIMARY KEY,
  layout          JSONB NOT NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS widget_customizations (
  page            TEXT NOT NULL,
  widget_id       TEXT NOT NULL,
  customizations  JSONB NOT NULL,
  PRIMARY KEY (page, widget_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
  key             TEXT PRIMARY KEY,
  value           JSONB NOT NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Outbox for future two-way sync (not actively drained in Phase 1) ----

CREATE TABLE IF NOT EXISTS sync_outbox (
  id              BIGSERIAL PRIMARY KEY,
  kind            TEXT NOT NULL,
  entity_id       INTEGER,
  action          TEXT NOT NULL,
  payload         JSONB NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending',
  attempts        INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  pushed_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS sync_outbox_status_idx ON sync_outbox(status, created_at);

-- Saved Views for the People Matrix (filter bundles) ------------------

CREATE TABLE IF NOT EXISTS skill_matrix_views (
  id              SERIAL PRIMARY KEY,
  name            TEXT NOT NULL UNIQUE,
  is_default      BOOLEAN NOT NULL DEFAULT FALSE,
  hidden_skills   TEXT[]  NOT NULL DEFAULT '{}',
  visible_people  TEXT[],
  active_filter   TEXT NOT NULL DEFAULT 'active'
                  CHECK (active_filter IN ('active','inactive','all')),
  reserve_filter  TEXT NOT NULL DEFAULT 'all'
                  CHECK (reserve_filter IN ('include','exclude','only','all')),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS skill_matrix_views_default_idx
  ON skill_matrix_views (is_default) WHERE is_default = TRUE;

-- Per-WC display settings for the leaderboards page ------------------

CREATE TABLE IF NOT EXISTS leaderboard_wc_settings (
  kind         TEXT NOT NULL DEFAULT 'wc',
  wc_name      TEXT NOT NULL,
  sort_order   INTEGER NOT NULL DEFAULT 0,
  is_inactive  BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, wc_name)
);

-- Idempotent: add `kind` column to a pre-existing table that has the
-- legacy single-column PK on wc_name.
ALTER TABLE leaderboard_wc_settings ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'wc';

-- Persistent cache for past-day Zira leaderboard results -------------
-- Past-day production is immutable; survive Railway redeploys without
-- re-paying the Zira API cost. Today's data stays in-process only.

CREATE TABLE IF NOT EXISTS zira_daily_cache (
  meter_id    TEXT NOT NULL,
  day         DATE NOT NULL,
  payload     JSONB NOT NULL,
  computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (meter_id, day)
);
CREATE INDEX IF NOT EXISTS idx_zira_daily_cache_day ON zira_daily_cache(day);

-- Migrate single-column PK to composite (kind, wc_name) when needed.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'leaderboard_wc_settings_pkey'
      AND conrelid = 'leaderboard_wc_settings'::regclass
  ) AND NOT EXISTS (
    SELECT 1 FROM pg_index i
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
    WHERE i.indrelid = 'leaderboard_wc_settings'::regclass
      AND i.indisprimary
      AND a.attname = 'kind'
  ) THEN
    ALTER TABLE leaderboard_wc_settings DROP CONSTRAINT leaderboard_wc_settings_pkey;
    ALTER TABLE leaderboard_wc_settings ADD PRIMARY KEY (kind, wc_name);
  END IF;
END $$;

-- Award overrides ------------------------------------------------------
-- Trophy/badge/award winners are computed live from daily_records.
-- This table stores manual reassignments + deletions; the unique
-- index ensures one override per slot.

CREATE TABLE IF NOT EXISTS award_overrides (
  id            SERIAL PRIMARY KEY,
  scope         TEXT NOT NULL,
  group_name    TEXT,
  wc_name       TEXT,
  year          INT,
  month         INT,
  position      INT NOT NULL,
  action        TEXT NOT NULL,
  name          TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  note          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS award_overrides_slot ON award_overrides
  (scope, COALESCE(group_name,''), COALESCE(wc_name,''),
   COALESCE(year,0), COALESCE(month,0), position);

-- Precompute fact table -------------------------------------------------
-- One row per (day, person, WC). Written nightly for past days, written
-- by the live warmer for today. Every leaderboard / player-card /
-- trophy / value-stream page reads from here.
CREATE TABLE IF NOT EXISTS production_daily (
  day         DATE   NOT NULL,
  emp_id      TEXT   NOT NULL,
  name        TEXT   NOT NULL,
  wc_name     TEXT   NOT NULL,
  units       NUMERIC NOT NULL DEFAULT 0,
  downtime    NUMERIC NOT NULL DEFAULT 0,
  hours       NUMERIC NOT NULL DEFAULT 0,
  days_worked NUMERIC NOT NULL DEFAULT 0,
  computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, emp_id, wc_name)
);
CREATE INDEX IF NOT EXISTS idx_production_daily_name_day
  ON production_daily (name, day);
CREATE INDEX IF NOT EXISTS idx_production_daily_wc_day
  ON production_daily (wc_name, day);

-- Live cache tables ----------------------------------------------------
-- Single-row JSONB blobs keyed by today's date. The live warmer
-- overwrites them every 45 s. Routes read from here instead of calling
-- StratusTime / Odoo in the request path. `refreshed_at` lets routes
-- detect staleness for a cold-start safety valve.
CREATE TABLE IF NOT EXISTS today_attendance_cache (
  day          DATE PRIMARY KEY,
  payload      JSONB NOT NULL,
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS today_timeoff_cache (
  day          DATE PRIMARY KEY,
  payload      JSONB NOT NULL,
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS today_production_cache (
  day          DATE PRIMARY KEY,
  payload      JSONB NOT NULL,
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Odoo open-attendance snapshot (2026-06-01) ---------------------------
-- Single-row mirror of every currently-open hr.attendance (check_out IS
-- NULL), keyed by person_odoo_id inside the JSONB snapshot. The ~30s
-- warmer (_warm_odoo_attendance_loop in app.py) overwrites it; the
-- timeclock punch screen reconciles it against timeclock_punches_log so
-- punches added/closed/deleted directly in Odoo show up without an
-- XML-RPC call on the tap. Forced single row (id=1) so refreshed_at is a
-- GLOBAL freshness marker: "person absent from snapshot" only means
-- clocked-out when the snapshot is known-fresh.
CREATE TABLE IF NOT EXISTS odoo_open_attendance_cache (
  id           INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  snapshot     JSONB NOT NULL DEFAULT '{}'::jsonb,
  refreshed_at TIMESTAMPTZ
);

-- TV display registry ---------------------------------------------------
-- Each row is a TV mounted somewhere in the plant. Carries a friendly
-- name, the dashboard it shows (kind + optional wc_name), and the theme
-- (light/dark) for that physical display. The /tv/{slug} route looks
-- up the row and dispatches to the underlying dashboard with the row's
-- theme. Seed list of 12 rows inserts on first boot only.
CREATE TABLE IF NOT EXISTS tv_displays (
  id                  SERIAL PRIMARY KEY,
  name                TEXT NOT NULL,
  slug                TEXT NOT NULL UNIQUE,
  kind                TEXT NOT NULL CHECK (kind IN (
    'vs_recycling',
    'vs_new',
    'vs_recycling_leaderboard',
    'vs_new_leaderboard',
    'wc'
  )),
  wc_name             TEXT,
  theme               TEXT NOT NULL DEFAULT 'dark' CHECK (theme IN ('light', 'dark')),
  sort_order          INTEGER NOT NULL DEFAULT 0,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Tear-down (2026-05-14): workshop + custom dashboards experiment is gone.
-- This block runs idempotently on every boot. It drops every workshop
-- artifact (tables, the tv_displays FK + column that referenced them,
-- the 'custom' kind in CHECK constraints, any leftover rows).
--
-- Order matters:
--   1. Drop tv_displays.custom_dashboard_id FK before dropping the
--      table it references — otherwise DROP TABLE fails.
--   2. Drop dashboard_widgets before custom_dashboards / widget_definitions
--      (it FKs both).
ALTER TABLE tv_displays
  DROP CONSTRAINT IF EXISTS tv_displays_custom_dashboard_id_fkey;
ALTER TABLE tv_displays DROP COLUMN IF EXISTS custom_dashboard_id;

DROP TABLE IF EXISTS dashboard_widgets;
DROP TABLE IF EXISTS custom_dashboards;
DROP TABLE IF EXISTS widget_definitions;
DROP TABLE IF EXISTS tv_dashboard_templates;
DROP TABLE IF EXISTS pinned_dashboards;

-- Tighten tv_displays.kind CHECK back down to the live kinds.
DELETE FROM tv_displays WHERE kind = 'custom';
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'tv_displays_kind_check'
      AND conrelid = 'tv_displays'::regclass
  ) THEN
    ALTER TABLE tv_displays DROP CONSTRAINT tv_displays_kind_check;
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'tv_displays_kind_check'
      AND conrelid = 'tv_displays'::regclass
  ) THEN
    ALTER TABLE tv_displays ADD CONSTRAINT tv_displays_kind_check
      CHECK (kind IN (
        'vs_recycling',
        'vs_new',
        'vs_recycling_leaderboard',
        'vs_new_leaderboard',
        'wc'
      ));
  END IF;
END $$;

-- Operator dashboard switch (2026-05-14): the per-WC widget layouts
-- saved under page='wc:{slug}' are orphaned now that every /wc/{slug}
-- reads/writes a single shared key 'operator'. Drop them so the table
-- stays clean. Idempotent — once empty, this is a no-op.
DELETE FROM widget_layouts        WHERE page LIKE 'wc:%';
DELETE FROM widget_customizations WHERE page LIKE 'wc:%';

-- GOAT Watch alerts (2026-05-15): finalized at shift-end whenever a
-- person-day strictly beats the prior group GOAT record. Banner on the
-- Recycling department dashboard reads from this table — visible until
-- next_business_day(achieved_day) or until manually dismissed.
CREATE TABLE IF NOT EXISTS goat_alerts (
  id                  SERIAL PRIMARY KEY,
  achieved_day        DATE NOT NULL,
  group_name          TEXT NOT NULL,
  person              TEXT NOT NULL,
  wc_name             TEXT NOT NULL,
  units               INTEGER NOT NULL,
  prior_record_units  INTEGER,
  prior_record_holder TEXT,
  prior_record_day    DATE,
  dismissed_at        TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (achieved_day, group_name, wc_name)
);
CREATE INDEX IF NOT EXISTS idx_goat_alerts_day ON goat_alerts (achieved_day);

-- Server-to-server API keys for the Odoo-like object API.
CREATE TABLE IF NOT EXISTS api_keys (
  id           SERIAL PRIMARY KEY,
  name         TEXT NOT NULL,
  key_prefix   TEXT NOT NULL,
  key_hash     TEXT NOT NULL UNIQUE,
  scopes       JSONB NOT NULL DEFAULT '[]'::jsonb,
  allowed_ips  JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_by   TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ,
  revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS api_keys_active_idx
  ON api_keys (key_hash) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS api_audit_log (
  id              BIGSERIAL PRIMARY KEY,
  api_key_id      INTEGER REFERENCES api_keys(id),
  app_name        TEXT NOT NULL,
  actor           TEXT,
  model           TEXT,
  method          TEXT,
  request_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  status          TEXT NOT NULL,
  error_code      TEXT,
  duration_ms     INTEGER,
  client_ip       TEXT,
  user_agent      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS api_audit_log_created_idx
  ON api_audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS api_audit_log_key_idx
  ON api_audit_log (api_key_id, created_at DESC);

-- Long-lived signed device tokens for shop-floor TV displays.
-- Bound to /tv/* paths in middleware. Revocation is instant via
-- setting `revoked_at` (no blacklist cache needed).
CREATE TABLE IF NOT EXISTS device_tokens (
  id           SERIAL PRIMARY KEY,
  name         TEXT NOT NULL,
  token        TEXT UNIQUE NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by   TEXT NOT NULL,
  last_used_at TIMESTAMPTZ,
  revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS device_tokens_active_idx
  ON device_tokens (token) WHERE revoked_at IS NULL;

-- Kiosk pilot (2026-05-21): timeclock for clock in/out + WC transfers,
-- replacing StratusTime in stages. Phase 0 = Dale-only pilot writing to
-- Odoo hr.attendance; Phase 1 = plant-wide cutover. Auth is name-pick
-- only — no PIN, by design.

-- Local mirror of every kiosk punch action. NOT the source of truth —
-- Odoo hr.attendance is. This table is for audit + offline-tolerant retry:
-- rows are written with synced_to_odoo=FALSE first, then flipped to TRUE
-- once the Odoo write succeeds. The background sync worker reconciles
-- rows still at FALSE every 60s.
CREATE TABLE IF NOT EXISTS timeclock_punches_log (
  id                  BIGSERIAL PRIMARY KEY,
  person_odoo_id      INTEGER NOT NULL,
  action              TEXT NOT NULL CHECK (action IN ('clock_in','clock_out','transfer_out','transfer_in')),
  wc_name             TEXT,
  odoo_attendance_id  INTEGER,
  occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  synced_to_odoo      BOOLEAN NOT NULL DEFAULT FALSE,
  sync_error          TEXT,
  synced_at           TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_timeclock_punches_log_unsynced
  ON timeclock_punches_log (occurred_at) WHERE synced_to_odoo = FALSE;
CREATE INDEX IF NOT EXISTS idx_timeclock_punches_log_person
  ON timeclock_punches_log (person_odoo_id, occurred_at DESC);

-- Variance log: every time an employee picks a WC different from what
-- the scheduler said for today. reviewed_by/at let supervisors triage
-- (Phase 1 UI). For Phase 0 (Dale-only), variances still get logged so
-- we have data to design the review UI against.
CREATE TABLE IF NOT EXISTS timeclock_schedule_variances (
  id                  BIGSERIAL PRIMARY KEY,
  person_odoo_id      INTEGER NOT NULL,
  scheduled_wc_name   TEXT,
  actual_wc_name      TEXT NOT NULL,
  occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_by         TEXT,
  reviewed_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_timeclock_schedule_variances_day
  ON timeclock_schedule_variances (occurred_at);

-- Rounding settings (2026-05-27): plant-wide timeclock punch rounding,
-- modeled on StratusTime's "Round To Schedule" feature. Singleton row
-- (id=1) holds four integers — the four window edges. Zero on all four
-- = no rounding (ships disabled).
CREATE TABLE IF NOT EXISTS rounding_settings (
  id              INT PRIMARY KEY DEFAULT 1,
  in_before_min   INT NOT NULL DEFAULT 0,
  in_after_min    INT NOT NULL DEFAULT 0,
  out_before_min  INT NOT NULL DEFAULT 0,
  out_after_min   INT NOT NULL DEFAULT 0,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT rounding_settings_singleton CHECK (id = 1)
);
INSERT INTO rounding_settings (id) VALUES (1) ON CONFLICT DO NOTHING;

-- Per-work-schedule rounding overrides (2026-06-01). One row per Odoo
-- working schedule (resource.calendar) that gets its own punch-rounding
-- windows. `work_hours` (per-weekday "HH:MM" boundaries) is synced FROM
-- Odoo; the four window columns are app-owned (set on the settings page).
-- Row existence == an active override; employees inherit it via
-- people.resource_calendar_id. Everyone else uses rounding_settings.
CREATE TABLE IF NOT EXISTS work_schedules (
  resource_calendar_id  INTEGER PRIMARY KEY,
  name                  TEXT NOT NULL DEFAULT '',
  work_hours            JSONB NOT NULL DEFAULT '{}'::jsonb,
  in_before_min         INT NOT NULL DEFAULT 0,
  in_after_min          INT NOT NULL DEFAULT 0,
  out_before_min        INT NOT NULL DEFAULT 0,
  out_after_min         INT NOT NULL DEFAULT 0,
  last_synced_at        TIMESTAMPTZ,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Store both raw and rounded timestamps so historical audit is preserved.
-- Columns added separately (not in the CREATE TABLE above) because
-- timeclock_punches_log already exists in production.
ALTER TABLE timeclock_punches_log
  ADD COLUMN IF NOT EXISTS rounded_at TIMESTAMPTZ;

-- Expression index for the effective punch time. timeclock_windows
-- filters/orders on COALESCE(rounded_at, occurred_at); must live after the
-- rounded_at ALTER above so fresh installs have the column.
CREATE INDEX IF NOT EXISTS idx_punches_log_effective_at
  ON timeclock_punches_log ((COALESCE(rounded_at, occurred_at)));

-- Time-off requests (2026-05-27): local mirror of Odoo hr.leave + sync state.
-- `originating_kiosk_user` = TRUE for rows submitted via the kiosk (we own
-- the lifecycle and push to Odoo); FALSE for rows pulled in by the poller
-- because HR entered them directly in Odoo (Odoo owns the lifecycle, we
-- only mirror). `shape` carries the partial-day intent (full_day vs.
-- late_arrival / early_leave / midday_gap) so the scheduler can render
-- partials without re-deriving from hour_from/hour_to.
CREATE TABLE IF NOT EXISTS time_off_requests (
  id                       BIGSERIAL PRIMARY KEY,
  person_odoo_id           INTEGER NOT NULL,
  originating_kiosk_user   BOOLEAN NOT NULL DEFAULT TRUE,
  shape                    TEXT NOT NULL CHECK (shape IN ('full_day','late_arrival','early_leave','midday_gap')),
  holiday_status_id        INTEGER NOT NULL,
  date_from                DATE NOT NULL,
  date_to                  DATE NOT NULL,
  hour_from                NUMERIC(4,2),
  hour_to                  NUMERIC(4,2),
  working_hours_json       JSONB,
  note                     TEXT,
  state                    TEXT NOT NULL DEFAULT 'draft'
                           CHECK (state IN ('draft','draft_edit','draft_cancel','confirm','validate1','validate','refuse','cancel')),
  odoo_leave_id            INTEGER,
  synced_to_odoo           BOOLEAN NOT NULL DEFAULT FALSE,
  sync_error               TEXT,
  local_record             BOOLEAN NOT NULL DEFAULT FALSE,
  backfill_attempts        INTEGER NOT NULL DEFAULT 0,
  backfill_next_at         TIMESTAMPTZ,
  last_pulled_at           TIMESTAMPTZ,
  last_pushed_at           TIMESTAMPTZ,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- `local_record` = TRUE marks a row whose state is owned by this app, not
-- Odoo: an absence approved here after Odoo refused to validate the leave
-- because the employee's Working Schedule doesn't include the day(s). The
-- poller must neither overwrite nor delete such rows (their Odoo copy was
-- settled as refused).
ALTER TABLE time_off_requests ADD COLUMN IF NOT EXISTS local_record BOOLEAN NOT NULL DEFAULT FALSE;
-- Backoff state for the Odoo backfill reconciler (time_off_local_backfill):
-- failed replays retry exponentially (attempts) and prediction-skipped rows
-- rotate out of the candidate window until backfill_next_at.
ALTER TABLE time_off_requests ADD COLUMN IF NOT EXISTS backfill_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE time_off_requests ADD COLUMN IF NOT EXISTS backfill_next_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS time_off_requests_person_date_idx
  ON time_off_requests (person_odoo_id, date_from);
CREATE INDEX IF NOT EXISTS time_off_requests_range_idx
  ON time_off_requests (date_from, date_to);
CREATE INDEX IF NOT EXISTS time_off_requests_unsynced_idx
  ON time_off_requests (id) WHERE synced_to_odoo = FALSE;
CREATE INDEX IF NOT EXISTS time_off_requests_state_idx
  ON time_off_requests (state, date_from);
CREATE UNIQUE INDEX IF NOT EXISTS time_off_requests_odoo_leave_id_uniq
  ON time_off_requests (odoo_leave_id) WHERE odoo_leave_id IS NOT NULL;

-- Per-(person, leave_type) balance cache. Refreshed by the poller from
-- Odoo `hr.leave.allocation` + tallied `hr.leave` rows so the kiosk can
-- show "X days available" without an Odoo round-trip per render.
-- `available_practical` is the manager's safe-to-spend number (subtracts
-- pending requests from `available`).
CREATE TABLE IF NOT EXISTS time_off_balances (
  person_odoo_id       INTEGER NOT NULL,
  holiday_status_id    INTEGER NOT NULL,
  unit                 TEXT NOT NULL CHECK (unit IN ('days','hours')),
  allocated_total      NUMERIC(8,2) NOT NULL,
  taken                NUMERIC(8,2) NOT NULL,
  pending              NUMERIC(8,2) NOT NULL DEFAULT 0,
  available            NUMERIC(8,2) NOT NULL,
  available_practical  NUMERIC(8,2) NOT NULL,
  last_pulled_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (person_odoo_id, holiday_status_id)
);

-- Employee-facing kiosk notifications. One row = one thing to tell an
-- employee at their next time-clock sign-in. Currently sourced only from
-- time-off resolutions (approved/denied/cancelled). `acknowledged_at`
-- records the "Got it" tap so a notification never shows twice. Leave
-- dates are snapshotted so the message stays correct even if the source
-- time_off_requests row later changes or is deleted.
CREATE TABLE IF NOT EXISTS employee_notifications (
  id                   BIGSERIAL PRIMARY KEY,
  person_odoo_id       INTEGER NOT NULL,
  kind                 TEXT NOT NULL,
  time_off_request_id  BIGINT,
  odoo_leave_id        INTEGER,
  title                TEXT NOT NULL,
  body                 TEXT NOT NULL,
  leave_date_from      DATE,
  leave_date_to        DATE,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  acknowledged_at      TIMESTAMPTZ
);
-- Hard dedupe backstop: generation only fires on observed transitions, but
-- this guarantees at most one notification per (request, kind) even if a
-- poll double-processes a row.
CREATE UNIQUE INDEX IF NOT EXISTS employee_notifications_dedupe
  ON employee_notifications (time_off_request_id, kind);
-- Sign-in hot path: "does this person have anything to show?"
CREATE INDEX IF NOT EXISTS employee_notifications_unack
  ON employee_notifications (person_odoo_id) WHERE acknowledged_at IS NULL;

-- Audit log of scheduler reassignments caused by time-off cascade. Bucket
-- vocabulary: `from_bucket` / `to_bucket` are either a WC name from
-- `staffing.LOCATIONS`, the special `TIME_OFF_KEY` constant `'__time_off'`
-- (meaning the unscheduled / time-off pool), or NULL (only valid for
-- `from_bucket`, indicating the person wasn't assigned anywhere before
-- the move). `reason` is a short human-readable tag (e.g. 'time_off_added').
CREATE TABLE IF NOT EXISTS scheduler_moves (
  id              BIGSERIAL PRIMARY KEY,
  person_odoo_id  INTEGER NOT NULL,
  schedule_date   DATE NOT NULL,
  from_bucket     TEXT,
  to_bucket       TEXT NOT NULL,
  reason          TEXT NOT NULL,
  occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS scheduler_moves_person_date_idx
  ON scheduler_moves (person_odoo_id, schedule_date);

-- Cached hr.leave.type list, refreshed every ~10min by poller. The kiosk
-- picker reads from here so the leave-type dropdown renders instantly and
-- still survives an Odoo outage. `request_unit` and `requires_allocation`
-- mirror the Odoo field names/values verbatim (Odoo stores them as plain
-- text, not enums) so we can pass them straight back when creating leaves.
CREATE TABLE IF NOT EXISTS leave_types_cache (
  holiday_status_id    INTEGER PRIMARY KEY,
  name                 TEXT NOT NULL,
  request_unit         TEXT NOT NULL CHECK (request_unit IN ('day','half_day','hour')),
  requires_allocation  TEXT NOT NULL CHECK (requires_allocation IN ('yes','no')),
  color                INTEGER,
  active               BOOLEAN NOT NULL DEFAULT TRUE,
  last_pulled_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2026-06-02 auto-lunch: tag system-generated punches so the worker can
-- recognize its own actions and reports can filter them out.
ALTER TABLE timeclock_punches_log
  ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'employee'
  CHECK (source IN ('employee', 'auto_lunch'));

-- Flex flag mirrored from each person's Odoo work schedule (Schedule Type =
-- flexible). Stored on people (always present) rather than work_schedules
-- (rows exist only for rounding overrides). Drives the elapsed-time lunch trigger.
ALTER TABLE people ADD COLUMN IF NOT EXISTS is_flexible BOOLEAN NOT NULL DEFAULT FALSE;

-- Per-person/per-day lunch state machine. UNIQUE(person, day) enforces one
-- lunch per day and survives restarts (no double-deduct after a redeploy).
CREATE TABLE IF NOT EXISTS auto_lunch_runs (
  id              BIGSERIAL PRIMARY KEY,
  person_odoo_id  INTEGER NOT NULL,
  day             DATE    NOT NULL,
  kind            TEXT    NOT NULL CHECK (kind IN ('scheduled','flex')),
  state           TEXT    NOT NULL CHECK (state IN
                    ('pending','auto_out','done','skipped','ended_by_employee')),
  target_out_at   TIMESTAMPTZ,
  target_in_at    TIMESTAMPTZ,
  wc_name         TEXT,
  out_punch_id    BIGINT,
  in_punch_id     BIGINT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (person_odoo_id, day)
);

-- Singleton settings row (id=1). Defaults: OFF, and the first enable runs
-- observe-only. flex rule defaults to 5h -> 30min.
CREATE TABLE IF NOT EXISTS auto_lunch_settings (
  id                INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  enabled           BOOLEAN NOT NULL DEFAULT FALSE,
  observe_only      BOOLEAN NOT NULL DEFAULT TRUE,
  flex_after_hours  NUMERIC NOT NULL DEFAULT 5.0,
  flex_minutes      INTEGER NOT NULL DEFAULT 30
);
INSERT INTO auto_lunch_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- Forklift demand-advisor settings (2026-06-27). Singleton row (id=1). Tunes
-- the scheduler's forklift-driver recommendation: enabled toggle, per-driver
-- throughput (calls_per_hour) trimmed by target_utilization to an effective
-- rate, which work centers count toward coverage, how much same-weekday history
-- to use, and a manual cold-start daily volume (0 = auto from weekly trends).
CREATE TABLE IF NOT EXISTS forklift_settings (
  id                        INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  enabled                   BOOLEAN NOT NULL DEFAULT TRUE,
  calls_per_hour            NUMERIC NOT NULL DEFAULT 16,
  target_utilization        NUMERIC NOT NULL DEFAULT 0.65,
  include_loading_jockeying BOOLEAN NOT NULL DEFAULT FALSE,
  history_samples           INTEGER NOT NULL DEFAULT 8,
  coldstart_calls_per_day   NUMERIC NOT NULL DEFAULT 0
);
INSERT INTO forklift_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
-- Forklift settings redesign (2026-06-27): each tunable is now a NULLABLE
-- OVERRIDE (NULL = "auto" / follow the algorithm's own value). Additive +
-- idempotent for fresh and existing installs. The prior non-null param columns
-- (calls_per_hour / target_utilization / history_samples) are superseded and
-- left in place, unread; a later cleanup can drop them.
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS throughput_override NUMERIC;
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS utilization_override NUMERIC;
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS plan_for_percentile_override NUMERIC;
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS history_samples_override INTEGER;
-- Forklift recognition / GOAT-score settings (2026-06-29): nullable overrides
-- for the composite-score config (NULL = "auto" / use forklift_score's own
-- DEFAULT_SCORE_CONFIG value). Weights stored raw (renormalized at compute
-- time). Additive + idempotent for fresh and existing installs.
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS score_w_calls NUMERIC;
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS score_w_ontime NUMERIC;
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS score_w_speed NUMERIC;
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS score_w_util NUMERIC;
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS score_target_calls NUMERIC;
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS score_ontime_floor NUMERIC;
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS score_fast_secs NUMERIC;
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS score_slow_secs NUMERIC;
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS score_min_calls INTEGER;
-- Forklift SLA recommender (2026-06-29): target time-to-claim the crew is sized
-- to (NULL = "auto" / 240s = 4 min default). Nullable override; additive +
-- idempotent. Replaces the throughput/utilization knobs in the recommendation
-- path (those columns stay, unread, per the prior redesign's approach).
ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS target_claim_seconds NUMERIC NULL;

-- Department-driven rounding (2026-06-04). Named rounding "systems" (each a set
-- of the four windows) are selected by the static department an employee works
-- that day (staffing.Location.department). rounding_settings id=1 remains the
-- plant-default fallback for any punch that doesn't resolve to a mapped dept.
CREATE TABLE IF NOT EXISTS rounding_systems (
  id              SERIAL PRIMARY KEY,
  name            TEXT NOT NULL UNIQUE,
  in_before_min   INT NOT NULL DEFAULT 0,
  in_after_min    INT NOT NULL DEFAULT 0,
  out_before_min  INT NOT NULL DEFAULT 0,
  out_after_min   INT NOT NULL DEFAULT 0,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS department_rounding (
  department  TEXT PRIMARY KEY,
  system_id   INTEGER REFERENCES rounding_systems(id) ON DELETE SET NULL
);

-- Seed the three systems (idempotent via UNIQUE(name)). Plant Operator inherits
-- the current plant-default windows so Recycled/New behavior is preserved on
-- migration; Transportation seeds to the known driver policy; Supervisor starts
-- at no-rounding for Dale to set.
INSERT INTO rounding_systems (name, in_before_min, in_after_min, out_before_min, out_after_min)
  SELECT 'Plant Operator', in_before_min, in_after_min, out_before_min, out_after_min
  FROM rounding_settings WHERE id = 1
  ON CONFLICT (name) DO NOTHING;
INSERT INTO rounding_systems (name, in_before_min, in_after_min, out_before_min, out_after_min)
  VALUES ('Transportation', 20, 0, 0, 0)
  ON CONFLICT (name) DO NOTHING;
INSERT INTO rounding_systems (name)
  VALUES ('Supervisor')
  ON CONFLICT (name) DO NOTHING;

-- Seed the department->system map (idempotent via PRIMARY KEY(department)).
INSERT INTO department_rounding (department, system_id)
  SELECT 'Recycled', id FROM rounding_systems WHERE name = 'Plant Operator'
  ON CONFLICT (department) DO NOTHING;
INSERT INTO department_rounding (department, system_id)
  SELECT 'New', id FROM rounding_systems WHERE name = 'Plant Operator'
  ON CONFLICT (department) DO NOTHING;
INSERT INTO department_rounding (department, system_id)
  SELECT 'Supervisor', id FROM rounding_systems WHERE name = 'Supervisor'
  ON CONFLICT (department) DO NOTHING;
INSERT INTO department_rounding (department, system_id)
  SELECT 'Transportation', id FROM rounding_systems WHERE name = 'Transportation'
  ON CONFLICT (department) DO NOTHING;
INSERT INTO department_rounding (department, system_id)
  SELECT 'Maintenance', id FROM rounding_systems WHERE name = 'Plant Operator'
  ON CONFLICT (department) DO NOTHING;

-- Missing-work-center alert (2026-06-04). Cache of Odoo hr.attendance rows
-- (last 14 days) lacking a kiosk work-center tag, refreshed by a warmer; plus
-- a suppression table for records a manager has assigned or dismissed.
CREATE TABLE IF NOT EXISTS missing_wc_cache (
  id           INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  snapshot     JSONB NOT NULL DEFAULT '[]'::jsonb,
  refreshed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS missing_wc_resolved (
  attendance_id BIGINT PRIMARY KEY,
  action        TEXT NOT NULL CHECK (action IN ('assigned','dismissed')),
  name          TEXT,
  wc_name       TEXT,
  resolved_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS missed_punch_out (
  attendance_id    BIGINT PRIMARY KEY,
  employee_odoo_id BIGINT NOT NULL,
  name             TEXT,
  check_in         TIMESTAMPTZ NOT NULL,
  auto_closed_at   TIMESTAMPTZ NOT NULL,
  corrected_at     TIMESTAMPTZ,
  resolved_at      TIMESTAMPTZ,
  flagged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feedback (
  id           SERIAL PRIMARY KEY,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  submitter    TEXT,
  page_url     TEXT,
  category     TEXT,
  message      TEXT NOT NULL,
  task_type    TEXT,
  odoo_task_id BIGINT
);
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS task_type TEXT;
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS odoo_task_id BIGINT;

-- 2026-06-24: append-only audit log of time-off approve/deny decisions made
-- in-app. Deliberately denormalized (no FK to time_off_requests): the leave
-- poller hard-deletes mirror rows when a leave is deleted in Odoo, and the
-- decision history must survive that. request_id is the mirror id at decision
-- time, kept for correlation only.
CREATE TABLE IF NOT EXISTS time_off_decisions (
  id              SERIAL PRIMARY KEY,
  request_id      INTEGER,
  odoo_leave_id   INTEGER,
  person_odoo_id  INTEGER,
  person_name     TEXT,
  leave_type      TEXT,
  date_from       DATE,
  date_to         DATE,
  hour_from       NUMERIC,
  hour_to         NUMERIC,
  action          TEXT NOT NULL CHECK (action IN ('approve','deny')),
  result_state    TEXT,
  reason          TEXT,
  actor_upn       TEXT,
  actor_name      TEXT,
  source          TEXT,
  decided_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE time_off_decisions
  ADD COLUMN IF NOT EXISTS hour_from NUMERIC,
  ADD COLUMN IF NOT EXISTS hour_to NUMERIC;
CREATE INDEX IF NOT EXISTS time_off_decisions_decided_at_idx
  ON time_off_decisions (decided_at DESC);

-- 2026-06-26: unified Exception Inbox activity log — the archive + audit trail.
-- One append-only row per resolution across every inbox category. Denormalized
-- (no FK) so history survives source-row deletion, like time_off_decisions.
-- actor_upn NULL => auto-resolved/system; otherwise the manager who acted.
CREATE TABLE IF NOT EXISTS inbox_events (
  id            SERIAL PRIMARY KEY,
  item_kind     TEXT NOT NULL,
  item_key      TEXT NOT NULL,
  person_name   TEXT,
  category_label TEXT,
  action        TEXT NOT NULL,
  outcome       TEXT,
  before_value  TEXT,
  after_value   TEXT,
  reason        TEXT,
  actor_upn     TEXT,
  actor_name    TEXT,
  source        TEXT,
  detail        JSONB,
  reversible    BOOLEAN NOT NULL DEFAULT FALSE,
  undone_at     TIMESTAMPTZ,
  undo_event_id INTEGER,
  resolved_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS inbox_events_resolved_at_idx ON inbox_events (resolved_at DESC);
CREATE INDEX IF NOT EXISTS inbox_events_actor_idx ON inbox_events (actor_upn);
CREATE INDEX IF NOT EXISTS inbox_events_item_idx ON inbox_events (item_kind, item_key);

-- Forklift integration (gpiforklift.com) -------------------------------
-- Daily snapshots of forklift demand + per-driver performance. The API
-- only exposes "today", so a warmer writes one row per day and history
-- accumulates here (mirrors production_daily).
CREATE TABLE IF NOT EXISTS forklift_calls_daily (
  day              DATE PRIMARY KEY,
  total_calls      INTEGER NOT NULL DEFAULT 0,
  urgent_calls     INTEGER NOT NULL DEFAULT 0,
  overload_count   INTEGER NOT NULL DEFAULT 0,
  neglected_count  INTEGER NOT NULL DEFAULT 0,
  by_hour          JSONB NOT NULL DEFAULT '{}'::jsonb,
  by_station       JSONB NOT NULL DEFAULT '{}'::jsonb,
  by_skill         JSONB NOT NULL DEFAULT '{}'::jsonb,
  computed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS forklift_driver_daily (
  day              DATE NOT NULL,
  driver_id        TEXT NOT NULL,
  name             TEXT NOT NULL,
  calls            INTEGER NOT NULL DEFAULT 0,
  on_time          INTEGER NOT NULL DEFAULT 0,
  late             INTEGER NOT NULL DEFAULT 0,
  avg_ms           BIGINT NOT NULL DEFAULT 0,
  max_ms           BIGINT NOT NULL DEFAULT 0,
  utilization_pct  NUMERIC NOT NULL DEFAULT 0,
  on_call_ms       BIGINT NOT NULL DEFAULT 0,
  available_ms     BIGINT NOT NULL DEFAULT 0,
  computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, driver_id)
);
CREATE INDEX IF NOT EXISTS idx_forklift_driver_daily_name_day
  ON forklift_driver_daily (name, day);
-- Override map for the few forklift names that don't match the plant roster
-- (driver -> plant person) or work centers (workstation -> WC).
CREATE TABLE IF NOT EXISTS forklift_name_map (
  kind           TEXT NOT NULL,   -- 'driver' | 'workstation'
  forklift_name  TEXT NOT NULL,
  plant_name     TEXT NOT NULL,
  PRIMARY KEY (kind, forklift_name)
);
-- 2026-06-26: live "what's open right now" mirror for the Exception Inbox.
-- Bookkeeping for the reconcile tick (inbox_reconcile): diffed against the
-- freshly-computed open set to detect items that left without a human action
-- (logged as auto_resolved). Not a source of truth — rebuilt from the snapshot.
CREATE TABLE IF NOT EXISTS inbox_open_items (
  item_key       TEXT PRIMARY KEY,
  item_kind      TEXT NOT NULL,
  person_name    TEXT,
  category_label TEXT,
  priority       TEXT,
  first_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2026-06-29: weekly Odoo calendar-conflict monitor state (single row).
-- reported_emp_ids is the conflict set last reported; last_run_at gates the
-- ~weekly cadence so frequent redeploys only re-check the gate.
CREATE TABLE IF NOT EXISTS calendar_conflict_monitor (
  id                INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  odoo_task_id      INTEGER,
  reported_emp_ids  INTEGER[] NOT NULL DEFAULT '{}',
  last_run_at       TIMESTAMPTZ
);

-- 2026-07-01: page-usage tracking. One row per (day, matched route pattern,
-- method, signed-in user) with a running view count, upserted from an
-- in-memory counter on the warmer tick (never per-request). Storing the route
-- *pattern* (e.g. /staffing/people/{name}) not the concrete URL is what makes
-- the counts aggregable; a row per user gives distinct-user counts exactly.
-- user_email is '' for anonymous kiosk/TV traffic. Feeds /admin/page-usage.
CREATE TABLE IF NOT EXISTS page_views (
  day         DATE NOT NULL,
  route       TEXT NOT NULL,
  method      TEXT NOT NULL,
  user_email  TEXT NOT NULL DEFAULT '',
  views       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, route, method, user_email)
);
-- Report scans a recent day-window then groups by route.
CREATE INDEX IF NOT EXISTS page_views_day ON page_views (day);

-- 2026-07-08: machine breakdown incidents (Exception Inbox). One open
-- incident per (wc_name, day) at a time — a card stays open until it's
-- resolved (recovered / handled / dismissed) before a new one for the same
-- machine can open. No FK to keep this denormalized like the rest of the
-- inbox tables (a resolved incident's row must survive independently).
CREATE TABLE IF NOT EXISTS machine_breakdowns (
  id                BIGSERIAL PRIMARY KEY,
  wc_name           TEXT NOT NULL,
  day               DATE NOT NULL,
  detected_stop_utc TIMESTAMPTZ NOT NULL,  -- when output was last seen before the breakdown
  source            TEXT NOT NULL DEFAULT 'auto' CHECK (source IN ('auto', 'manual')),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at       TIMESTAMPTZ,
  resolution        TEXT CHECK (resolution IN ('recovered', 'handled', 'dismissed')),  -- NULL while open
  resume_utc        TIMESTAMPTZ  -- when the machine started producing again; may precede resolved_at if a manager still has to act
);
-- Hard dedupe backstop (mirrors employee_notifications_dedupe): enforces the
-- "one open incident per (wc_name, day)" invariant at the DB level, not just
-- via application logic.
CREATE UNIQUE INDEX IF NOT EXISTS machine_breakdowns_open_idx
  ON machine_breakdowns (wc_name, day) WHERE resolved_at IS NULL;

-- 2026-07-08: per-operator 15-minute deferral on a breakdown card row.
-- Mirrors late_snoozes.
CREATE TABLE IF NOT EXISTS breakdown_snoozes (
  breakdown_id  BIGINT NOT NULL,
  person_name   TEXT NOT NULL,
  until_utc     TIMESTAMPTZ NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (breakdown_id, person_name)
);

-- 2026-07-08: link wc_time_attributions rows back to the machine_breakdowns
-- incident that created them, so a dismiss ("Not a breakdown") can delete
-- exactly this incident's exclusion rows without touching a different,
-- already-resolved incident on the same machine/day.
ALTER TABLE wc_time_attributions ADD COLUMN IF NOT EXISTS breakdown_id BIGINT;
CREATE INDEX IF NOT EXISTS wc_time_attributions_breakdown_idx
  ON wc_time_attributions (breakdown_id) WHERE breakdown_id IS NOT NULL;

-- 2026-07-08: per-record minutes excluded from a person's expected due to a
-- machine breakdown (source='breakdown' wc_time_attributions windows). Written
-- by precompute alongside units/downtime/hours; read by the leaderboard
-- averages and the recycling per-WC expected calc to shrink the expected
-- denominator without touching units.
ALTER TABLE production_daily ADD COLUMN IF NOT EXISTS excluded_minutes NUMERIC NOT NULL DEFAULT 0;
"""
