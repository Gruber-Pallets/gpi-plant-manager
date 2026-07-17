# Finish Stalled Plan Integrations — Design

**Date:** 2026-07-17
**Status:** Approved for specification review

## Goal

Finish the two identified unfinished plan deliveries without changing unrelated
behavior: integrate the per-day Auto work-center implementation and port only
the Recycling dashboard scaling implementation from its mixed historical branch.

## Scope

### Per-day Auto work centers

Reconcile `codex/default-auto-work-centers-by-day` with current `main`. The
target behavior is the existing implementation-plan contract: Settings stores a
default template, each schedule owns its enabled-center list, new schedules copy
the default once, and later Settings changes do not rewrite a saved day.

The integration must preserve all behavior already present on `main`; conflicts
are resolved in favor of the newer `main` scheduler lifecycle and its persisted
schedule state. The branch's focused persistence, Settings, staffing, and
Saturday-recruiting tests remain the acceptance evidence.

### Recycling dashboard scaling

Port only these plan-owned artifacts from `fix/recycling-dashboard-scaling`:

- `src/zira_dashboard/static/recycling.css`
- `src/zira_dashboard/static/dashboard-grid.js`
- `src/zira_dashboard/static/wc_dashboard.css`
- `src/zira_dashboard/templates/recycling.html`
- `scripts/preview_recycling.py`
- `tests/test_recycling_scaling_static.py`
- the preview-output ignore rule in `.gitignore`

Do not port the branch's Trim Saw rotation, People Matrix skills, Odoo,
auto-lunch, time-off, Slack, template, or test-debt changes. The scaling result
continues to use the current Gridstack/container-query layout and adds only the
proportional sizing rules, the matching operator-dashboard KPI rule, the
default bar-widget height, obsolete media-query removal, and font-ready TV-grid
refit defined in the existing scaling plan.

## Integration strategy

1. Work on an isolated branch rooted at current `main`.
2. Before porting either delivery, add or extract a focused regression test in
   the integration worktree and observe the relevant failure on `main`.
3. Reconcile the daily-auto commits in their original order, resolving only
   conflicts caused by changes already present on `main`.
4. Apply the scaling change as a file-scoped port, never as a branch merge.
5. Run the plan-specific tests plus a combined regression suite, inspect the
   final diff, and make one commit per completed delivery.
6. Push the commits and fast-forward `main` only after verification succeeds.

## Non-goals

- Merge or otherwise publish unrelated work from
  `fix/recycling-dashboard-scaling`.
- Rewrite the historical scaling plan or add new dashboard features.
- Change the current schedule delivery lifecycle, Saturday-recruiting rules, or
  existing Settings defaults beyond the already-planned daily ownership model.

## Failure handling

If the daily-auto branch cannot be reconciled without changing a current-main
behavior, retain the failing regression test and port the smallest independent
behavioral slice instead of merging the branch wholesale. If the scaling port
needs files outside the five listed artifacts, stop and record that dependency
for a separate decision rather than broadening scope.

## Verification

- Daily-auto: relevant schedule-persistence, Settings, staffing, rotation, and
  Saturday-recruiting tests pass.
- Scaling: the static regression test fails before the port and passes after;
  the preview script executes against its configured local test database when
  available.
- Final: targeted combined suite, `git diff --check`, and a review of both
  commits' changed-file lists prove the scope boundaries above.
