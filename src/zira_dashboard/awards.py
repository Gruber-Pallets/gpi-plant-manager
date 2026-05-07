"""Trophy system computation engine.

Pure functions over `production_history.daily_records` plus a
`work_centers_store` lookup. No caching beyond what daily_records
already does (postgres-backed). Override layer is in the same module
(see Task 5).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from calendar import monthrange


def _all_time_range() -> tuple[date, date]:
    """Earliest day in zira_daily_cache (or today if empty) → today."""
    from datetime import datetime, timezone
    from . import db
    today = datetime.now(timezone.utc).date()
    rows = db.query("SELECT MIN(day) AS d FROM zira_daily_cache")
    earliest = rows[0]["d"] if rows and rows[0].get("d") else today
    return (earliest, today)


def _wc_names_for_group(group_name: str) -> set[str]:
    from . import work_centers_store
    return {loc.name for loc in work_centers_store.members("group", group_name)}


def person_days_in_group(group_name: str, start: date, end: date) -> list[dict]:
    """Returns one row per (person, day) summing units/hours across the
    group's WCs. Filters days where total units == 0.

    Each row: {"name": str, "day": date, "units": float, "hours": float}.
    """
    from . import production_history
    wc_names = _wc_names_for_group(group_name)
    if not wc_names:
        return []
    raw = production_history.daily_records(start, end, None)
    agg: dict[tuple[str, date], dict] = defaultdict(lambda: {"units": 0.0, "hours": 0.0})
    for r in raw:
        if r["wc"] not in wc_names:
            continue
        key = (r["person"], r["day"])
        agg[key]["units"] += r["units"]
        agg[key]["hours"] += r["hours"]
    return [
        {"name": person, "day": day, "units": v["units"], "hours": v["hours"]}
        for (person, day), v in agg.items()
        if v["units"] > 0
    ]


def person_days_in_wc(wc_name: str, start: date, end: date) -> list[dict]:
    """Same shape as person_days_in_group but for a single WC."""
    from . import production_history
    raw = production_history.daily_records(start, end, None)
    return [
        {"name": r["person"], "day": r["day"], "units": r["units"], "hours": r["hours"]}
        for r in raw
        if r["wc"] == wc_name and r["units"] > 0
    ]


def _rank_single_day(rows: list[dict], top_n: int) -> list[dict]:
    """Order rows by units desc, then pph desc, then name asc.
    Return top N with positions 1..N attached."""
    def _key(r):
        pph = (r["units"] / r["hours"]) if r["hours"] > 0 else 0.0
        return (-r["units"], -pph, r["name"])
    ranked = sorted(rows, key=_key)[:top_n]
    out = []
    for i, r in enumerate(ranked, start=1):
        pph = round(r["units"] / r["hours"], 1) if r["hours"] > 0 else 0.0
        out.append({
            "position": i,
            "name": r["name"],
            "day": r["day"],
            "units": r["units"],
            "pph": pph,
        })
    return out


def _month_range(year: int, month: int) -> tuple[date, date]:
    last_day = monthrange(year, month)[1]
    return (date(year, month, 1), date(year, month, last_day))


def _year_range(year: int) -> tuple[date, date]:
    return (date(year, 1, 1), date(year, 12, 31))


def monthly_badges(group_name: str, year: int, month: int) -> list[dict]:
    """Top-3 person-days in the group during [year, month]."""
    start, end = _month_range(year, month)
    rows = person_days_in_group(group_name, start, end)
    return _rank_single_day(rows, top_n=3)


def annual_top_days(group_name: str, year: int) -> list[dict]:
    """Top-3 person-days in the group during [year]."""
    start, end = _year_range(year)
    rows = person_days_in_group(group_name, start, end)
    return _rank_single_day(rows, top_n=3)


def goat(group_name: str) -> dict | None:
    """All-time best person-day in the group. Earliest day wins on tie.
    Returns {name, day, units, pph} or None when no data.
    """
    start, end = _all_time_range()
    rows = person_days_in_group(group_name, start, end)
    if not rows:
        return None
    rows_sorted = sorted(rows, key=lambda r: (-r["units"], r["day"], r["name"]))
    top = rows_sorted[0]
    pph = round(top["units"] / top["hours"], 1) if top["hours"] > 0 else 0.0
    return {"name": top["name"], "day": top["day"], "units": top["units"], "pph": pph}


def _rank_avg(rows: list[dict], min_days: int) -> dict | None:
    """Group rows by name, sum units/hours, count days. Filter days >= min_days.
    Highest avg pph wins. Tie-break: more days → more units → name asc.
    Returns the top {name, pph, days, units, hours} or None."""
    by_person: dict[str, dict] = defaultdict(lambda: {"units": 0.0, "hours": 0.0, "days": 0})
    for r in rows:
        if r["hours"] <= 0:
            continue  # defensive — see spec edge case 4
        d = by_person[r["name"]]
        d["units"] += r["units"]
        d["hours"] += r["hours"]
        d["days"] += 1
    qualifiers = []
    for name, v in by_person.items():
        if v["days"] < min_days or v["hours"] <= 0:
            continue
        qualifiers.append({
            "name": name,
            "pph": round(v["units"] / v["hours"], 1),
            "days": v["days"],
            "units": v["units"],
            "hours": v["hours"],
        })
    if not qualifiers:
        return None
    qualifiers.sort(key=lambda q: (-q["pph"], -q["days"], -q["units"], q["name"]))
    return qualifiers[0]


def annual_best_avg_group(group_name: str, year: int) -> dict | None:
    """Highest avg pph across the group's WCs in [year], gated days >= 30."""
    start, end = _year_range(year)
    rows = person_days_in_group(group_name, start, end)
    return _rank_avg(rows, min_days=30)


