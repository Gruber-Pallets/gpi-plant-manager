from datetime import date

import pytest
from unittest.mock import patch, MagicMock

from zira_dashboard import odoo_client


def test_fetch_employee_celebration_dates_reads_only_available_fields(monkeypatch):
    calls = []

    def fake_execute(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        if (model, method) == ("hr.employee", "fields_get"):
            return {"birthday": {"type": "date"}}
        if (model, method) == ("hr.employee", "search_read"):
            return [{"id": 7, "birthday": "1991-07-04"}]
        raise AssertionError((model, method))

    monkeypatch.setattr(odoo_client, "execute", fake_execute)

    source = odoo_client.fetch_employee_celebration_dates()

    assert source.birthday_available is True
    assert source.first_contract_date_available is False
    assert source.rows_by_employee_id[7]["birthday"] == "1991-07-04"
    assert calls[0][3]["attributes"] == ["type"]
    assert calls[1][3]["fields"] == ["id", "birthday"]


def test_authenticate_raises_when_env_vars_missing(monkeypatch):
    # authenticate() returns _uid_cache before it validates config, so a prior
    # test that authenticated successfully would otherwise mask this check.
    odoo_client._reset_cache_for_tests()
    for k in ("ODOO_URL", "ODOO_DB", "ODOO_LOGIN", "ODOO_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(odoo_client.OdooConfigError):
        odoo_client.authenticate()


def test_sync_rejects_inactive_employee_payload_before_any_write(monkeypatch):
    """The employee lookup explicitly requests active people only.

    An upstream response marking every returned person inactive is therefore
    contradictory and must not be allowed to hide the entire local roster.
    """
    from zira_dashboard import odoo_sync

    monkeypatch.setattr(odoo_sync, "_read_last_sync", lambda: None)
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employees",
        lambda: [{"id": 1, "name": "Unexpectedly Inactive", "active": False}],
    )
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employee_statuses", lambda: [])
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employee_celebration_dates",
        lambda: pytest.fail("unsafe employee payload must stop before celebration-date reads"),
    )
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_skills_for",
        lambda _ids: pytest.fail("unsafe employee payload must stop before any further sync"),
    )

    result = odoo_sync.sync(force=True)

    assert result.ok is False
    assert result.refreshed is False
    assert "malformed" in (result.error or "").lower()


def test_sync_rejects_non_boolean_active_employee_payload_before_any_write(monkeypatch):
    """Only an actual True value is safe for an active-only Odoo query."""
    from zira_dashboard import odoo_sync

    monkeypatch.setattr(odoo_sync, "_read_last_sync", lambda: None)
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employees",
        lambda: [{"id": 1, "name": "Malformed Active", "active": 0}],
    )
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employee_statuses", lambda: [])
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_skills_for",
        lambda _ids: pytest.fail("unsafe employee payload must stop before any further sync"),
    )

    result = odoo_sync.sync(force=True)

    assert result.ok is False
    assert result.refreshed is False
    assert "malformed" in (result.error or "").lower()


def test_sync_records_an_alert_for_a_non_boolean_active_value(monkeypatch):
    """An unsafe roster is visible to managers without mutating people."""
    from zira_dashboard import app_settings, odoo_sync

    saved = []
    monkeypatch.setattr(odoo_sync, "_read_last_sync", lambda: None)
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employees",
        lambda: [{"id": 1, "name": "Malformed Active", "active": 0}],
    )
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employee_statuses", lambda: [])
    monkeypatch.setattr(
        app_settings,
        "set_setting",
        lambda key, value: saved.append((key, value)),
    )

    result = odoo_sync.sync(force=True)

    assert result.ok is False
    assert saved[0][0] == odoo_sync.ROSTER_SYNC_ALERT_KEY
    assert saved[0][1]["invalid_count"] == 1
    assert "malformed" in saved[0][1]["error"]


