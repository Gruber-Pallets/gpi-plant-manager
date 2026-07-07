"""Entry point: loads config, runs every probe, writes CAPABILITY_REPORT.md."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from zira_probe.client import ZiraClient
from zira_probe.config import load_config
from zira_probe.probes.reads import ALL_READ_PROBES
from zira_probe.probes.undocumented import ALL_UNDOCUMENTED_PROBES
from zira_probe.probes.writes_error_surface import ALL_WRITE_ERROR_PROBES
from zira_probe.probes.writes_happy import ALL_WRITE_HAPPY_PROBES
from zira_probe.report import render_report
from zira_probe.results import ProbeResult
from datetime import UTC


def _auth_preflight(client: ZiraClient, meter_id: str) -> bool:
    """Hit one cheap read to confirm the API key works."""
    from datetime import datetime

    end = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        client.get_readings(meter_id=meter_id, end_time=end, limit=1)
        return True
    except Exception as exc:
        print(f"[preflight] auth check failed: {exc}", file=sys.stderr)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zira API capability probe.")
    parser.add_argument("--env", default=".env", help="Path to .env file (default: .env)")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml (default: config.yaml)")
    parser.add_argument("--results-dir", default="results", help="Where to write raw per-probe logs")
    parser.add_argument("--report", default="CAPABILITY_REPORT.md", help="Report output path")
    parser.add_argument("--auth-only", action="store_true", help="Run preflight only, then exit")
    parser.add_argument("--skip-writes", action="store_true", help="Skip both write categories")
    args = parser.parse_args(argv)

    config = load_config(env_path=args.env, yaml_path=args.config)
    client = ZiraClient(api_key=config.api_key, base_url=config.base_url)

    preflight_target = (
        config.read_data_sources[0].meter_id
        if config.read_data_sources
        else config.test_ds_meter_id
    )
    ok = _auth_preflight(client, preflight_target)
    if not ok:
        return 2
    print("[preflight] auth OK", file=sys.stderr)
    if args.auth_only:
        return 0

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    report_path = Path(args.report)
    if report_path.parent != Path(""):
        report_path.parent.mkdir(parents=True, exist_ok=True)

    probe_groups: list[tuple[str, list]] = [("reads", ALL_READ_PROBES)]
    if not args.skip_writes:
        probe_groups.append(("writes_happy", ALL_WRITE_HAPPY_PROBES))
        probe_groups.append(("writes_error_surface", ALL_WRITE_ERROR_PROBES))
    probe_groups.append(("undocumented", ALL_UNDOCUMENTED_PROBES))

    all_results: list[ProbeResult] = []
    for group_name, probes in probe_groups:
        for probe_fn in probes:
            probe_name = probe_fn.__name__
            print(f"[{group_name}] running {probe_name}...", file=sys.stderr)
            try:
                res = probe_fn(client, config, results_dir)
                all_results.extend(res)
            except Exception as exc:
                print(f"[{group_name}] {probe_name} exploded: {exc}", file=sys.stderr)
                all_results.append(
                    ProbeResult(
                        name=probe_name,
                        category=group_name,
                        endpoint="(runner-level failure)",
                        status="unexpected_failure",
                        observations=[f"Probe function raised: {type(exc).__name__}: {exc}"],
                    )
                )

    md = render_report(all_results)
    Path(args.report).write_text(md, encoding="utf-8")
    print(f"[done] wrote {args.report} with {len(all_results)} probe results", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
