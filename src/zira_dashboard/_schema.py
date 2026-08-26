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
ALTER TABLE people ADD COLUMN IF NOT EXISTS spanish_level SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE people DROP CONSTRAINT IF EXISTS people_spanish_level_check;
ALTER TABLE people ADD CONSTRAINT people_spanish_level_check
  CHECK (spanish_level BETWEEN 0 AND 3);
ALTER TABLE people ADD COLUMN IF NOT EXISTS resource_calendar_id INTEGER;
-- Raw Odoo name, kept alongside the compact roster label in `name` so the
-- leaderboards can display un-abbreviated names.
ALTER TABLE people ADD COLUMN IF NOT EXISTS full_name TEXT;

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
ALTER TABLE work_centers
  ADD COLUMN IF NOT EXISTS odoo_work_center_id INTEGER;
ALTER TABLE work_centers
  ADD COLUMN IF NOT EXISTS odoo_work_center_name TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS work_centers_odoo_work_center_id_unique
  ON work_centers (odoo_work_center_id)
  WHERE odoo_work_center_id IS NOT NULL;

-- Repair 4 received its Zira source after the work-center row already existed.
-- Fill only a blank mapping so a later deliberate replacement stays intact.
UPDATE work_centers
   SET meter_id = '44483'
 WHERE name = 'Repair 4'
   AND COALESCE(meter_id, '') = '';

-- Hand Build #1 received its Zira source after the work-center row existed.
-- Preserve later nonblank meter and nonzero goal choices.
UPDATE work_centers
   SET meter_id = '44484'
 WHERE name = 'Hand Build #1'
   AND COALESCE(meter_id, '') = '';

UPDATE work_centers
   SET goal_per_day_override = 400
 WHERE name = 'Hand Build #1'
   AND COALESCE(goal_per_day_override, 0) = 0;

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

CREATE TABLE IF NOT EXISTS group_default_people (
  group_name      TEXT NOT NULL REFERENCES groups(name) ON DELETE CASCADE,
  person_id       INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  sort_order      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (group_name, person_id)
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
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS saturday_availability_overrides JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE schedules
  ADD COLUMN IF NOT EXISTS published_delivery JSONB NOT NULL DEFAULT '{}'::jsonb;

UPDATE schedules
   SET published_delivery = jsonb_build_object('version', 'legacy-' || day::text)
 WHERE published
   AND COALESCE(published_delivery->>'version', '') = '';

CREATE TABLE IF NOT EXISTS person_rotation_preferences (
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  rotation_group TEXT NOT NULL,
  preference TEXT NOT NULL CHECK (preference IN ('primary', 'regular', 'occasional', 'never')),
  PRIMARY KEY (person_id, rotation_group)
);

-- Scheduling targets now include every qualified work center, not only the
-- original Recycled rotation groups. Remove the former static constraint from
-- existing databases; request validation remains the source of truth.
ALTER TABLE person_rotation_preferences
  DROP CONSTRAINT IF EXISTS person_rotation_preferences_rotation_group_check;

CREATE TABLE IF NOT EXISTS rotation_training_blocks (
  id BIGSERIAL PRIMARY KEY,
  trainee_id INTEGER NOT NULL REFERENCES people(id),
  trainer_id INTEGER NOT NULL REFERENCES people(id),
  skill_id INTEGER NOT NULL REFERENCES skills(id),
  start_day DATE NOT NULL,
  planned_attended_days SMALLINT NOT NULL CHECK (planned_attended_days > 0),
  status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'completing', 'completed', 'ended')),
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE rotation_training_blocks ADD COLUMN IF NOT EXISTS work_center TEXT;
ALTER TABLE rotation_training_blocks ADD COLUMN IF NOT EXISTS skill_ids INTEGER[];
UPDATE rotation_training_blocks SET skill_ids = ARRAY[skill_id] WHERE skill_ids IS NULL;
-- A durable completion claim prevents concurrent reconciliation requests from
-- both sending the external level-promotion write. Rebuild the original
-- unnamed-by-us CHECK as an idempotent migration for existing databases.
ALTER TABLE rotation_training_blocks
  DROP CONSTRAINT IF EXISTS rotation_training_blocks_status_check;
