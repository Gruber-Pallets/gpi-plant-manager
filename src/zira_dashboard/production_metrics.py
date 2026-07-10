from __future__ import annotations

from calendar import month_abbr, monthrange
from collections import defaultdict
from datetime import date, timedelta
from math import ceil


def normalized_daily_scores(
    records: list[dict],
    *,
    wc_names: set[str],
    standard_full_day_hours: float,
    min_hours: float = 4.0,
) -> list[dict]:
    """One normalized score per (person, day) inside a WC scope.

    Records are summed by person/day before the cutoff so split time across
    multiple WCs in the same scope counts fairly.
    """
    if standard_full_day_hours <= 0:
        return []
    scoped = [r for r in records if r.get("wc") in wc_names]
    by_person_day: dict[tuple[str, date], dict] = defaultdict(
        lambda: {"units": 0.0, "hours": 0.0}
    )
    for r in scoped:
        person = str(r["person"])
        day = r["day"]
        bucket = by_person_day[(person, day)]
        bucket["units"] += float(r.get("units") or 0.0)
        bucket["hours"] += float(r.get("hours") or 0.0)

    out: list[dict] = []
    for (person, day), totals in by_person_day.items():
        hours = totals["hours"]
        if hours < min_hours or hours <= 0:
            continue
        normalized = totals["units"] / hours * standard_full_day_hours
        out.append(
            {
                "name": person,
                "day": day,
                "units": totals["units"],
                "hours": hours,
                "normalized_units": normalized,
            }
        )
    out.sort(key=lambda r: (r["day"], r["name"].lower()))
    return out


def normalized_average_by_person(
    records: list[dict],
    *,
    wc_names: set[str],
    standard_full_day_hours: float,
    min_hours: float = 4.0,
) -> list[dict]:
    """Average normalized pallets/day by person for one WC/group/role scope."""
    scores = normalized_daily_scores(
        records,
        wc_names=wc_names,
        standard_full_day_hours=standard_full_day_hours,
        min_hours=min_hours,
    )
    by_person: dict[str, dict] = defaultdict(
        lambda: {
            "total_normalized_units": 0.0,
            "total_units": 0.0,
            "total_hours": 0.0,
            "days": 0,
        }
    )
    for s in scores:
        bucket = by_person[s["name"]]
        bucket["total_normalized_units"] += s["normalized_units"]
        bucket["total_units"] += s["units"]
        bucket["total_hours"] += s["hours"]
        bucket["days"] += 1

    out: list[dict] = []
    for name, totals in by_person.items():
        days = totals["days"]
        if days <= 0:
            continue
        out.append(
            {
                "name": name,
                "avg_units": totals["total_normalized_units"] / days,
                "days": days,
                "total_units": totals["total_units"],
                "total_hours": totals["total_hours"],
            }
        )
    out.sort(key=lambda r: (-r["avg_units"], -r["days"], r["name"].lower()))
    return out


def _threshold(rows: list[dict]) -> int:
    leader_days = max((r["days"] for r in rows), default=0)
    return ceil(leader_days * 0.10) if leader_days > 0 else 0


def _by_name(rows: list[dict]) -> dict[str, dict]:
    return {r["name"]: r for r in rows}


def _span_cell(row: dict | None, threshold: int) -> dict:
    if row is None:
        return {
            "eligible": False,
            "label": "not enough days",
            "avg_units": None,
            "days": 0,
        }
    eligible = threshold > 0 and row["days"] >= threshold
    return {
        "eligible": eligible,
        "label": None if eligible else "not enough days",
        "avg_units": row["avg_units"] if eligible else None,
        "days": row["days"],
    }


def _role_rows(
    *,
    ytd_rows: list[dict],
    l30_rows: list[dict],
    ytd_threshold: int,
    l30_threshold: int,
) -> list[dict]:
    ytd = _by_name(ytd_rows)
    l30 = _by_name(l30_rows)
    names = {
        r["name"] for r in ytd_rows if ytd_threshold > 0 and r["days"] >= ytd_threshold
    } | {
        r["name"] for r in l30_rows if l30_threshold > 0 and r["days"] >= l30_threshold
    }
    rows: list[dict] = []
    for name in names:
        ytd_cell = _span_cell(ytd.get(name), ytd_threshold)
        l30_cell = _span_cell(l30.get(name), l30_threshold)
        rows.append({"name": name, "ytd": ytd_cell, "l30": l30_cell})

    def sort_key(row):
        if row["ytd"]["eligible"]:
            return (0, -row["ytd"]["avg_units"], -row["ytd"]["days"], row["name"].lower())
        return (
            1,
            -(row["l30"]["avg_units"] or 0.0),
            -row["l30"]["days"],
            row["name"].lower(),
        )

    rows.sort(key=sort_key)
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows


def _month_bounds(year: int, month: int, today: date) -> tuple[date, date]:
    end_day = monthrange(year, month)[1]
    start = date(year, month, 1)
    end = date(year, month, end_day)
    if start <= today <= end:
        end = today
    return start, end


def _add_months(d: date, months: int) -> date:
    month_index = d.year * 12 + (d.month - 1) + months
    year = month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _best_ribbon(
    records: list[dict],
    *,
    wc_names: set[str],
    standard_full_day_hours: float,
) -> dict | None:
    scores = normalized_daily_scores(
        records,
        wc_names=wc_names,
        standard_full_day_hours=standard_full_day_hours,
    )
    if not scores:
        return None
    scores.sort(
        key=lambda r: (
            -r["normalized_units"],
            -r["units"],
            r["name"].lower(),
            r["day"],
        )
    )
    best = scores[0]
    return {
        "name": best["name"],
        "day": best["day"],
        "amount": best["normalized_units"],
        "days": 1,
    }


def build_family_leaderboard(
    records: list[dict],
    *,
    today: date,
    standard_full_day_hours: float,
    family_wc_names: dict[str, set[str]],
) -> dict:
    ytd_start = date(today.year, 1, 1)
    ytd_end = today
    l30_start = today - timedelta(days=29)
    l30_end = today
    ytd_records = [r for r in records if ytd_start <= r["day"] <= ytd_end]
    l30_records = [r for r in records if l30_start <= r["day"] <= l30_end]

    families: dict[str, dict] = {}
    active_families: list[str] = []
    for family, wc_names in family_wc_names.items():
        ytd_rows = normalized_average_by_person(
            ytd_records,
            wc_names=wc_names,
            standard_full_day_hours=standard_full_day_hours,
        )
        l30_rows = normalized_average_by_person(
            l30_records,
            wc_names=wc_names,
            standard_full_day_hours=standard_full_day_hours,
        )
        ytd_threshold = _threshold(ytd_rows)
        l30_threshold = _threshold(l30_rows)
        rows = _role_rows(
            ytd_rows=ytd_rows,
            l30_rows=l30_rows,
            ytd_threshold=ytd_threshold,
            l30_threshold=l30_threshold,
        )
        families[family] = {
            "rows": rows,
            "thresholds": {"ytd": ytd_threshold, "l30": l30_threshold},
        }
        if rows:
            active_families.append(family)

    ribbons: list[dict] = []
    current_month = date(today.year, today.month, 1)
    for offset in range(12):
        month_start = _add_months(current_month, -offset)
        start, end = _month_bounds(month_start.year, month_start.month, today)
        month_records = [r for r in records if start <= r["day"] <= end]
        ribbons.append(
            {
                "year": month_start.year,
                "month": month_start.month,
                "month_label": month_abbr[month_start.month],
                "winners": {
                    family: _best_ribbon(
                        month_records,
                        wc_names=wc_names,
                        standard_full_day_hours=standard_full_day_hours,
                    )
                    for family, wc_names in family_wc_names.items()
                },
            }
        )

    return {
        "ytd_start": ytd_start,
        "ytd_end": ytd_end,
        "l30_start": l30_start,
        "l30_end": l30_end,
        "families": families,
        "active_families": active_families,
        "ribbons": ribbons,
    }


def build_recycling_leaderboard(
    records: list[dict],
    *,
    today: date,
    standard_full_day_hours: float,
    wc_role_by_name: dict[str, str],
) -> dict:
    family_wc_names = {
        "Repair": {wc for wc, role in wc_role_by_name.items() if role == "Repair"},
        "Dismantler": {
            wc for wc, role in wc_role_by_name.items() if role == "Dismantler"
        },
    }
    data = build_family_leaderboard(
        records,
        today=today,
        standard_full_day_hours=standard_full_day_hours,
        family_wc_names=family_wc_names,
    )
    return {
        "ytd_start": data["ytd_start"],
        "ytd_end": data["ytd_end"],
        "l30_start": data["l30_start"],
        "l30_end": data["l30_end"],
        "roles": data["families"],
        "ribbons": [
            {
                "year": row["year"],
                "month": row["month"],
                "month_label": row["month_label"],
                "repair": row["winners"]["Repair"],
                "dismantler": row["winners"]["Dismantler"],
            }
            for row in data["ribbons"]
        ],
    }
