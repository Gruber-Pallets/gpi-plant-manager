"""Private Odoo skill operations used by the stable client facade."""

from __future__ import annotations

from typing import Any, Callable


SKILL_TYPE_NAMES = ("Production Skills", "Supervisor Skills", "Certifications")


def fetch_skill_columns_with_types(
    execute_fn: Callable[..., Any],
    unwrap_m2o_fn: Callable[[Any], Any],
) -> list[dict]:
    """Return ordered list of {id, name, type} dicts: all skills from the
    Production type (alphabetical), then all from Supervisor (alphabetical)."""
    types = execute_fn(
        "hr.skill.type", "search_read",
        [("name", "in", list(SKILL_TYPE_NAMES))],
        fields=["id", "name"],
    )
    type_order = {name: i for i, name in enumerate(SKILL_TYPE_NAMES)}
    types.sort(key=lambda t: type_order.get(t["name"], 999))
    type_ids = [t["id"] for t in types]
    type_name_by_id = {t["id"]: t["name"] for t in types}
    if not type_ids:
        return []
    skills = execute_fn(
        "hr.skill", "search_read",
        [("skill_type_id", "in", type_ids)],
        fields=["id", "name", "skill_type_id"],
    )
    by_type: dict[int, list[dict]] = {tid: [] for tid in type_ids}
    for s in skills:
        tid = unwrap_m2o_fn(s["skill_type_id"])
        by_type.setdefault(tid, []).append(s)
    out: list[dict] = []
    for tid in type_ids:
        out.extend(
            {
                "id": skill["id"],
                "name": skill["name"],
                "type": type_name_by_id[tid],
            }
            for skill in sorted(
                by_type.get(tid, []), key=lambda row: str(row["name"]).lower()
            )
        )
    return out


def fetch_skill_columns(
    execute_fn: Callable[..., Any],
    unwrap_m2o_fn: Callable[[Any], Any],
) -> list[str]:
    """Backwards-compatible name-only view."""
    return [c["name"] for c in fetch_skill_columns_with_types(execute_fn, unwrap_m2o_fn)]


def _bucket_for_level_count(rank: int, count: int) -> int:
    if count <= 1:
        return 0
    return max(0, min(3, round(rank * 3 / (count - 1))))


def fetch_skill_level_buckets(
    execute_fn: Callable[..., Any],
    unwrap_m2o_fn: Callable[[Any], Any],
) -> dict[int, int]:
    """Map hr.skill.level.id → bucket (0–3) using rank-within-type.

    For each skill type, sort levels ascending by level_progress, assign
    rank index, then bucket = round(rank * 3 / max(N-1, 1)) clamped 0..3.
    """
    levels = execute_fn(
        "hr.skill.level", "search_read",
        [],
        fields=["id", "level_progress", "skill_type_id"],
    )
    by_type: dict[int, list[dict]] = {}
    for lvl in levels:
        tid = unwrap_m2o_fn(lvl["skill_type_id"])
        by_type.setdefault(tid, []).append(lvl)
    out: dict[int, int] = {}
    for tid, lvls in by_type.items():
        lvls.sort(key=lambda l: l.get("level_progress", 0))
        n = len(lvls)
        for rank, lvl in enumerate(lvls):
            out[lvl["id"]] = _bucket_for_level_count(rank, n)
    return out


def _skill_type_id_for_skill(
    execute_fn: Callable[..., Any],
    unwrap_m2o_fn: Callable[[Any], Any],
    skill_odoo_id: int,
) -> int:
    rows = execute_fn(
        "hr.skill",
        "read",
        [skill_odoo_id],
        fields=["skill_type_id"],
    )
    if not rows:
        raise ValueError(f"Skill {skill_odoo_id} not found in Odoo")
    type_id = unwrap_m2o_fn(rows[0].get("skill_type_id"))
    if not type_id:
        raise ValueError(f"Skill {skill_odoo_id} has no skill type in Odoo")
    return int(type_id)


def _skill_level_id_for_bucket(
    execute_fn: Callable[..., Any],
    skill_type_odoo_id: int,
    bucket: int,
) -> int:
    levels = execute_fn(
        "hr.skill.level",
        "search_read",
        [("skill_type_id", "=", skill_type_odoo_id)],
        fields=["id", "level_progress", "skill_type_id"],
    )
    if not levels:
        raise ValueError(f"Skill type {skill_type_odoo_id} has no levels in Odoo")
    levels.sort(key=lambda lvl: lvl.get("level_progress", 0))
    by_bucket: dict[int, list[dict]] = {}
    count = len(levels)
    for rank, level_row in enumerate(levels):
        by_bucket.setdefault(_bucket_for_level_count(rank, count), []).append(level_row)
    candidates = by_bucket.get(bucket)
    if not candidates:
        raise ValueError(
            f"Skill type {skill_type_odoo_id} has no level mapped to bucket {bucket}"
        )
    return int(candidates[-1]["id"])


