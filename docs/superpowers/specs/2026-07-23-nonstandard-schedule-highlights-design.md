# Nonstandard schedule highlights design

## Goal

Use soft-blue enabled-work-center highlighting whenever a schedule is not the
plant's normal Monday–Friday default schedule.

## Rule

The page derives one view-only `nonstandard_schedule` flag. It is true when
the schedule has custom hours or its day is Saturday or Sunday. Enabled work
centers are soft blue when the flag is true; normal Monday–Friday default
schedules keep their existing soft-green treatment. Disabled centers and bay
cells retain their current styling.

## Validation

Add a static test for the new row class and its soft-blue CSS override, then
run focused staffing static tests.
