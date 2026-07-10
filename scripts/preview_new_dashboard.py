"""Render the New dashboard to static HTML for visual QA.

Uses a deterministic busy-day fixture so the editor and both TV themes can be
opened locally without making Zira calls. Output goes to
``scripts/_preview_new_out/``; its ``static`` symlink makes root-absolute
asset paths work when serving the directory with ``python -m http.server``.

Run:
    .venv/bin/python scripts/preview_new_dashboard.py
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("SESSION_SECRET", "preview-secret-32-bytes-of-data!!!!")
os.environ.setdefault("ZIRA_API_KEY", "preview-dummy")

from fastapi.testclient import TestClient  # noqa: E402

from zira_dashboard.app import app  # noqa: E402
from zira_dashboard.routes import departments  # noqa: E402
from zira_dashboard.stations import Station  # noqa: E402

OUT = Path(__file__).parent / "_preview_new_out"


def _busy_day(d, now, is_today_d, align_to_standard=False):
    station = Station("42345", "Junior #2", "New", "New")
    buckets = [
        {"label": "7:00", "actual": 47, "target": 48, "in_progress": False},
        {"label": "7:15", "actual": 51, "target": 48, "in_progress": True},
    ]
    return {
        "total_units": 98,
        "total_downtime": 0,
        "elapsed": 30,
        "available": 30,
        "uptime_minutes": 30,
        "total_man_hours": 0.5,
        "total_recycling_people": 1,
        "per_wc_units": {"Junior #2": 98},
        "per_wc_downtime": {"Junior #2": 0},
        "per_wc_expected": {"Junior #2": 96.0},
        "per_wc_who": {"Junior #2": "Lauro"},
        "per_wc_state": {"Junior #2": "working"},
        "per_wc_category": {"Junior #2": "New"},
        "per_wc_station_obj": {"Junior #2": station},
        "active_wc_names": {"Junior #2"},
        "schedule_assignments": {"Junior #2": ["Lauro"]},
        "group_buckets": {"New": buckets},
        "shift_start_label": "07:00",
    }


def _render(client, url):
    response = client.get(url)
    assert response.status_code == 200, (url, response.status_code, response.text[:500])
    return response.text


def main():
    OUT.mkdir(exist_ok=True)
    # Root-absolute /static/... refs resolve when the output directory is served.
    link = OUT / "static"
    real_static = Path(__file__).resolve().parent.parent / "src/zira_dashboard/static"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(real_static)

    client = TestClient(app)
    variants = [
        ("editor.html", "/new"),
        ("tv_dark.html", "/tv/new?theme=dark"),
        ("tv_light.html", "/tv/new?theme=light"),
    ]
    for filename, url in variants:
        with patch.object(departments, "_new_day_data", _busy_day):
            # Bypass the per-variant response cache between renders.
            from zira_dashboard import _http_cache

            _http_cache.invalidate_all_cache()
            html = _render(client, url)
        (OUT / filename).write_text(html)
        print("wrote", OUT / filename, len(html), "bytes")


if __name__ == "__main__":
    main()
