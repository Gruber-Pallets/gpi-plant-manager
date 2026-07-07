"""Render a list of ProbeResults as CAPABILITY_REPORT.md."""

from __future__ import annotations

import json
from datetime import datetime, UTC

from zira_probe.results import ProbeResult


_GLYPH = {
    "success": "✅",
    "expected_failure": "⚠️",
    "unexpected_failure": "❌",
    "skipped": "⏭️",
}

_CATEGORY_SECTIONS = [
    ("reads", "## Reads"),
    ("writes_happy", "## Writes — Happy Path"),
    ("writes_error_surface", "## Writes — Error Surface"),
    ("undocumented", "## Undocumented Endpoints"),
]


def _format_result(r: ProbeResult) -> str:
    glyph = _GLYPH.get(r.status, "?")
    parts = [f"### {glyph} `{r.name}`", f"**Endpoint:** `{r.endpoint}`"]
    if r.observations:
        parts.append("**Observations:**")
        parts.extend(f"- {o}" for o in r.observations)
    if r.request_summary:
        parts.append(
            "<details><summary>Request</summary>\n\n```json\n"
            + json.dumps(r.request_summary, indent=2, default=str)
            + "\n```\n\n</details>"
        )
    if r.response_summary:
        parts.append(
            "<details><summary>Response</summary>\n\n```json\n"
            + json.dumps(r.response_summary, indent=2, default=str)
            + "\n```\n\n</details>"
        )
    if r.raw_log_path:
        parts.append(f"_Raw log: `{r.raw_log_path}`_")
    return "\n\n".join(parts)


def render_report(results: list[ProbeResult]) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Zira API Capability Report",
        "",
        f"Generated: {now}",
        "",
        "Glyphs: ✅ success · ⚠️ expected failure (informative) · ❌ unexpected failure · ⏭️ skipped",
        "",
    ]

    by_cat: dict[str, list[ProbeResult]] = {cat: [] for cat, _ in _CATEGORY_SECTIONS}
    for r in results:
        by_cat.setdefault(r.category, []).append(r)

    for cat, heading in _CATEGORY_SECTIONS:
        lines.append("")
        lines.append(heading)
        lines.append("")
        if not by_cat.get(cat):
            lines.append("_No probes in this category ran._")
            continue
        for r in by_cat[cat]:
            lines.append(_format_result(r))
            lines.append("")

    # Open Questions: surface anything labeled INTERESTING
    interesting = [
        r
        for r in results
        if any("INTERESTING" in o for o in r.observations)
    ]
    if interesting:
        lines.append("")
        lines.append("## Open Questions")
        lines.append("")
        lines.append("The following probes returned surprising results and warrant investigation:")
        lines.append("")
        for r in interesting:
            obs = "; ".join(r.observations)
            lines.append(f"- **{r.name}** (`{r.endpoint}`): {obs}")

    return "\n".join(lines)
