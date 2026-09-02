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
from zira_dashboard.people_performance_view import (  # noqa: E402
    _filter_summary,
    _schedule_time_groups,
    _schedule_track_width_rem,
)
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
        "hover_points": (),
        "hover_start_ms": "",
        "hover_end_ms": "",
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


def _production_summary(goal, uptime, downtime, production):
    return (
        ("Goal", goal),
        ("Uptime", uptime),
        ("Downtime", downtime),
        ("Production", production),
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


def _schedule_time(index: int) -> str:
    hour = 6 + index // 4
    display_hour = hour if hour <= 12 else hour - 12
    return f"{display_hour}:{15 * (index % 4):02d}"


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
            _production_summary("75%", "92%", "35 min", "126/168"),
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
            _production_summary("112%", "99%", "4 min", "190/170"),
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
            _production_summary("103%", "96%", "9 min", "206/200"),
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
            _production_summary("108%", "97%", "8 min", "108/100"),
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
    schedule_markers = tuple(
        {
            "left_pct": 100.0 * index / 32,
            "kind": "start" if index == 0 else "end" if index == 32 else "break",
            "visible_label": (
                "6:00 AM" if index == 0 else "2:00 PM" if index == 32 else _schedule_time(index)
            ),
            "aria_label": (
                "Shift starts at 6:00 AM"
                if index == 0
                else "Shift ends at 2:00 PM"
                if index == 32
                else f"Custom break {index} starts"
            ),
        }
        for index in range(33)
    )
    schedule_time_groups = _schedule_time_groups(schedule_markers)

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
        "schedule_markers": schedule_markers,
        "schedule_time_groups": schedule_time_groups,
        "schedule_track_width_rem": _schedule_track_width_rem(schedule_time_groups),
        "source_warnings": (
            {
                "key": "111111111111111111111111",
                "kind": "production_metric_unavailable",
                "label": "Production metric unavailable: Trim Saw 1",
                "summary": "Trim Saw 1 production could not be calculated.",
            },
            {
                "key": "222222222222222222222222",
                "kind": "production_metric_unavailable",
                "label": "Production metric unavailable: Hand Build #1",
                "summary": "Hand Build #1 production could not be calculated.",
            },
            {
                "key": "333333333333333333333333",
                "kind": "unmatched_forklift_calls",
                "label": "Unmatched forklift calls: 107",
                "summary": "Forklift calls could not be matched to active employees.",
            },
        ),
        "working_now": 8,
        "worked_earlier": 2,
        "needs_attention": 4,
        "status_filter": "working",
        "attention_only": False,
        "active": "people",
        "active_dashboard_key": "people",
        "today": "2026-08-28",
        "rows_url": "/people-performance/rows",
        "poll_disabled": True,
    }


def _zero_count_context() -> dict:
    context = _context()
    sections = tuple(
        {
            **section,
            "rows": tuple(row for row in section["rows"] if row["is_active"]),
        }
        for section in context["sections"]
    )
    return {
        **context,
        "sections": sections,
        "worked_earlier": 0,
        "status_filter": None,
    }


def _filter_context(status_filter: str | None, *, attention_only: bool = False) -> dict:
    context = _context()
    rows = tuple(row for section in context["sections"] for row in section["rows"])
    visible_rows = tuple(
        row
        for row in rows
        if (
            status_filter is None
            or (status_filter == "working" and row["is_active"])
            or (status_filter == "earlier" and not row["is_active"])
        )
        and (not attention_only or row["attention_reasons"])
    )
    visible_ids = {row["employee_odoo_id"] for row in visible_rows}
    sections = tuple(
        {
            **section,
            "rows": tuple(row for row in section["rows"] if row["employee_odoo_id"] in visible_ids),
        }
        for section in context["sections"]
    )
    return {
        **context,
        "sections": sections,
        "status_filter": status_filter,
        "attention_only": attention_only,
        "total_people": len(rows),
        "visible_people": len(visible_rows),
        "filtered_empty": bool((status_filter is not None or attention_only) and not visible_rows),
        "filter_summary": _filter_summary(
            status_filter=status_filter,
            attention_only=attention_only,
            visible=len(visible_rows),
            total=len(rows),
            working=context["working_now"],
            earlier=context["worked_earlier"],
        ),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copytree(STATIC, OUT / "static", dirs_exist_ok=True)
    client = TestClient(app)
    for filename, context in (
        ("index.html", _context()),
        ("all.html", _filter_context(None)),
        ("earlier.html", _filter_context("earlier")),
        (
            "working-attention.html",
            _filter_context("working", attention_only=True),
        ),
        ("zero-count.html", _zero_count_context()),
    ):
        with (
            patch.object(
                people_performance,
                "_context",
                lambda *args, context=context, **kwargs: context,
            ),
            patch.dict(
                templates.env.globals,
                {"nav_inbox_summary": lambda: EMPTY_NAV_SUMMARY},
            ),
        ):
            response = client.get("/people-performance?day=2026-08-28")
        response.raise_for_status()
        html = response.text.replace('href="/static/', 'href="static/').replace(
            'src="/static/', 'src="static/'
        )
        (OUT / filename).write_text(html, encoding="utf-8")
    print(OUT.resolve())


if __name__ == "__main__":
    main()
