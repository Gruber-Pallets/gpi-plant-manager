"""Read probes against documented endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
from pathlib import Path
import json

import requests

from zira_probe.client import ZiraClient
from zira_probe.config import Config
from zira_probe.results import ProbeResult, redact_api_key, write_raw_log


def _iso_utc(dt: datetime) -> str:
    """Zira-friendly ISO 8601 with trailing Z, millisecond precision."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _safe_call(fn, *args, **kwargs):
    """Run `fn`; return (response_or_none, error_dict_or_none)."""
    try:
        return fn(*args, **kwargs), None
    except requests.HTTPError as exc:
        resp = exc.response
        return None, {
            "type": "HTTPError",
            "status_code": resp.status_code if resp is not None else None,
            "body": resp.text if resp is not None else str(exc),
        }
    except Exception as exc:
        return None, {"type": type(exc).__name__, "message": str(exc)}


def probe_read_ds_single_window(
    client: ZiraClient, config: Config, results_dir: Path
) -> list[ProbeResult]:
    out: list[ProbeResult] = []
    now = datetime.now(UTC)
    end_time = _iso_utc(now)
    start_time = _iso_utc(now - timedelta(days=config.read_window_days))

    for ds in config.read_data_sources:
        name = f"read_ds_single_window[{ds.label}]"
        request_summary = {
            "method": "GET",
            "url": f"{client.base_url}reading",
            "params": {
                "meterId": ds.meter_id,
                "startTime": start_time,
                "endTime": end_time,
            },
            "headers": {"X-API-Key": redact_api_key(client.api_key)},
        }

        data, err = _safe_call(
            client.get_readings,
            meter_id=ds.meter_id,
            end_time=end_time,
            start_time=start_time,
        )

        if err is not None:
            response_summary = {"error": err}
            status = "unexpected_failure"
            observations = [f"Request failed: {err['type']}"]
        else:
            count = len(data) if isinstance(data, list) else None
            response_summary = {
                "status_code": 200,
                "kind": type(data).__name__,
                "count": count,
                "body_excerpt": data[:3] if isinstance(data, list) else data,
            }
            status = "success" if count and count > 0 else "expected_failure"
            observations = [
                f"Returned {count} readings over {config.read_window_days} days"
                if count is not None
                else f"Unexpected response type: {type(data).__name__}"
            ]

        log_path = write_raw_log(
            results_dir, name, request_summary, response_summary
        )
        out.append(
            ProbeResult(
                name=name,
                category="reads",
                endpoint="GET /reading",
                status=status,
                request_summary=request_summary,
                response_summary=response_summary,
                observations=observations,
                raw_log_path=str(log_path),
            )
        )
    return out


def probe_read_ds_paginated(
    client: ZiraClient, config: Config, results_dir: Path
) -> list[ProbeResult]:
    out: list[ProbeResult] = []
    now = datetime.now(UTC)
    end_time = _iso_utc(now)
    start_time = _iso_utc(now - timedelta(days=config.read_window_days))

    for ds in config.read_data_sources:
        name = f"read_ds_paginated[{ds.label}]"
        observations: list[str] = []

        page1, err1 = _safe_call(
            client.get_readings,
            meter_id=ds.meter_id,
            end_time=end_time,
            start_time=start_time,
            limit=5,
        )
        if err1 is not None or not isinstance(page1, list) or not page1:
            response_summary = {"page1": err1 or {"body": page1}}
            out.append(
                ProbeResult(
                    name=name,
                    category="reads",
                    endpoint="GET /reading (paginated)",
                    status="skipped",
                    request_summary={"limit": 5, "meter_id": ds.meter_id},
                    response_summary=response_summary,
                    observations=["No data returned for DS in window; pagination untestable."],
                    raw_log_path=str(
                        write_raw_log(results_dir, name, {}, response_summary)
                    ),
                )
            )
            continue

        last_key = None
        for item in page1:
            if isinstance(item, dict):
                for k in ("id", "key", "lastValue", "uuid"):
                    if k in item:
                        last_key = item[k]
                        break
            if last_key:
                break

        if last_key is None:
            observations.append(
                "Could not locate a last-value key in page 1; pagination unverified."
            )
            status = "expected_failure"
            page2 = None
            err2 = None
        else:
            page2, err2 = _safe_call(
                client.get_readings,
                meter_id=ds.meter_id,
                end_time=end_time,
                start_time=start_time,
                limit=5,
                last_value=str(last_key),
            )
            if err2 is not None:
                status = "unexpected_failure"
                observations.append(f"Page 2 errored: {err2['type']}")
            else:
                def _fingerprint(item):
                    try:
                        return json.dumps(item, sort_keys=True, default=str)
                    except Exception:
                        return repr(item)
                page1_ids = {_fingerprint(x) for x in page1}
                overlap = False
                if isinstance(page2, list):
                    for x in page2:
                        if _fingerprint(x) in page1_ids:
                            overlap = True
                            break
                status = "success"
                observations.append(
                    f"Page 1 returned {len(page1)}; page 2 returned "
                    f"{len(page2) if isinstance(page2, list) else 'n/a'}; "
                    f"overlap detected: {overlap}"
                )

        response_summary = {
            "page1_count": len(page1),
            "last_key_guess": last_key,
            "page2": err2 or ({"count": len(page2)} if isinstance(page2, list) else page2),
        }
        log_path = write_raw_log(results_dir, name, {"meter_id": ds.meter_id}, response_summary)
        out.append(
            ProbeResult(
                name=name,
                category="reads",
                endpoint="GET /reading (paginated)",
                status=status,
                request_summary={"meter_id": ds.meter_id, "limit": 5},
                response_summary=response_summary,
                observations=observations,
                raw_log_path=str(log_path),
            )
        )
    return out


