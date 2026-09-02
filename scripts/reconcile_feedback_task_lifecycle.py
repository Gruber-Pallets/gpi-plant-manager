"""Queue existing owner tasks whose lifecycle lags local feedback."""

from __future__ import annotations

import argparse
import json
import sys

from zira_dashboard import db


SAFE_FAILURE = "feedback task lifecycle reconciliation failed safely"

_ELIGIBLE = """
SELECT COUNT(*) AS eligible FROM (
  SELECT f.id
  FROM feedback f
  JOIN feedback_task_delivery td ON td.feedback_id = f.id
  WHERE f.lifecycle_origin = 'local'
    AND f.status IN ('requested', 'in_progress', 'completed', 'declined')
    AND td.odoo_task_id IS NOT NULL
    AND td.state <> 'blocked'
    AND (
      td.desired_version <> f.projection_version
      OR td.desired_status <> f.status
      OR td.last_synced_version < f.projection_version
    )
  ORDER BY f.id
  LIMIT 100
) candidates
"""

_APPLY = """
WITH candidates AS (
  SELECT f.id, f.projection_version, f.status
  FROM feedback f
  JOIN feedback_task_delivery td ON td.feedback_id = f.id
  WHERE f.lifecycle_origin = 'local'
    AND f.status IN ('requested', 'in_progress', 'completed', 'declined')
    AND td.odoo_task_id IS NOT NULL
    AND td.state <> 'blocked'
    AND (
      td.desired_version <> f.projection_version
      OR td.desired_status <> f.status
      OR td.last_synced_version < f.projection_version
    )
  ORDER BY f.id
  FOR UPDATE OF td SKIP LOCKED
  LIMIT 100
), updated AS (
  UPDATE feedback_task_delivery td
  SET desired_version = candidates.projection_version,
      desired_status = candidates.status,
      state = 'pending', due_at = now(), attempt_count = 0,
      claim_owner = NULL, claim_token = NULL, claim_expires_at = NULL,
      last_error_summary = NULL, blocked_reason = NULL, updated_at = now()
  FROM candidates
  WHERE td.feedback_id = candidates.id
  RETURNING td.feedback_id
)
SELECT COUNT(*) AS queued FROM updated
"""


def _count(row: object, key: str) -> int:
    if not isinstance(row, dict) or type(row.get(key)) is not int or row[key] < 0:
        raise ValueError("reconciliation count is malformed")
    return row[key]


def run(*, apply: bool) -> dict[str, object]:
    with db.cursor() as cursor:
        cursor.execute(_ELIGIBLE)
        eligible = _count(cursor.fetchone(), "eligible")
        queued = 0
        if apply:
            cursor.execute(_APPLY)
            queued = _count(cursor.fetchone(), "queued")
    return {"eligible": eligible, "queued": queued, "applied": apply}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    try:
        try:
            db.init_pool()
            payload = run(apply=args.yes)
        finally:
            db.shutdown_pool()
    except Exception:
        raise SystemExit(SAFE_FAILURE) from None
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