@pytest.mark.parametrize(
    ("active_rows", "status_rows", "message"),
    [
        ([], [], "empty"),
        (
            [{"id": 1, "name": "A", "active": True}],
            [{"id": 1, "active": 1}],
            "malformed",
        ),
        (
            [{"id": 1, "name": "A", "active": True}],
            [{"id": 1, "active": True}, {"id": 1, "active": True}],
            "duplicate",
        ),
        (
            [{"id": 1, "name": "A", "active": True}],
            [{"id": 1, "active": False}],
            "contradict",
        ),
    ],
)
def test_sync_rejects_unsafe_employee_snapshots_before_writes(
    monkeypatch, active_rows, status_rows, message
):
    from zira_dashboard import app_settings, db, odoo_sync

    saved = []
    monkeypatch.setattr(odoo_sync, "_read_last_sync", lambda: None)
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employees", lambda: active_rows)
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employee_statuses", lambda: status_rows)
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_skills_for",
        lambda _ids: pytest.fail("unsafe snapshot reached dependent reads"),
    )
    monkeypatch.setattr(db, "cursor", lambda: pytest.fail("unsafe snapshot reached writes"))
    monkeypatch.setattr(
        app_settings,
        "set_setting",
        lambda key, value: saved.append((key, value)),
    )

    result = odoo_sync.sync(force=True)

    assert result.ok is False
    assert result.refreshed is False
    assert message in (result.error or "").lower()
    assert saved[0][0] == odoo_sync.ROSTER_SYNC_ALERT_KEY
    assert saved[0][1]["error"] == result.error
    assert all(key != "odoo_last_sync" for key, _value in saved)


def test_successful_fresh_sync_clears_roster_alert(monkeypatch):
    """A verified fresh roster lets the Inbox clear the prior warning."""
    from contextlib import contextmanager

    from zira_dashboard import app_settings, db, odoo_sync

    saved = []

    @contextmanager
    def fake_cursor():
        yield MagicMock()

    monkeypatch.setattr(odoo_sync, "_read_last_sync", lambda: None)
    monkeypatch.setattr(odoo_sync, "_write_last_sync", lambda _now: None)
    monkeypatch.setattr(odoo_sync, "_claim_celebration_source_generation", lambda: 1)
    monkeypatch.setattr(odoo_sync, "refresh_work_schedule_hours", lambda: None)
    monkeypatch.setattr(db, "cursor", fake_cursor)
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employees",
        lambda: [{"id": 1, "name": "Healthy Person", "active": True}],
    )
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employee_statuses",
        lambda: [{"id": 1, "active": True}],
    )
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skills_for", lambda _ids: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_spanish_skill_level_ids", lambda: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_columns_with_types", lambda: [])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_level_buckets", lambda: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_departments", lambda: [])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_work_schedules", lambda: [])
    monkeypatch.setattr(
        app_settings,
        "set_setting",
        lambda key, value: saved.append((key, value)),
    )

    result = odoo_sync.sync(force=True)

    assert result.ok is True
    assert saved == [(odoo_sync.ROSTER_SYNC_ALERT_KEY, None)]


def test_authenticate_returns_uid_on_success(monkeypatch):
    monkeypatch.setenv("ODOO_URL", "https://example.odoo.com")
    monkeypatch.setenv("ODOO_DB", "Production")
    monkeypatch.setenv("ODOO_LOGIN", "dale@example.com")
    monkeypatch.setenv("ODOO_API_KEY", "secret-key")
    fake_common = MagicMock()
    fake_common.authenticate.return_value = 42
    with patch("xmlrpc.client.ServerProxy", return_value=fake_common) as proxy:
        odoo_client._reset_cache_for_tests()
        uid = odoo_client.authenticate()
    assert uid == 42
    args, kwargs = proxy.call_args
    assert args[0] == "https://example.odoo.com/xmlrpc/2/common"
    # https URL → SafeTransport subclass carrying the socket timeout.
    assert isinstance(kwargs["transport"], odoo_client._TimeoutSafeTransport)
    fake_common.authenticate.assert_called_with("Production", "dale@example.com", "secret-key", {})


def test_authenticate_raises_on_failure(monkeypatch):
    monkeypatch.setenv("ODOO_URL", "https://example.odoo.com")
    monkeypatch.setenv("ODOO_DB", "Production")
    monkeypatch.setenv("ODOO_LOGIN", "dale@example.com")
    monkeypatch.setenv("ODOO_API_KEY", "wrong")
    fake_common = MagicMock()
    fake_common.authenticate.return_value = False
    with patch("xmlrpc.client.ServerProxy", return_value=fake_common):
        odoo_client._reset_cache_for_tests()
        with pytest.raises(odoo_client.OdooAuthError):
            odoo_client.authenticate()


