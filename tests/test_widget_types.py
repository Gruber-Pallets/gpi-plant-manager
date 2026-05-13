"""Unit tests for the widget type registry."""
from __future__ import annotations

import pytest

from zira_dashboard import widget_types


def test_registry_has_three_phase1_types():
    types = widget_types.all_types()
    type_ids = {t["type"] for t in types}
    assert {"pallets_by_wc", "goat_race", "ribbons"}.issubset(type_ids)


def test_each_entry_has_required_fields():
    for entry in widget_types.all_types():
        assert isinstance(entry["type"], str) and entry["type"]
        assert isinstance(entry["label"], str) and entry["label"]
        assert isinstance(entry["data_params_schema"], list)
        assert isinstance(entry["visual_params_schema"], list)
        assert isinstance(entry["resolver"], str) and entry["resolver"]
        assert isinstance(entry["partial"], str) and entry["partial"]


def test_resolver_names_resolve_to_real_functions():
    from zira_dashboard import widget_data
    for entry in widget_types.all_types():
        fn = getattr(widget_data, entry["resolver"], None)
        assert callable(fn), f"resolver {entry['resolver']} not found in widget_data"


@pytest.mark.xfail(reason="partials are created in Task 5; this becomes a real assertion then")
def test_partial_paths_point_to_existing_files():
    import os
    template_dir = os.path.join(
        os.path.dirname(__file__), "..", "src", "zira_dashboard", "templates"
    )
    for entry in widget_types.all_types():
        path = os.path.join(template_dir, entry["partial"])
        assert os.path.exists(path), f"partial not found: {entry['partial']}"


def test_get_returns_entry_by_type():
    entry = widget_types.get("goat_race")
    assert entry is not None
    assert entry["type"] == "goat_race"


def test_get_unknown_type_returns_none():
    assert widget_types.get("nonexistent_type") is None


def test_options_from_values_are_in_allow_list():
    allowed = {"groups", "value_streams", "wcs"}
    for entry in widget_types.all_types():
        for field in entry["data_params_schema"]:
            if "options_from" in field:
                assert field["options_from"] in allowed, \
                    f"unknown options_from: {field['options_from']}"
