"""Static regression tests for the operator dashboard downtime bar."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "src/zira_dashboard/static/wc_dashboard.css").read_text(encoding="utf-8")


def _rule_body(selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{(?P<body>[^}]*)\}", CSS, re.DOTALL)
    assert match is not None, f"missing CSS rule: {selector}"
    return match.group("body")


def test_operator_downtime_label_can_overflow_left_into_green():
    selector = (
        '.wc-dashboard .grid-stack-item[gs-id="downtime-row"] '
        ".stacked-track .bad"
    )

    body = _rule_body(selector)

    assert re.search(r"\boverflow:\s*visible\s*;", body)
