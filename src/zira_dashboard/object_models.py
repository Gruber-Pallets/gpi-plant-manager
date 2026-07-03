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
        sched = staffing.Schedule(
            day=day,
            published=bool(values.get("published", current.published)),
            assignments=dict(values.get("assignments") or current.assignments or {}),
            notes=str(values.get("notes", current.notes or "")),
            wc_notes=dict(values.get("work_center_notes") or current.wc_notes or {}),
            testing_day=bool(values.get("testing_day", current.testing_day)),
            custom_hours=current.custom_hours,
            published_snapshot=current.published_snapshot,
        )
        staffing.save_schedule(sched)
        return day.isoformat()

    def write_records(self, ids: list, values: dict, context: dict) -> bool:
        for raw_id in ids:
            day = date.fromisoformat(str(raw_id))
            current = staffing.load_schedule(day)
            merged = {
                "day": day.isoformat(),
                "published": current.published,
                "assignments": current.assignments,
                "notes": current.notes,
                "work_center_notes": current.wc_notes,
                "testing_day": current.testing_day,
            }
            merged.update(values)
            self.create_record(merged, context)
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
    reg.register(WorkCenterModel())
    reg.register(ScheduleModel())
    reg.register(TimeOffRequestModel())
    return reg
