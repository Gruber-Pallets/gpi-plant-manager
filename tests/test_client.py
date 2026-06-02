import json

import pytest
import responses

from zira_probe.client import ZiraClient


@pytest.fixture
def client():
    return ZiraClient(api_key="test-key-1234", base_url="https://api.zira.us/public/")


@responses.activate
def test_get_readings_sends_api_key_header_and_params(client):
    responses.add(
        method=responses.GET,
        url="https://api.zira.us/public/reading",
        json=[{"id": "r1"}, {"id": "r2"}],
        status=200,
    )

    result = client.get_readings(meter_id="999", end_time="2026-04-24T00:00:00Z")

    assert result == [{"id": "r1"}, {"id": "r2"}]
    call = responses.calls[0]
    assert call.request.headers["X-API-Key"] == "test-key-1234"
    assert "meterId=999" in call.request.url
    assert "endTime=2026-04-24T00%3A00%3A00Z" in call.request.url


@responses.activate
def test_get_readings_passes_optional_params(client):
    responses.add(
        method=responses.GET,
        url="https://api.zira.us/public/reading",
        json=[],
        status=200,
    )

    client.get_readings(
        meter_id="999",
        end_time="2026-04-24T00:00:00Z",
        start_time="2026-04-17T00:00:00Z",
        limit=50,
        last_value="abc",
    )

    url = responses.calls[0].request.url
    assert "startTime=2026-04-17T00%3A00%3A00Z" in url
    assert "limit=50" in url
    assert "lastValue=abc" in url


@responses.activate
def test_get_channel_analysis_builds_url_and_params(client):
    responses.add(
        method=responses.GET,
        url="https://api.zira.us/public/channels/42301/analysis",
        json={"points": []},
        status=200,
    )

    result = client.get_channel_analysis(
        channel_id="42301",
        interval="1 days",
        from_time="2026-04-01T00:00:00Z",
        to_time="2026-04-10T00:00:00Z",
    )

    assert result == {"points": []}
    url = responses.calls[0].request.url
    assert "interval=1+days" in url or "interval=1%20days" in url
    assert "fromTime=2026-04-01T00%3A00%3A00Z" in url
    assert "toTime=2026-04-10T00%3A00%3A00Z" in url


@responses.activate
def test_add_readings_posts_json_payload(client):
    responses.add(
        method=responses.POST,
        url="https://api.zira.us/public/reading/ids/",
        json={"ok": True},
        status=200,
    )

    payload = [
        {
            "meterId": "3978",
            "timestamp": "2026-04-24T12:00:00Z",
            "values": [{"metricId": "6", "value": 0}],
        }
    ]
    result = client.add_readings(payload)

    assert result == {"ok": True}
    sent = responses.calls[0].request
    assert sent.headers["X-API-Key"] == "test-key-1234"
    assert sent.headers["Content-Type"] == "application/json"
    assert json.loads(sent.body) == payload


@responses.activate
def test_request_returns_response_object(client):
    responses.add(
        method=responses.GET,
        url="https://api.zira.us/public/data-sources",
        json={"items": []},
        status=200,
    )

    resp = client.request("GET", "data-sources")

    assert resp.status_code == 200
    assert resp.json() == {"items": []}


@responses.activate
def test_request_does_not_raise_on_4xx(client):
    responses.add(
        method=responses.GET,
        url="https://api.zira.us/public/does-not-exist",
        json={"error": "not found"},
        status=404,
    )

    resp = client.request("GET", "does-not-exist")

    assert resp.status_code == 404


@responses.activate
def test_request_passes_through_json_body(client):
    responses.add(
        method=responses.POST,
        url="https://api.zira.us/public/whatever",
        json={"ok": True},
        status=200,
    )

    client.request("POST", "whatever", json_body={"hello": "world"})

    import json as _json
    sent = responses.calls[0].request
    assert _json.loads(sent.body) == {"hello": "world"}


def test_session_is_thread_local(client):
    """Each thread gets its OWN requests.Session.

    requests.Session is not safe for concurrent use, and leaderboard() fans
    get_readings() across a ThreadPoolExecutor — a single shared session lets
    threads interleave on one pooled connection and corrupt it. Per-thread
    sessions avoid that while keeping the X-API-Key header on every one.
    """
    import threading

    seen: dict[str, object] = {}

    def grab(name):
        seen[name] = client.session

    for name in ("A", "B"):
        t = threading.Thread(target=grab, args=(name,))
        t.start()
        t.join()

    assert seen["A"] is not seen["B"]
    assert seen["A"].headers["X-API-Key"] == "test-key-1234"
    assert seen["B"].headers["X-API-Key"] == "test-key-1234"
