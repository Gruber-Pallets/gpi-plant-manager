"""Happy-path write probes against the sandbox data source."""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
from pathlib import Path

import requests

from zira_probe.client import ZiraClient
from zira_probe.config import Config
from zira_probe.results import ProbeResult, redact_api_key, write_raw_log


# All happy-path writes use timestamps in a clearly-labeled future range so
# they're easy to spot in the Zira UI and ignore for real analytics.
WRITE_MARKER_BASE = datetime(2099, 1, 1, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _safe_post(client: ZiraClient, payload):
    try:
        return client.add_readings(payload), None
    except requests.HTTPError as exc:
        r = exc.response
        return None, {
            "type": "HTTPError",
            "status_code": r.status_code if r is not None else None,
            "body": r.text if r is not None else str(exc),
        }
    except Exception as exc:
        return None, {"type": type(exc).__name__, "message": str(exc)}


def _run_write(
    client: ZiraClient,
    config: Config,
    results_dir: Path,
    name: str,
    payload: list[dict],
) -> ProbeResult:
    request_summary = {
        "method": "POST",
        "url": f"{client.base_url}reading/ids/",
        "headers": {"X-API-Key": redact_api_key(client.api_key), "Content-Type": "application/json"},
        "body": payload,
    }
    data, err = _safe_post(client, payload)
    if err is not None:
        response_summary = {"error": err}
        status = "unexpected_failure"
        obs = [f"Write errored: {err['type']} — {err.get('status_code')}"]
    else:
        response_summary = {"status_code": 200, "body_excerpt": data}
        status = "success"
        obs = [f"Write returned: {data!r}"]
    log_path = write_raw_log(results_dir, name, request_summary, response_summary)
    return ProbeResult(
        name=name,
        category="writes_happy",
        endpoint="POST /reading/ids/",
        status=status,
        request_summary=request_summary,
        response_summary=response_summary,
        observations=obs,
        raw_log_path=str(log_path),
    )


def probe_write_single_number(
    client: ZiraClient, config: Config, results_dir: Path
) -> list[ProbeResult]:
    ts = _iso(WRITE_MARKER_BASE + timedelta(minutes=1))
    payload = [
        {
            "meterId": config.test_ds_meter_id,
            "timestamp": ts,
            "values": [{"metricId": config.test_ds_number_metric, "value": 42}],
        }
    ]
    return [_run_write(client, config, results_dir, "write_single_number", payload)]


def probe_write_single_text(
    client: ZiraClient, config: Config, results_dir: Path
) -> list[ProbeResult]:
    ts = _iso(WRITE_MARKER_BASE + timedelta(minutes=2))
    payload = [
        {
            "meterId": config.test_ds_meter_id,
            "timestamp": ts,
            "values": [{"metricId": config.test_ds_text_metric, "value": "probe_marker"}],
        }
    ]
    return [_run_write(client, config, results_dir, "write_single_text", payload)]


def probe_write_both_metrics(
    client: ZiraClient, config: Config, results_dir: Path
) -> list[ProbeResult]:
    ts = _iso(WRITE_MARKER_BASE + timedelta(minutes=3))
    payload = [
        {
            "meterId": config.test_ds_meter_id,
            "timestamp": ts,
            "values": [
                {"metricId": config.test_ds_number_metric, "value": 7},
                {"metricId": config.test_ds_text_metric, "value": "probe_both"},
            ],
        }
    ]
    return [_run_write(client, config, results_dir, "write_both_metrics", payload)]


def probe_write_batch_5(
    client: ZiraClient, config: Config, results_dir: Path
) -> list[ProbeResult]:
    payload = [
        {
            "meterId": config.test_ds_meter_id,
            "timestamp": _iso(WRITE_MARKER_BASE + timedelta(hours=i + 1)),
            "values": [
                {"metricId": config.test_ds_number_metric, "value": i},
                {"metricId": config.test_ds_text_metric, "value": f"batch_{i}"},
            ],
        }
        for i in range(5)
    ]
    return [_run_write(client, config, results_dir, "write_batch_5", payload)]


ALL_WRITE_HAPPY_PROBES = [
    probe_write_single_number,
    probe_write_single_text,
    probe_write_both_metrics,
    probe_write_batch_5,
]
