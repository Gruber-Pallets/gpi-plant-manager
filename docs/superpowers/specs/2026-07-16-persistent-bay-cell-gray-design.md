# Persistent Bay Cell Gray Design

## Goal

Keep every bay-label cell in the on-screen staffing schedule on the existing
darker gray surface at all times.

## Current behavior

`table.sched td.bay` uses `--panel-3`, but the later active-work-center rule
applies `--accent-dim` to every cell in the row. Because the bay cell belongs
to the first row for its bay, an enabled first work center makes that bay label
green. The inactive-row rule can also override the bay background.

## Design

Add a narrowly scoped override in `src/zira_dashboard/static/staffing.css`
after the active and inactive work-center row rules. It will target only bay
cells participating in those states and restore `background: var(--panel-3)`.
It will use selector specificity sufficient to override both existing row
background rules without affecting other table cells.

## Scope

- Applies only to the interactive on-screen staffing schedule.
- Applies for both enabled and disabled first work-center states.
- Retains the existing `--panel-3` theme token, so light and dark themes keep
  their established darker-gray palette.
- Does not change the print/Slack PDF stylesheet, templates, JavaScript, or
  scheduling data.

## Verification

Add a static regression test that checks the persistent bay override is placed
after the row-state rules and targets the bay cell with `--panel-3`.
