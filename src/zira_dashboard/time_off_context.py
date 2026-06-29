"""Decision context for time-off approvals: remaining balance and
same-day coverage, computed from local mirrors only (no live Odoo).

Coverage is scoped to the requester's department, derived from their
default work-center membership (work_center_default_people ->
work_centers.department), with a plant-wide fallback when no department
resolves. Balance reads the time_off_balances cache.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import db, time_off_calendar


def department_for_person(person_odoo_id: int) -> set[str]:
    """Departments the person is a default member of (via their default
    work centers). Empty set when they map to no department."""
    rows = db.query(
        "SELECT DISTINCT wc.department "
        "FROM work_center_default_people wcdp "
        "JOIN work_centers wc ON wc.id = wcdp.wc_id "
        "JOIN people pe ON pe.id = wcdp.person_id "
        "WHERE pe.odoo_id = %s AND wc.department IS NOT NULL "
        "AND wc.department <> ''",
        (person_odoo_id,),
    )
    return {r["department"] for r in rows if r.get("department")}


def _timing_label(r: dict) -> str:
    """Privacy-safe timing for one off-person ('full day', 'arrives 9:00am',
    'leaves 2:00pm', '10:00am–12:00pm'). Reuses the kiosk label engine; a
    leave with no hour bounds is a full day."""
    if r.get("hour_from") is None or r.get("hour_to") is None:
        return "full day"
    return time_off_calendar.label_for(r)


def _person_entry(r: dict, pending: bool, requester_depts: set[str]) -> dict:
    person_depts = set(r.get("depts") or ())
    return {
        "name": r["name"],
        "dept": (sorted(person_depts)[0] if person_depts else None),
        "label": _timing_label(r),
        "pending": pending,
        "same_dept": bool(person_depts & requester_depts),
    }


def coverage_breakdown(
    approved: list[dict],
    pending: list[dict],
    holiday_names: dict[date, str],
    requester_depts: set[str],
    date_from: date,
    date_to: date,
    requester_odoo_id: int,
    plant_peak_threshold: int = 3,
    max_days: int = 10,
) -> dict:
    """Per-day 'who else is off' breakdown over [date_from, date_to].

    Pure (no I/O). ``approved``/``pending`` rows carry person_odoo_id, name,
    shape, hour_from, hour_to, date_from, date_to, and a ``depts`` set. The
    requester is always excluded; a person counts once per day (approved wins
    over pending). Holidays are surfaced as a per-day flag, never added to the
    people count. Returns the peak day, that day's department count, severity,
    and a capped per-day list (only days with someone off OR a plant closure)."""
    requester_depts = set(requester_depts or ())

    def collect(day: date) -> list[dict]:
        seen: dict[int, dict] = {}
        for r in approved:
            pid = r["person_odoo_id"]
            if pid == requester_odoo_id or pid in seen:
                continue
            if r["date_from"] <= day <= r["date_to"]:
                seen[pid] = _person_entry(r, False, requester_depts)
        for r in pending:
            pid = r["person_odoo_id"]
            if pid == requester_odoo_id or pid in seen:
                continue
            if r["date_from"] <= day <= r["date_to"]:
                seen[pid] = _person_entry(r, True, requester_depts)
        return list(seen.values())

    full: list[dict] = []
    day = date_from
    while day <= date_to:
        people = collect(day)
        holiday = holiday_names.get(day)
        if people or holiday:
            people.sort(key=lambda p: (not p["same_dept"], p["name"].lower()))
            full.append({
                "date": day,
                "count": len(people),
                "dept_count": sum(1 for p in people if p["same_dept"]),
                "holiday": holiday,
                "people": people,
            })
        day = day + timedelta(days=1)

    peak_count, peak_date, peak_dept_count = 0, None, 0
    for entry in full:
        if entry["count"] > peak_count:
            peak_count = entry["count"]
            peak_date = entry["date"]
            peak_dept_count = entry["dept_count"]

    if peak_count == 0:
        severity = "clear"
    elif peak_dept_count > 0 or peak_count >= plant_peak_threshold:
        severity = "warn"
    else:
        severity = "ok"

    return {
        "severity": severity,
        "peak_count": peak_count,
        "peak_date": peak_date,
        "peak_dept_count": peak_dept_count,
        "scope": "department" if requester_depts else "plant",
        "dept_label": sorted(requester_depts)[0] if requester_depts else None,
        "has_holiday": any(e["holiday"] for e in full),
        "by_day": full[:max_days],
        "more_days": max(0, len(full) - max_days),
    }


_PENDING_STATES = ("draft", "draft_edit", "confirm", "validate1")

_OVERLAP_SELECT = (
    "SELECT r.person_odoo_id, "
    "COALESCE(p.name, '#' || r.person_odoo_id::text) AS name, "
    "r.shape, r.date_from, r.date_to, r.hour_from, r.hour_to "
    "FROM time_off_requests r "
    "LEFT JOIN people p ON p.odoo_id = r.person_odoo_id "
)


def _departments_by_person(ids: list[int]) -> dict[int, set[str]]:
    """Map each person_odoo_id to the set of departments they default into."""
    if not ids:
        return {}
    rows = db.query(
        "SELECT pe.odoo_id AS person_odoo_id, wc.department AS department "
        "FROM work_center_default_people wcdp "
        "JOIN work_centers wc ON wc.id = wcdp.wc_id "
        "JOIN people pe ON pe.id = wcdp.person_id "
        "WHERE pe.odoo_id = ANY(%s) AND wc.department IS NOT NULL "
        "AND wc.department <> ''",
        (ids,),
    )
    out: dict[int, set[str]] = {}
    for r in rows:
        out.setdefault(r["person_odoo_id"], set()).add(r["department"])
    return out


def _holiday_names(start_d: date, end_d: date) -> dict[date, str]:
    """Public-holiday closures fanned out to {date: name}. Cached via
    odoo_client; a failing fetch degrades to {} so coverage still renders."""
    from . import odoo_client

    try:
        rows = odoo_client.fetch_public_holidays(start_d, end_d)
    except Exception:  # noqa: BLE001 — never let the holiday fetch break the inbox
        return {}
    out: dict[date, str] = {}
    for h in rows:
        hs = time_off_calendar.parse_holiday_date(h.get("date_from"))
        he = time_off_calendar.parse_holiday_date(h.get("date_to"))
        if not hs or not he:
            continue
        cur = max(hs, start_d)
        end = min(he, end_d)
        while cur <= end:
            out.setdefault(cur, h.get("name") or "Plant closed")
            cur = cur + timedelta(days=1)
    return out


def coverage_breakdowns_for(rows: list[dict]) -> dict[int, dict]:
    """For each pending inbox row, compute its coverage breakdown.

    Three batched DB queries (approved leaves, other pending requests, and
    departments for everyone involved) over the union date-range of all rows,
    plus one cached holiday fetch — independent of the row count. Returns
    ``{request_id: breakdown}``."""
    if not rows:
        return {}
    window_start = min(r["date_from"] for r in rows)
    window_end = max(r["date_to"] for r in rows)

    approved = db.query(
        _OVERLAP_SELECT + "WHERE r.state = 'validate' "
        "AND r.date_to >= %s AND r.date_from <= %s",
        (window_start, window_end),
    )
    pending = db.query(
        _OVERLAP_SELECT + "WHERE r.state IN ('draft', 'draft_edit', "
        "'confirm', 'validate1') AND r.date_to >= %s AND r.date_from <= %s",
        (window_start, window_end),
    )
    holiday_names = _holiday_names(window_start, window_end)

    ids = {r["person_odoo_id"] for r in rows}
    ids |= {a["person_odoo_id"] for a in approved}
    ids |= {p["person_odoo_id"] for p in pending}
    dept_map = _departments_by_person(list(ids))
    for a in approved:
        a["depts"] = dept_map.get(a["person_odoo_id"], set())
    for p in pending:
        p["depts"] = dept_map.get(p["person_odoo_id"], set())

    out: dict[int, dict] = {}
    for r in rows:
        out[r["id"]] = coverage_breakdown(
            approved, pending, holiday_names,
            dept_map.get(r["person_odoo_id"], set()),
            r["date_from"], r["date_to"], r["person_odoo_id"],
        )
    return out


def coverage_for(person_odoo_id: int, date_from: date, date_to: date) -> dict:
    """Count OTHER people with an approved leave overlapping [date_from,
    date_to]. Scoped to the requester's department when known, else
    plant-wide. Returns {'count': int, 'scope': 'department'|'plant'}."""
    depts = department_for_person(person_odoo_id)
    if depts:
        rows = db.query(
            "SELECT COUNT(DISTINCT r.person_odoo_id) AS n "
            "FROM time_off_requests r "
            "JOIN people pe ON pe.odoo_id = r.person_odoo_id "
            "JOIN work_center_default_people wcdp ON wcdp.person_id = pe.id "
            "JOIN work_centers wc ON wc.id = wcdp.wc_id "
            "WHERE r.state = 'validate' AND r.person_odoo_id <> %s "
            "AND r.date_to >= %s AND r.date_from <= %s "
            "AND wc.department = ANY(%s)",
            (person_odoo_id, date_from, date_to, list(depts)),
        )
        return {"count": int(rows[0]["n"] if rows else 0), "scope": "department"}
    rows = db.query(
        "SELECT COUNT(DISTINCT r.person_odoo_id) AS n "
        "FROM time_off_requests r "
        "WHERE r.state = 'validate' AND r.person_odoo_id <> %s "
        "AND r.date_to >= %s AND r.date_from <= %s",
        (person_odoo_id, date_from, date_to),
    )
    return {"count": int(rows[0]["n"] if rows else 0), "scope": "plant"}


def balance_for(person_odoo_id: int, holiday_status_id: int) -> dict | None:
    """Remaining balance for one (person, leave type) from the local cache,
    or None when no balance row exists. {'remaining': float, 'unit': str}."""
    rows = db.query(
        "SELECT available, unit FROM time_off_balances "
        "WHERE person_odoo_id = %s AND holiday_status_id = %s",
        (person_odoo_id, holiday_status_id),
    )
    if not rows:
        return None
    return {"remaining": float(rows[0]["available"]), "unit": rows[0]["unit"]}


def request_amount(row: dict) -> tuple[float, str]:
    """Approximate amount + unit a request consumes, for the balance warning.
    Hour-bounded requests -> hours; otherwise inclusive day count."""
    hf, ht = row.get("hour_from"), row.get("hour_to")
    if hf is not None and ht is not None:
        return (float(ht) - float(hf), "hours")
    days = (row["date_to"] - row["date_from"]).days + 1
    return (float(days), "days")
