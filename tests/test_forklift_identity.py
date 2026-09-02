import pytest

from zira_dashboard import forklift_identity_store, forklift_store, staffing


def _person(name: str, employee_id: int):
    return staffing.Person(name=name, employee_id=employee_id)


@pytest.fixture(autouse=True)
def no_explicit_identity_mappings(monkeypatch):
    monkeypatch.setattr(forklift_identity_store, "mapping_ids", lambda: {})


def test_unique_external_driver_name_resolves_to_odoo_employee_id(monkeypatch):
    monkeypatch.setattr(forklift_store, "name_map", lambda kind: {})
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [_person("Trent Iverson", 60), _person("Louie Smothers", 61)],
    )

    assert forklift_store.resolve_forklift_driver_ids(
        {"driver-Trent": {"Trent"}}, allowed_employee_ids={60, 61}
    ) == {"driver-Trent": 60}


def test_unchanged_ambiguous_name_is_not_accepted_as_identity(monkeypatch):
    monkeypatch.setattr(forklift_store, "name_map", lambda kind: {})
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [_person("Jesus Ramos", 62), _person("Jesus Diaz", 63)],
    )

    assert (
        forklift_store.resolve_forklift_driver_ids(
            {"driver-Jesus": {"Jesus"}}, allowed_employee_ids={62, 63}
        )
        == {}
    )


def test_conflicting_names_for_one_external_driver_are_refused(monkeypatch):
    monkeypatch.setattr(forklift_store, "name_map", lambda kind: {})
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [_person("Trent Iverson", 60), _person("Louie Smothers", 61)],
    )

    assert (
        forklift_store.resolve_forklift_driver_ids(
            {"driver-1": {"Trent", "Louie"}}, allowed_employee_ids={60, 61}
        )
        == {}
    )


def test_two_external_driver_ids_cannot_claim_one_employee(monkeypatch):
    monkeypatch.setattr(forklift_store, "name_map", lambda kind: {})
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [_person("Trent Iverson", 60)],
    )

    assert (
        forklift_store.resolve_forklift_driver_ids(
            {"driver-1": {"Trent"}, "driver-2": {"Trent Iverson"}},
            allowed_employee_ids={60},
        )
        == {}
    )


def test_manual_override_still_requires_one_active_odoo_identity(monkeypatch):
    monkeypatch.setattr(
        forklift_store,
        "name_map",
        lambda kind: {"T-Man": "Trent Iverson"},
    )
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [_person("Trent Iverson", 60)],
    )

    assert forklift_store.resolve_forklift_driver_ids(
        {"driver-1": {"T-Man"}}, allowed_employee_ids={60}
    ) == {"driver-1": 60}


def test_explicit_mapping_resolves_an_ambiguous_name(monkeypatch):
    monkeypatch.setattr(
        forklift_identity_store,
        "mapping_ids",
        lambda: {"driver-Jesus": 63},
    )
    monkeypatch.setattr(forklift_store, "name_map", lambda kind: {})
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [_person("Jesus Ramos", 62), _person("Jesus Diaz", 63)],
    )

    assert forklift_store.resolve_forklift_driver_ids(
        {"driver-Jesus": {"Jesus"}}, allowed_employee_ids={62, 63}
    ) == {"driver-Jesus": 63}


def test_explicit_mapping_to_inactive_employee_fails_closed(monkeypatch):
    monkeypatch.setattr(
        forklift_identity_store,
        "mapping_ids",
        lambda: {"driver-Trent": 60},
    )
    monkeypatch.setattr(forklift_store, "name_map", lambda kind: {})
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [
            staffing.Person(name="Trent Former", employee_id=60, active=False),
            _person("Trent Iverson", 61),
        ],
    )

    assert (
        forklift_store.resolve_forklift_driver_ids(
            {"driver-Trent": {"Trent"}}, allowed_employee_ids={60, 61}
        )
        == {}
    )


def test_automatic_match_cannot_steal_explicitly_reserved_employee(monkeypatch):
    monkeypatch.setattr(
        forklift_identity_store,
        "mapping_ids",
        lambda: {"driver-explicit": 60},
    )
    monkeypatch.setattr(forklift_store, "name_map", lambda kind: {})
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [_person("Trent Iverson", 60)],
    )

    assert forklift_store.resolve_forklift_driver_ids(
        {
            "driver-explicit": {"Unknown Source Name"},
            "driver-inferred": {"Trent"},
        },
        allowed_employee_ids={60},
    ) == {"driver-explicit": 60}


def test_explicit_mapping_outside_allowed_employee_ids_fails_closed(monkeypatch):
    monkeypatch.setattr(
        forklift_identity_store,
        "mapping_ids",
        lambda: {"driver-Trent": 60},
    )
    monkeypatch.setattr(forklift_store, "name_map", lambda kind: {})
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [_person("Trent Former", 60), _person("Trent Iverson", 61)],
    )

    assert (
        forklift_store.resolve_forklift_driver_ids(
            {"driver-Trent": {"Trent Iverson"}}, allowed_employee_ids={61}
        )
        == {}
    )


def test_absent_explicit_driver_still_reserves_its_active_employee(monkeypatch):
    monkeypatch.setattr(
        forklift_identity_store,
        "mapping_ids",
        lambda: {"driver-A": 60},
    )
    monkeypatch.setattr(forklift_store, "name_map", lambda kind: {})
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [_person("Trent Iverson", 60)],
    )

    assert (
        forklift_store.resolve_forklift_driver_ids(
            {"driver-B": {"Trent"}}, allowed_employee_ids={60}
        )
        == {}
    )