ALTER TABLE rotation_training_blocks
  ADD CONSTRAINT rotation_training_blocks_status_check
  CHECK (status IN ('active', 'paused', 'completing', 'completed', 'ended'));

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

ALTER TABLE late_arrivals
  ADD COLUMN IF NOT EXISTS minutes_late INTEGER
  CHECK (minutes_late IS NULL OR minutes_late > 0);

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

CREATE TABLE IF NOT EXISTS late_expected_arrivals (
  day             DATE NOT NULL,
  emp_id          TEXT NOT NULL,
  name            TEXT NOT NULL,
  expected_at_utc TIMESTAMPTZ NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, emp_id)
);
CREATE INDEX IF NOT EXISTS late_expected_arrivals_day_idx
  ON late_expected_arrivals(day);

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

ALTER TABLE schedules ADD COLUMN IF NOT EXISTS auto_enabled_work_centers JSONB;

-- Initialize the Settings-owned template before copying it into legacy
-- schedules.  A missing key follows the same recent-history first-run rule as
-- the application; an existing legacy key is reduced to known, ordered names.
INSERT INTO app_settings (key, value, updated_at)
SELECT 'rotation_auto_enabled_work_centers',
       COALESCE((
         SELECT jsonb_agg(ordered.name ORDER BY ordered.ordinality)
         FROM unnest(ARRAY[
           'Repair 1', 'Repair 2', 'Repair 3', 'Dismantler 4', 'Dismantler 3',
           'Dismantler 2', 'Dismantler 1', 'Trim Saw 1', 'Master Recycler',
           'Repair 4', 'Repair 5', 'Hand Build #2', 'Hand Build #1',
           'Chop/Notch', 'Big Build #1', 'Woodpecker #1', 'Junior #1',
           'Junior #2', 'Junior #3', 'Loading/Jockeying', 'Tablets',
           'Work Orders', 'Truck Driver'
         ]) WITH ORDINALITY AS ordered(name, ordinality)
         WHERE EXISTS (
           SELECT 1
           FROM schedule_assignments sa
           JOIN schedules s ON s.day = sa.day
           JOIN work_centers wc ON wc.id = sa.wc_id
           WHERE wc.name = ordered.name
             AND s.day < CURRENT_DATE
             AND s.day >= CURRENT_DATE - 28
             AND COALESCE((s.published_snapshot->>'testing_day')::boolean, s.testing_day, FALSE) = FALSE
         )
       ), '[]'::jsonb), now()
WHERE NOT EXISTS (
  SELECT 1 FROM app_settings WHERE key = 'rotation_auto_enabled_work_centers'
)
ON CONFLICT (key) DO NOTHING;

UPDATE app_settings settings
   SET value = COALESCE((
         SELECT jsonb_agg(ordered.name ORDER BY ordered.ordinality)
         FROM unnest(ARRAY[
           'Repair 1', 'Repair 2', 'Repair 3', 'Dismantler 4', 'Dismantler 3',
           'Dismantler 2', 'Dismantler 1', 'Trim Saw 1', 'Master Recycler',
           'Repair 4', 'Repair 5', 'Hand Build #2', 'Hand Build #1',
           'Chop/Notch', 'Big Build #1', 'Woodpecker #1', 'Junior #1',
           'Junior #2', 'Junior #3', 'Loading/Jockeying', 'Tablets',
           'Work Orders', 'Truck Driver'
         ]) WITH ORDINALITY AS ordered(name, ordinality)
         JOIN (
           SELECT DISTINCT value AS name
           FROM jsonb_array_elements_text(
             CASE WHEN jsonb_typeof(settings.value) = 'array'
                  THEN settings.value ELSE '[]'::jsonb END
           )
         ) legacy USING (name)
       ), '[]'::jsonb),
       updated_at = now()
 WHERE settings.key = 'rotation_auto_enabled_work_centers';

UPDATE schedules
   SET auto_enabled_work_centers = (
         SELECT value FROM app_settings
          WHERE key = 'rotation_auto_enabled_work_centers'
       )
 WHERE auto_enabled_work_centers IS NULL;

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

