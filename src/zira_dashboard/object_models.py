"""Registered model adapters for the Odoo-like object API."""
from __future__ import annotations

from datetime import date

from . import db, object_api, staffing, work_centers_store


class PersonModel(object_api.ObjectModel):
    name = "plant.person"
    display_name = "People"
    default_order = "name asc"
    writable_fields = {"active", "reserve", "excluded", "spanish_speaker"}
    fields = {
        "id": object_api.FieldSpec("integer", "ID", readonly=True),
        "odoo_id": object_api.FieldSpec("integer", "Odoo ID", readonly=True),
        "name": object_api.FieldSpec("char", "Name", readonly=True),
        "active": object_api.FieldSpec("boolean", "Active"),
        "reserve": object_api.FieldSpec("boolean", "Reserve"),
        "excluded": object_api.FieldSpec("boolean", "Excluded"),
        "wage_type": object_api.FieldSpec("char", "Wage Type", readonly=True),
        "spanish_speaker": object_api.FieldSpec("boolean", "Spanish Speaker"),
        "skills": object_api.FieldSpec("json", "Skills", readonly=True),
        "departments": object_api.FieldSpec("json", "Departments", readonly=True),
    }

    def all_records(self, context: dict) -> list[dict]:
        return db.query(
            "SELECT p.id, p.odoo_id, p.name, p.active, p.reserve, p.excluded, "
            "p.wage_type, p.spanish_speaker, "
            "COALESCE(jsonb_object_agg(s.name, ps.level) "
            "FILTER (WHERE s.name IS NOT NULL), '{}'::jsonb) AS skills, "
            "COALESCE(jsonb_agg(DISTINCT wc.department) "
            "FILTER (WHERE wc.department IS NOT NULL AND wc.department <> ''), '[]'::jsonb) "
            "AS departments "
            "FROM people p "
            "LEFT JOIN person_skills ps ON ps.person_id = p.id "
            "LEFT JOIN skills s ON s.id = ps.skill_id "
            "LEFT JOIN work_center_default_people wcdp ON wcdp.person_id = p.id "
            "LEFT JOIN work_centers wc ON wc.id = wcdp.wc_id "
            "GROUP BY p.id "
            "ORDER BY lower(p.name)"
        )

    def write_records(self, ids: list, values: dict, context: dict) -> bool:
        clean = {key: bool(value) for key, value in values.items() if key in self.writable_fields}
        if not ids or not clean:
            return True
        sets = ", ".join(f"{key} = %s" for key in clean.keys())
        db.execute(
            f"UPDATE people SET {sets}, local_dirty = TRUE WHERE id = ANY(%s)",
            (*clean.values(), ids),
        )
        staffing._invalidate_roster_cache()
        return True


class SkillModel(object_api.ObjectModel):
    name = "plant.skill"
    display_name = "Skills"
    default_order = "skill_type asc"
    fields = {
        "id": object_api.FieldSpec("integer", "ID", readonly=True),
        "odoo_id": object_api.FieldSpec("integer", "Odoo ID", readonly=True),
        "name": object_api.FieldSpec("char", "Name", readonly=True),
        "skill_type": object_api.FieldSpec("char", "Skill Type", readonly=True),
        "sort_order": object_api.FieldSpec("integer", "Sort Order", readonly=True),
    }

    def all_records(self, context: dict) -> list[dict]:
        return db.query(
            "SELECT id, odoo_id, name, skill_type, sort_order "
            "FROM skills ORDER BY skill_type, sort_order, lower(name)"
        )


