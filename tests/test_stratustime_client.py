"""Tests for the StratusTime client module.

Real network calls are mocked. Tests cover the token+post flow, health
states, and response parsing — not actual auth against the live service.
"""
from unittest.mock import patch

import pytest

from zira_dashboard import stratustime_client as stc


@pytest.fixture
def env_creds(monkeypatch):
    monkeypatch.setenv("STRATUSTIME_SHARED_KEY", "test-key-uuid")
    monkeypatch.setenv("STRATUSTIME_WS_PASSWORD", "test-password")
    monkeypatch.setenv("STRATUSTIME_CUSTOMER_ALIAS", "test-customer")


@pytest.fixture(autouse=True)
def reset_token_cache():
    stc._token_cache = None
    stc._data_cache.clear()
    yield
    stc._token_cache = None
    stc._data_cache.clear()


def test_health_check_unconfigured_when_no_env_vars(monkeypatch):
    for v in ("STRATUSTIME_SHARED_KEY", "STRATUSTIME_WS_PASSWORD", "STRATUSTIME_CUSTOMER_ALIAS"):
        monkeypatch.delenv(v, raising=False)
    result = stc.health_check()
    assert result["configured"] is False
    assert result["ok"] is False
    assert "STRATUSTIME_SHARED_KEY" in result["token_error"]
    assert "STRATUSTIME_WS_PASSWORD" in result["token_error"]
    assert "STRATUSTIME_CUSTOMER_ALIAS" in result["token_error"]


def test_health_check_partial_config_lists_only_missing(monkeypatch):
    monkeypatch.setenv("STRATUSTIME_SHARED_KEY", "k")
    monkeypatch.delenv("STRATUSTIME_WS_PASSWORD", raising=False)
    monkeypatch.delenv("STRATUSTIME_CUSTOMER_ALIAS", raising=False)
    result = stc.health_check()
    assert result["configured"] is False
    assert "STRATUSTIME_SHARED_KEY" not in result["token_error"]
    assert "STRATUSTIME_WS_PASSWORD" in result["token_error"]
    assert "STRATUSTIME_CUSTOMER_ALIAS" in result["token_error"]


def test_health_check_full_success(env_creds):
    # First call (PingTest) returns 200 "true", second (CreateToken) returns a token.
    responses = iter([
        (200, "true"),                 # PingTest
        (200, '"abc.def.token"'),      # CreateToken returns JSON-quoted string
    ])
    with patch.object(stc, "_post", side_effect=lambda *a, **k: next(responses)):
        result = stc.health_check()
    assert result["ok"] is True
    assert result["configured"] is True
    assert result["ping_ok"] is True
    assert result["ping_status"] == 200
    assert result["token_ok"] is True
    assert result["token_error"] == ""


def test_health_check_ping_ok_but_token_fails(env_creds):
    responses = iter([
        (200, "true"),
        (401, '{"ErrorCode":"CreateToken","Message":"Bad credentials"}'),
    ])
    with patch.object(stc, "_post", side_effect=lambda *a, **k: next(responses)):
        result = stc.health_check()
    assert result["ok"] is False
    assert result["ping_ok"] is True
    assert result["token_ok"] is False
    assert "401" in result["token_error"]


def test_health_check_ping_fails(env_creds):
    responses = iter([
        (500, "internal error"),
        (200, '"abc.token"'),
    ])
    with patch.object(stc, "_post", side_effect=lambda *a, **k: next(responses)):
        result = stc.health_check()
    assert result["ok"] is False
    assert result["ping_ok"] is False
    assert result["ping_status"] == 500


def test_create_token_unwraps_json_string(env_creds):
    with patch.object(stc, "_post", return_value=(200, '"my.token.value"')):
        token, err = stc._create_token()
    assert token == "my.token.value"
    assert err == ""


def test_create_token_handles_http_error(env_creds):
    with patch.object(stc, "_post", return_value=(401, "unauthorized")):
        token, err = stc._create_token()
    assert token is None
    assert "401" in err


def test_get_token_caches(env_creds):
    call_count = {"n": 0}

    def fake_post(*a, **k):
        call_count["n"] += 1
        return 200, '"cached.token"'

    with patch.object(stc, "_post", side_effect=fake_post):
        t1, _ = stc.get_token()
        t2, _ = stc.get_token()
    assert t1 == t2 == "cached.token"
    assert call_count["n"] == 1  # second call was served from cache