def annual_best_avg_wc(wc_name: str, year: int) -> dict | None:
    """Highest avg pph in this WC alone in [year], gated days >= 30."""
    start, end = _year_range(year)
    rows = person_days_in_wc(wc_name, start, end)
    return _rank_avg(rows, min_days=30)


# ---- Override layer ----------------------------------------------------

def _load_overrides() -> list[dict]:
    """Read all override rows. Cheap query — table is tiny."""
    from . import db
    return db.query(
        "SELECT scope, group_name, wc_name, year, month, position, action, name "
        "FROM award_overrides"
    )


def _override_matches(o: dict, *, scope: str, group_name: str | None = None,
                      wc_name: str | None = None, year: int | None = None,
                      month: int | None = None, position: int | None = None) -> bool:
    if o["scope"] != scope:
        return False
    if (o["group_name"] or None) != group_name:
        return False
    if (o["wc_name"] or None) != wc_name:
        return False
    if (o["year"] or None) != year:
        return False
    if (o["month"] or None) != month:
        return False
    if position is not None and o["position"] != position:
        return False
    return True


def apply_overrides(slot_list: list[dict], *, scope: str, group_name: str | None = None,
                    wc_name: str | None = None, year: int | None = None,
                    month: int | None = None, overrides: list[dict] | None = None) -> list[dict]:
    """Apply replace/delete overrides to a list of position-keyed slots."""
    if overrides is None:
        overrides = _load_overrides()
    out = []
    for s in slot_list:
        match = next(
            (o for o in overrides if _override_matches(
                o, scope=scope, group_name=group_name, wc_name=wc_name,
                year=year, month=month, position=s["position"])),
            None,
        )
        if match is None:
            out.append(s)
            continue
        if match["action"] == "delete":
            continue
        if match["action"] == "replace":
            out.append({**s, "name": match["name"]})
            continue
        out.append(s)
    return out


