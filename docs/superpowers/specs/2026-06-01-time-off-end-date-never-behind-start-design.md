# Time-off end date never behind start

**Problem:** On the full-day time-off request form, the end date can end
up behind the start date. Two gaps:

1. The current handler (commit `83c2c7e`, branch `default-end-date`)
   *unconditionally* resets the end date to the start date on every start
   change. That fixes a start-after-end pick, but it also wipes a
   deliberately-set multi-day end date whenever the user merely nudges the
   start.
2. The end-date input only enforces `min=today`, not `min=start`, and a
   change to the end date re-runs `recalc` but never re-syncs against the
   start. So a user can pick a future start (end auto-fills to match), then
   edit the end to an earlier date (e.g. start Jun 20, end Jun 10). The
   inverted range shows a request size of 0 but is still submittable; the
   server then *swaps* it into a multi-day span the user never picked.

**Fix (frontend only):** Enforce one invariant — *the end date is never
behind the start date* — by snapping the end **up** to the start whenever
either date changes and the end is behind. Correct only when actually
inverted, so a multi-day end the user set on purpose is preserved.

## Behavior

| User action | End date result |
|---|---|
| Form load | start = end = today (unchanged) |
| Pick a future start (end was behind) | Snaps up to start → one-tap single day |
| Push end out to a later date | Kept (it's on/after start) |
| Nudge start earlier, still ≤ end | **Kept** — multi-day preserved |
| Set end before start | **Snaps back up to start** |

The "pick a future start → end follows" one-tap single-day flow from
`83c2c7e` is preserved: on load the end sits at today, so choosing any
future start leaves the end behind it and triggers the snap.

## Implementation

Single file: `src/zira_dashboard/static/timeclock_time_off.js`
(lines ~217–241, the listener-wiring block). No server, template, or
Python changes.

1. Replace the unconditional `onStartDateChange` with a `clampEndToStart()`
   that:
   - sets `dateTo.min = dateFrom.value` (greys out earlier days in the
     native picker — a hint, not the enforcement), and
   - if `dateTo.value && dateTo.value < dateFrom.value`, sets
     `dateTo.value = dateFrom.value` (the hard enforcement).
   ISO `YYYY-MM-DD` values compare correctly with `<`.
2. Wire `clampEndToStart()` + `recalc()` to **both** the start and end
   inputs' `change` and `input` events (today the end input only triggers
   `recalc`). A small `onDateOrTimeChange` wrapper covers
   `typeSel`/`dateFrom`/`dateTo`/`timeA`/`timeB`.
3. Call `clampEndToStart()` once on load before the initial `recalc()` so
   edit-mode prefills with a future start also get the correct `min`.

Guard stays `if (dateTo && dateFrom && dateFrom.value)`. Partial-day
shapes render the end as a hidden `date-to-hidden` element, so
`getElementById("date-to")` is null and the clamp is a no-op there —
identical to today's behavior.

## Verification

- No JS test runner exists in this repo, so the change is verified by
  reasoning plus a manual browser check of the table above (the full-day
  request form, and an edit-mode prefill).
- No Python touched, so no suite run needed for this change.

## Out of scope

- **Server swap left as-is.** Both POST handlers
  (`routes/timeclock_time_off.py:612`, `:1050`) still *swap* an inverted
  full-day range rather than collapsing it. The JS guard means inverted
  ranges effectively never reach the server; aligning the server to
  collapse was considered and deliberately deferred.
- Partial-day shapes (late arrival / early leave / mid-day gap) — already
  single-day and server-forced; untouched.
- The `min=today` floor on the start date — unchanged.
