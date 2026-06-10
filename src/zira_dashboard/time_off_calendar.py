"""Pure Who's-Out calendar engine — privacy-safe leave labels, holiday-date
parse, month-grid assembly, and per-day fan-out of approved leaves/holidays.
No I/O — the route does the DB/Odoo fetch and passes rows in. Extracted from
routes/timeclock_time_off.py."""

from __future__ import annotations

import calendar as _cal
from datetime import date as _date, timedelta as _td

from .time_format import fmt_decimal_hour


def label_for(r: dict) -> str:
    """Render a privacy-safe timing label for one approved leave row.

    Deliberately omits the leave-type name — coworkers should see that
    someone is out and when, but not why. The four shapes map to:
      - ``full_day``   -> ``"full day"``
      - ``late_arrival`` -> ``"arrives 9:00am"`` (arrival = hour_to)
      - ``early_leave``  -> ``"leaves 2:00pm"`` (leave = hour_from)
      - ``midday_gap``   -> ``"10:00am–12:00pm"`` (gap = hour_from..hour_to)
    """
    if r["shape"] == "full_day":
        return "full day"
    hf = float(r["hour_from"] or 0)
    ht = float(r["hour_to"] or 0)
    if r["shape"] == "late_arrival":
        return f"arrives {fmt_decimal_hour(ht)}"
    if r["shape"] == "early_leave":
        return f"leaves {fmt_decimal_hour(hf)}"
    return f"{fmt_decimal_hour(hf)}–{fmt_decimal_hour(ht)}"


# How much shorter than the full company shift an off-window may be and still
# count as "the whole working day" — absorbs lunch/rounding and small shift
# mismatches between the global schedule and an individual's resource calendar.
_FULL_DAY_TOL = 0.5


def is_full_day(shape, hour_from, hour_to, shift_len: float) -> bool:
    """Whether a leave row occupies essentially the whole working day.

    ``full_day`` shape is always full. Hour-bounded leaves need a closer
    look: leaves entered directly in Odoo and synced in are tagged
    ``midday_gap`` whenever they carry hour bounds (``time_off_sync`` can't
    tell late/early/gap from Odoo alone), so a full *unpaid* day off arrives
    here looking like a partial. We recover the distinction from the
    off-window span: if it covers at least the whole company shift
    (``shift_len`` decimal hours, minus a small tolerance) the person is out
    all day; the three genuine partials (arrive late / leave early / mid-day
    gap) only ever cover part of it. Missing bounds → treat as full (there's
    no timing to show anyway)."""
    if shape == "full_day":
        return True
    if hour_from is None or hour_to is None:
        return True
    return (float(hour_to) - float(hour_from)) >= shift_len - _FULL_DAY_TOL


def parse_holiday_date(s):
    """Odoo returns 'YYYY-MM-DD HH:MM:SS' strings for datetime fields.
    Strip the time component to get a date for the per-day fan-out.

    Tolerant of already-parsed values (passing in a ``date`` returns it
    as-is) so callers don't have to branch on type; returns ``None`` on
    anything unparseable so the caller can skip that holiday rather than
    crash the whole calendar render."""
    if not s:
        return None
    if hasattr(s, "isoformat"):  # already a date
        return s
    try:
        return _date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def month_bounds(month: str | None):
    """Parse ``month`` ("YYYY-MM") into the bounds the route needs to do its
    I/O and render the grid.

    Returns ``(first, range_start, range_end, prev_month_str, next_month_str)``:
      - ``first``           first-of-month ``date`` for the requested month.
      - ``range_start``     the Monday on/before ``first`` (grid's left edge).
      - ``range_end``       the Sunday on/after the month's last day.
      - ``prev_month_str``  ``"YYYY-MM"`` anchor for the prev-month nav link.
      - ``next_month_str``  ``"YYYY-MM"`` anchor for the next-month nav link.

    ``month`` comes from the prev/next nav links; anything missing or
    malformed falls back to the current month so a stale or typo'd URL
    never 500s the kiosk. The Dec→Jan year bump is preserved exactly."""
    today = _date.today()
    first = today.replace(day=1)
    if month:
        try:
            y, m = str(month).split("-")
            first = _date(int(y), int(m), 1)
        except (ValueError, TypeError):
            first = today.replace(day=1)
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    last = next_first - _td(days=1)
    range_start = first - _td(days=first.weekday())
    range_end = last + _td(days=(6 - last.weekday()))
    prev_first = (first - _td(days=1)).replace(day=1)
    return (
        first,
        range_start,
        range_end,
        prev_first.strftime("%Y-%m"),
        next_first.strftime("%Y-%m"),
    )