def test_timeout_transports_set_socket_timeout():
    """A hung TCP connection must not block a warmer thread forever — both
    transports stamp the 15s socket timeout onto the connection they make.
    (make_connection only constructs the http.client connection; no network.)"""
    conn = odoo_client._TimeoutTransport().make_connection("example.invalid")
    assert conn.timeout == odoo_client._XMLRPC_TIMEOUT_SECONDS
    conn = odoo_client._TimeoutSafeTransport().make_connection("example.invalid")
    assert conn.timeout == odoo_client._XMLRPC_TIMEOUT_SECONDS


def test_server_proxy_picks_transport_for_scheme(monkeypatch):
    """https → SafeTransport subclass, http → Transport subclass; both carry
    the timeout. Mismatching transport to scheme breaks the TLS handshake."""
    captured = {}

    def fake_proxy(url, transport=None):
        captured[url] = transport
        return MagicMock()

    monkeypatch.setattr("xmlrpc.client.ServerProxy", fake_proxy)
    odoo_client._server_proxy("https://x/xmlrpc/2/object")
    odoo_client._server_proxy("http://x/xmlrpc/2/object")
    assert isinstance(captured["https://x/xmlrpc/2/object"],
                      odoo_client._TimeoutSafeTransport)
    assert isinstance(captured["http://x/xmlrpc/2/object"],
                      odoo_client._TimeoutTransport)
    assert not isinstance(captured["http://x/xmlrpc/2/object"],
                          odoo_client._TimeoutSafeTransport)


