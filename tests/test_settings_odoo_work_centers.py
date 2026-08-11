"""Contracts for the Settings-only Odoo work-center mapping controls."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from zira_dashboard import odoo_client, staffing
from zira_dashboard.routes import settings


OPTIONS = [
    {"id": 41, "name": "Repair #1"},
    {"id": 42, "name": "Dismantler #1"},
]
REPAIR_1 = next(location for location in staffing.LOCATIONS if location.name == "Repair 1")
REPAIR_2 = next(location for location in staffing.LOCATIONS if location.name == "Repair 2")


def test_settings_template_posts_odoo_record_ids_and_keeps_unavailable_catalog_read_only():
    html = Path("src/zira_dashboard/templates/settings.html").read_text()

    assert "<th>Odoo Work Center</th>" in html
    assert html.index("<th>Work Center</th>") < html.index("<th>Odoo Work Center</th>")
    assert html.index("<th>Odoo Work Center</th>") < html.index("<th>Auto</th>")
    assert 'name="{{ p }}odoo_work_center_id"' in html
    assert 'value="{{ option.id }}"' in html
    assert "Needs mapping" in html
    assert "inactive — choose a replacement" in html
    assert "odoo_work_centers_error" in html


def test_settings_autosave_shows_the_server_mapping_error_message():
    javascript = Path("src/zira_dashboard/static/settings.js").read_text()

    assert "function responseErrorMessage(response)" in javascript
    assert "Save failed: ${body.error}" in javascript
    assert "responseErrorMessage(r).then(showSavedToast)" in javascript


def _mapping_field(location) -> str:
    key = location.meter_id or f"name:{location.name}"
    return f"wc__{key}__odoo_work_center_id"


def test_mapping_updates_resolve_posted_ids_to_trusted_option_names():
    posted = {_mapping_field(REPAIR_1): "41"}

    assert settings._odoo_work_center_updates(posted, OPTIONS) == {
        "Repair 1": {"odoo_id": 41, "odoo_name": "Repair #1"}
    }


@pytest.mark.parametrize("posted", [
    {_mapping_field(REPAIR_1): "999"},
    {_mapping_field(REPAIR_1): "not-an-id"},
    {
        _mapping_field(REPAIR_1): "41",
        _mapping_field(REPAIR_2): "41",
    },
])
def test_mapping_updates_reject_unknown_non_numeric_or_duplicate_ids(posted):
    with pytest.raises(settings.InvalidOdooWorkCenterMapping):
        settings._odoo_work_center_updates(posted, OPTIONS)


def test_mapping_updates_allow_an_explicit_blank_value_to_clear_a_mapping():
    posted = {_mapping_field(REPAIR_1): ""}

    assert settings._odoo_work_center_updates(posted, OPTIONS) == {
        "Repair 1": {"odoo_id": None, "odoo_name": None}
    }


def test_fetch_manufacturing_work_centers_returns_active_id_name_catalog_and_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda *args, **kwargs: calls.append((args, kwargs)) or [
            {"id": 41, "name": " Repair #1 "},
            {"id": 0, "name": "Missing"},
            {"id": 42, "name": ""},
        ],
    )
    odoo_client._reset_cache_for_tests()

    assert odoo_client.fetch_manufacturing_work_centers() == [
        {"id": 41, "name": "Repair #1"}
    ]
    assert odoo_client.fetch_manufacturing_work_centers() == [
        {"id": 41, "name": "Repair #1"}
    ]
    assert odoo_client.fetch_manufacturing_work_centers(force=True) == [
        {"id": 41, "name": "Repair #1"}
    ]
    assert calls == [
        (("mrp.workcenter", "search_read", [("active", "=", True)]),
         {"fields": ["id", "name"], "order": "name"}),
        (("mrp.workcenter", "search_read", [("active", "=", True)]),
         {"fields": ["id", "name"], "order": "name"}),
    ]


class _Form(dict):
    def getlist(self, key):
        value = self.get(key, [])
        return value if isinstance(value, list) else [value]


class _Request:
    headers = {"accept": "application/json"}

    def __init__(self, form):
        self._form = _Form(form)

    async def form(self):
        return self._form


async def _run_inline(work):
    return work()


def _stub_work_center_save(monkeypatch, *, replacements):
    monkeypatch.setattr(settings.asyncio, "to_thread", _run_inline)
    monkeypatch.setattr(settings.work_centers_store, "registered_groups", lambda: [])
    monkeypatch.setattr(settings.work_centers_store, "all_group_names", lambda _kind: [])
    monkeypatch.setattr(settings.work_centers_store, "default_people", lambda _loc: [])
    monkeypatch.setattr(
        settings.work_centers_store, "_normalize_default_targets", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        settings.work_centers_store, "replace_default_targets", lambda **_kwargs: None
    )
    monkeypatch.setattr(settings.work_centers_store, "save_one", lambda *_args: None)
    monkeypatch.setattr(
        settings.work_centers_store,
        "effective",
        lambda _location: {"odoo_work_center_id": None},
    )
    monkeypatch.setattr(
        settings.work_centers_store,
        "replace_odoo_work_center_mappings",
        lambda updates: replacements.append(updates),
    )


def test_work_center_post_uses_fresh_catalog_and_replaces_mappings_once(monkeypatch):
    replacements = []
    _stub_work_center_save(monkeypatch, replacements=replacements)
    monkeypatch.setattr(
        settings.odoo_client,
        "fetch_manufacturing_work_centers",
        lambda *, force: OPTIONS if force else pytest.fail("mapping post must refresh"),
    )
    location = staffing.LOCATIONS[0]

    response = asyncio.run(settings.settings_save_work_centers(
        _Request({_mapping_field(location): "41"})
    ))

    assert response.status_code == 200
    assert replacements == [{
        location.name: {"odoo_id": 41, "odoo_name": "Repair #1"}
    }]


def test_invalid_mapping_rejects_before_any_settings_mutation(monkeypatch):
    monkeypatch.setattr(settings.asyncio, "to_thread", _run_inline)
    monkeypatch.setattr(
        settings.odoo_client, "fetch_manufacturing_work_centers", lambda *, force: OPTIONS
    )
    for name in (
        "registered_groups", "default_people", "save_one", "replace_default_targets",
        "replace_odoo_work_center_mappings",
    ):
        monkeypatch.setattr(
            settings.work_centers_store,
            name,
            lambda *_args, _name=name, **_kwargs: pytest.fail(
                f"{_name} must not run after invalid mapping input"
            ),
        )

    response = asyncio.run(settings.settings_save_work_centers(
        _Request({_mapping_field(staffing.LOCATIONS[0]): "999"})
    ))

    assert response.status_code == 422


def test_partial_mapping_post_rejects_id_owned_by_an_unposted_location(monkeypatch):
    monkeypatch.setattr(settings.asyncio, "to_thread", _run_inline)
    monkeypatch.setattr(
        settings.odoo_client, "fetch_manufacturing_work_centers", lambda *, force: OPTIONS
    )
    monkeypatch.setattr(
        settings.work_centers_store,
        "effective",
        lambda location: {"odoo_work_center_id": 41 if location is REPAIR_2 else None},
    )
    for name in (
        "registered_groups", "default_people", "save_one", "replace_default_targets",
        "replace_odoo_work_center_mappings",
    ):
        monkeypatch.setattr(
            settings.work_centers_store,
            name,
            lambda *_args, _name=name, **_kwargs: pytest.fail(
                f"{_name} must not run after an unposted mapping collision"
            ),
        )

    response = asyncio.run(settings.settings_save_work_centers(
        _Request({_mapping_field(REPAIR_1): "41"})
    ))

    assert response.status_code == 422


def test_unavailable_catalog_rejects_mapping_post_before_any_settings_mutation(monkeypatch):
    monkeypatch.setattr(settings.asyncio, "to_thread", _run_inline)
    monkeypatch.setattr(
        settings.odoo_client,
        "fetch_manufacturing_work_centers",
        lambda *, force: (_ for _ in ()).throw(RuntimeError("Odoo offline")),
    )
    for name in (
        "registered_groups", "default_people", "save_one", "replace_default_targets",
        "replace_odoo_work_center_mappings",
    ):
        monkeypatch.setattr(
            settings.work_centers_store,
            name,
            lambda *_args, _name=name, **_kwargs: pytest.fail(
                f"{_name} must not run while the catalog is unavailable"
            ),
        )

    response = asyncio.run(settings.settings_save_work_centers(
        _Request({_mapping_field(staffing.LOCATIONS[0]): "41"})
    ))

    assert response.status_code == 503


def test_save_without_mapping_field_never_fetches_catalog_or_replaces_mapping(monkeypatch):
    replacements = []
    _stub_work_center_save(monkeypatch, replacements=replacements)
    monkeypatch.setattr(
        settings.odoo_client,
        "fetch_manufacturing_work_centers",
        lambda **_kwargs: pytest.fail("unrelated autosave must not fetch catalog"),
    )
    saved = []
    monkeypatch.setattr(
        settings.work_centers_store, "save_one", lambda location, updates: saved.append((location, updates))
    )
    location = staffing.LOCATIONS[0]

    response = asyncio.run(settings.settings_save_work_centers(
        _Request({f"wc__{location.meter_id}__goal_per_day": "101"})
    ))

    assert response.status_code == 200
    assert saved == [(location, {"goal_per_day": "101"})]
    assert replacements == []
