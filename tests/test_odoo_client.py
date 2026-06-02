import pytest
from unittest.mock import patch, MagicMock

from zira_dashboard import odoo_client


def test_authenticate_raises_when_env_vars_missing(monkeypatch):
    for k in ("ODOO_URL", "ODOO_DB", "ODOO_LOGIN", "ODOO_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(odoo_client.OdooConfigError):
        odoo_client.authenticate()


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
    proxy.assert_called_with("https://example.odoo.com/xmlrpc/2/common")
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

    def make_proxy(url):
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