class PersonSkillModel(object_api.ObjectModel):
    name = "plant.person_skill"
    display_name = "Person Skills"
    default_order = "person_name asc"
    writable_fields = {"person_id", "person_name", "skill_id", "skill_name", "level"}
    fields = {
        "id": object_api.FieldSpec("char", "ID", readonly=True),
        "person_id": object_api.FieldSpec("integer", "Person ID"),
        "person_odoo_id": object_api.FieldSpec("integer", "Person Odoo ID", readonly=True),
        "person_name": object_api.FieldSpec("char", "Person"),
        "skill_id": object_api.FieldSpec("integer", "Skill ID"),
        "skill_name": object_api.FieldSpec("char", "Skill"),
        "skill_type": object_api.FieldSpec("char", "Skill Type", readonly=True),
        "level": object_api.FieldSpec("integer", "Level"),
    }

    def all_records(self, context: dict) -> list[dict]:
        rows = db.query(
            "SELECT (ps.person_id::text || ':' || ps.skill_id::text) AS id, "
            "ps.person_id, pe.odoo_id AS person_odoo_id, pe.name AS person_name, "
            "ps.skill_id, sk.name AS skill_name, sk.skill_type, ps.level "
            "FROM person_skills ps "
            "JOIN people pe ON pe.id = ps.person_id "
            "JOIN skills sk ON sk.id = ps.skill_id "
            "ORDER BY lower(pe.name), sk.skill_type, sk.sort_order, lower(sk.name)"
        )
        return [dict(row) for row in rows]

    def _level(self, raw) -> int:
        try:
            level = int(raw)
        except (TypeError, ValueError) as exc:
            raise object_api.ObjectAPIError("invalid_field", "level must be an integer", 400) from exc
        if level < 0 or level > 3:
            raise object_api.ObjectAPIError("invalid_field", "level must be between 0 and 3", 400)
        return level

    def _parse_id(self, raw_id) -> tuple[int, int]:
        try:
            person_id, skill_id = str(raw_id).split(":", 1)
            return int(person_id), int(skill_id)
        except (TypeError, ValueError) as exc:
            raise object_api.ObjectAPIError(
                "invalid_request",
                "person_skill ids must look like person_id:skill_id",
                400,
            ) from exc

    def _resolve_person_id(self, values: dict) -> int:
        if values.get("person_id") is not None:
            rows = db.query("SELECT id FROM people WHERE id = %s", (int(values["person_id"]),))
        elif values.get("person_name"):
            rows = db.query(
                "SELECT id FROM people WHERE lower(name) = lower(%s)",
                (str(values["person_name"]).strip(),),
            )
        else:
            raise object_api.ObjectAPIError(
                "invalid_request",
                "person_id or person_name is required",
                400,
            )
        if not rows:
            raise object_api.ObjectAPIError("not_found", "Person not found", 404)
        return int(rows[0]["id"])

    def _resolve_skill_id(self, values: dict) -> int:
        if values.get("skill_id") is not None:
            rows = db.query("SELECT id FROM skills WHERE id = %s", (int(values["skill_id"]),))
        elif values.get("skill_name"):
            rows = db.query(
                "SELECT id FROM skills WHERE lower(name) = lower(%s)",
                (str(values["skill_name"]).strip(),),
            )
        else:
            raise object_api.ObjectAPIError(
                "invalid_request",
                "skill_id or skill_name is required",
                400,
            )
        if not rows:
            raise object_api.ObjectAPIError("not_found", "Skill not found", 404)
        return int(rows[0]["id"])

    def _set_level(self, person_id: int, skill_id: int, level: int) -> None:
        with db.cursor() as cur:
            if level == 0:
                cur.execute(
                    "DELETE FROM person_skills WHERE person_id = %s AND skill_id = %s",
                    (person_id, skill_id),
                )
            else:
                cur.execute(
                    "INSERT INTO person_skills "
                    "(person_id, skill_id, level, local_dirty) "
                    "VALUES (%s, %s, %s, TRUE) "
                    "ON CONFLICT (person_id, skill_id) DO UPDATE SET "
                    "level = EXCLUDED.level, local_dirty = TRUE",
                    (person_id, skill_id, level),
                )
        staffing._invalidate_roster_cache()

    def create_record(self, values: dict, context: dict):
        if "level" not in values:
            raise object_api.ObjectAPIError("invalid_request", "level is required", 400)
        person_id = self._resolve_person_id(values)
        skill_id = self._resolve_skill_id(values)
        self._set_level(person_id, skill_id, self._level(values["level"]))
        return f"{person_id}:{skill_id}"

    def write_records(self, ids: list, values: dict, context: dict) -> bool:
        if set(values.keys()) != {"level"}:
            raise object_api.ObjectAPIError(
                "invalid_field",
                "Only level can be written on existing person_skill records",
                400,
            )
        level = self._level(values["level"])
        for raw_id in ids:
            person_id, skill_id = self._parse_id(raw_id)
            self._set_level(person_id, skill_id, level)
        return True


class WorkCenterModel(object_api.ObjectModel):
    name = "plant.work_center"
    display_name = "Work Centers"
    default_order = "id asc"
    writable_fields = {
        "goal_per_day",
        "min_ops",
        "max_ops",
        "department",
        "groups",
        "required_skills",
        "default_people",
        "note",
    }
    fields = {
        "id": object_api.FieldSpec("char", "ID", readonly=True),
        "name": object_api.FieldSpec("char", "Name", readonly=True),
        "bay": object_api.FieldSpec("char", "Bay", readonly=True),
        "department": object_api.FieldSpec("char", "Department"),
        "groups": object_api.FieldSpec("json", "Groups"),
        "required_skills": object_api.FieldSpec("json", "Required Skills"),
        "default_people": object_api.FieldSpec("json", "Default People"),
        "goal_per_day": object_api.FieldSpec("integer", "Goal Per Day"),
        "min_ops": object_api.FieldSpec("integer", "Min Operators"),
        "max_ops": object_api.FieldSpec("integer", "Max Operators"),
        "note": object_api.FieldSpec("text", "Note"),
    }

    def _loc_by_id(self, value: str):
        return next((loc for loc in staffing.LOCATIONS if loc.name == value), None)

    def all_records(self, context: dict) -> list[dict]:
        rows = []
        for loc in staffing.LOCATIONS:
            eff = work_centers_store.effective(loc)
            rows.append({"id": loc.name, "name": loc.name, "bay": loc.bay, **eff})
        return rows

    def write_records(self, ids: list, values: dict, context: dict) -> bool:
        for raw_id in ids:
            loc = self._loc_by_id(str(raw_id))
            if loc is not None:
                work_centers_store.save_one(loc, values)
        return True


