# Operator Downtime Label Overflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every downtime-minute character visible when the operator dashboard's red downtime segment is narrower than its label.

**Architecture:** Preserve the existing Jinja markup, percentage math, and flex alignment. Lock the intended operator-only CSS behavior with a static regression test, then allow only the red segment's contents to overflow leftward across the adjacent green segment.

**Tech Stack:** CSS, Python 3.11+, pytest, Jinja/FastAPI operator dashboard, browser visual QA

## Global Constraints

- Affect only the operator dashboard's `downtime-row` widget in editor and TV views.
- Keep the downtime value right-aligned within the red segment.
- When the value is wider than the red segment, allow its left side to paint over green so every digit and the `m` suffix remain visible.
- Keep the label inside the overall bar with its current typography.
- Do not change templates, percentages, routes, downtime calculations, or the green uptime label's clipping behavior.
- Add no dependencies.

---

### Task 1: Permit the operator downtime label to cross the segment boundary

**Files:**
- Create: `tests/test_operator_downtime_static.py`
- Modify: `src/zira_dashboard/static/wc_dashboard.css:495-500`
- Verify: `src/zira_dashboard/templates/wc_dashboard.html:207-223`

**Interfaces:**
- Consumes: the existing `.wc-dashboard .grid-stack-item[gs-id="downtime-row"] .stacked-track .bad` CSS selector and `.down-label` markup.
- Produces: an operator-only red segment whose label can paint beyond the segment's left edge while retaining `justify-content: flex-end`.

- [x] **Step 1: Write the failing CSS regression test**

Create `tests/test_operator_downtime_static.py` with:

```python
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
```

- [x] **Step 2: Run the new test and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_operator_downtime_static.py -q
```

Expected: one failure at the final assertion because the matched rule currently contains `overflow: hidden` rather than `overflow: visible`.

- [x] **Step 3: Make the minimal operator-only CSS change**

In `src/zira_dashboard/static/wc_dashboard.css`, keep all existing declarations in the red-segment rule and change only its overflow declaration:

```css
.wc-dashboard .grid-stack-item[gs-id="downtime-row"] .stacked-track .bad {
  display: flex;
  align-items: center;
  justify-content: flex-end;   /* value stays on the RIGHT edge of red */
  overflow: visible;          /* wide values may extend left over green */
}
```

- [x] **Step 4: Run the regression test and verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_operator_downtime_static.py -q
```

Expected: `1 passed`.

- [x] **Step 5: Run the related operator-dashboard checks**

Run:

```bash
.venv/bin/pytest tests/test_operator_downtime_static.py tests/test_operator_dashboard_day_links.py tests/test_wc_dashboard.py -q
```

Expected: all runnable tests pass; tests in `test_wc_dashboard.py` may be explicitly skipped when `DATABASE_URL` is unavailable because that module declares a database skip marker.

Run:

```bash
.venv/bin/ruff check tests/test_operator_downtime_static.py
```

Expected: `All checks passed!`.

- [x] **Step 6: Verify the narrow segment visually in the browser**

Start the local FastAPI app with the repository's existing environment and open an operator dashboard. Use browser developer tools to set a representative narrow red segment (for example, `.bad { width: 12%; }`) and a multi-character label (for example, `123m`). Verify all of the following in both editor and TV routes:

- `123m` is fully visible.
- Its right edge remains inset from the overall bar's right edge by the existing label padding.
- Its excess left width paints over green.
- The bar remains inside the widget and the green uptime label is unchanged.

- [x] **Step 7: Review and commit the focused change**

Run:

```bash
git diff --check
git diff -- tests/test_operator_downtime_static.py src/zira_dashboard/static/wc_dashboard.css docs/superpowers/plans/2026-07-10-operator-downtime-label-overflow.md
git status --short
```

Confirm no unrelated files are staged. Then run:

```bash
git add tests/test_operator_downtime_static.py src/zira_dashboard/static/wc_dashboard.css docs/superpowers/plans/2026-07-10-operator-downtime-label-overflow.md
git commit -m "fix: keep operator downtime minutes visible"
```
