# Task 7 Report: Posted snapshot browser immutability

## TDD evidence

- **RED:** Added static assertions for the posted-view disablement loop, picker event cancellation before the posted-view return, and the Slack guard. Ran `.venv/bin/pytest tests/test_staffing_static.py -k "posted_snapshot or clear_schedule" -v`; the posted-snapshot test failed because the disablement loop was absent.
- **GREEN:** Added the smallest guards in `staffing.js`: disable interactive buttons, non-hidden inputs, and selects except `action=discard_draft`; cancel picker events before returning for a posted snapshot; return from `postToSlack()` before any action. The targeted test command then passed (2 passed), and the complete static test file passed (17 passed).

## Verification

- `.venv/bin/pytest tests/test_staffing_static.py -v` — 17 passed
- `node --check src/zira_dashboard/static/staffing.js` — passed
- `git diff --check` — passed

## Self-review

- The posted-view loop disables scheduled, Auto, and Training checkboxes, while leaving `discard_draft` enabled and preserving readonly notes.
- The generic picker handler prevents the native checkbox default and stops propagation before returning in posted view.
- `postToSlack()` exits before changing UI state, flushing autosave, or publishing.
- No unrelated tracked or untracked work was modified.