def fan_out_approved(leave_rows, holiday_rows, start_d: _date, end_d: _date) -> dict:
    """Fan approved leaves and public holidays out to ``{date: [entry, ...]}``.

    Pure half of the route's ``_approved_by_day``: the route fetches the
    ``state='validate'`` leave rows (joined to ``people`` for the name) and
    the company-wide public-holiday rows, then hands both lists here.

    ``leave_rows`` carry ``shape``/``date_from``/``date_to``/``hour_from``/
    ``hour_to``/``person_name``; each is expanded over every day it overlaps
    ``[start_d, end_d]`` into ``{name, label, full, shape, hour_from,
    hour_to}``. The ``full`` flag is the simple shape-only test (kept for the
    Who's Out kiosk grid); the raw ``shape``/``hour_from``/``hour_to`` ride
    along so a consumer that knows the shift length can refine full-vs-partial
    via :func:`is_full_day` (the staffing calendar does). ``holiday_rows``
    carry ``name``/``date_from``/``date_to``; each in-range day gets a
    ``{name, label: "Plant Closed", source: "holiday"}`` entry."""
    by_day: dict = {}
    for r in leave_rows:
        label = label_for(r)
        cur = max(r["date_from"], start_d)
        end = min(r["date_to"], end_d)
        while cur <= end:
            by_day.setdefault(cur, []).append({
                "name": r["person_name"], "label": label,
                "full": r["shape"] == "full_day",
                "shape": r["shape"],
                "hour_from": r["hour_from"], "hour_to": r["hour_to"],
            })
            cur = cur + _td(days=1)

    for h in holiday_rows:
        h_start = parse_holiday_date(h.get("date_from"))
        h_end = parse_holiday_date(h.get("date_to"))
        if not h_start or not h_end:
            continue
        cur = max(h_start, start_d)
        end = min(h_end, end_d)
        while cur <= end:
            by_day.setdefault(cur, []).append({
                "name": h.get("name") or "Plant Closed",
                "label": "Plant Closed",
                "source": "holiday",
            })
            cur = cur + _td(days=1)
    return by_day


def build_calendar_grid(month: str | None, off_map: dict) -> dict:
    """Assemble the Who's Out month grid from an already-fetched ``off_map``.

    Pure half of the route's ``_build_calendar_context``: the route resolves
    ``off_map`` via its (patchable) ``_approved_by_day`` and passes it in.
    Returns exactly the template fields common to both entry points — the
    heading, the Mon–Sat week cells (Sundays dropped, since the plant is
    closed), and the prev/next month anchors. Callers layer on
    ``token``/``public`` and ``bilingual`` themselves.

    Re-derives the month bounds via :func:`month_bounds` (pure/cheap) so the
    same malformed-month fallback applies whether or not the route already
    computed them for its I/O."""
    today = _date.today()
    first, _range_start, _range_end, prev_month, next_month = month_bounds(month)

    weeks = _cal.Calendar(firstweekday=0).monthdatescalendar(
        first.year, first.month,
    )
    week_cells = []
    for week in weeks:
        w = []
        for d in week:
            # Drop Sundays — the plant is closed, so the column carried no
            # signal; removing it gives the remaining Mon–Sat columns more room.
            if d.weekday() == 6:
                continue
            w.append({
                "num": d.day,
                "outside": d.month != first.month,
                "is_today": d == today,
                "weekend": d.weekday() >= 5,
                "names": off_map.get(d, []),
            })
        week_cells.append(w)

    return {
        "heading": first.strftime("%B %Y"),
        "weeks": week_cells,
        "prev_month": prev_month,
        "next_month": next_month,
        "is_current_month": first == today.replace(day=1),
    }
