"""Widget type registry — Phase 1 (3 types).

Each registered type carries:
  - data_params_schema: list of fields the placement provides to the
    resolver (e.g. group name, WC name).
  - visual_params_schema: list of fields the workshop offers for the
    visual preset (color, sort order, etc.).
  - resolver: name of the function in `widget_data` to call.
  - partial: Jinja partial relative to the templates dir.

Adding a new type later: append a dict here, add the resolver function
to widget_data.py, drop the partial under templates/widgets/.
"""
from __future__ import annotations


_REGISTRY: list[dict] = [
    {
        "type": "pallets_by_wc",
        "label": "Pallets by Work Center",
        "data_params_schema": [
            {"key": "group", "label": "Group", "input": "select",
             "options_from": "groups", "required": True},
        ],
        "visual_params_schema": [
            {"key": "color", "label": "Bar color", "input": "color", "default": "#22c55e"},
            {"key": "sort", "label": "Sort order", "input": "select",
             "options": [
                 {"value": "preset", "label": "By preset order"},
                 {"value": "desc",   "label": "Most pallets first"},
                 {"value": "asc",    "label": "Fewest pallets first"},
                 {"value": "alpha",  "label": "Alphabetical"},
             ],
             "default": "preset"},
            {"key": "number_position", "label": "Number position", "input": "select",
             "options": [
                 {"value": "widget", "label": "Right of bar"},
                 {"value": "bar",    "label": "End of bar"},
                 {"value": "inside", "label": "Inside bar"},
                 {"value": "hidden", "label": "Hidden"},
             ],
             "default": "widget"},
        ],
        "resolver": "_resolve_pallets_by_wc",
        "partial": "widgets/_widget_pallets_by_wc.html",
    },
    {
        "type": "goat_race",
        "label": "Vs. Goat Pace",
        "data_params_schema": [
            {"key": "group", "label": "Group", "input": "select",
             "options_from": "groups", "required": True},
        ],
        "visual_params_schema": [
            {"key": "color", "label": "Accent color", "input": "color", "default": "#22c55e"},
        ],
        "resolver": "_resolve_goat_race",
        "partial": "widgets/_widget_goat_race.html",
    },
    {
        "type": "ribbons",
        "label": "Monthly Ribbons",
        "data_params_schema": [
            {"key": "group", "label": "Group", "input": "select",
             "options_from": "groups", "required": True},
        ],
        "visual_params_schema": [],
        "resolver": "_resolve_ribbons",
        "partial": "widgets/_widget_ribbons.html",
    },
]


def all_types() -> list[dict]:
    return list(_REGISTRY)


def get(type_id: str) -> dict | None:
    for entry in _REGISTRY:
        if entry["type"] == type_id:
            return entry
    return None
