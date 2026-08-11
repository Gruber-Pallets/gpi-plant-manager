from datetime import datetime, timezone

from zira_dashboard import odoo_client


def test_attendance_facade_resolves_dependencies_at_call_time(monkeypatch):
    assert hasattr(odoo_client, "_odoo_attendance"), (
        "attendance operations have not been extracted"
    )

    calls = []
    execute_fn = lambda *args, **kwargs: None
    monkeypatch.setattr(odoo_client, "execute", execute_fn)
    monkeypatch.setattr(odoo_client, "_kiosk_wc_field", lambda: "x_current_wc")
    monkeypatch.setattr(
        odoo_client,
        "_kiosk_department_field",
        lambda: "x_current_department",
    )
    monkeypatch.setattr(
        odoo_client._odoo_attendance,
        "fetch_open_attendances",
        lambda *args: calls.append(args) or [],
    )

    assert odoo_client.fetch_open_attendances() == []
    assert calls == [
        (
            execute_fn,
            "x_current_wc",
            "x_current_department",
            odoo_client._app_wc_name_for_odoo_id,
        )
    ]


def test_facade_uses_execute_replaced_after_import(monkeypatch):
    calls = []

    def fake(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        if model == "hr.skill.type":
            return [{"id": 1, "name": "Production Skills"}]
        if model == "hr.skill":
            return [{"id": 11, "name": "Planer", "skill_type_id": [1, "Production Skills"]}]
        raise AssertionError(model)

    monkeypatch.setattr(odoo_client, "execute", fake)
    assert odoo_client.fetch_skill_columns_with_types() == [
        {"id": 11, "name": "Planer", "type": "Production Skills"}
    ]
    assert [call[0:2] for call in calls] == [
        ("hr.skill.type", "search_read"),
        ("hr.skill", "search_read"),
    ]


def test_facade_uses_unwrap_m2o_replaced_after_import(monkeypatch):
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda model, method, *args, **kwargs: (
            [{"id": 1, "name": "Production Skills"}]
            if model == "hr.skill.type"
            else [{"id": 11, "name": "Planer", "skill_type_id": "patched-type"}]
        ),
    )
    monkeypatch.setattr(
        odoo_client,
        "unwrap_m2o",
        lambda value: 1 if value == "patched-type" else value,
    )

    assert odoo_client.fetch_skill_columns_with_types() == [
        {"id": 11, "name": "Planer", "type": "Production Skills"}
    ]


def test_facade_schedule_type_field_is_resolved_at_call_time(monkeypatch):
    field_name = "x_schedule_type"
    calls = []

    def fake(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        return [{"id": 7, "name": "Flexible", field_name: "flexible"}]

    monkeypatch.setattr(odoo_client, "SCHEDULE_TYPE_FIELD", field_name)
    monkeypatch.setattr(odoo_client, "execute", fake)

    assert odoo_client.fetch_work_schedules() == [
        {"id": 7, "name": "Flexible", "is_flexible": True}
    ]
    assert calls == [
        (
            "resource.calendar",
            "search_read",
            ([("active", "=", True)],),
            {"fields": ["id", "name", field_name]},
        )
    ]


def test_facade_leave_cache_remains_assignable(monkeypatch):
    expected = [{"id": 7, "name": "Vacation", "request_unit": "day"}]
    monkeypatch.setattr(odoo_client, "_leave_types_cache", (expected, float("inf")))
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cache miss")),
    )
    assert odoo_client.fetch_leave_types() is expected


def test_invalidate_leave_types_cache_clears_facade_cache(monkeypatch):
    monkeypatch.setattr(
        odoo_client,
        "_leave_types_cache",
        ([{"id": 1}], float("inf")),
    )

    odoo_client.invalidate_leave_types_cache()

    assert odoo_client._leave_types_cache is None


def test_time_off_facade_resolves_dependencies_at_call_time(monkeypatch):
    assert hasattr(odoo_client, "_odoo_time_off"), (
        "time-off operations have not been extracted"
    )

    calls = []
    execute_fn = lambda *args, **kwargs: None
    unwrap_m2o_fn = lambda value: value
    types = [{"id": 7, "request_unit": "day"}]
    expected = {3: [{"holiday_status_id": 7}]}
    monkeypatch.setattr(odoo_client, "execute", execute_fn)
    monkeypatch.setattr(odoo_client, "unwrap_m2o", unwrap_m2o_fn)
    monkeypatch.setattr(odoo_client, "fetch_leave_types", lambda: types)
    monkeypatch.setattr(
        odoo_client._odoo_time_off,
        "fetch_balances_for_many",
        lambda *args: calls.append(args) or expected,
    )

    assert odoo_client.fetch_balances_for_many([3]) is expected
    assert calls == [
        (
            execute_fn,
            unwrap_m2o_fn,
            types,
            [3],
            odoo_client._aggregate_balances,
        )
    ]