def apply_overrides_single(slot: dict | None, *, scope: str,
                           group_name: str | None = None,
                           wc_name: str | None = None,
                           year: int | None = None,
                           month: int | None = None,
                           overrides: list[dict] | None = None) -> dict | None:
    """Single-winner version (goat, best-avg). Returns None if deleted."""
    if overrides is None:
        overrides = _load_overrides()
    match = next(
        (o for o in overrides if _override_matches(
            o, scope=scope, group_name=group_name, wc_name=wc_name,
            year=year, month=month, position=1)),
        None,
    )
    if match is None:
        return slot
    if match["action"] == "delete":
        return None
    if match["action"] == "replace":
        if slot is None:
            return {"name": match["name"]}
        return {**slot, "name": match["name"]}
    return slot


# ---- Reverse lookup for player card -----------------------------------

def awards_earned_by(name: str, today: date) -> list[dict]:
    """Return every award this person currently holds.

    Each entry: {type, group, wc, year, month, position, day, units, pph, days}
    where the irrelevant keys are None. type is one of:
      'goat' | 'trophy_top_day' | 'trophy_best_avg_group' |
      'trophy_best_avg_wc' | 'badge'.
    """
    from . import work_centers_store
    overrides = _load_overrides()
    earned: list[dict] = []
    groups = work_centers_store.registered_groups()

    # GOATs
    for g in groups:
        live = goat(g)
        final = apply_overrides_single(live, scope="award_goat", group_name=g, overrides=overrides)
        if final and final.get("name") == name:
            earned.append({
                "type": "goat", "group": g, "wc": None,
                "year": None, "month": None, "position": 1,
                "day": final.get("day"), "units": final.get("units"),
                "pph": final.get("pph"), "days": None,
            })

    # Annual + monthly — current year + prior 2
    years = [today.year, today.year - 1, today.year - 2]
    for y in years:
        for g in groups:
            top = apply_overrides(
                annual_top_days(g, y),
                scope="trophy_top_day", group_name=g, year=y, overrides=overrides,
            )
            for s in top:
                if s["name"] == name:
                    earned.append({
                        "type": "trophy_top_day", "group": g, "wc": None,
                        "year": y, "month": None, "position": s["position"],
                        "day": s["day"], "units": s["units"], "pph": s["pph"],
                        "days": None,
                    })

            ba = apply_overrides_single(
                annual_best_avg_group(g, y),
                scope="trophy_best_avg_group", group_name=g, year=y, overrides=overrides,
            )
            if ba and ba.get("name") == name:
                earned.append({
                    "type": "trophy_best_avg_group", "group": g, "wc": None,
                    "year": y, "month": None, "position": 1,
                    "day": None, "units": ba.get("units"),
                    "pph": ba.get("pph"), "days": ba.get("days"),
                })

        # Per-WC best avg, dedup across groups
        seen_wcs: set[str] = set()
        for g in groups:
            for wc_name in _wc_names_for_group(g):
                if wc_name in seen_wcs:
                    continue
                seen_wcs.add(wc_name)
                bw = apply_overrides_single(
                    annual_best_avg_wc(wc_name, y),
                    scope="trophy_best_avg_wc", wc_name=wc_name, year=y, overrides=overrides,
                )
                if bw and bw.get("name") == name:
                    earned.append({
                        "type": "trophy_best_avg_wc", "group": None, "wc": wc_name,
                        "year": y, "month": None, "position": 1,
                        "day": None, "units": bw.get("units"),
                        "pph": bw.get("pph"), "days": bw.get("days"),
                    })

        # Monthly badges (current year + prior year only)
        if y >= today.year - 1:
            for m in range(12, 0, -1):
                if y == today.year and m > today.month:
                    continue
                for g in groups:
                    badges = apply_overrides(
                        monthly_badges(g, y, m),
                        scope="badge", group_name=g, year=y, month=m, overrides=overrides,
                    )
                    for s in badges:
                        if s["name"] == name:
                            earned.append({
                                "type": "badge", "group": g, "wc": None,
                                "year": y, "month": m, "position": s["position"],
                                "day": s["day"], "units": s["units"], "pph": s["pph"],
                                "days": None,
                            })
    return earned