CREATE TABLE IF NOT EXISTS production_identity_aliases (
  legacy_emp_id    TEXT PRIMARY KEY,
  canonical_emp_id TEXT NOT NULL,
  confirmed_name   TEXT NOT NULL,
  confirmed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  source           TEXT NOT NULL,
  CHECK (legacy_emp_id <> canonical_emp_id)
);

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
  category_key        TEXT NOT NULL,
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
-- Legacy GOAT Watch alerts predate category keys. Keep them intact and let
-- the feature-owned finalizer populate the key for all new category alerts.
ALTER TABLE goat_alerts ADD COLUMN IF NOT EXISTS category_key TEXT;
CREATE INDEX IF NOT EXISTS idx_goat_alerts_day ON goat_alerts (achieved_day);

CREATE TABLE IF NOT EXISTS goat_notification_state (
  id          SMALLINT PRIMARY KEY CHECK (id = 1),
  enabled_on  DATE NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS goat_notification_days (
  day           DATE PRIMARY KEY,
  finalized_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS goat_slack_deliveries (
  id                BIGSERIAL PRIMARY KEY,
  goat_alert_id     INTEGER NOT NULL UNIQUE REFERENCES goat_alerts(id) ON DELETE CASCADE,
  client_msg_id     UUID NOT NULL,
  claim_token       UUID,
  status            TEXT NOT NULL DEFAULT 'pending'
                    CONSTRAINT goat_slack_deliveries_status_check
                    CHECK (status IN ('pending', 'sending', 'sent', 'suppressed')),
  attempts          INTEGER NOT NULL DEFAULT 0,
  last_error        TEXT,
  attempted_at      TIMESTAMPTZ,
  sent_at           TIMESTAMPTZ,
  suppressed_at     TIMESTAMPTZ,
  slack_message_ts  TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- The initial GOAT outbox shipped without this id. Keep bootstrap safe for
-- already-created databases; legacy rows receive an id when first claimed.
ALTER TABLE goat_slack_deliveries ADD COLUMN IF NOT EXISTS client_msg_id UUID;
ALTER TABLE goat_slack_deliveries ADD COLUMN IF NOT EXISTS claim_token UUID;
ALTER TABLE goat_slack_deliveries ADD COLUMN IF NOT EXISTS suppressed_at TIMESTAMPTZ;
ALTER TABLE goat_slack_deliveries
  DROP CONSTRAINT IF EXISTS goat_slack_deliveries_status_check;
ALTER TABLE goat_slack_deliveries
  ADD CONSTRAINT goat_slack_deliveries_status_check
  CHECK (status IN ('pending', 'sending', 'sent', 'suppressed'));
-- Only Task 3/4 feature alerts have a durable delivery. Backfill those rows
-- before adding the category-day uniqueness rule; dashboard-only alerts stay
-- NULL so their historical behavior is unchanged.
-- A prior bootstrap may already have made a newer keyed alert for the same
-- category-day. Make the keyed delivery use the legacy Slack id first, so an
-- in-flight legacy request and a later canonical retry share Slack's dedupe.
UPDATE goat_slack_deliveries canonical_delivery
SET client_msg_id = COALESCE(legacy_delivery.client_msg_id, canonical_delivery.client_msg_id)
FROM goat_alerts canonical_alert
JOIN goat_alerts legacy_alert
  ON legacy_alert.achieved_day = canonical_alert.achieved_day
JOIN goat_slack_deliveries legacy_delivery
  ON legacy_delivery.goat_alert_id = legacy_alert.id
WHERE canonical_delivery.goat_alert_id = canonical_alert.id
  AND legacy_alert.category_key IS NULL
  AND legacy_alert.group_name IN ('Repairs', 'Dismantlers', 'Juniors', 'Woodpecker', 'Hand Build')
  AND canonical_alert.category_key = CASE legacy_alert.group_name
    WHEN 'Repairs' THEN 'repairs'
    WHEN 'Dismantlers' THEN 'dismantlers'
    WHEN 'Juniors' THEN 'juniors'
    WHEN 'Woodpecker' THEN 'woodpecker'
    WHEN 'Hand Build' THEN 'hand_build'
  END;
-- Keep the duplicate alert as history, but take it out of the dashboard. The
-- durable-delivery join deliberately leaves dashboard-only legacy rows alone.
UPDATE goat_alerts legacy_alert
SET dismissed_at = now()
FROM goat_slack_deliveries delivery
WHERE delivery.goat_alert_id = legacy_alert.id
  AND legacy_alert.dismissed_at IS NULL
  AND legacy_alert.category_key IS NULL
  AND legacy_alert.group_name IN ('Repairs', 'Dismantlers', 'Juniors', 'Woodpecker', 'Hand Build')
  AND EXISTS (
    SELECT 1
    FROM goat_alerts keyed
    WHERE keyed.achieved_day = legacy_alert.achieved_day
      AND keyed.category_key = CASE legacy_alert.group_name
        WHEN 'Repairs' THEN 'repairs'
        WHEN 'Dismantlers' THEN 'dismantlers'
        WHEN 'Juniors' THEN 'juniors'
        WHEN 'Woodpecker' THEN 'woodpecker'
        WHEN 'Hand Build' THEN 'hand_build'
      END
  );
-- Retire only the dismissed duplicate's pending delivery. A currently sending
-- row may already be inside Slack and is instead suppressed by the claim guard.
UPDATE goat_slack_deliveries delivery
SET status = 'sent',
    sent_at = COALESCE(delivery.sent_at, now()),
    last_error = 'Migration deduplicated duplicate category-day GOAT alert'
FROM goat_alerts alert
WHERE delivery.goat_alert_id = alert.id
  AND delivery.status = 'pending'
  AND alert.category_key IS NULL
  AND alert.dismissed_at IS NOT NULL
  AND alert.group_name IN ('Repairs', 'Dismantlers', 'Juniors', 'Woodpecker', 'Hand Build')
  AND EXISTS (
    SELECT 1
    FROM goat_alerts keyed
    WHERE keyed.achieved_day = alert.achieved_day
      AND keyed.category_key = CASE alert.group_name
        WHEN 'Repairs' THEN 'repairs'
        WHEN 'Dismantlers' THEN 'dismantlers'
        WHEN 'Juniors' THEN 'juniors'
        WHEN 'Woodpecker' THEN 'woodpecker'
        WHEN 'Hand Build' THEN 'hand_build'
      END
  );
UPDATE goat_alerts alert
SET category_key = CASE alert.group_name
  WHEN 'Repairs' THEN 'repairs'
  WHEN 'Dismantlers' THEN 'dismantlers'
  WHEN 'Juniors' THEN 'juniors'
  WHEN 'Woodpecker' THEN 'woodpecker'
  WHEN 'Hand Build' THEN 'hand_build'
END
FROM goat_slack_deliveries delivery
WHERE delivery.goat_alert_id = alert.id
  AND alert.category_key IS NULL
  AND alert.group_name IN ('Repairs', 'Dismantlers', 'Juniors', 'Woodpecker', 'Hand Build')
  AND NOT EXISTS (
    SELECT 1
    FROM goat_alerts keyed
    WHERE keyed.achieved_day = alert.achieved_day
      AND keyed.category_key = CASE alert.group_name
        WHEN 'Repairs' THEN 'repairs'
        WHEN 'Dismantlers' THEN 'dismantlers'
        WHEN 'Juniors' THEN 'juniors'
        WHEN 'Woodpecker' THEN 'woodpecker'
        WHEN 'Hand Build' THEN 'hand_build'
      END
  );
CREATE UNIQUE INDEX IF NOT EXISTS idx_goat_alerts_category_day
  ON goat_alerts (achieved_day, category_key) WHERE category_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_goat_slack_deliveries_claim
  ON goat_slack_deliveries (status, attempted_at, id);

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

-- A durable staffing-resolution task created when an employee works despite
-- an approved full-day leave. Deliberately no FK to time_off_requests: the
-- leave mirror is Odoo-owned and can be deleted by its poller, while this
-- operational audit record must remain available to the Exception Inbox.
CREATE TABLE IF NOT EXISTS unexpected_worker_events (
  id                  BIGSERIAL PRIMARY KEY,
  day                 DATE NOT NULL,
  person_odoo_id      INTEGER NOT NULL,
  time_off_request_id BIGINT,
  odoo_leave_id       INTEGER,
  clock_in_wc         TEXT NOT NULL,
  confirmed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at         TIMESTAMPTZ,
  UNIQUE (day, person_odoo_id)
);
CREATE INDEX IF NOT EXISTS unexpected_worker_events_open_day_idx
  ON unexpected_worker_events (day, confirmed_at)
  WHERE resolved_at IS NULL;

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

CREATE TABLE IF NOT EXISTS company_holidays (
  odoo_id          INTEGER PRIMARY KEY,
  name             TEXT NOT NULL,
  date_from        DATE NOT NULL,
  date_to          DATE NOT NULL,
  odoo_date_from   TEXT NOT NULL,
  odoo_date_to     TEXT NOT NULL,
  last_pulled_at   TIMESTAMPTZ NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (date_to >= date_from)
);

CREATE TABLE IF NOT EXISTS company_holiday_sync_state (
  singleton        BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  last_success_at  TIMESTAMPTZ,
  last_attempt_at  TIMESTAMPTZ,
  last_error       TEXT
);

-- Optional Saturday-work recruiting. A Saturday starts closed for everyone;
-- only a voluntary commitment can later become an Unassigned schedule entry.
CREATE TABLE IF NOT EXISTS saturday_recruitments (
  day DATE PRIMARY KEY,
  day_kind TEXT NOT NULL DEFAULT 'saturday'
    CHECK (day_kind IN ('saturday', 'holiday')),
  event_name TEXT,
  holiday_odoo_id INTEGER,
  status TEXT NOT NULL CHECK (status IN ('recruiting', 'closed', 'published', 'cancelled')),
  shift_start TIME NOT NULL,
  shift_end TIME NOT NULL,
  response_deadline TIMESTAMPTZ NOT NULL,
  activated_by TEXT,
  activated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed_at TIMESTAMPTZ,
  published_at TIMESTAMPTZ,
  staffing_prepared_at TIMESTAMPTZ,
  cancelled_by TEXT,
  cancelled_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (shift_end > shift_start)
);

ALTER TABLE saturday_recruitments
  ADD COLUMN IF NOT EXISTS staffing_prepared_at TIMESTAMPTZ;
ALTER TABLE saturday_recruitments
  DROP CONSTRAINT IF EXISTS saturday_recruitments_day_check;
ALTER TABLE saturday_recruitments
  ADD COLUMN IF NOT EXISTS day_kind TEXT NOT NULL DEFAULT 'saturday';
ALTER TABLE saturday_recruitments
  ADD COLUMN IF NOT EXISTS event_name TEXT;
ALTER TABLE saturday_recruitments
  ADD COLUMN IF NOT EXISTS holiday_odoo_id INTEGER;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'saturday_recruitments_day_kind_check'
  ) THEN
    ALTER TABLE saturday_recruitments
      ADD CONSTRAINT saturday_recruitments_day_kind_check
      CHECK (day_kind IN ('saturday', 'holiday'));
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS saturday_recruitment_openings (
  day DATE NOT NULL REFERENCES saturday_recruitments(day) ON DELETE CASCADE,
  wc_id INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE RESTRICT,
  requested_count INTEGER NOT NULL CHECK (requested_count > 0),
  PRIMARY KEY (day, wc_id)
);

CREATE TABLE IF NOT EXISTS saturday_work_responses (
  day DATE NOT NULL REFERENCES saturday_recruitments(day) ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK (status IN ('later', 'declined', 'committed', 'cancelled')),
  availability_start TIME,
  availability_end TIME,
  eligible_wc_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  responded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  committed_at TIMESTAMPTZ,
  cancelled_at TIMESTAMPTZ,
  cancelled_by TEXT,
  cancellation_reason TEXT,
  punch_reminder_shown_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, person_id),
  CHECK (
    (status IN ('later', 'declined') AND availability_start IS NULL AND availability_end IS NULL)
    OR
    (status IN ('committed', 'cancelled') AND availability_start IS NOT NULL
      AND availability_end IS NOT NULL AND availability_end > availability_start)
  )
);

ALTER TABLE employee_notifications ADD COLUMN IF NOT EXISTS saturday_day DATE;
CREATE UNIQUE INDEX IF NOT EXISTS employee_notifications_saturday_dedupe
  ON employee_notifications (person_odoo_id, saturday_day, kind)
  WHERE saturday_day IS NOT NULL;

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

-- Auto salaried punch scoreboard: one row per fixed-wage person per plant day.
-- Each *_punch_id column is the timeclock_punches_log id of that slot's punch
-- (0 = simulated punch written in dry-run mode; NULL = not yet punched).
-- See docs/superpowers/specs/2026-08-26-auto-salaried-punch-design.md.
CREATE TABLE IF NOT EXISTS auto_salaried_runs (
  person_odoo_id       INTEGER NOT NULL,
  day                  DATE NOT NULL,
  skipped              BOOLEAN NOT NULL DEFAULT FALSE,
  skip_reason          TEXT,
  morning_in_punch_id  BIGINT,
  lunch_out_punch_id   BIGINT,
  lunch_in_punch_id    BIGINT,
  day_out_punch_id     BIGINT,
  lunch_dept_id        INTEGER,
  lunch_dept_name      TEXT,
  dept_patch_state     TEXT NOT NULL DEFAULT 'none'
                       CHECK (dept_patch_state IN ('none','pending','done','failed')),
  reverted             BOOLEAN NOT NULL DEFAULT FALSE,
  flagged              BOOLEAN NOT NULL DEFAULT FALSE,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (person_odoo_id, day)
);
CREATE INDEX IF NOT EXISTS idx_auto_salaried_runs_day ON auto_salaried_runs (day);

-- Days the auto-salaried robot could not handle safely ("needs a human").
CREATE TABLE IF NOT EXISTS auto_salaried_flags (
  id              BIGSERIAL PRIMARY KEY,
  person_odoo_id  INTEGER NOT NULL,
  day             DATE NOT NULL,
  reason          TEXT NOT NULL,
  details         TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at     TIMESTAMPTZ,
  UNIQUE (person_odoo_id, day, reason)
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

-- Append-only Auto-Lunch setting history. A NULL before-value marks the first
-- observed baseline; settings/external rows snapshot both sides of a change.
CREATE TABLE IF NOT EXISTS auto_lunch_setting_events (
  id                         BIGSERIAL PRIMARY KEY,
  before_enabled             BOOLEAN,
  before_observe_only        BOOLEAN,
  before_flex_after_hours    NUMERIC,
  before_flex_minutes        INTEGER,
  after_enabled              BOOLEAN NOT NULL,
  after_observe_only         BOOLEAN NOT NULL,
  after_flex_after_hours     NUMERIC NOT NULL,
  after_flex_minutes         INTEGER NOT NULL,
  actor_upn                  TEXT,
  actor_name                 TEXT,
  source                     TEXT NOT NULL
                             CHECK (source IN ('settings','external','baseline')),
  changed_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS auto_lunch_setting_events_changed_at_idx
  ON auto_lunch_setting_events (changed_at DESC, id DESC);

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
-- idempotent. RETAINED-BUT-UNUSED: the 2026-07-21 capacity-coverage redesign
-- dropped the time-to-claim SLA recommender, so nothing reads or writes this
-- column anymore. Kept (not dropped) to avoid a destructive migration; safe to
-- remove in a future cleanup.
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
  odoo_improvement_id BIGINT,
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
  remote_id BIGINT,
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

DO $feedback_remote_ids_bigint$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_attribute
    WHERE attrelid = 'feedback_odoo_sync'::regclass
      AND attname = 'odoo_improvement_id'
      AND atttypid = 'integer'::regtype
      AND NOT attisdropped
  ) THEN
    ALTER TABLE feedback_odoo_sync
      ALTER COLUMN odoo_improvement_id
      TYPE BIGINT USING odoo_improvement_id::BIGINT;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_attribute
    WHERE attrelid = 'feedback_odoo_attempts'::regclass
      AND attname = 'remote_id'
      AND atttypid = 'integer'::regtype
      AND NOT attisdropped
  ) THEN
    ALTER TABLE feedback_odoo_attempts
      ALTER COLUMN remote_id
      TYPE BIGINT USING remote_id::BIGINT;
  END IF;
END
$feedback_remote_ids_bigint$;

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

-- 2026-08-03: append-only Odoo payroll guard audit and singleton alert state.
CREATE TABLE IF NOT EXISTS payroll_work_entry_corrections (
  id                       BIGSERIAL PRIMARY KEY,
  odoo_work_entry_id       INTEGER NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('duration_update', 'delete_zero_regular')),
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
  corrected_at             TIMESTAMPTZ NOT NULL,
  CONSTRAINT payroll_work_entry_corrections_action_duration_check CHECK (
    (action = 'delete_zero_regular' AND after_duration = 0.0)
    OR (action = 'duration_update' AND after_duration > 0.0)
  ),
  CONSTRAINT payroll_work_entry_corrections_finite_totals_check CHECK (
    before_duration > '-Infinity'::DOUBLE PRECISION
    AND before_duration < 'Infinity'::DOUBLE PRECISION
    AND after_duration > '-Infinity'::DOUBLE PRECISION
    AND after_duration < 'Infinity'::DOUBLE PRECISION
    AND attendance_regular > '-Infinity'::DOUBLE PRECISION
    AND attendance_regular < 'Infinity'::DOUBLE PRECISION
    AND attendance_overtime > '-Infinity'::DOUBLE PRECISION
    AND attendance_overtime < 'Infinity'::DOUBLE PRECISION
    AND work_regular_before > '-Infinity'::DOUBLE PRECISION
    AND work_regular_before < 'Infinity'::DOUBLE PRECISION
    AND work_overtime > '-Infinity'::DOUBLE PRECISION
    AND work_overtime < 'Infinity'::DOUBLE PRECISION
  ),
  CONSTRAINT payroll_work_entry_corrections_verification_detail_check
    CHECK (btrim(verification_detail) <> '')
);

