"""Atomic approval capture for all local time-off writers."""

TIME_OFF_EMAIL_DDL = """
ALTER TABLE time_off_requests
  ADD COLUMN IF NOT EXISTS approval_source_updated_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS time_off_email_installation (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  installed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO time_off_email_installation (singleton) VALUES (TRUE)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS time_off_approval_email (
  request_id BIGINT PRIMARY KEY,
  delivery_key UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  person_odoo_id INTEGER NOT NULL,
  date_from DATE NOT NULL,
  date_to DATE NOT NULL,
  shape TEXT NOT NULL,
  hour_from NUMERIC(4,2),
  hour_to NUMERIC(4,2),
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending', 'sending', 'queued', 'sent', 'skipped', 'attention')),
  odoo_mail_id BIGINT,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS time_off_approval_email_due
  ON time_off_approval_email (next_attempt_at)
  WHERE state IN ('pending', 'sending', 'queued');

CREATE OR REPLACE FUNCTION capture_time_off_approval_email()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  require_source_timestamp BOOLEAN := TRUE;
BEGIN
  IF NEW.state <> 'validate'
     OR NEW.date_to < (now() AT TIME ZONE 'America/Chicago')::date THEN
    RETURN NEW;
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF OLD.state IN ('validate', 'draft_cancel') THEN
      RETURN NEW;
    END IF;
    -- Polls advance last_pulled_at; a local approval only advances its push
    -- timestamp and must not be suppressed by an old retained Odoo timestamp.
    require_source_timestamp := NEW.last_pulled_at IS DISTINCT FROM OLD.last_pulled_at;
  END IF;
  -- Historical imports include stale existing mirrors reconciled after boot.
  -- Fresh local approved inserts explicitly supply their approval timestamp.
  IF require_source_timestamp AND (
     NEW.approval_source_updated_at IS NULL OR
     NEW.approval_source_updated_at <
       (SELECT installed_at FROM time_off_email_installation WHERE singleton)) THEN
    RETURN NEW;
  END IF;
  INSERT INTO time_off_approval_email
    (request_id, person_odoo_id, date_from, date_to, shape, hour_from, hour_to)
  VALUES
    (NEW.id, NEW.person_odoo_id, NEW.date_from, NEW.date_to,
     NEW.shape, NEW.hour_from, NEW.hour_to)
  ON CONFLICT (request_id) DO NOTHING;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS time_off_approval_email_capture ON time_off_requests;
CREATE TRIGGER time_off_approval_email_capture
AFTER INSERT OR UPDATE OF state ON time_off_requests
FOR EACH ROW EXECUTE FUNCTION capture_time_off_approval_email();
"""
