# Auto-Lunch Remediation Design

## Problem

Production Auto-Lunch is set to Off. The worker therefore created no runs or
system punches after 2026-08-17, even though the plant schedule contains an
11:00–11:30 Lunch break. Completed attendance on 2026-08-18 through
2026-08-20 needs the same unpaid-lunch split that the live worker would have
created.

## Decision

Keep the existing live worker unchanged. Restore its existing Live setting,
then use a dry-run-first, idempotent backfill command for completed affected
days. The command reads the real Odoo interval IDs, chooses only an interval
that covered the person’s scheduled lunch-out instant, closes it at lunch-out,
and creates a same-work-center follow-on interval at lunch-in only when the
original interval extended that far.

The repair also creates the two local `source='auto_lunch'` audit punches and
a terminal `auto_lunch_runs` row. Re-running it skips an existing run, so it
cannot deduct a second lunch.

## Eligibility

- A fixed-schedule person needs a plant workday, a resolved Lunch break, and
  an Odoo attendance interval satisfying `check_in <= lunch_out < check_out`.
  Their own Odoo calendar lunch remains the authority when configured, matching
  the live worker.
- A flexible-schedule person needs a local first `clock_in`; their window is
  that punch plus the saved flex hours and minutes. The same interval rule
  applies.
- An interval ending during lunch is shortened to lunch-out and receives no
  return record, matching a person ending their shift during an active lunch
  gap. Intervals beginning after lunch-out are not repaired.
- The current partial day is eligible only once its lunch window has passed.

## Safety and Verification

The command defaults to reporting plans; `--apply` is explicit. Before apply,
the live production report is checked for settings, scheduled windows, and
candidate names. After apply, local log pairs, terminal runs, and Odoo
intervals are compared for every changed person/day. The existing Live setting
is restored only after the historical repair completes, preventing the live
worker from racing the backfill.
