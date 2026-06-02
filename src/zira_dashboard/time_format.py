"""Small, dependency-free time-of-day formatters.

Relocated out of the (deleted) stratustime_client so the staffing scheduler
can render compact assignment time ranges without dragging in anything else.
"""
from __future__ import annotations

from datetime import datetime


def fmt_time_short(dt_str: str) -> str:
    """Format an ISO datetime string like '2026-04-29T09:00:00' as a short
    time-of-day: '9a', '9:30a', '12p', '1:15p'. Returns '' on parse failure.
    """
    try:
        dt = datetime.fromisoformat(dt_str)
    except (ValueError, TypeError):
        return ""
    h, m = dt.hour, dt.minute
    period = "a" if h < 12 else "p"
    h12 = h % 12 or 12
    if m == 0:
        return f"{h12}{period}"
    return f"{h12}:{m:02d}{period}"


def fmt_time_range(start_str: str, end_str: str) -> str:
    """Compact time range. Drops am/pm from start when both share the same period.
    Examples: '9-10a', '11a-1p', '9:30-10:15a', '12-1p'.
    """
    s = fmt_time_short(start_str)
    e = fmt_time_short(end_str)
    if not s or not e:
        return ""
    if s[-1] == e[-1]:
        s = s[:-1]
    return f"{s}-{e}"
