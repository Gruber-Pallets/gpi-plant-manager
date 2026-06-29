from datetime import date
from zoneinfo import ZoneInfo

from zira_dashboard import forklift_ingest

TZ = ZoneInfo("America/Chicago")

# Plant-local timestamps (epoch ms), computed for America/Chicago (CDT, UTC-5):
#   1782484200000 = 2026-06-26 09:30 local
#   1782485100000 = 2026-06-26 09:45 local
#   1782501300000 = 2026-06-26 14:15 local
#   1782520200000 = 2026-06-27 00:30 UTC == 2026-06-26 19:30 local (still day1)
#   1782565200000 = 2026-06-27 08:00 local
COMPLETIONS = [
    {"id": "c1", "workstationName": "Prosaw #4", "completedBy": "fk-1",
     "createdAt": 1782484200000, "responseMs": 120000, "handlingMs": 300000},
    {"id": "c2", "workstationName": "Prosaw #4", "completedBy": "fk-1",
     "createdAt": 1782485100000, "responseMs": 180000, "handlingMs": 200000},
    {"id": "c3", "workstationName": "Junior #3", "completedBy": "fk-2",
     "createdAt": 1782501300000, "responseMs": 60000, "handlingMs": 90000},
    {"id": "c4", "workstationName": "Junior #3", "completedBy": "fk-1",
     "createdAt": 1782520200000, "responseMs": 240000, "handlingMs": 150000},
    {"id": "c5", "workstationName": "Prosaw #4", "completedBy": "fk-2",
     "createdAt": 1782565200000, "responseMs": 30000, "handlingMs": 60000},
    {"id": "skip-no-created", "workstationName": "X", "completedBy": "fk-1"},
    {"id": "skip-no-driver", "workstationName": "X", "createdAt": 1782484200000},
]
ID2NAME = {"fk-1": "Trent", "fk-2": "Louie"}


def test_aggregate_completions_buckets_by_local_day_and_hour():
    calls_rows, _ = forklift_ingest.aggregate_completions(COMPLETIONS, ID2NAME, TZ)
    by_day = {r["day"]: r for r in calls_rows}
    assert set(by_day) == {date(2026, 6, 26), date(2026, 6, 27)}

    d1 = by_day[date(2026, 6, 26)]
    assert d1["total_calls"] == 4          # c1,c2,c3,c4 (skips ignored)
    assert d1["by_station"] == {"Prosaw #4": 2, "Junior #3": 2}
    # hour 9 (c1,c2), hour 14 (c3), hour 19 (c4 -- UTC said 6/27 but local is 6/26)
    assert d1["by_hour"]["9"]["calls"] == 2
    assert d1["by_hour"]["14"]["calls"] == 1
    assert d1["by_hour"]["19"]["calls"] == 1
    assert "19" in d1["by_hour"]  # proves plant-tz bucketing, not UTC
    # feed has no priority/skill/overload data
    assert d1["urgent_calls"] == 0 and d1["by_skill"] == {}
    assert d1["by_hour"]["9"]["overload"] == 0

    d2 = by_day[date(2026, 6, 27)]
    assert d2["total_calls"] == 1
    assert d2["by_hour"]["8"]["calls"] == 1


def test_aggregate_completions_driver_rows():
    _, driver_rows = forklift_ingest.aggregate_completions(COMPLETIONS, ID2NAME, TZ)
    by_key = {(r["day"], r["driver_id"]): r for r in driver_rows}

    # fk-1 on day1: c1,c2,c4 -> 3 calls
    r = by_key[(date(2026, 6, 26), "fk-1")]
    assert r["name"] == "Trent"
    assert r["calls"] == 3
    assert r["on_call_ms"] == 300000 + 200000 + 150000   # sum(handlingMs)
    assert r["avg_ms"] == round((120000 + 180000 + 240000) / 3)
    assert r["max_ms"] == 240000
    # fields the feed can't supply
    assert r["on_time"] == 0 and r["late"] == 0 and r["utilization_pct"] == 0
    assert r["available_ms"] == 0

    # fk-2 on day1: only c3
    r2 = by_key[(date(2026, 6, 26), "fk-2")]
    assert r2["name"] == "Louie"
    assert r2["calls"] == 1 and r2["on_call_ms"] == 90000

    # fk-2 on day2: only c5
    r3 = by_key[(date(2026, 6, 27), "fk-2")]
    assert r3["calls"] == 1


