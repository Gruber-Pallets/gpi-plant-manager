"""Weekly monitor: diff the Odoo calendar-conflict set and keep one Odoo task.

Runs on the in-process warmer (see app.py), gated to ≥7 days via the
calendar_conflict_monitor state row. Best-effort — the warmer logs/swallows.
See docs/superpowers/specs/2026-06-29-calendar-conflict-monitor-design.md
"""

from __future__ import annotations


def decide(current_ids, reported_ids) -> dict:
    """Pure diff of the conflict employee-id sets.

    Returns {changed, added (sorted ids), removed (sorted ids), now_empty}.
    """
    current = set(current_ids)
    reported = set(reported_ids)
    added = sorted(current - reported)
    removed = sorted(reported - current)
    return {
        "changed": bool(added or removed),
        "added": added,
        "removed": removed,
        # Only meaningful when changed; run_once reads it solely inside the
        # `if changed` branch. "changed AND current set empty" → archive.
        "now_empty": bool(added or removed) and len(current) == 0,
    }