def _employee_skill_ids(
    execute_fn: Callable[..., Any],
    employee_odoo_id: int,
    skill_odoo_id: int,
) -> list[int]:
    return [
        int(i)
        for i in execute_fn(
            "hr.employee.skill",
            "search",
            [
                ("employee_id", "=", int(employee_odoo_id)),
                ("skill_id", "=", int(skill_odoo_id)),
            ],
        )
    ]


def _keep_one_employee_skill_row(
    execute_fn: Callable[..., Any],
    existing_ids: list[int],
    values: dict,
    *,
    preferred_id: int | None = None,
) -> None:
    if not existing_ids:
        return
    keep_id = preferred_id if preferred_id in existing_ids else min(existing_ids)
    execute_fn("hr.employee.skill", "write", [keep_id], values)
    duplicate_ids = [i for i in existing_ids if i != keep_id]
    if duplicate_ids:
        execute_fn("hr.employee.skill", "unlink", duplicate_ids)


def set_employee_skill_level(
    execute_fn: Callable[..., Any],
    unwrap_m2o_fn: Callable[[Any], Any],
    employee_odoo_id: int,
    skill_odoo_id: int,
    bucket: int,
) -> None:
    """Create, update, or remove an Odoo hr.employee.skill row.

    `bucket` is the dashboard's 0-3 scale. Bucket 0 removes the employee/skill
    relation. Buckets 1-3 map back to the matching hr.skill.level for the
    skill's type.
    """
    if bucket not in (0, 1, 2, 3):
        raise ValueError("bucket must be 0, 1, 2, or 3")

    existing_ids = _employee_skill_ids(execute_fn, employee_odoo_id, skill_odoo_id)

    if bucket == 0:
        if existing_ids:
            execute_fn("hr.employee.skill", "unlink", existing_ids)
        return

    skill_type_id = _skill_type_id_for_skill(execute_fn, unwrap_m2o_fn, int(skill_odoo_id))
    skill_level_id = _skill_level_id_for_bucket(execute_fn, skill_type_id, bucket)
    values = {"skill_level_id": skill_level_id}

    if existing_ids:
        _keep_one_employee_skill_row(execute_fn, existing_ids, values)
        return

    created_id = execute_fn(
        "hr.employee.skill",
        "create",
        {
            "employee_id": int(employee_odoo_id),
            "skill_id": int(skill_odoo_id),
            "skill_type_id": skill_type_id,
            "skill_level_id": skill_level_id,
        },
    )
    post_create_ids = _employee_skill_ids(execute_fn, employee_odoo_id, skill_odoo_id)
    _keep_one_employee_skill_row(
        execute_fn, post_create_ids, values, preferred_id=int(created_id)
    )


def fetch_skills_for(
    execute_fn: Callable[..., Any],
    employee_ids: list[int],
    unwrap_m2o_fn: Callable[[Any], Any],
) -> dict[int, list[dict]]:
    """Return {employee_id: [{skill_id, skill_name, level_id}, ...]}."""
    if not employee_ids:
        return {}
    rows = execute_fn(
        "hr.employee.skill", "search_read",
        [("employee_id", "in", employee_ids)],
        fields=["id", "employee_id", "skill_id", "skill_level_id"],
    )
    out: dict[int, list[dict]] = {eid: [] for eid in employee_ids}
    for r in rows:
        eid = unwrap_m2o_fn(r["employee_id"])
        sid = unwrap_m2o_fn(r["skill_id"])
        lid = unwrap_m2o_fn(r["skill_level_id"])
        sname = r["skill_id"][1] if isinstance(r["skill_id"], list) else ""
        out.setdefault(eid, []).append({"skill_id": sid, "skill_name": sname, "level_id": lid})
    return out


def fetch_spanish_speaker_ids(
    execute_fn: Callable[..., Any],
    unwrap_m2o_fn: Callable[[Any], Any],
) -> set[int]:
    """Odoo employee ids who have a 'Spanish' skill (Languages type) at a
    non-zero level — i.e. level 1-3 in Odoo's Languages rating.

    Matches the skill by name (ilike 'Spanish') so it works regardless of
    skill-type wiring, and filters on hr.employee.skill.level_progress > 0
    so a level-0 / unrated entry doesn't count. Used to flag bilingual
    kiosk users; deliberately separate from fetch_skills_for so it never
    adds Languages columns to the production skills matrix.
    """
    skills = execute_fn(
        "hr.skill", "search_read",
        [("name", "ilike", "Spanish")],
        fields=["id", "name"],
    )
    skill_ids = [s["id"] for s in skills]
    if not skill_ids:
        return set()
    rows = execute_fn(
        "hr.employee.skill", "search_read",
        [("skill_id", "in", skill_ids), ("level_progress", ">", 0)],
        fields=["employee_id"],
    )
    out: set[int] = set()
    for r in rows:
        eid = r["employee_id"]
        out.add(unwrap_m2o_fn(eid))
    return out
