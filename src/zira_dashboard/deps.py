"""Shared dependencies for route modules.

Holds the FastAPI-level singletons (the Zira HTTP client, the Jinja2 templates
loader) and the small leaf helpers that several route modules need. Route
modules import from here rather than from ``app`` to keep the import graph
acyclic: ``app`` depends on ``deps`` and ``routes``; ``routes`` depend on
``deps``; ``deps`` only depends on small leaf modules (``stations``,
``leaderboard``, ``staffing``).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi.templating import Jinja2Templates

from zira_probe.client import ZiraClient

from . import staffing
from .leaderboard import StationTotal
from .stations import STATIONS

load_dotenv()

_api_key = os.environ.get("ZIRA_API_KEY")
if not _api_key:
    raise RuntimeError("ZIRA_API_KEY missing. Set it in .env.")
_base_url = os.environ.get("ZIRA_BASE_URL", "https://api.zira.us/public/")

client = ZiraClient(api_key=_api_key, base_url=_base_url)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

RUNNING_STALENESS = timedelta(minutes=10)


def _parse_day(day: str | None) -> date:
    if not day:
        return datetime.now(timezone.utc).date()
    return date.fromisoformat(day)


def _window_dates(window: str, today_d: date) -> tuple[date, date]:
    """Return (start, end) inclusive for one of:
    today|yesterday|week|last_week|month|last_month|quarter|year.
    """
    if window == "today":
        return today_d, today_d
    if window == "yesterday":
        y = today_d - timedelta(days=1)
        return y, y
    if window == "last_week":
        monday = today_d - timedelta(days=today_d.weekday())
        return monday - timedelta(days=7), monday - timedelta(days=1)
    if window == "month":
        return today_d.replace(day=1), today_d
    if window == "last_month":
        last_of_prev = today_d.replace(day=1) - timedelta(days=1)
        return last_of_prev.replace(day=1), last_of_prev
    if window == "quarter":
        q_start_month = ((today_d.month - 1) // 3) * 3 + 1
        return today_d.replace(month=q_start_month, day=1), today_d
    if window == "year":
        return today_d.replace(month=1, day=1), today_d
    # default: week (Monday → today)
    monday = today_d - timedelta(days=today_d.weekday())
    return monday, today_d


def _filter_stations(category: str | None):
    if not category or category == "All":
        return list(STATIONS)
    return [s for s in STATIONS if s.category == category]


def _state(total: StationTotal, now: datetime, is_today: bool) -> str:
    if total.last_reading_at is None:
        return "Offline"
    if not is_today:
        return "—"
    if now - total.last_reading_at > RUNNING_STALENESS:
        return "Offline"
    if total.last_status == "Working":
        return "Running"
    return "Stopped"


def _relative(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "no data today"
    delta = now - dt
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s ago"
    m = s // 60
    if m < 60:
        return f"{m} min ago"
    h = m // 60
    return f"{h}h {m % 60}m ago"


def _fmt_duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60}m"


def _iter_saved_schedule_files():
    """Yield (date, Schedule) for every saved schedule file, sorted newest first."""
    d = staffing.SCHEDULES_DIR
    if not d.exists():
        return
    files = sorted(d.glob("*.json"), reverse=True)
    for p in files:
        stem = p.stem
        try:
            day = date.fromisoformat(stem)
        except ValueError:
            continue
        sched = staffing.load_schedule(day)
        yield day, sched
