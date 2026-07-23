# Saturday recruitment autosave design

## Goal

Prevent Saturday recruitment from checking an older saved schedule while the
Scheduler already looks empty in the browser.

## Behavior

When a manager clicks **Recruit**, disable the button and wait for the
Scheduler's existing `flushAutosave()` operation to finish. Start recruitment
only after that save succeeds.

If the save fails, do not start recruitment. Re-enable the button and explain
that the schedule could not be saved, so recruiting was not started. Keep the
existing server-side assignment guard as protection against overwriting saved
work.

## Scope

Change only the Saturday recruitment click flow. Reuse the Scheduler's existing
autosave API rather than deleting assignments on the server or combining the
save and recruitment endpoints.

## Validation

Add a regression test that requires the recruitment script to await
`window.flushAutosave()` before posting activation. Keep the existing direct
activation, error-message, and no-confirmation behavior covered.
