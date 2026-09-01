from __future__ import annotations

import os
from pathlib import Path
import shutil
from unittest.mock import patch

os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("SESSION_SECRET", "preview-secret-32-bytes-of-data")
os.environ.setdefault("ZIRA_API_KEY", "preview-dummy")

from fastapi.testclient import TestClient  # noqa: E402

from zira_dashboard.app import app  # noqa: E402
from zira_dashboard.deps import templates  # noqa: E402
from zira_dashboard.routes import people_performance  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "scripts/_preview_out/people_performance"
STATIC = ROOT / "src/zira_dashboard/static"
EMPTY_NAV_SUMMARY = {"total": 0, "urgent_total": 0, "source_errors": ()}


def _interval(
    key,
    left,
    width,
    location,
    role,
    state="neutral",
    transfer=False,
    detail="",
    line_runs=(),
    buckets=(),
    *,
    is_open=True,
    time_label="6:00 AM · Working now",
):
    return {
        "key": key,
        "left_pct": left,
        "width_pct": width,
        "location_name": location,
        "location_class": "location-" + str((sum(map(ord, location)) % 8) + 1),
        "role": role,
        "state": state,
        "is_transfer": transfer,
        "is_open": is_open,
        "time_label": time_label,
        "needs_touch_target": width <= 6.25,
        "line_runs": line_runs,
        "buckets": buckets,
        "detail": detail,
        "aria_label": ("Transferred to " if transfer else "") + detail,
    }


def _row(
    person_id,
    name,
    status,
    role,
    intervals,
    summary,
    reasons=(),
    active=True,
):
    short_intervals = tuple(item for item in intervals if item["needs_touch_target"])
    return {
        "employee_odoo_id": person_id,
        "person_name": name,
        "is_active": active,
        "status": status,
        "primary_role": role,
        "attention_reasons": reasons,
        "intervals": intervals,
        "short_intervals": short_intervals,
        "breaks": [{"left_pct": 68.75, "width_pct": 6.25, "label": "Planned break"}],
        "summary": summary,
        "unattached_forklift_calls": 0,
    }


def _ordered_rows(rows):
    def key(row):
        if not row["is_active"]:
            group = 2
        elif row["attention_reasons"]:
            group = 0
        else:
            group = 1
        return group, row["person_name"].casefold()

    return tuple(sorted(rows, key=key))


def _production_summary(goal, uptime, downtime, centers="1"):
    return (
        ("Goal", goal),
        ("Uptime", uptime),
        ("Downtime", downtime),
        ("Centers", centers),
    )


def _forklift_summary(calls, on_time, handling, score):
    return (
        ("Calls", calls),
        ("On time", on_time),
        ("Handling", handling),
        ("Score", score),
    )


def _other_summary(clocked, location, locations, status):
    return (
        ("Clocked", clocked),
        ("Location", location),
        ("Locations", locations),
        ("Status", status),
    )


