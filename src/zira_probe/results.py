"""ProbeResult dataclass and raw-log writer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path


@dataclass
class ProbeResult:
    """Outcome of a single probe. See spec for semantics of `status`."""

    name: str
    category: str  # "reads" | "writes_happy" | "writes_error_surface" | "undocumented"
    endpoint: str
    status: str  # "success" | "expected_failure" | "unexpected_failure" | "skipped"
    request_summary: dict = field(default_factory=dict)
    response_summary: dict = field(default_factory=dict)
    observations: list[str] = field(default_factory=list)
    raw_log_path: str = ""


def redact_api_key(key: str) -> str:
    if len(key) < 4:
        return "****"
    return "****" + key[-4:]


def write_raw_log(
    results_dir: Path,
    name: str,
    request: dict,
    response: dict,
) -> Path:
    """Write a timestamped raw request/response log; return its path."""

    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = results_dir / f"{name}_{stamp}.json"
    payload = {
        "name": name,
        "timestamp": stamp,
        "request": request,
        "response": response,
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path
