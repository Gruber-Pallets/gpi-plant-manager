# CI Failure and Run Noise Design

## Problem

The `tests` GitHub Actions workflow is failing on every push to `main`.
GitHub therefore sends a failure notification for each completed run that Dale
triggered.

The current failure is deterministic:

`test_current_recycling_goats_uses_awards_with_overrides` expects the Recycling
leaderboard to show Dismantler GOAT before Repair GOAT, but the route now returns
them in the opposite order.

Commit `2a85e73` centralized GOAT category definitions. The shared category list
intentionally starts with Repairs, while the Recycling leaderboard had
historically used the display order Dismantlers then Repairs. Reusing the shared
list directly changed that screen's order and left `main` red. Later unrelated
pushes inherited the same failure.

Rapid pushes can also start overlapping workflow runs. Older runs continue even
after a newer commit has replaced the code they are testing.

## Goals

- Restore the established Recycling leaderboard order: Dismantler GOAT, then
  Repair GOAT.
- Preserve the shared category order used by other GOAT features.
- Keep GitHub Actions failure notifications enabled.
- Cancel an older in-progress test run when a newer push targets the same Git
  reference.
- Verify the workflow configuration and the behavior with automated tests.

## Non-goals

- Do not change global GitHub notification settings.
- Do not disable tests on pushes to `main` or pull requests.
- Do not reorder every GOAT category consumer.
- Do not address the unrelated GitHub Actions Node.js deprecation warning in
  this change.

## Design

### Recycling leaderboard order

The Recycling leaderboard route will define its display order by category key:
`dismantlers`, then `repairs`. It will still obtain labels and group names from
the centralized `goat_categories` records. This keeps shared category metadata
centralized while making the route's presentation order explicit.

The existing failing test is the regression test for this behavior. It must fail
before the fix and pass afterward. The global category-order test remains
unchanged, proving the repair does not alter other consumers.

### Superseded workflow runs

The workflow will receive a top-level concurrency group based on the workflow
name and Git reference:

```yaml
concurrency:
  group: tests-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
```

Pushes to `main` will therefore share one concurrency group. A newer push will
cancel an older in-progress run before starting the latest run. Pull-request
references remain isolated from `main` and from other pull requests.

This reduces obsolete work during rapid pushes. It does not hide a real failure
in the newest commit, and it does not prevent notifications for distinct failed
runs that finish before another push arrives.

### Workflow contract test

A small static test will read `.github/workflows/tests.yml` and assert that the
workflow includes the intended concurrency group and
`cancel-in-progress: true`. This protects the noise-control behavior from being
removed accidentally.

### Patch notes

The implementation push will add a short, plain-language `CHANGELOG.md` entry
explaining that the Recycling leaderboard order is restored and that outdated
test runs stop when a newer update arrives.

## Verification

1. Re-run the currently failing Recycling leaderboard test and confirm it
   passes after the route change.
2. Run the global GOAT category tests to confirm their order remains unchanged.
3. Run the workflow contract test.
4. Run Ruff across `src`, `tests`, and `scripts`.
5. Run the local test suite with external database access disabled.
6. Push to `origin/main`, inspect the resulting GitHub Actions run, and require
   the Postgres-backed CI suite to finish successfully.

The work is complete only when the pushed `main` workflow is green.