def test_time_off_normalizer_is_resolved_at_call_time(monkeypatch):
    monkeypatch.setattr(odoo_client, "_leave_types_cache", None)
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda *args, **kwargs: [
            {
                "id": 7,
                "name": "Vacation",
                "request_unit": "day",
                "requires_allocation": True,
                "color": 1,
                "active": True,
            }
        ],
    )
    monkeypatch.setattr(
        odoo_client,
        "_norm_requires_allocation",
        lambda value: f"patched:{value!r}",
    )

    assert odoo_client.fetch_leave_types()[0]["requires_allocation"] == (
        "patched:True"
    )


def test_time_off_datetime_converter_is_resolved_at_call_time(monkeypatch):
    calls = []
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda model, method, *args, **kwargs: calls.append(
            (model, method, args, kwargs)
        )
        or [],
    )
    monkeypatch.setattr(odoo_client, "_to_odoo_dt", lambda value: "patched")

    odoo_client.fetch_leaves_for_range(
        datetime(2026, 7, 1).date(),
        datetime(2026, 7, 31).date(),
        datetime(2026, 7, 9, tzinfo=timezone.utc),
    )

    assert ("write_date", ">", "patched") in calls[0][2][0]


def test_facade_lunch_cache_remains_assignable(monkeypatch):
    calendar_id = 987_654
    expected = {calendar_id: {"0": ["11:00", "11:30"]}}
    monkeypatch.setattr(
        odoo_client,
        "_calendar_lunch_windows_cache",
        {(calendar_id,): (expected, float("inf"))},
        raising=False,
    )
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cache miss")),
    )

    assert odoo_client.fetch_calendar_lunch_windows([calendar_id]) is expected


def test_facade_lunch_cache_miss_uses_current_execute_and_ttl(monkeypatch):
    calendar_id = 987_655
    calls = []

    def fake(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        return [
            {
                "calendar_id": [calendar_id, "Plant"],
                "dayofweek": "0",
                "day_period": "lunch",
                "hour_from": 11.0,
                "hour_to": 11.5,
            }
        ]

    facade_cache = {}
    monkeypatch.setattr(
        odoo_client,
        "_calendar_lunch_windows_cache",
        facade_cache,
        raising=False,
    )
    monkeypatch.setattr(
        odoo_client, "_CALENDAR_LUNCH_TTL_SECONDS", 0, raising=False
    )
    monkeypatch.setattr(odoo_client, "execute", fake)

    expected = {calendar_id: {"0": ["11:00", "11:30"]}}
    assert odoo_client.fetch_calendar_lunch_windows([calendar_id, calendar_id]) == expected
    assert odoo_client.fetch_calendar_lunch_windows([calendar_id]) == expected

    assert len(calls) == 2
    assert calls[0] == (
        "resource.calendar.attendance",
        "search_read",
        ([("calendar_id", "in", [calendar_id])],),
        {
            "fields": [
                "calendar_id",
                "dayofweek",
                "hour_from",
                "hour_to",
                "day_period",
            ]
        },
    )
    assert facade_cache[(calendar_id,)][0] == expected


def test_facade_department_helper_is_resolved_at_call_time(monkeypatch):
    calls = []
    monkeypatch.setenv("ODOO_KIOSK_DEPARTMENT_FIELD", "x_kiosk_department_id")
    monkeypatch.setattr(odoo_client, "_department_id_for_wc", lambda wc: 44)
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda model, method, *args, **kwargs: calls.append(
            (model, method, args, kwargs)
        )
        or 91,
    )
    attendance_id = odoo_client.clock_in(3, "Repair 2", odoo_client.datetime.now(odoo_client.UTC))
    assert attendance_id == 91
    assert calls[-1][0:2] == ("hr.attendance", "create")
    assert calls[-1][2][0]["x_kiosk_department_id"] == 44
