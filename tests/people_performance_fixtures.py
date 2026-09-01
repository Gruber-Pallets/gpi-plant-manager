from datetime import UTC, date, datetime, timedelta

from zira_dashboard.attendance_timeline import LocationSpan
from zira_dashboard.forklift_ingest import ForkliftCompletionEvent
from zira_dashboard.people_performance import (
    BreakSpan,
    ForkliftDayMetric,
    assemble_dashboard,
)
from zira_dashboard.production_segments import SegmentScore


DAY = date(2026, 8, 28)
START = datetime(2026, 8, 28, 11, 0, tzinfo=UTC)
END = START + timedelta(hours=8)


def span(
    employee_id,
    name,
    start_minute,
    end_minute,
    wc,
    status="valid",
    *,
    is_open=None,
):
    return LocationSpan(
        employee_odoo_id=employee_id,
        employee_name=name,
        start_utc=START + timedelta(minutes=start_minute),
        end_utc=START + timedelta(minutes=end_minute),
        status=status,
        app_work_center_name=wc,
        odoo_work_center_id=(100 + employee_id if wc else None),
        odoo_work_center_name=wc,
        attendance_ids=(1000 + employee_id,),
        department_repair=None,
        is_open=(end_minute == 480 if is_open is None else is_open),
    )


def score(employee_id, name, wc, start_minute, end_minute, actual, goal):
    minutes = end_minute - start_minute
    return SegmentScore(
        segment_id=employee_id,
        wc_name=wc,
        person_name=name,
        start_utc=START + timedelta(minutes=start_minute),
        end_utc=START + timedelta(minutes=end_minute),
        source="odoo",
        productive_minutes=minutes,
        actual_units=actual,
        goal_units=goal,
        runway_units=max(actual, goal),
        is_active=end_minute == 480,
        result="ahead" if actual >= goal else "behind",
        person_odoo_id=employee_id,
    )


def event(name, minute, *, on_time=None, late=None, event_id="call"):
    return ForkliftCompletionEvent(
        event_id=f"{event_id}-{minute}",
        driver_id=f"driver-{name}",
        driver_name=name,
        created_at_utc=START + timedelta(minutes=minute),
        workstation_name="Repair 1",
        on_time=on_time,
        late=late,
        response_ms=60000,
        handling_ms=120000,
    )


def driver_metric(calls, on_time, late, *, score_value=82.0, timeline_available=True):
    return ForkliftDayMetric(
        calls=calls,
        on_time=on_time,
        late=late,
        handling_minutes=calls * 2.0,
        score=score_value,
        ontime_floor_pct=80.0,
        timeline_available=timeline_available,
    )


def busy_dashboard_model():
    spans = (
        span(44, "Amy Behind", 0, 480, "Repair 1"),
        span(45, "Zed Ahead", 0, 480, "Repair 2"),
        span(46, "Ben Driver", 0, 480, "Tablets"),
        span(47, "Cal Missing", 0, 480, None, "missing_required_location"),
        span(49, "Sam Stale", 0, 480, "Repair 1", "stale_open_location"),
        span(48, "Mia Mixed", 0, 90, "Repair 1"),
        span(48, "Mia Mixed", 90, 95, "Repair 2"),
        span(48, "Mia Mixed", 95, 180, "Tablets", is_open=False),
    )
    scores = (
        score(44, "Amy Behind", "Repair 1", 0, 480, 120, 180),
        score(45, "Zed Ahead", "Repair 2", 0, 480, 210, 180),
        score(48, "Mia Mixed", "Repair 1", 0, 90, 40, 35),
        score(48, "Mia Mixed", "Repair 2", 90, 95, 1, 2),
    )
    calls = {
        46: (
            event("Ben Driver", 20, on_time=True),
            event("Ben Driver", 465, late=True),
        ),
        48: (event("Mia Mixed", 120, on_time=True),),
    }
    return assemble_dashboard(
        day=DAY,
        as_of_utc=END,
        window_start_utc=START,
        window_end_utc=END,
        spans=spans,
        production_scores=scores,
        downtime_by_wc={
            "Repair 1": ((START + timedelta(minutes=60), START + timedelta(minutes=75)),),
            "Repair 2": (),
        },
        breakdown_exclusions_by_person_wc={},
        forklift_events_by_employee_id=calls,
        forklift_day_metrics_by_employee_id={
            46: driver_metric(2, 1, 1),
            48: driver_metric(1, 1, 0),
        },
        breaks=(
            BreakSpan(
                START + timedelta(minutes=270),
                START + timedelta(minutes=300),
                "Planned break",
            ),
        ),
        metered_wc_names={"Repair 1", "Repair 2"},
        source_warnings=("Forklift data unavailable",),
        is_today=True,
    )
