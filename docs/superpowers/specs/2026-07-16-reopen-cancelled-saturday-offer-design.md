# Reopen Cancelled Saturday Offers

## Goal

When an employee cancels an optional Saturday commitment before the response deadline, their next timeclock login must show the same Saturday response screen again. They can select Yes, No, Decide later, or choose partial availability.

## Design

The existing employee offer lookup is the sole login gate for Saturday recruiting. It currently treats a `cancelled` response as terminal, alongside `declined` and `committed`. Remove `cancelled` from that terminal set. A cancelled employee will therefore be evaluated again for the still-open recruitment, using the existing qualification and remaining-coverage checks.

No new state or template is required. The existing offer view already provides Yes, No, Decide later, and the partial-shift link. The partial workflow already limits availability to half-hour increments within the offered shift and requires confirmation before committing.

## Boundaries and failures

- A cancelled employee is reoffered only while recruiting remains open, before its deadline, and while a compatible opening remains.
- Declined and committed responses remain terminal and are not reoffered.
- Cancellation after the recruiting cutoff remains blocked by the existing route.
- Store or lookup failures remain fail-safe and continue to route the employee to the dashboard.

## Verification

Add a store-level regression test proving a cancelled response can receive the offer again, and route-level coverage proving the employee's next login is routed to that offer. Retain the current partial-hours test coverage and add an end-to-end route assertion that an offered employee can submit a valid late-arrival/early-departure availability selection to the confirmation screen.
