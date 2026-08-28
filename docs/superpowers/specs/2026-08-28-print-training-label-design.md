# Print training label design

## Goal

Make printed Staffing schedules show a small `Training` label beside each
active trainee's scheduled name. Remove the large Training sidebar from the
print/PDF output.

## Scope

- This applies only to the Staffing print stylesheet, including the PDF used
  for Slack schedule posts.
- The normal on-screen Training sidebar and its controls remain unchanged.
- The label applies only to trainees, never to their trainers.

## Design

The Staffing route already supplies `active_training_blocks` for the displayed
day. The template will derive the active trainee names from that day-scoped
data and append a `Training` marker to matching scheduled names.

The marker is hidden in the normal screen stylesheet and displayed by the
print stylesheet. The print stylesheet will hide the entire Training sidebar,
whether or not it contains cards, so its large cards and progress details do
not consume paper space.

If a trainee is not scheduled, there is no name in the schedule table to mark.
Paused, completed, future, and non-day-scoped training plans do not produce a
label because they are absent from `active_training_blocks`.

## Verification

Add a focused static regression test that checks that:

1. the schedule template keys the marker from day-scoped active training data;
2. the normal stylesheet hides the marker;
3. the print stylesheet shows the marker and hides the full Training sidebar.

Run that focused test and the related Staffing static tests after the
implementation.