class ScheduleModel(object_api.ObjectModel):
    name = "plant.schedule"
    display_name = "Schedules"
    default_order = "day desc"
    writable_fields = {
        "day",
        "assignments",
        "notes",
        "work_center_notes",
        "testing_day",
        "published",
    }
    fields = {
        "id": object_api.FieldSpec("char", "ID", readonly=True),
        "day": object_api.FieldSpec("date", "Day", required=True),
        "published": object_api.FieldSpec("boolean", "Published"),
        "assignments": object_api.FieldSpec("json", "Assignments"),
        "notes": object_api.FieldSpec("text", "Notes"),
        "work_center_notes": object_api.FieldSpec("json", "Work Center Notes"),
        "testing_day": object_api.FieldSpec("boolean", "Testing Day"),
    }

    def _shape(self, day: date, sched: staffing.Schedule) -> dict:
        return {
            "id": day.isoformat(),
            "day": day.isoformat(),
            "published": bool(sched.published),
            "assignments": dict(sched.assignments or {}),
            "notes": sched.notes or "",
            "work_center_notes": dict(sched.wc_notes or {}),
            "testing_day": bool(sched.testing_day),
        }

    def all_records(self, context: dict) -> list[dict]:
        return [self._shape(day, sched) for day, sched in staffing.load_schedules_bulk()]

    def create_record(self, values: dict, context: dict):
        day = date.fromisoformat(str(values["day"]))
        current = staffing.load_schedule(day)
        content_fields = {"assignments", "notes", "work_center_notes", "testing_day"}
        starts_draft = current.published and bool(content_fields.intersection(values))
        if starts_draft:
            current = staffing.draft_from_posted(current)
        sched = staffing.Schedule(
            day=day,
            published=False if starts_draft else bool(values.get("published", current.published)),
            assignments=dict(values.get("assignments") or current.assignments or {}),
            notes=str(values.get("notes", current.notes or "")),
            wc_notes=dict(values.get("work_center_notes") or current.wc_notes or {}),
            testing_day=bool(values.get("testing_day", current.testing_day)),
            custom_hours=current.custom_hours,
            published_snapshot=current.published_snapshot,
            published_delivery=current.published_delivery,
            rotation_mode=current.rotation_mode,
            assignment_sources={
                wc_name: dict(sources or {})
                for wc_name, sources in current.assignment_sources.items()
            },
        )
        staffing.save_schedule(sched)
        return day.isoformat()

    def write_records(self, ids: list, values: dict, context: dict) -> bool:
        for raw_id in ids:
            day = date.fromisoformat(str(raw_id))
            self.create_record({"day": day.isoformat(), **values}, context)
        return True


class TimeOffRequestModel(object_api.ObjectModel):
    name = "plant.time_off_request"
    display_name = "Time Off Requests"
    default_order = "start_date desc"
    fields = {
        "id": object_api.FieldSpec("integer", "ID", readonly=True),
        "person_odoo_id": object_api.FieldSpec("integer", "Person Odoo ID", readonly=True),
        "person_name": object_api.FieldSpec("char", "Person", readonly=True),
        "start_date": object_api.FieldSpec("date", "Start Date", readonly=True),
        "end_date": object_api.FieldSpec("date", "End Date", readonly=True),
        "shape": object_api.FieldSpec("char", "Shape", readonly=True),
        "hour_from": object_api.FieldSpec("float", "Hour From", readonly=True),
        "hour_to": object_api.FieldSpec("float", "Hour To", readonly=True),
        "status": object_api.FieldSpec("char", "Status", readonly=True),
        "source": object_api.FieldSpec("char", "Source", readonly=True),
    }

    def all_records(self, context: dict) -> list[dict]:
        rows = db.query(
            "SELECT r.id, r.person_odoo_id, "
            "COALESCE(p.name, '#' || r.person_odoo_id::text) AS person_name, "
            "r.date_from AS start_date, r.date_to AS end_date, r.shape, "
            "r.hour_from, r.hour_to, r.state AS status, "
            "CASE WHEN r.odoo_leave_id IS NULL THEN 'local' ELSE 'odoo' END AS source "
            "FROM time_off_requests r LEFT JOIN people p ON p.odoo_id = r.person_odoo_id "
            "ORDER BY r.date_from DESC, r.id DESC"
        )
        for row in rows:
            if hasattr(row.get("start_date"), "isoformat"):
                row["start_date"] = row["start_date"].isoformat()
            if hasattr(row.get("end_date"), "isoformat"):
                row["end_date"] = row["end_date"].isoformat()
        return rows


def build_registry() -> object_api.Registry:
    reg = object_api.Registry()
    reg.register(PersonModel())
    reg.register(SkillModel())
    reg.register(PersonSkillModel())
    reg.register(WorkCenterModel())
    reg.register(ScheduleModel())
    reg.register(TimeOffRequestModel())
    return reg