def probe_read_ds_empty_window(
    client: ZiraClient, config: Config, results_dir: Path
) -> list[ProbeResult]:
    out: list[ProbeResult] = []
    far_past_end = _iso_utc(datetime(2000, 1, 1, 0, 1, tzinfo=UTC))
    far_past_start = _iso_utc(datetime(2000, 1, 1, 0, 0, tzinfo=UTC))

    for ds in config.read_data_sources:
        name = f"read_ds_empty_window[{ds.label}]"
        data, err = _safe_call(
            client.get_readings,
            meter_id=ds.meter_id,
            end_time=far_past_end,
            start_time=far_past_start,
        )
        if err is not None:
            response_summary = {"error": err}
            status = "unexpected_failure"
            obs = [f"Empty window errored: {err['type']}"]
        else:
            response_summary = {"kind": type(data).__name__, "body": data}
            status = "success" if isinstance(data, list) and not data else "expected_failure"
            obs = [f"Empty-window response: {type(data).__name__}={data}"]
        log_path = write_raw_log(
            results_dir, name, {"meter_id": ds.meter_id}, response_summary
        )
        out.append(
            ProbeResult(
                name=name,
                category="reads",
                endpoint="GET /reading (empty window)",
                status=status,
                request_summary={"meter_id": ds.meter_id, "window": "1 minute in year 2000"},
                response_summary=response_summary,
                observations=obs,
                raw_log_path=str(log_path),
            )
        )
    return out


def probe_read_channel_analysis_intervals(
    client: ZiraClient, config: Config, results_dir: Path
) -> list[ProbeResult]:
    out: list[ProbeResult] = []
    now = datetime.now(UTC)
    to_time = _iso_utc(now)
    from_time = _iso_utc(now - timedelta(days=config.read_window_days))
    intervals = ["5 minutes", "1 hours", "1 days", "1 weeks"]

    for ch in config.read_channels:
        for interval in intervals:
            name = f"read_channel_analysis[{ch.label}][{interval}]"
            data, err = _safe_call(
                client.get_channel_analysis,
                channel_id=ch.channel_id,
                interval=interval,
                from_time=from_time,
                to_time=to_time,
            )
            if err is not None:
                response_summary = {"error": err}
                status = "unexpected_failure"
                obs = [f"Errored at interval={interval}: {err['type']}"]
            else:
                response_summary = {"kind": type(data).__name__, "body_excerpt": _truncate(data)}
                status = "success"
                obs = [f"Returned data of type {type(data).__name__}"]
            log_path = write_raw_log(
                results_dir, name, {"channel_id": ch.channel_id, "interval": interval}, response_summary
            )
            out.append(
                ProbeResult(
                    name=name,
                    category="reads",
                    endpoint="GET /channels/{id}/analysis",
                    status=status,
                    request_summary={"channel_id": ch.channel_id, "interval": interval},
                    response_summary=response_summary,
                    observations=obs,
                    raw_log_path=str(log_path),
                )
            )
    return out


def probe_read_channel_analysis_too_many_points(
    client: ZiraClient, config: Config, results_dir: Path
) -> list[ProbeResult]:
    out: list[ProbeResult] = []
    now = datetime.now(UTC)
    to_time = _iso_utc(now)
    from_time = _iso_utc(now - timedelta(days=90))

    for ch in config.read_channels:
        name = f"read_channel_analysis_too_many[{ch.label}]"
        data, err = _safe_call(
            client.get_channel_analysis,
            channel_id=ch.channel_id,
            interval="5 minutes",
            from_time=from_time,
            to_time=to_time,
        )
        if err is not None:
            response_summary = {"error": err}
            body = err.get("body") or ""
            matched = "02-001" in str(body)
            status = "success" if matched else "expected_failure"
            obs = [
                f"Got error {err['type']}; contained '02-001': {matched}",
            ]
        else:
            response_summary = {"kind": type(data).__name__, "body_excerpt": _truncate(data)}
            status = "expected_failure"
            obs = ["Expected error 02-001 but request succeeded."]

        log_path = write_raw_log(
            results_dir, name, {"channel_id": ch.channel_id, "interval": "5 minutes", "window": "90 days"}, response_summary
        )
        out.append(
            ProbeResult(
                name=name,
                category="reads",
                endpoint="GET /channels/{id}/analysis (too-many-points)",
                status=status,
                request_summary={"channel_id": ch.channel_id},
                response_summary=response_summary,
                observations=obs,
                raw_log_path=str(log_path),
            )
        )
    return out


def _truncate(data, max_len: int = 500):
    s = repr(data)
    if len(s) <= max_len:
        return data
    return s[:max_len] + "...[truncated]"


ALL_READ_PROBES = [
    probe_read_ds_single_window,
    probe_read_ds_paginated,
    probe_read_ds_empty_window,
    probe_read_channel_analysis_intervals,
    probe_read_channel_analysis_too_many_points,
]