def test_get_token_force_refresh(env_creds):
    call_count = {"n": 0}

    def fake_post(*a, **k):
        call_count["n"] += 1
        return 200, '"token.v"'

    with patch.object(stc, "_post", side_effect=fake_post):
        stc.get_token()
        stc.get_token(force_refresh=True)
    assert call_count["n"] == 2


def test_authenticated_post_injects_token(env_creds):
    captured = {}

    def fake_post(path, body, **k):
        captured["path"] = path
        captured["body"] = body
        if path == "CreateToken":
            return 200, '"the.token"'
        return 200, '{"Results": [{"id": 1}]}'

    with patch.object(stc, "_post", side_effect=fake_post):
        status, parsed = stc.authenticated_post("GetUserBasic", {"DataAction": {"Name": "SELECT-ALL"}})
    assert status == 200
    assert isinstance(parsed, dict)
    assert captured["path"] == "GetUserBasic"
    assert captured["body"]["AuthToken"] == "the.token"
    assert captured["body"]["DataAction"] == {"Name": "SELECT-ALL"}


def test_list_employees_returns_results_list(env_creds):
    employees = [{"FirstName": "Alice"}, {"FirstName": "Bob"}]

    def fake_post(path, body, **k):
        if path == "CreateToken":
            return 200, '"tok"'
        return 200, '{"Report": {}, "Results": ' + str(employees).replace("'", '"') + '}'

    with patch.object(stc, "_post", side_effect=fake_post):
        result = stc.list_employees()
    assert result == employees


def test_list_employees_returns_empty_on_no_token(env_creds):
    with patch.object(stc, "_post", return_value=(401, "denied")):
        result = stc.list_employees()
    assert result == []


# --- Time-off helpers (sub-project #2) ---

from datetime import date


def _fake_emp_data(empid, first, last):
    return {"EmpIdentifier": empid, "FirstName": first, "LastName": last}


def _fake_request(empid, start_iso, end_iso, status=1, secs=28800, paytype="PTO", include_weekends=False):
    return {
        "ID": 1,
        "EmpIdentifier": empid,
        "StartDateTimeSchema": start_iso + "T07:00:00",
        "EndDateTimeSchema": end_iso + "T15:00:00",
        "StatusType": status,
        "DurationPerDaySecs": secs,
        "PayTypeName": paytype,
        "IncludeWeekends": include_weekends,
    }


def test_fmt_time_short_basic():
    assert stc._fmt_time_short("2026-04-29T09:00:00") == "9a"
    assert stc._fmt_time_short("2026-04-29T13:00:00") == "1p"
    assert stc._fmt_time_short("2026-04-29T12:00:00") == "12p"
    assert stc._fmt_time_short("2026-04-29T00:00:00") == "12a"
    assert stc._fmt_time_short("2026-04-29T09:30:00") == "9:30a"
    assert stc._fmt_time_short("2026-04-29T15:45:00") == "3:45p"
    assert stc._fmt_time_short("garbage") == ""
    assert stc._fmt_time_short("") == ""


def test_fmt_time_range_drops_period_when_same():
    assert stc._fmt_time_range("2026-04-29T09:00:00", "2026-04-29T10:00:00") == "9-10a"
    assert stc._fmt_time_range("2026-04-29T13:00:00", "2026-04-29T14:00:00") == "1-2p"
    assert stc._fmt_time_range("2026-04-29T11:00:00", "2026-04-29T13:00:00") == "11a-1p"
    assert stc._fmt_time_range("2026-04-29T09:30:00", "2026-04-29T10:15:00") == "9:30-10:15a"
    assert stc._fmt_time_range("2026-04-29T12:00:00", "2026-04-29T13:00:00") == "12-1p"
    assert stc._fmt_time_range("", "2026-04-29T10:00:00") == ""