-- CREATE TABLE IF NOT EXISTS does not add constraints to the table created by
-- an earlier release. Add each named backstop exactly once during bootstrap.
DO $payroll_correction_constraints$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'payroll_work_entry_corrections'::regclass
      AND conname = 'payroll_work_entry_corrections_action_duration_check'
  ) THEN
    ALTER TABLE payroll_work_entry_corrections
      ADD CONSTRAINT payroll_work_entry_corrections_action_duration_check CHECK (
        (action = 'delete_zero_regular' AND after_duration = 0.0)
        OR (action = 'duration_update' AND after_duration > 0.0)
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'payroll_work_entry_corrections'::regclass
      AND conname = 'payroll_work_entry_corrections_finite_totals_check'
  ) THEN
    ALTER TABLE payroll_work_entry_corrections
      ADD CONSTRAINT payroll_work_entry_corrections_finite_totals_check CHECK (
        before_duration > '-Infinity'::DOUBLE PRECISION
        AND before_duration < 'Infinity'::DOUBLE PRECISION
        AND after_duration > '-Infinity'::DOUBLE PRECISION
        AND after_duration < 'Infinity'::DOUBLE PRECISION
        AND attendance_regular > '-Infinity'::DOUBLE PRECISION
        AND attendance_regular < 'Infinity'::DOUBLE PRECISION
        AND attendance_overtime > '-Infinity'::DOUBLE PRECISION
        AND attendance_overtime < 'Infinity'::DOUBLE PRECISION
        AND work_regular_before > '-Infinity'::DOUBLE PRECISION
        AND work_regular_before < 'Infinity'::DOUBLE PRECISION
        AND work_overtime > '-Infinity'::DOUBLE PRECISION
        AND work_overtime < 'Infinity'::DOUBLE PRECISION
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'payroll_work_entry_corrections'::regclass
      AND conname = 'payroll_work_entry_corrections_verification_detail_check'
  ) THEN
    ALTER TABLE payroll_work_entry_corrections
      ADD CONSTRAINT payroll_work_entry_corrections_verification_detail_check
      CHECK (btrim(verification_detail) <> '');
  END IF;