def _context() -> dict:
    uptime_runs = (
        (
            {"x": 0.0, "y": 0.0},
            {"x": 18.0, "y": 0.0},
            {"x": 28.0, "y": 60.0},
            {"x": 42.0, "y": 28.0},
            {"x": 49.0, "y": 5.0},
        ),
        (
            {"x": 57.0, "y": 10.0},
            {"x": 68.75, "y": 0.0},
        ),
        (
            {"x": 75.0, "y": 0.0},
            {"x": 100.0, "y": 0.0},
        ),
    )
    forklift_runs = (
        ({"x": 7.0, "y": 0.0}, {"x": 21.0, "y": 0.0}),
        ({"x": 75.0, "y": 50.0}, {"x": 84.0, "y": 35.0}),
    )
    busy_buckets = (
        {
            "left_pct": 5.0,
            "width_pct": 3.125,
            "height_pct": 70.0,
            "late_markers": (),
        },
        {
            "left_pct": 90.0,
            "width_pct": 3.125,
            "height_pct": 100.0,
            "late_markers": (92.0,),
        },
        {
            "left_pct": 82.0,
            "width_pct": 3.125,
            "height_pct": 45.0,
            "late_markers": (),
        },
    )

    production_rows = (
        _row(
            101,
            "Amy Behind",
            "working now",
            "production",
            (
                _interval(
                    "101:repair-1:open",
                    0.0,
                    100.0,
                    "Repair 1",
                    "production",
                    "behind",
                    detail=(
                        "Repair 1, 6:00 AM. Working now. Behind goal. "
                        "126 credited units against a 168 goal. Uptime 92%. "
                        "Downtime 35 minutes."
                    ),
                    line_runs=uptime_runs,
                ),
            ),
            _production_summary("75%", "92%", "35 min"),
            ("behind goal",),
        ),
        _row(
            102,
            "Zed Ahead",
            "working now",
            "production",
            (
                _interval(
                    "102:repair-2:open",
                    0.0,
                    100.0,
                    "Repair 2",
                    "production",
                    "ahead",
                    detail=(
                        "Repair 2, 6:00 AM. Working now. Ahead goal. "
                        "190 credited units against a 170 goal. Uptime 99%. "
                        "Downtime 4 minutes."
                    ),
                    line_runs=(
                        (
                            {"x": 0.0, "y": 0.0},
                            {"x": 49.0, "y": 0.0},
                        ),
                        (
                            {"x": 57.0, "y": 2.0},
                            {"x": 68.75, "y": 0.0},
                        ),
                        (
                            {"x": 75.0, "y": 0.0},
                            {"x": 100.0, "y": 0.0},
                        ),
                    ),
                ),
            ),
            _production_summary("112%", "99%", "4 min"),
        ),
        _row(
            103,
            "Mia Mixed",
            "working now",
            "production",
            (
                _interval(
                    "103:repair-1:closed",
                    0.0,
                    20.0,
                    "Repair 1",
                    "production",
                    "ahead",
                    detail="Repair 1, 6:00 AM to 7:36 AM. Ahead goal.",
                    is_open=False,
                    time_label="6:00 AM to 7:36 AM",
                ),
                _interval(
                    "103:repair-2:five-minute",
                    20.0,
                    1.0417,
                    "Repair 2",
                    "production",
                    "behind",
                    True,
                    "Repair 2, 7:36 AM to 7:41 AM. Behind goal.",
                    is_open=False,
                    time_label="7:36 AM to 7:41 AM",
                ),
                _interval(
                    "103:tablets:closed",
                    21.0417,
                    43.9583,
                    "Tablets",
                    "forklift",
                    transfer=True,
                    detail="Tablets, 7:41 AM to 11:12 AM. 8 forklift calls.",
                    buckets=busy_buckets,
                    is_open=False,
                    time_label="7:41 AM to 11:12 AM",
                ),
                _interval(
                    "103:repair-3:open",
                    65.0,
                    35.0,
                    "Repair 3",
                    "production",
                    "ahead",
                    True,
                    "Repair 3, 11:12 AM. Working now. Ahead goal.",
                    line_runs=(
                        (
                            {"x": 0.0, "y": 0.0},
                            {"x": 10.71, "y": 0.0},
                        ),
                        (
                            {"x": 28.58, "y": 0.0},
                            {"x": 100.0, "y": 0.0},
                        ),
                    ),
                    time_label="11:12 AM · Working now",
                ),
            ),
            _production_summary("103%", "96%", "9 min", "3"),
        ),
        _row(
            104,
            "Chris Complete",
            "clocked out at 12:00 PM",
            "production",
            (
                _interval(
                    "104:repair-1:closed",
                    0.0,
                    75.0,
                    "Repair 1",
                    "production",
                    "ahead",
                    detail="Repair 1, 6:00 AM to 12:00 PM. Ahead goal.",
                    is_open=False,
                    time_label="6:00 AM to 12:00 PM",
                ),
            ),
            _production_summary("108%", "97%", "8 min"),
            active=False,
        ),
    )

    forklift_rows = (
        _row(
            201,
            "Ben Busy Driver",
            "working now",
            "forklift",
            (
                _interval(
                    "201:tablets:open",
                    0.0,
                    100.0,
                    "Tablets",
                    "forklift",
                    detail=(
                        "Tablets, 6:00 AM. Working now. 42 forklift calls. "
                        "Latest rolling on-time 88%. 1 Late call."
                    ),
                    line_runs=forklift_runs,
                    buckets=busy_buckets,
                ),
            ),
            _forklift_summary("42", "93%", "188 min", "91"),
        ),
        _row(
            202,
            "Dana Needs Attention",
            "working now",
            "forklift",
            (
                _interval(
                    "202:tablets:open",
                    8.0,
                    92.0,
                    "Tablets",
                    "forklift",
                    detail=(
                        "Tablets, 6:38 AM. Working now. 12 forklift calls. "
                        "Latest rolling on-time 67%. 3 Late calls."
                    ),
                    line_runs=(
                        (
                            {"x": 10.0, "y": 33.0},
                            {"x": 66.03, "y": 33.0},
                        ),
                        (
                            {"x": 72.83, "y": 33.0},
                            {"x": 90.0, "y": 33.0},
                        ),
                    ),
                    buckets=busy_buckets,
                ),
            ),
            _forklift_summary("12", "67%", "82 min", "64"),
            ("below on-time floor", "late call in last 30 minutes"),
        ),
        _row(
            203,
            "Eli Completed Driver",
            "clocked out at 1:20 PM",
            "forklift",
            (
                _interval(
                    "203:tablets:closed",
                    0.0,
                    91.67,
                    "Tablets",
                    "forklift",
                    detail="Tablets, 6:00 AM to 1:20 PM. 31 forklift calls.",
                    line_runs=(
                        (
                            {"x": 10.0, "y": 4.0},
                            {"x": 74.99, "y": 4.0},
                        ),
                        (
                            {"x": 81.82, "y": 4.0},
                            {"x": 95.0, "y": 4.0},
                        ),
                    ),
                    buckets=busy_buckets,
                    is_open=False,
                    time_label="6:00 AM to 1:20 PM",
                ),
            ),
            _forklift_summary("31", "96%", "154 min", "94"),
            active=False,
        ),
    )

    other_rows = (
        _row(
            301,
            "Noah Shipping",
            "working now",
            "other",
            (
                _interval(
                    "301:shipping:open",
                    0.0,
                    100.0,
                    "Shipping",
                    "other",
                    detail="Shipping, 6:00 AM. Working now. No metered goal applies.",
                ),
            ),
            _other_summary("8 hr", "Shipping", "1", "Working now"),
        ),
        _row(
            302,
            "Olivia Missing",
            "location missing",
            "other",
            (
                _interval(
                    "302:missing:open",
                    0.0,
                    100.0,
                    "Location missing",
                    "other",
                    state="unavailable",
                    detail=(
                        "Location unavailable because the attendance source reports "
                        "a missing location. Goal and uptime unavailable."
                    ),
                ),
            ),
            _other_summary("8 hr", "Location missing", "1", "Working now"),
            ("location missing",),
        ),
        _row(
            303,
            "Parker Stale",
            "source stale",
            "other",
            (
                _interval(
                    "303:stale:open",
                    0.0,
                    100.0,
                    "Shipping",
                    "other",
                    state="unavailable",
                    detail=(
                        "Location unavailable because the attendance source is stale. "
                        "Goal and uptime unavailable."
                    ),
                ),
            ),
            _other_summary("8 hr", "Shipping", "1", "Working now"),
            ("source stale",),
        ),
    )

    production_rows = _ordered_rows(production_rows)
    forklift_rows = _ordered_rows(forklift_rows)
    other_rows = _ordered_rows(other_rows)

    return {
        "day": "2026-08-28",
        "is_today": True,
        "as_of": "2:00 PM",
        "as_of_iso": "2026-08-28T19:00:00+00:00",
        "sections": (
            {
                "key": "production",
                "label": "Metered production",
                "rows": production_rows,
            },
            {"key": "forklift", "label": "Tablet forklift", "rows": forklift_rows},
            {
                "key": "other",
                "label": "Other non-metered people",
                "rows": other_rows,
            },
        ),
        "axis_labels": tuple(
            {
                "label": label,
                "left_pct": index * 12.5,
            }
            for index, label in enumerate(
                (
                    "6:00 AM",
                    "7:00 AM",
                    "8:00 AM",
                    "9:00 AM",
                    "10:00 AM",
                    "11:00 AM",
                    "12:00 PM",
                    "1:00 PM",
                )
            )
        ),
        "source_warnings": (),
        "working_now": 8,
        "worked_earlier": 2,
        "needs_attention": 4,
        "attention_only": False,
        "active": "people",
        "active_dashboard_key": "people",
        "today": "2026-08-28",
        "rows_url": "/people-performance/rows",
        "poll_disabled": True,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copytree(STATIC, OUT / "static", dirs_exist_ok=True)
    with (
        patch.object(people_performance, "_context", lambda *args, **kwargs: _context()),
        patch.dict(
            templates.env.globals,
            {"nav_inbox_summary": lambda: EMPTY_NAV_SUMMARY},
        ),
    ):
        response = TestClient(app).get("/people-performance?day=2026-08-28")
    response.raise_for_status()
    html = response.text.replace('href="/static/', 'href="static/').replace(
        'src="/static/', 'src="static/'
    )
    (OUT / "index.html").write_text(html, encoding="utf-8")
    print(OUT.resolve())


if __name__ == "__main__":
    main()
