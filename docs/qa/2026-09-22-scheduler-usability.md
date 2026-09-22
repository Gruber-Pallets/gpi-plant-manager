# Scheduler usability review — September 22, 2026

Scope: approved items 2 (actionable warnings), 4 (save/post status), 6 (phone cards), and 7 (measured responsiveness). Scheduling rules and external integrations are unchanged.

## Changes and checks

- Warnings group duplicate issues while keeping distinct reasons. Review buttons open the existing picker; off and posted centers stay unchanged. Find buttons focus unassigned people. Unknown targets remain plain text. Unsafe-looking messages render as text. Persistent sync warnings survive refreshes.
- Save status distinguishes draft, posted, pending, saving, and failure. Retry retains changes and errors until persistence succeeds. Publish and ordinary app navigation drain queued saves. Failed navigation stays on the original day. Explicit Posted views use the same read-only state for controls and autosave.
- Phone cards use the original inputs, preserve bay information, and offer a remembered Table choice. Checked 375, 768, and 1440 px without page overflow. Switching views preserves input identity. Print keeps the original full-width table and training labels.
- Identical validation requests share pending work. Changed assignments still replace obsolete checks; later checks remain fresh. There is no persistent cache of validation results.

The browser tests found and fixed an outside-click handler that closed warning-opened pickers, phone navigation overflow, interrupted print grid placement, navigation before autosave, and inconsistent read-only handling of a newly posted schedule.

## Measurements

Chromium, isolated local FastAPI app and Postgres, five synthetic people, external requests blocked. Compare frozen source at `6b191aab` with the implementation, same database and enabled center. One warmup and five measured samples. Day load uses navigation DOMContentLoaded, excluding the artificial network-idle wait. Picker and rebuild include browser automation overhead.

| Operation | Before median | After median |
| --- | ---: | ---: |
| Day load | 18.2 ms | 23.4 ms |
| Open picker | 28.4 ms | 37.2 ms |
| Rebuild | 54.1 ms | 50.7 ms |

These small local samples do not establish a general wall-clock speed improvement. The measured improvement is reduced duplicate work under slow responses: with an 800 ms validation delay and three identical checks 250 ms apart, requests fell from **3 to 1**. A later check made a new request in both versions. Changed-snapshot and stale-response behavior is covered by regression tests.

## Validation

- Baseline scheduler tests: 260 passed, 2 skipped.
- Final focused scheduler tests: **298 passed, 2 skipped**.
- Full suite with disposable Postgres and isolated training transaction tests: **7,266 passed, 21 skipped**; two previously verified baseline failures remained (exception-inbox quick-punch mirror coverage and the old skills-cache key expectation). The later Posted-state refinement passed the final focused suite.
- Ruff across src/tests/scripts, JavaScript syntax checks, and whitespace checks pass.
- Real browser workflows covered save failure/retry/reload, day-navigation save/failure, publish with pending changes, posted read-only notes, warning links, card/table identity, phone/tablet/desktop layout, and full-width printing.
- The training browser regression covered create validation, edit, Enter save, cancel, pause/resume, complete/end, skills screen, scheduling failure/retry, phone controls, posted training, and print. All passed using the current synthetic schedule date; a first run using yesterday's date was invalidated by normal past-day attendance reconciliation.
- Independent review identified navigation and print issues; both were fixed and regression-tested. Follow-up review found no additional concrete defects.

No production schedule, employee, training, or integration records were changed during testing. Local mutations used synthetic QA people. Production verification is read-only.

## Delivery

Implementation push and read-only deployment verification are pending.
