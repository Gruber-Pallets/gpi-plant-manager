#!/usr/bin/env python3
"""One-shot migration: if app_settings['skill_filter'] exists and there
are no views yet, create a "Default" view from it and mark it default.

Idempotent — safe to re-run; bails when views already exist.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from zira_dashboard import db, skill_matrix_views_store as views


def main() -> int:
    db.init_pool()
    db.bootstrap_schema()
    if views.list_views():
        print("Views already exist; skipping seed.")
        return 0
    rows = db.query("SELECT value FROM app_settings WHERE key = 'skill_filter'")
    hidden: list[str] = []
    if rows:
        v = rows[0]["value"]
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                v = None
        if isinstance(v, dict) and isinstance(v.get("hidden"), list):
            hidden = [str(x) for x in v["hidden"] if isinstance(x, str)]
    views.create_view("Default", {"hidden_skills": hidden})
    views.set_default("Default")
    print(f"Created 'Default' view with {len(hidden)} hidden skills; marked as default.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
