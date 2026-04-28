"""Server-persisted hidden-column list for the People Matrix.

A flat list of skill names that should be hidden in the matrix UI. When
the user toggles a skill type off, the JS adds every skill in that type
to this list (and removes them when toggled back on). Hiding is purely
visual — the underlying data in roster.json is untouched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

FILTER_PATH = Path("skill_filter.json")


def load_hidden() -> list[str]:
    if not FILTER_PATH.exists():
        return []
    try:
        data = json.loads(FILTER_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, dict) and isinstance(data.get("hidden"), list):
        return [str(x) for x in data["hidden"] if isinstance(x, str)]
    return []


def save_hidden(hidden: list[str]) -> None:
    payload = {"hidden": sorted(set(s.strip() for s in hidden if s and s.strip()))}
    tmp = FILTER_PATH.with_suffix(FILTER_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, FILTER_PATH)
