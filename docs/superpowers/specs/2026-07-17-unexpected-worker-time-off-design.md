# Unexpected Worker Time-Off Override Design

## Goal

Let an hourly employee who was approved off for today work without being blocked by Odoo, while creating a management action that brings today’s posted schedule back into sync with the floor.

## Employee flow

The kiosk detects an approved, full-day local time-off mirror for the employee on the plant day. It replaces the normal clock-in shortcut with a work-center picker. After the employee picks a work center, the kiosk displays a confirmation screen naming the employee and explaining that continuing will cancel today’s approved time off in Odoo and clock them in.

On confirmation, the kiosk synchronously refuses the matching Odoo leave, marks the local mirror refused, records the clock-in, and creates one idempotent unexpected-worker event for that employee and day. If Odoo cannot cancel the leave, it shows an error and does not create the punch.

## Management flow

The Exception Inbox shows each unresolved event as an urgent “Unexpected / unassigned worker” row. Its detail identifies the employee’s selected clock-in work center and recommends enabled work centers below their configured minimum. The row opens today’s staffing schedule.

The management user places the employee in a work center, then saves and publishes the amended schedule using the existing scheduler controls. The inbox event automatically clears only after the day is published and the employee is assigned somewhere in that published schedule. When no enabled center is below minimum, the inbox says no shortage exists and management may select any qualified work center.

## Data and safety

A local `unexpected_worker_events` table provides a durable, deduplicated audit record keyed by plant day and employee. It records the employee, matched leave, selected punch center, confirmation time, and resolution time. Events are never removed; the inbox reads only unresolved events.

Only approved full-day leave for the current plant day triggers the override. Partial and pending leave retain their current flows. The leave refusal occurs before the timeclock log entry so Odoo cannot receive an attendance record that conflicts with an active leave. Repeated confirmation requests reuse the same event instead of creating duplicates.

## Testing

Tests cover detection, the confirmation gate, successful immediate cancellation and event creation, cancellation failure (no punch), deduplication, Inbox copy/recommendations, and resolution after placement and publishing.
