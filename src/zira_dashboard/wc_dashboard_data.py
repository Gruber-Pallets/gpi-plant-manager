"""Per-WC dashboard data-prep helpers.

Pure functions over the existing `cached_leaderboard`, `awards`, and
`work_centers_store` modules. Each helper takes a WC name (or slug) +
a date and returns a widget-ready dict the template can iterate.

The single-page dashboard at /wc/{slug} (editor) and /tv/wc/{slug}
(TV) compose these helpers into one render. No FastAPI / template
imports here — keep this module testable without standing up the app.
"""
from __future__ import annotations

import re


def slug_for_wc(name: str) -> str:
    """URL-safe slug derived from a work-center name.

    Lowercase, alphanumerics + hyphens; everything else collapses to
    a single hyphen. Used as the dashboard layout key (`wc:{slug}`)
    and in URLs (`/wc/{slug}`).

    Examples:
      'Repair 1'       -> 'repair-1'
      'Hand Build #1'  -> 'hand-build-1'
      'Trim Saw 12'    -> 'trim-saw-12'
    """
    s = (name or "").strip().lower()
    # Replace every run of non-alphanumeric chars with a single hyphen.
    s = re.sub(r"[^a-z0-9]+", "-", s)
    # Strip leading + trailing hyphens.
    return s.strip("-")
