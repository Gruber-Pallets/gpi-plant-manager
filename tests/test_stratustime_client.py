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
    yield
    stc._token_cache = None


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
