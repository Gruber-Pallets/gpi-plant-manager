from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    meter_id: str
    name: str
    category: str
    cell: str


STATIONS: tuple[Station, ...] = (
    Station(meter_id="42711", name="Dismantler 1", category="Dismantler", cell="Recycling"),
    Station(meter_id="42713", name="Dismantler 2", category="Dismantler", cell="Recycling"),
    Station(meter_id="42714", name="Dismantler 3", category="Dismantler", cell="Recycling"),
    Station(meter_id="42715", name="Dismantler 4", category="Dismantler", cell="Recycling"),
    Station(meter_id="40721", name="Repair 1",    category="Repair",    cell="Recycling"),
    Station(meter_id="40720", name="Repair 2",    category="Repair",    cell="Recycling"),
    Station(meter_id="40719", name="Repair 3",    category="Repair",    cell="Recycling"),
    Station(meter_id="44483", name="Repair 4",    category="Repair",    cell="Recycling"),
    Station(meter_id="43286", name="Trim Saw",    category="Other",     cell="Other"),
    Station(meter_id="42345", name="Junior 2",    category="Other",     cell="Other"),
    Station(meter_id="44484", name="Hand Build #1", category="Hand Build", cell="New"),
)

CATEGORIES: tuple[str, ...] = ("Dismantler", "Repair", "Other")


def recycling_stations() -> list[Station]:
    return [s for s in STATIONS if s.cell == "Recycling"]
