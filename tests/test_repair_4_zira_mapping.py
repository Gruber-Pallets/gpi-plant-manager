from zira_dashboard import staffing
from zira_dashboard._schema import SCHEMA_DDL
from zira_dashboard.stations import STATIONS, recycling_stations


def test_repair_4_uses_its_zira_meter_in_both_registries():
    station = next(station for station in STATIONS if station.name == "Repair 4")
    location = next(location for location in staffing.LOCATIONS if location.name == "Repair 4")

    assert station.meter_id == "44483"
    assert location.meter_id == station.meter_id
    assert station in recycling_stations()


def test_repair_4_meter_backfills_blank_work_center_rows():
    assert "SET meter_id = '44483'" in SCHEMA_DDL
    assert "WHERE name = 'Repair 4'" in SCHEMA_DDL
    assert "COALESCE(meter_id, '') = ''" in SCHEMA_DDL
