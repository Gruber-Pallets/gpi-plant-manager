# Scheduler lifecycle task 2 report

## Scope completed

- Restricted the staffing form mutation route to `save` and `publish`; posted-view mutations remain rejected.
- Applied `draft_from_posted` before normal saves, Hours updates/resets, testing-day clears, and partial-time-off clear/restore mutations.
- Removed the legacy form-route `save_notes`, `discard_draft`, and `unpublish` branches without changing the separate past-schedules route.
- Successful publishes now create a fresh `published_delivery` version; blocked publishes create/retain a Draft snapshot and do not create a version.
- Added `GET /staffing/live?day=YYYY-MM-DD` with `Cache-Control: no-store` and lifecycle revision fields.
- Added lifecycle fields (`revision`, `published`, `has_snapshot`, `posted_version`) to successful form JSON responses.

## Tests

- Red: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_schedule_metadata.py tests/test_staffing_custom_hours.py tests/test_staffing_saturday_recruiting.py -q` failed on the intended missing lifecycle behavior (plus one existing test fixture that needed to mock its database-backed page context).
- Green: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_schedule_metadata.py tests/test_staffing_custom_hours.py tests/test_staffing_saturday_recruiting.py tests/test_staffing_rotations.py -q`
- Result: `146 passed, 6 skipped` (the skipped custom-hours storage tests require `DATABASE_URL`).

## Review notes

- `git diff --check` is clean.
- `tests/test_staffing_rotations.py` has one added revision stub because its autosave test uses a mocked `save_schedule`; this is required by the new successful JSON-response contract.