END
$payroll_correction_constraints$;

CREATE OR REPLACE FUNCTION reject_payroll_correction_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $payroll_correction_function$
BEGIN
  RAISE EXCEPTION 'payroll_work_entry_corrections is append-only; % is not allowed', TG_OP
    USING ERRCODE = '55000';
  RETURN NULL;
END
$payroll_correction_function$;

DO $payroll_correction_trigger$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'payroll_work_entry_corrections'::regclass
      AND tgname = 'payroll_work_entry_corrections_append_only'
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER payroll_work_entry_corrections_append_only
      BEFORE UPDATE OR DELETE ON payroll_work_entry_corrections
      FOR EACH ROW EXECUTE FUNCTION reject_payroll_correction_mutation();
  END IF;
END
$payroll_correction_trigger$;

DO $payroll_correction_truncate_trigger$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'payroll_work_entry_corrections'::regclass
      AND tgname = 'payroll_work_entry_corrections_reject_truncate'
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER payroll_work_entry_corrections_reject_truncate
      BEFORE TRUNCATE ON payroll_work_entry_corrections
      FOR EACH STATEMENT EXECUTE FUNCTION reject_payroll_correction_mutation();
  END IF;
END
$payroll_correction_truncate_trigger$;

CREATE INDEX IF NOT EXISTS payroll_work_entry_corrections_entry_idx
  ON payroll_work_entry_corrections (odoo_work_entry_id, corrected_at DESC);

