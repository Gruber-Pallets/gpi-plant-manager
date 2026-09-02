# Task 1 Report: Typed People Warning Contract

## Status

DONE

## Implementation

- Added immutable `WarningAction` and `DashboardWarning` domain records, typed warning/action identifiers, opaque stable warning keys, timezone validation, and all eight specified warning builders.
- Replaced every People data-loader warning string with a typed builder result.
- Classified production failures as `missing_totals`, `incomplete_data`, `duplicate_data`, `missing_goal`, `metric_mismatch`, or `calculation_failure` in the required precedence order.
- Preserved source check time and forklift last-success time, reduced attendance freshness details to a blocker count, and aggregated only unresolved in-shift forklift identities without exposing raw events or exception messages.
- Typed `DashboardModel.source_warnings`, rejected legacy string values, and deduplicated by stable key while preserving the first record and source order.
- Kept the People template server-rendered and changed warning output to use `warning.label` only.
- Converted shared test and preview fixtures to typed warnings and added the reusable `unmatched_warning_fixture()` helper.
- Kept attendance ownership, source fail-closed behavior, row content, scoring, and section ordering unchanged.

## Files changed

- `src/zira_dashboard/people_performance_warnings.py` (new)
- `src/zira_dashboard/people_performance.py`
- `src/zira_dashboard/people_performance_data.py`
- `src/zira_dashboard/templates/_people_performance_rows.html`
- `scripts/preview_people_performance.py`
- `tests/test_people_performance_warnings.py` (new)
- `tests/people_performance_fixtures.py`
- `tests/test_people_performance_data.py`
- `tests/test_people_performance_rows.py`
- `tests/test_people_performance_template.py`
- `tests/test_people_performance_view.py`

`scripts/preview_people_performance.py` was included because the required legacy-warning sweep identified it as a remaining People fixture. `tests/test_people_performance_view.py` now removes unrelated warnings from its hover-only JSON serialization setup; warning view serialization remains deferred to Task 5 as planned.

## TDD evidence

### RED

1. `.venv/bin/pytest tests/test_people_performance_warnings.py -q`
   - Exit 2 during collection.
   - Expected failure: `ModuleNotFoundError: No module named 'zira_dashboard.people_performance_warnings'`.
2. `.venv/bin/pytest tests/test_people_performance_data.py tests/test_people_performance_rows.py tests/test_people_performance_template.py -q`
   - Exit 1: `18 failed, 49 passed`.
   - Expected failures showed data loaders still returned strings, dashboard warnings were not key-deduplicated, and strings were not rejected.
3. `.venv/bin/pytest tests/test_people_performance_template.py::test_page_uses_one_compact_live_manager_strip -q`
   - Exit 1: the template rendered the full `DashboardWarning(...)` representation instead of only its label.
4. `.venv/bin/pytest tests/test_preview_people_performance.py -q`
   - The non-browser preview assertion failed because the preview still supplied warning strings; browser cases also could not launch inside the filesystem sandbox.
5. First elevated affected-suite run:
   - `.venv/bin/pytest tests/test_people_performance*.py tests/test_preview_people_performance.py -q`
   - Exit 1: `1 failed, 156 passed`.
   - The remaining failure showed a hover-only test JSON-serializing an unrelated typed warning record.

### GREEN

1. `.venv/bin/pytest tests/test_people_performance_warnings.py -q`
   - `3 passed in 0.08s`.
2. `.venv/bin/pytest tests/test_people_performance_warnings.py tests/test_people_performance_data.py tests/test_people_performance_rows.py tests/test_people_performance_template.py -q`
   - `70 passed in 0.71s`.
3. `.venv/bin/pytest tests/test_preview_people_performance.py::test_preview_contains_busy_people_fixture -q`
   - `1 passed in 0.62s`.
4. Required focused command:
   - `.venv/bin/pytest tests/test_people_performance_warnings.py tests/test_people_performance_data.py tests/test_people_performance_rows.py -q`
   - `57 passed in 0.23s`.
5. Final affected suite, run outside the filesystem sandbox so Playwright could launch:
   - `.venv/bin/pytest tests/test_people_performance*.py tests/test_preview_people_performance.py -q`
   - `157 passed in 15.87s`.
6. Final static verification:
   - `.venv/bin/ruff check src/zira_dashboard/people_performance_warnings.py src/zira_dashboard/people_performance.py src/zira_dashboard/people_performance_data.py scripts/preview_people_performance.py tests/people_performance_fixtures.py tests/test_people_performance_warnings.py tests/test_people_performance_data.py tests/test_people_performance_rows.py tests/test_people_performance_template.py tests/test_people_performance_view.py && git diff --check && ! rg -n 'source_warnings=.*"|in model.source_warnings' tests scripts && test -z "$(git diff --name-only -- CHANGELOG.md)"`
   - Exit 0: `All checks passed!`; no legacy warning matches, whitespace errors, or changelog edits.

## Self-review

- Verified all eight builder functions use the exact required kinds, labels, manager-safe summaries, impacts, reason codes, facts, actions, and URLs.
- Verified warning keys are 24-character BLAKE2s hex digests based on kind and trimmed subject, never presentation copy.
- Verified timestamps are normalized to UTC and naive timestamps are rejected by the shared constructor path.
- Verified production reason precedence matches the brief and scorer exception text never enters the warning.
- Verified unmatched identity facts are bounded to 20 identities and are aggregated from unresolved in-shift events only.
- Verified first-seen warning-key order and legacy-type rejection with direct model tests.
- Verified rendered warning text remains unchanged and dataclass representations are not exposed.
- Verified no People warning behavior parses presentation labels.
- Verified `CHANGELOG.md` was not touched and no push was performed.
- No unrelated production behavior was changed.

## Concerns

None.