def _stub_execute(monkeypatch, responses):
    """Map (model, method) → return value. Calls not in the map raise."""
    calls = []
    def fake(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        key = (model, method)
        if key not in responses:
            raise AssertionError(f"unexpected call: {key}")
        return responses[key]
    monkeypatch.setattr(odoo_client, "execute", fake)
    return calls


def test_fetch_skill_columns_returns_production_then_supervisor(monkeypatch):
    responses = {
        ("hr.skill.type", "search_read"): [
            {"id": 1, "name": "Production Skills"},
            {"id": 2, "name": "Supervisor Skills"},
        ],
        ("hr.skill", "search_read"): [
            {"id": 10, "name": "Repair", "skill_type_id": [1, "Production Skills"]},
            {"id": 11, "name": "Dismantler", "skill_type_id": [1, "Production Skills"]},
            {"id": 12, "name": "Floor Lead", "skill_type_id": [2, "Supervisor Skills"]},
        ],
    }
    _stub_execute(monkeypatch, responses)
    cols = odoo_client.fetch_skill_columns()
    # Production skills first (alphabetical), then Supervisor (alphabetical)
    assert cols == ["Dismantler", "Repair", "Floor Lead"]


def test_fetch_skill_columns_with_types_includes_odoo_id(monkeypatch):
    responses = {
        ("hr.skill.type", "search_read"): [
            {"id": 1, "name": "Production Skills"},
            {"id": 2, "name": "Supervisor Skills"},
        ],
        ("hr.skill", "search_read"): [
            {"id": 10, "name": "Repair", "skill_type_id": [1, "Production Skills"]},
            {"id": 11, "name": "Dismantle", "skill_type_id": [1, "Production Skills"]},
            {"id": 20, "name": "Lead", "skill_type_id": [2, "Supervisor Skills"]},
        ],
    }
    _stub_execute(monkeypatch, responses)

    assert odoo_client.fetch_skill_columns_with_types() == [
        {"id": 11, "name": "Dismantle", "type": "Production Skills"},
        {"id": 10, "name": "Repair", "type": "Production Skills"},
        {"id": 20, "name": "Lead", "type": "Supervisor Skills"},
    ]


def test_fetch_skill_level_buckets_rank_maps_4_levels(monkeypatch):
    responses = {
        ("hr.skill.level", "search_read"): [
            {"id": 100, "level_progress": 0,   "skill_type_id": [1, "Production"]},
            {"id": 101, "level_progress": 33,  "skill_type_id": [1, "Production"]},
            {"id": 102, "level_progress": 67,  "skill_type_id": [1, "Production"]},
            {"id": 103, "level_progress": 100, "skill_type_id": [1, "Production"]},
        ],
    }
    _stub_execute(monkeypatch, responses)
    buckets = odoo_client.fetch_skill_level_buckets()
    assert buckets == {100: 0, 101: 1, 102: 2, 103: 3}


def test_fetch_skill_level_buckets_rank_maps_3_levels(monkeypatch):
    responses = {
        ("hr.skill.level", "search_read"): [
            {"id": 200, "level_progress": 0,   "skill_type_id": [2, "Supervisor"]},
            {"id": 201, "level_progress": 50,  "skill_type_id": [2, "Supervisor"]},
            {"id": 202, "level_progress": 100, "skill_type_id": [2, "Supervisor"]},
        ],
    }
    _stub_execute(monkeypatch, responses)
    buckets = odoo_client.fetch_skill_level_buckets()
    # 3 levels -> rank 0,1,2 -> 0, round(1*3/2)=2, round(2*3/2)=3
    assert buckets == {200: 0, 201: 2, 202: 3}


def test_set_employee_skill_level_creates_missing_skill_row(monkeypatch):
    calls = []

    def fake_execute(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        if (model, method) == ("hr.skill", "read"):
            return [{"id": 10, "skill_type_id": [1, "Production Skills"]}]
        if (model, method) == ("hr.skill.level", "search_read"):
            return [
                {"id": 100, "level_progress": 0, "skill_type_id": [1, "Production Skills"]},
                {"id": 101, "level_progress": 33, "skill_type_id": [1, "Production Skills"]},
                {"id": 102, "level_progress": 67, "skill_type_id": [1, "Production Skills"]},
                {"id": 103, "level_progress": 100, "skill_type_id": [1, "Production Skills"]},
            ]
        if (model, method) == ("hr.employee.skill", "search"):
            return []
        if (model, method) == ("hr.employee.skill", "create"):
            return 555
        raise AssertionError(f"unexpected call: {(model, method)}")

    monkeypatch.setattr(odoo_client, "execute", fake_execute)

    odoo_client.set_employee_skill_level(7, 10, 3)

    create_calls = [c for c in calls if c[0:2] == ("hr.employee.skill", "create")]
    assert create_calls == [
        (
            "hr.employee.skill",
            "create",
            ({
                "employee_id": 7,
                "skill_id": 10,
                "skill_type_id": 1,
                "skill_level_id": 103,
            },),
            {},
        )
    ]


def test_set_employee_skill_level_deduplicates_rows_after_create_race(monkeypatch):
    calls = []
    searches = iter([[], [54, 555]])

    def fake_execute(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        if (model, method) == ("hr.skill", "read"):
            return [{"id": 10, "skill_type_id": [1, "Production Skills"]}]
        if (model, method) == ("hr.skill.level", "search_read"):
            return [
                {"id": 100, "level_progress": 0, "skill_type_id": [1, "Production Skills"]},
                {"id": 101, "level_progress": 33, "skill_type_id": [1, "Production Skills"]},
                {"id": 102, "level_progress": 67, "skill_type_id": [1, "Production Skills"]},
                {"id": 103, "level_progress": 100, "skill_type_id": [1, "Production Skills"]},
            ]
        if (model, method) == ("hr.employee.skill", "search"):
            return next(searches)
        if (model, method) == ("hr.employee.skill", "create"):
            return 555
        if (model, method) == ("hr.employee.skill", "write"):
            return True
        if (model, method) == ("hr.employee.skill", "unlink"):
            return True
        raise AssertionError(f"unexpected call: {(model, method)}")

    monkeypatch.setattr(odoo_client, "execute", fake_execute)

    odoo_client.set_employee_skill_level(7, 10, 3)

    assert (
        "hr.employee.skill",
        "write",
        ([555], {"skill_level_id": 103}),
        {},
    ) in calls
    assert (
        "hr.employee.skill",
        "unlink",
        ([54],),
        {},
    ) in calls


def test_set_employee_skill_level_updates_first_duplicate_and_unlinks_rest(monkeypatch):
    calls = []

    def fake_execute(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        if (model, method) == ("hr.skill", "read"):
            return [{"id": 10, "skill_type_id": [1, "Production Skills"]}]
        if (model, method) == ("hr.skill.level", "search_read"):
            return [
                {"id": 100, "level_progress": 0, "skill_type_id": [1, "Production Skills"]},
                {"id": 101, "level_progress": 33, "skill_type_id": [1, "Production Skills"]},
                {"id": 102, "level_progress": 67, "skill_type_id": [1, "Production Skills"]},
                {"id": 103, "level_progress": 100, "skill_type_id": [1, "Production Skills"]},
            ]
        if (model, method) == ("hr.employee.skill", "search"):
            return [55, 56]
        if (model, method) == ("hr.employee.skill", "write"):
            return True
        if (model, method) == ("hr.employee.skill", "unlink"):
            return True
        raise AssertionError(f"unexpected call: {(model, method)}")

    monkeypatch.setattr(odoo_client, "execute", fake_execute)

    odoo_client.set_employee_skill_level(7, 10, 2)

    assert (
        "hr.employee.skill",
        "write",
        ([55], {"skill_level_id": 102}),
        {},
    ) in calls
    assert (
        "hr.employee.skill",
        "unlink",
        ([56],),
        {},
    ) in calls


def test_set_employee_skill_level_zero_unlinks_all_existing_rows(monkeypatch):
    calls = []

    def fake_execute(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        if (model, method) == ("hr.employee.skill", "search"):
            return [55, 56]
        if (model, method) == ("hr.employee.skill", "unlink"):
            return True
        raise AssertionError(f"unexpected call: {(model, method)}")

    monkeypatch.setattr(odoo_client, "execute", fake_execute)

    odoo_client.set_employee_skill_level(7, 10, 0)

    assert calls == [
        (
            "hr.employee.skill",
            "search",
            ([("employee_id", "=", 7), ("skill_id", "=", 10)],),
            {},
        ),
        ("hr.employee.skill", "unlink", ([55, 56],), {}),
    ]


def test_set_employee_skill_level_rejects_invalid_bucket():
    with pytest.raises(ValueError, match="bucket must be 0, 1, 2, or 3"):
        odoo_client.set_employee_skill_level(7, 10, 4)


def test_fetch_employees_returns_active_only_with_required_fields(monkeypatch):
    responses = {
        ("hr.employee", "search_read"): [
            {"id": 1, "name": "Alice", "active": True, "work_email": "alice@x"},
            {"id": 2, "name": "Bob",   "active": True, "work_email": False},
        ],
    }
    calls = _stub_execute(monkeypatch, responses)
    out = odoo_client.fetch_employees()
    assert out == [
        {"id": 1, "name": "Alice", "active": True, "work_email": "alice@x"},
        {"id": 2, "name": "Bob",   "active": True, "work_email": False},
    ]
    # Search must filter to active only
    args = calls[0][2]
    assert ("active", "=", True) in args[0]


def test_fetch_employee_statuses_includes_archived_records(monkeypatch):
    responses = {
        ("hr.employee", "search_read"): [
            {"id": 1, "active": True},
            {"id": 2, "active": False},
        ],
    }
    calls = _stub_execute(monkeypatch, responses)

    assert odoo_client.fetch_employee_statuses() == responses[("hr.employee", "search_read")]
    _model, _method, args, kwargs = calls[0]
    assert args[0] == []
    assert kwargs["fields"] == ["id", "active"]
    assert kwargs["context"] == {"active_test": False}


def test_fetch_skills_for_groups_by_employee_id(monkeypatch):
    responses = {
        ("hr.employee.skill", "search_read"): [
            {"id": 5, "employee_id": [1, "Alice"], "skill_id": [10, "Repair"],     "skill_level_id": [103, "Expert"]},
            {"id": 6, "employee_id": [1, "Alice"], "skill_id": [11, "Dismantler"], "skill_level_id": [101, "Beginner"]},
            {"id": 7, "employee_id": [2, "Bob"],   "skill_id": [10, "Repair"],     "skill_level_id": [102, "Adv"]},
        ],
    }
    _stub_execute(monkeypatch, responses)
    out = odoo_client.fetch_skills_for([1, 2])
    assert out == {
        1: [
            {"skill_id": 10, "skill_name": "Repair",     "level_id": 103},
            {"skill_id": 11, "skill_name": "Dismantler", "level_id": 101},
        ],
        2: [
            {"skill_id": 10, "skill_name": "Repair", "level_id": 102},
        ],
    }


def test_object_proxy_is_thread_local(monkeypatch):
    """Each thread must get its OWN xmlrpc ServerProxy for the object endpoint.

    xmlrpc.client.ServerProxy keeps a single persistent http.client connection
    and is NOT thread-safe — two threads sharing one proxy interleave on the
    same connection and corrupt its state machine, which surfaces as
    CannotSendRequest('Request-sent') / ResponseNotReady('Idle'). The background
    warmers (asyncio.to_thread) and request handlers call execute() concurrently,
    so a shared module-level proxy is the bug behind the 'Request-sent' / 'Idle'
    errors in the logs.
    """
    import threading

    monkeypatch.setenv("ODOO_URL", "https://example.odoo.com")
    monkeypatch.setenv("ODOO_DB", "Production")
    monkeypatch.setenv("ODOO_LOGIN", "dale@example.com")
    monkeypatch.setenv("ODOO_API_KEY", "secret-key")

    def make_proxy(url, transport=None):
        m = MagicMock(name=url)
        m.authenticate.return_value = 7   # /common auth
        m.execute_kw.return_value = []    # /object call
        return m

    monkeypatch.setattr("xmlrpc.client.ServerProxy", make_proxy)
    odoo_client._reset_cache_for_tests()

    seen: dict[str, object] = {}

    def worker(name):
        odoo_client.execute("hr.employee", "search_read", [])
        seen[name] = odoo_client._object_proxy_for_thread()

    for name in ("A", "B"):
        t = threading.Thread(target=worker, args=(name,))
        t.start()
        t.join()

    assert seen["A"] is not seen["B"]


def test_reset_leave_to_confirm_writes_state(monkeypatch):
    # This Odoo has no usable reset action (action_draft removed,
    # action_reset_confirm broken upstream) — the reset is a state write.
    calls = _stub_execute(monkeypatch, {("hr.leave", "write"): True})
    odoo_client.reset_leave_to_confirm(112)
    assert calls == [("hr.leave", "write", ([112], {"state": "confirm"}), {})]


def test_fetch_leave_state_returns_state_or_none(monkeypatch):
    _stub_execute(monkeypatch, {
        ("hr.leave", "search_read"): [{"id": 112, "state": "refuse"}],
    })
    assert odoo_client.fetch_leave_state(112) == "refuse"

    _stub_execute(monkeypatch, {("hr.leave", "search_read"): []})
    assert odoo_client.fetch_leave_state(999) is None


def test_fetch_leave_snapshot_normalizes_verified_identity(monkeypatch):
    execute = MagicMock(
        return_value=[
            {
                "id": 91,
                "employee_id": [44, "Ana"],
                "holiday_status_id": [7, "Paid Time Off"],
                "request_date_from": "2026-08-20",
                "request_date_to": "2026-08-20",
                "state": "validate",
            }
        ]
    )
    monkeypatch.setattr(odoo_client, "execute", execute)

    assert odoo_client.fetch_leave_snapshot(91) == {
        "id": 91,
        "employee_id": 44,
        "holiday_status_id": 7,
        "date_from": date(2026, 8, 20),
        "date_to": date(2026, 8, 20),
        "state": "validate",
    }
    execute.assert_called_once_with(
        "hr.leave",
        "search_read",
        [("id", "=", 91)],
        fields=[
            "id",
            "employee_id",
            "holiday_status_id",
            "request_date_from",
            "request_date_to",
            "state",
        ],
        limit=2,
        context={"active_test": False},
    )


def test_fetch_leave_snapshot_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr(odoo_client, "execute", MagicMock(return_value=[]))

    assert odoo_client.fetch_leave_snapshot(91) is None


@pytest.mark.parametrize(
    "row",
    [
        {
            "id": True,
            "employee_id": [44, "Ana"],
            "holiday_status_id": [7, "Paid Time Off"],
            "request_date_from": "2026-08-20",
            "request_date_to": "2026-08-20",
            "state": "validate",
        },
        {
            "id": 92,
            "employee_id": [44, "Ana"],
            "holiday_status_id": [7, "Paid Time Off"],
            "request_date_from": "2026-08-20",
            "request_date_to": "2026-08-20",
            "state": "validate",
        },
        {
            "id": 91,
            "employee_id": [44, "Ana"],
            "holiday_status_id": [7, "Paid Time Off"],
            "request_date_from": "not-a-date",
            "request_date_to": "2026-08-20",
            "state": "validate",
        },
        {
            "id": 91,
            "employee_id": [44, "Ana"],
            "holiday_status_id": [7, "Paid Time Off"],
            "request_date_from": "2026-08-20",
            "request_date_to": "2026-08-20",
            "state": "",
        },
    ],
)
def test_fetch_leave_snapshot_rejects_malformed_or_mismatched_rows(
    monkeypatch, row
):
    monkeypatch.setattr(
        odoo_client, "execute", MagicMock(return_value=[row])
    )

    with pytest.raises(RuntimeError, match="leave payload"):
        odoo_client.fetch_leave_snapshot(91)


def test_find_matching_leaves_is_exact_bounded_and_terminal_inclusive(
    monkeypatch,
):
    rows = [
        {
            "id": 91,
            "employee_id": [44, "Ana"],
            "holiday_status_id": [7, "Paid Time Off"],
            "request_date_from": "2026-08-20",
            "request_date_to": "2026-08-20",
            "state": "validate",
        },
        {
            "id": 92,
            "employee_id": [44, "Ana"],
            "holiday_status_id": [7, "Paid Time Off"],
            "request_date_from": "2026-08-20",
            "request_date_to": "2026-08-20",
            "state": "refuse",
        },
    ]
    execute = MagicMock(return_value=rows)
    monkeypatch.setattr(odoo_client, "execute", execute)

    assert odoo_client.find_matching_leaves(44, 7, date(2026, 8, 20)) == [
        {
            "id": 91,
            "employee_id": 44,
            "holiday_status_id": 7,
            "date_from": date(2026, 8, 20),
            "date_to": date(2026, 8, 20),
            "state": "validate",
        },
        {
            "id": 92,
            "employee_id": 44,
            "holiday_status_id": 7,
            "date_from": date(2026, 8, 20),
            "date_to": date(2026, 8, 20),
            "state": "refuse",
        },
    ]
    execute.assert_called_once_with(
        "hr.leave",
        "search_read",
        [
            ("employee_id", "=", 44),
            ("holiday_status_id", "=", 7),
            ("request_date_from", "=", "2026-08-20"),
            ("request_date_to", "=", "2026-08-20"),
        ],
        fields=[
            "id",
            "employee_id",
            "holiday_status_id",
            "request_date_from",
            "request_date_to",
            "state",
        ],
        order="id asc",
        limit=2,
        context={"active_test": False},
    )


def test_find_matching_leaves_excludes_terminal_states_when_requested(
    monkeypatch,
):
    execute = MagicMock(return_value=[])
    monkeypatch.setattr(odoo_client, "execute", execute)

    assert odoo_client.find_matching_leaves(
        44, 7, date(2026, 8, 20), include_terminal=False
    ) == []
    execute.assert_called_once_with(
        "hr.leave",
        "search_read",
        [
            ("employee_id", "=", 44),
            ("holiday_status_id", "=", 7),
            ("request_date_from", "=", "2026-08-20"),
            ("request_date_to", "=", "2026-08-20"),
            ("state", "not in", ["cancel", "refuse"]),
        ],
        fields=[
            "id",
            "employee_id",
            "holiday_status_id",
            "request_date_from",
            "request_date_to",
            "state",
        ],
        order="id asc",
        limit=2,
    )


def test_find_matching_leaves_rejects_mismatched_echo(monkeypatch):
    execute = MagicMock(
        return_value=[
            {
                "id": 91,
                "employee_id": [45, "Wrong employee"],
                "holiday_status_id": [7, "Paid Time Off"],
                "request_date_from": "2026-08-20",
                "request_date_to": "2026-08-20",
                "state": "validate",
            }
        ]
    )
    monkeypatch.setattr(odoo_client, "execute", execute)

    with pytest.raises(RuntimeError, match="leave payload"):
        odoo_client.find_matching_leaves(44, 7, date(2026, 8, 20))
