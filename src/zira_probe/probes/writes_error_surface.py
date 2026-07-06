"""Deliberate-error write probes.

Each probe sends a malformed or boundary-testing payload and records exactly
how Zira responds. A 4xx response is this probe category's "success" — we
got information about validation behavior.
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
from pathlib import Path

import requests

from zira_probe.client import ZiraClient
from zira_probe.config import Config
from zira_probe.results import ProbeResult, redact_api_key, write_raw_log


WRITE_MARKER_BASE = datetime(2099, 6, 1, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _run(
    client: ZiraClient,
    config: Config,
    results_dir: Path,
    name: str,
    payload,
    expectation: str,
) -> ProbeResult:
    """Send `payload` via client.request (so 4xx doesn't raise); record response."""
    request_summary = {
        "method": "POST",
        "url": f"{client.base_url}reading/ids/",
        "headers": {"X-API-Key": redact_api_key(client.api_key), "Content-Type": "application/json"},
        "body": payload,
        "expectation": expectation,
    }
    try:
        resp = client.request("POST", "reading/ids/", json_body=payload)
        response_summary = {
            "status_code": resp.status_code,
            "body_excerpt": (resp.text[:500] + "...") if len(resp.text) > 500 else resp.text,
        }
        if 400 <= resp.status_code < 500:
            status = "success"  # rejected as expected
            obs = [f"Rejected with {resp.status_code} — as expected."]
        elif 200 <= resp.status_code < 300:
            status = "expected_failure"
            obs = ["Unexpected 2xx — payload was accepted."]
        else:
            status = "unexpected_failure"
            obs = [f"Unexpected status {resp.status_code}."]
    except requests.RequestException as exc:
        response_summary = {"exception": type(exc).__name__, "message": str(exc)}
        status = "unexpected_failure"
        obs = [f"Network/transport failure: {exc}"]

    log_path = write_raw_log(results_dir, name, request_summary, response_summary)
    return ProbeResult(
        name=name,
        category="writes_error_surface",
        endpoint="POST /reading/ids/",
        status=status,
        request_summary=request_summary,
        response_summary=response_summary,
        observations=obs,
        raw_log_path=str(log_path),
    )


def probe_write_bad_meter_id(client, config, results_dir):
    payload = [
        {
            "meterId": "this-meter-id-does-not-exist-0000",
            "timestamp": _iso(WRITE_MARKER_BASE),
            "values": [{"metricId": config.test_ds_number_metric, "value": 1}],
        }
    ]
    return [_run(client, config, results_dir, "write_bad_meter_id", payload,
                 "Zira rejects unknown meterId.")]


def probe_write_bad_metric_id(client, config, results_dir):
    payload = [
        {
            "meterId": config.test_ds_meter_id,
            "timestamp": _iso(WRITE_MARKER_BASE + timedelta(minutes=1)),
            "values": [{"metricId": "999999999", "value": 1}],
        }
    ]
    return [_run(client, config, results_dir, "write_bad_metric_id", payload,
                 "Zira rejects unknown metricId (or silently drops it).")]


def probe_write_wrong_value_type(client, config, results_dir):
    payload = [
        {
            "meterId": config.test_ds_meter_id,
            "timestamp": _iso(WRITE_MARKER_BASE + timedelta(minutes=2)),
            "values": [
                {"metricId": config.test_ds_number_metric, "value": "not_a_number"},
                {"metricId": config.test_ds_text_metric, "value": 12345},
            ],
        }
    ]
    return [_run(client, config, results_dir, "write_wrong_value_type", payload,
                 "Zira rejects or coerces mismatched value types.")]


def probe_write_missing_meter_id(client, config, results_dir):
    payload = [
        {
            "timestamp": _iso(WRITE_MARKER_BASE + timedelta(minutes=3)),
            "values": [{"metricId": config.test_ds_number_metric, "value": 1}],
        }
    ]
    return [_run(client, config, results_dir, "write_missing_meter_id", payload,
                 "Zira rejects payload lacking meterId.")]


def probe_write_missing_timestamp(client, config, results_dir):
    payload = [
        {
            "meterId": config.test_ds_meter_id,
            "values": [{"metricId": config.test_ds_number_metric, "value": 1}],
        }
    ]
    return [_run(client, config, results_dir, "write_missing_timestamp", payload,
                 "Zira rejects payload lacking timestamp.")]


def probe_write_duplicate_timestamp(client, config, results_dir):
    ts = _iso(WRITE_MARKER_BASE + timedelta(minutes=4))
    payload = [
        {
            "meterId": config.test_ds_meter_id,
            "timestamp": ts,
            "values": [{"metricId": config.test_ds_number_metric, "value": 1}],
        },
        {
            "meterId": config.test_ds_meter_id,
            "timestamp": ts,
            "values": [{"metricId": config.test_ds_number_metric, "value": 2}],
        },
    ]
    return [_run(client, config, results_dir, "write_duplicate_timestamp", payload,
                 "Observe dedup/overwrite/accept-both behavior for identical timestamps.")]


def probe_write_future_timestamp(client, config, results_dir):
    future = datetime(2199, 1, 1, tzinfo=UTC)
    payload = [
        {
            "meterId": config.test_ds_meter_id,
            "timestamp": _iso(future),
            "values": [{"metricId": config.test_ds_number_metric, "value": 1}],
        }
    ]
    return [_run(client, config, results_dir, "write_future_timestamp", payload,
                 "Observe whether Zira gates very-distant future timestamps.")]


def probe_write_large_batch(client, config, results_dir):
    payload = [
        {
            "meterId": config.test_ds_meter_id,
            "timestamp": _iso(WRITE_MARKER_BASE + timedelta(seconds=i)),
            "values": [{"metricId": config.test_ds_number_metric, "value": i}],
        }
        for i in range(500)
    ]
    return [_run(client, config, results_dir, "write_large_batch_500", payload,
                 "Observe batch-size limits.")]


def probe_write_empty_body(client, config, results_dir):
    return [_run(client, config, results_dir, "write_empty_body", [],
                 "Observe empty-array behavior.")]


ALL_WRITE_ERROR_PROBES = [
    probe_write_bad_meter_id,
    probe_write_bad_metric_id,
    probe_write_wrong_value_type,
    probe_write_missing_meter_id,
    probe_write_missing_timestamp,
    probe_write_duplicate_timestamp,
    probe_write_future_timestamp,
    probe_write_large_batch,
    probe_write_empty_body,
]
