# Saturday Unassigned/Off Swap Design

## Goal

Let a scheduler manager correct Saturday availability from the left rail when
someone verbally confirms a change. A committed person can be moved from
**Unassigned** to **Off**, and an off person can be moved to **Unassigned**.

## Interaction

On a Saturday recruiting schedule, each person row in the Unassigned and Off
sections gains a small swap button that is visually hidden until the row is
hovered or receives keyboard focus. The button has an accessible label that
states the destination, for example, “Move Ana to Off”. It is not rendered for
ordinary weekdays, Time Off, reserves, or work-center assignments.

Selecting the button opens a compact native dialog. It asks “Move Ana to Off?”
or “Move Ana to Unassigned?” and offers Cancel and Move. Cancel has no effect.
While the request is pending, the dialog action is disabled. A successful move
updates the two lists and counts in place; errors remain visible in the dialog
and leave the lists unchanged.

## Persistence and scheduling behavior

The two lists currently derive exclusively from Saturday recruiting responses:
“committed” means Unassigned and every other response means Off. Editing that
response would erase the employee’s actual answer and, in the other direction,
cannot safely manufacture an eligibility response. Instead, store manager-made
per-day availability overrides with the Schedule. The mapping is
`person name -> "unassigned" | "off"`; absence from the mapping means use the
recruiting response.

The effective Saturday available set is committed people, modified by these
overrides. That set drives:

- the Unassigned and Off left-rail lists;
- available options in Saturday work-center pickers;
- the server-side rejection of non-available Saturday assignments; and
- Saturday publish validation.

Full-day Time Off remains authoritative: a person with full-day time off stays
out of both lists and cannot be made schedulable by an override. A move does
not change the recruiting response, notifications, or availability hours. A
manager-added Unassigned person receives the Saturday shift’s normal hours;
existing committed people preserve their partial-availability badge.

The override is carried through normal saves, posted-schedule snapshots, and
draft-from-posted behavior. The targeted mutation produces a draft if the
current Saturday was posted, following the existing scheduler edit rule.

## Backend interface

Add a manager-only endpoint under the staffing API that accepts a Saturday day,
person name, and destination (`unassigned` or `off`). It validates that the day
is Saturday, that an active Saturday recruitment exists, that the person is an
active non-reserve roster member, that the destination is valid, and that the
person does not have full-day time off. It writes only the named availability
override to the day’s Schedule, invalidates scheduler caches, and returns the
effective counts and destination.

## Tests

Test the pure effective-availability derivation with both override directions
and full-day-time-off precedence. Test the mutation endpoint’s validation,
persistence, and posted-to-draft transition. Add static template/JavaScript/CSS
contract assertions for the Saturday-only control, dialog copy, and endpoint.

## Non-goals

This does not alter a worker’s Saturday recruiting response, send notifications,
or add swap controls to weekday schedules, reserves, Time Off, or assigned
work-center rows.