def test_aggregate_completions_unknown_driver_falls_back_to_id():
    items = [{"id": "x", "completedBy": "fk-99", "createdAt": 1782484200000,
              "responseMs": 1000, "handlingMs": 2000}]
    _, driver_rows = forklift_ingest.aggregate_completions(items, {}, TZ)
    assert driver_rows[0]["name"] == "fk-99"
    assert driver_rows[0]["driver_id"] == "fk-99"


def test_aggregate_completions_empty():
    calls_rows, driver_rows = forklift_ingest.aggregate_completions([], {}, TZ)
    assert calls_rows == [] and driver_rows == []

DASHBOARD = {
    "driverLeaderboard": [
        {"driverId": "fk-1", "name": "Trent", "total": 86, "onTime": 85, "late": 1,
         "avgMs": 190000, "maxMs": 700000, "utilizationPct": 95,
         "totalOnCallMs": 17000000, "availableMs": 17900000},
    ],
    "hourlyClaimAvgs": [
        {"slot": 8, "avgMinutes": 3.0, "calls": 40, "overloadCount": 0, "neglectedCount": 0},
        {"slot": 9, "avgMinutes": 5.0, "calls": 70, "overloadCount": 2, "neglectedCount": 1},
    ],
}
HISTORY = [
    {"workstationName": "Prosaw #4", "requiredSkillId": "sk-2", "priority": "urgent", "status": "completed"},
    {"workstationName": "Prosaw #4", "requiredSkillId": "sk-2", "priority": "normal", "status": "completed"},
    {"workstationName": "Junior #3", "requiredSkillId": "sk-1", "priority": "normal", "status": "completed"},
    {"workstationName": "Junior #3", "requiredSkillId": "sk-1", "priority": "normal", "status": "canceled"},
]


def test_build_calls_daily_aggregates_history_and_hours():
    row = forklift_ingest.build_calls_daily(date(2026, 6, 26), DASHBOARD, HISTORY)
    assert row["day"] == date(2026, 6, 26)
    assert row["total_calls"] == 3          # completed only
    assert row["urgent_calls"] == 1
    assert row["by_station"] == {"Prosaw #4": 2, "Junior #3": 1}
    assert row["by_skill"] == {"sk-2": 2, "sk-1": 1}
    assert row["overload_count"] == 2       # summed across hourly slots
    assert row["neglected_count"] == 1
    assert row["by_hour"]["9"]["calls"] == 70


def test_build_driver_daily_maps_leaderboard_rows():
    rows = forklift_ingest.build_driver_daily(date(2026, 6, 26), DASHBOARD)
    assert len(rows) == 1
    r = rows[0]
    assert r["driver_id"] == "fk-1"
    assert r["name"] == "Trent"
    assert r["calls"] == 86 and r["on_time"] == 85 and r["late"] == 1
    assert r["avg_ms"] == 190000 and r["utilization_pct"] == 95
    assert r["on_call_ms"] == 17000000 and r["available_ms"] == 17900000


def test_build_calls_daily_handles_empty_payloads():
    row = forklift_ingest.build_calls_daily(date(2026, 6, 26), {}, [])
    assert row["total_calls"] == 0 and row["by_station"] == {} and row["by_hour"] == {}


def test_driver_metrics_from_dashboard_maps_names_to_ids():
    dashboard = {"driverLeaderboard": [
        {"name": "Trent", "onTime": 18, "late": 2,
         "totalOnCallMs": 700000, "availableMs": 3600000, "utilizationPct": 19.4},
        {"name": "Ghost", "onTime": 5, "late": 0,
         "totalOnCallMs": 1000, "availableMs": 2000, "utilizationPct": 50.0},
    ]}
    id_to_name = {"d1": "Trent"}
    rows = forklift_ingest.driver_metrics_from_dashboard(dashboard, id_to_name)
    trent = next(r for r in rows if r["name"] == "Trent")
    assert trent["driver_id"] == "d1"      # resolved via name->id
    assert trent["on_time"] == 18 and trent["late"] == 2
    assert trent["on_call_ms"] == 700000 and trent["available_ms"] == 3600000
    ghost = next(r for r in rows if r["name"] == "Ghost")
    assert ghost["driver_id"] == "Ghost"   # fallback to name when unmapped
