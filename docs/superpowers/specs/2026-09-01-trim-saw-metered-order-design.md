# Trim Saw Metered Ordering Design

**Date:** 2026-09-01

## Goal

Keep Trim Saw workers visible in the Metered production section, but place them below every other metered work center until Trim Saw performance is better understood.

## Sorting contract

- A row is treated as Trim Saw when its current work center, or final work center for a completed day, starts with `Trim Saw` using a case-insensitive comparison.
- Normal metered rows sort before Trim Saw rows.
- The existing attention, deficit, rolling-performance, name, and employee-ID ordering remains unchanged within each subgroup.
- An earlier Trim Saw interval does not demote a person who later transferred to another work center; the row's current/final interval owns its placement, matching the dashboard's existing summary rule.
- Trim Saw remains part of Metered production. The fixed Production → Tablet forklift → Other section order does not change.

## Implementation boundary

Add one production-only subgroup rank to the existing `PersonRow.sort_key` construction in `people_performance.py`. Derive it from the final interval's canonical location name. Do not change source loading, scoring, attention reasons, section assignment, or templates.

## Failure behavior

Missing, stale-without-verified-location, conflicting, or unmapped locations continue to use their existing unavailable/Other behavior. Only a canonical final metered location matching `Trim Saw*` receives the lower metered subgroup rank.

## Verification

- A Trim Saw row with a worse result sorts below a non-Trim-Saw metered row.
- A Trim Saw row needing attention still sorts below all non-Trim-Saw metered rows.
- Multiple Trim Saw rows retain the existing attention ordering among themselves.
- A person who transfers from Trim Saw to another metered center uses the final center and is not demoted.
- Forklift and Other ordering remains unchanged.