def test_time_off_entries_includes_time_range_for_single_day(env_creds):
    requests_payload = {
        "Report": {},
        "Results": [{
            "ID": 1, "EmpIdentifier": "777", "StatusType": 1,
            "DurationPerDaySecs": 3600, "PayTypeName": "Early Leave",
            "StartDateTimeSchema": "2026-04-29T09:00:00",
            "EndDateTimeSchema": "2026-04-29T10:00:00",
            "IncludeWeekends": False,
        }],
    }
    employees_payload = {"Report": {}, "Results": [_fake_emp_data("777", "Jesus", "Martinez")]}
    import json as _json

    def fake_post(path, body, **k):
        if path == "CreateToken":
            return 200, '"tok"'
        if path == "GetUserTimeOffRequest":
            return 200, _json.dumps(requests_payload)
        if path == "GetUserBasic":
            return 200, _json.dumps(employees_payload)
        return 404, "not found"

    with patch.object(stc, "_post", side_effect=fake_post):
        entries = stc.time_off_entries_for_day(date(2026, 4, 29))
    assert len(entries) == 1
    assert entries[0]["time_range"] == "9-10a"


def test_request_covers_day_simple_range():
    req = _fake_request("1", "2026-05-04", "2026-05-06", include_weekends=True)
    assert stc._request_covers_day(req, date(2026, 5, 4)) is True
    assert stc._request_covers_day(req, date(2026, 5, 5)) is True
    assert stc._request_covers_day(req, date(2026, 5, 6)) is True
    assert stc._request_covers_day(req, date(2026, 5, 3)) is False
    assert stc._request_covers_day(req, date(2026, 5, 7)) is False


def test_request_covers_day_skips_weekends_when_flag_false():
    # Range covers Mon-Sun; flag false should hide Sat (5/9) and Sun (5/10).
    req = _fake_request("1", "2026-05-04", "2026-05-10", include_weekends=False)
    assert stc._request_covers_day(req, date(2026, 5, 8)) is True   # Friday
    assert stc._request_covers_day(req, date(2026, 5, 9)) is False  # Saturday
    assert stc._request_covers_day(req, date(2026, 5, 10)) is False # Sunday


def test_time_off_entries_for_day_filters_by_status(env_creds):
    requests_payload = {
        "Report": {},
        "Results": [
            _fake_request("100", "2026-05-04", "2026-05-04", status=1),
            _fake_request("200", "2026-05-04", "2026-05-04", status=2),  # pending? skipped
        ],
    }
    employees_payload = {
        "Report": {},
        "Results": [
            _fake_emp_data("100", "Alice", "Smith"),
            _fake_emp_data("200", "Bob", "Jones"),
        ],
    }
    import json as _json

    def fake_post(path, body, **k):
        if path == "CreateToken":
            return 200, '"tok"'
        if path == "GetUserTimeOffRequest":
            return 200, _json.dumps(requests_payload)
        if path == "GetUserBasic":
            return 200, _json.dumps(employees_payload)
        return 404, "not found"

    with patch.object(stc, "_post", side_effect=fake_post):
        entries = stc.time_off_entries_for_day(date(2026, 5, 4))
    assert len(entries) == 1
    assert entries[0]["name"] == "Alice Smith"


def test_time_off_entries_for_day_unmapped_emp_id(env_creds):
    requests_payload = {
        "Report": {},
        "Results": [_fake_request("999", "2026-05-04", "2026-05-04")],
    }
    employees_payload = {"Report": {}, "Results": []}
    import json as _json

    def fake_post(path, body, **k):
        if path == "CreateToken":
            return 200, '"tok"'
        if path == "GetUserTimeOffRequest":
            return 200, _json.dumps(requests_payload)
        if path == "GetUserBasic":
            return 200, _json.dumps(employees_payload)
        return 404, "not found"

    with patch.object(stc, "_post", side_effect=fake_post):
        entries = stc.time_off_entries_for_day(date(2026, 5, 4))
    assert len(entries) == 1
    assert "999" in entries[0]["name"]


def test_cache_clear_drops_data_cache(env_creds):
    stc._cache_set(("time_off", "x", "y"), [{"foo": "bar"}])
    assert stc._cache_get(("time_off", "x", "y")) is not None
    stc.cache_clear()
    assert stc._cache_get(("time_off", "x", "y")) is None


def test_cache_clear_resets_per_test(env_creds):
    """Sanity check that the autouse cache reset works."""
    stc._cache_set(("test_key",), "value")
    assert stc._cache_get(("test_key",)) == "value"