-- Existing deployments created the append-only audit before correction
-- attempts existed. Add the nullable recovery identity without rewriting old
-- history, then enforce exactly one audit row for each finalized attempt.
ALTER TABLE payroll_work_entry_corrections
  ADD COLUMN IF NOT EXISTS attempt_id UUID;
CREATE UNIQUE INDEX IF NOT EXISTS payroll_work_entry_corrections_attempt_idx
  ON payroll_work_entry_corrections (attempt_id)
  WHERE attempt_id IS NOT NULL;

-- Durable intent written before Odoo. One pending row per Work Entry lets a
-- later process determine whether a lost RPC response committed the change.
CREATE TABLE IF NOT EXISTS payroll_work_entry_correction_attempts (
  attempt_id              UUID PRIMARY KEY,
  odoo_work_entry_id      INTEGER NOT NULL,
  action                  TEXT NOT NULL CHECK (
    action IN ('duration_update', 'delete_zero_regular')
  ),
  employee_odoo_id        INTEGER NOT NULL,
  employee_name           TEXT NOT NULL,
  work_date               DATE NOT NULL,
  attendance_id           INTEGER NOT NULL,
  before_duration         DOUBLE PRECISION NOT NULL,
  after_duration          DOUBLE PRECISION NOT NULL,
  attendance_regular      DOUBLE PRECISION NOT NULL,
  attendance_overtime     DOUBLE PRECISION NOT NULL,
  work_regular_before     DOUBLE PRECISION NOT NULL,
  work_overtime           DOUBLE PRECISION NOT NULL,
  last_reason             TEXT NOT NULL DEFAULT 'pending_correction',
  last_detail             TEXT NOT NULL DEFAULT 'correction intent saved',
  created_at              TIMESTAMPTZ NOT NULL,
  updated_at              TIMESTAMPTZ NOT NULL,
  CONSTRAINT payroll_work_entry_correction_attempts_entry_unique
    UNIQUE (odoo_work_entry_id),
  CONSTRAINT payroll_work_entry_correction_attempts_action_duration_check CHECK (
    (action = 'delete_zero_regular' AND after_duration = 0.0)
    OR (action = 'duration_update' AND after_duration > 0.0)
  ),
  CONSTRAINT payroll_work_entry_correction_attempts_finite_totals_check CHECK (
    before_duration > '-Infinity'::DOUBLE PRECISION
    AND before_duration < 'Infinity'::DOUBLE PRECISION
    AND after_duration > '-Infinity'::DOUBLE PRECISION
    AND after_duration < 'Infinity'::DOUBLE PRECISION
    AND attendance_regular > '-Infinity'::DOUBLE PRECISION
    AND attendance_regular < 'Infinity'::DOUBLE PRECISION
    AND attendance_overtime > '-Infinity'::DOUBLE PRECISION
    AND attendance_overtime < 'Infinity'::DOUBLE PRECISION
    AND work_regular_before > '-Infinity'::DOUBLE PRECISION
    AND work_regular_before < 'Infinity'::DOUBLE PRECISION
    AND work_overtime > '-Infinity'::DOUBLE PRECISION
    AND work_overtime < 'Infinity'::DOUBLE PRECISION
  ),
  CONSTRAINT payroll_work_entry_correction_attempts_reason_check
    CHECK (btrim(last_reason) <> ''),
  CONSTRAINT payroll_work_entry_correction_attempts_detail_check
    CHECK (btrim(last_detail) <> '')
);

CREATE TABLE IF NOT EXISTS payroll_work_entry_guard_monitor (
  id                    INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  odoo_task_id          INTEGER,
  reported_issue_keys   TEXT[] NOT NULL DEFAULT '{}',
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
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
