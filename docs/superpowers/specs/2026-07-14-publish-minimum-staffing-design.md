# Publish Minimum Staffing Enforcement Design

## Goal

Allow supervisors to save an intentionally under-minimum default crew, while
preventing a schedule from being published until every configured work center
has its required minimum number of scheduled operators.

## Current behavior and cause

The Work Centers settings picker intentionally allows a partial default crew.
For example, a two-person work center can retain one default person after the
user acknowledges the “Fewer than min” warning. This is the desired behavior:
it makes an expected staffing gap visible on a new schedule.

The scheduler's publish route currently validates only partially staffed work
centers with a minimum of two or more operators, and accepts an `override=1`
form value that bypasses that validation. Its alert template exposes this as an
“Override & Publish” button. Empty work centers and one-person minimums are
also not included in the current validation. An AJAX publish used by “Post to
Slack” receives a successful JSON response even when publishing was blocked,
so it can continue to share an unpublished schedule.

## Behavior

### Default crews

No change to the Work Centers default-people picker. A supervisor can set one
default person on a work center whose configured minimum is two. Closing the
picker continues to warn that new schedules start understaffed, but saving the
default is allowed.

### Server-authoritative publishing

For every publish request, the scheduler compares the submitted assignment
count for every configured work center against that work center's effective
`min_ops`. A count below the minimum blocks publishing, including zero people
at a one-person work center and an empty two-person work center. The request's
`override` value is ignored; no request path can bypass the minimum.

When a normal form publish is blocked, the submitted assignments are saved as
a draft and the browser returns to that day. The page lists each short work
center as “<name> requires <minimum> operators — currently <count>.” The
“Override & Publish” control is removed; the user must staff each work center
to its minimum and publish again.

When a JSON/AJAX publish is blocked, the route returns a non-success response
with the same shortage details. The Post to Slack flow stops at that response
and does not call its Slack upload endpoint.

### Availability and scope

The validation counts submitted assignments, matching the scheduler's existing
publish route. Defaults remain a starting point only; they are never used as a
publish-time substitute for scheduled people. Save, unpublish, notes-only,
discard-draft, and automatic-save requests retain their existing behavior.

## Implementation

- Centralize the publish-shortage calculation in the staffing route so native
  form and JSON publish requests use identical server-side validation.
- Remove the `override` parsing and the template's override form controls.
- Expand the post-block display model to report every work center below its
  configured minimum, including an empty or minimum-one center.
- Return a publish failure response to JSON callers, allowing the existing
  Slack client flow to halt before its share request.

## Verification

Add tests that prove:

1. Settings still permits saving one default person at a two-person work
   center (the existing warning remains advisory).
2. A normal publish with one of two people saves a draft and redirects back
   without publishing, even if the request includes `override=1`.
3. A normal publish with an empty one-person work center is likewise blocked.
4. A JSON publish below minimum fails with the shortage details, preventing
   the client-side Post to Slack workflow from treating it as published.
5. The blocked-publish display lists zero-count and minimum-one shortages and
   the scheduler template no longer renders an override control.

## Non-goals

- Changing the settings warning or disallowing partial default crews.
- Changing work-center minimum configuration, automatic scheduling, or roster
  eligibility rules.
- Allowing privileged users to bypass a configured staffing minimum.
