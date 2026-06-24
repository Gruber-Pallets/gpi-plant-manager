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
-- theme. Seed list of 10 rows inserts on first boot only.
CREATE TABLE IF NOT EXISTS tv_displays (
  id                  SERIAL PRIMARY KEY,
  name                TEXT NOT NULL,
  slug                TEXT NOT NULL UNIQUE,
  kind                TEXT NOT NULL CHECK (kind IN ('vs_recycling', 'vs_new', 'wc')),
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

-- Tighten tv_displays.kind CHECK back down to the live kinds. Guarded so
-- the constraint is only added when missing — an unconditional DROP + ADD
-- takes an ACCESS EXCLUSIVE lock and re-validates the table on every boot.
DELETE FROM tv_displays WHERE kind = 'custom';
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'tv_displays_kind_check'
      AND conrelid = 'tv_displays'::regclass
  ) THEN
    ALTER TABLE tv_displays ADD CONSTRAINT tv_displays_kind_check
      CHECK (kind IN ('vs_recycling', 'vs_new', 'wc'));
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
  last_pulled_at           TIMESTAMPTZ,
  last_pushed_at           TIMESTAMPTZ,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
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

-- Manager handoff log. Each saved handoff snapshots the Exception Inbox
-- at that moment so unresolved daily work is preserved with the shift notes.
CREATE TABLE IF NOT EXISTS plant_shift_handoffs (
  id                  BIGSERIAL PRIMARY KEY,
  handoff_date        DATE NOT NULL,
  shift_label         TEXT NOT NULL DEFAULT 'Day',
  created_by          TEXT NOT NULL DEFAULT '',
  notes               TEXT NOT NULL DEFAULT '',
  follow_up_required  BOOLEAN NOT NULL DEFAULT FALSE,
  resolved_at         TIMESTAMPTZ,
  resolved_by         TEXT NOT NULL DEFAULT '',
  resolution_note     TEXT NOT NULL DEFAULT '',
  open_total          INTEGER NOT NULL DEFAULT 0,
  urgent_total        INTEGER NOT NULL DEFAULT 0,
  source_errors       JSONB NOT NULL DEFAULT '[]'::jsonb,
  exception_snapshot  JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS plant_shift_handoffs_date_idx
  ON plant_shift_handoffs (handoff_date DESC, created_at DESC);
ALTER TABLE plant_shift_handoffs
  ADD COLUMN IF NOT EXISTS follow_up_required BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS resolved_by TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS resolution_note TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS plant_shift_handoffs_open_followup_idx
  ON plant_shift_handoffs (created_at DESC)
  WHERE follow_up_required = TRUE AND resolved_at IS NULL;
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
"""
