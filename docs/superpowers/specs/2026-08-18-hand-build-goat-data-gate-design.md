# Hand Build GOAT 30-Day Data Gate Design

## Goal

Keep Hand Build production visible throughout the app while withholding every
Hand Build GOAT result until the category has at least 30 distinct workdays
with positive production data.

The gate applies automatically. Nobody has to remember to enable Hand Build
GOATs later.

## Qualifying-day rule

A qualifying data day is one date on which any work center in the Hand Build
family has positive attributed production in `production_daily`.

- Count distinct dates, not employee rows.
- Two employees who split one station's output still contribute one day.
- Multiple Hand Build stations producing on the same date still contribute one
  day.
- Zero-production rows and days without Hand Build production do not count.
- The threshold is category-wide, so future metered Hand Build stations join
  the same history instead of starting separate 30-day clocks.

## Category policy

Make the minimum data-day requirement part of the GOAT category definition.
Hand Build requires 30 qualifying days. Existing categories keep their current
effective minimum of one positive-production day.

Use one shared, pure readiness helper that receives the category's work-center
names and production records, then counts distinct positive-production dates.
The helper must recognize both names used by existing Hand Build GOAT paths:
`Hand Build` on the New-Leaderboard and notification category, and
`Hand Builds` for the persisted work-center group used by global badges.

## Suppressed surfaces

Before the threshold is met, Hand Build must not produce:

1. a current-GOAT chip on the New-Leaderboard,
2. a global GOAT badge next to an employee's name, or
3. a GOAT record alert or Slack celebration.

The readiness check runs before applying a manual GOAT override. An override
cannot create or reveal a Hand Build GOAT during the waiting period.

Normal Hand Build production remains available on dashboards, production
history, family rankings, player cards, and Gold Ribbons. Those features are
not GOAT awards and are outside the gate.

## Activation behavior

On the 30th qualifying day, Hand Build GOATs become eligible automatically.
The displayed GOAT is calculated from the complete Hand Build history,
including all 30 qualifying days.

Daily notification finalization continues marking earlier days complete while
Hand Build is ineligible. Reaching day 30 does not replay or announce records
from the first 29 days. The 30th or a later completed day can create a Slack
alert only when that day sets a new record under the existing notification
rules.

## Failure behavior

If production history cannot be read, Hand Build remains hidden from GOAT
surfaces for that request or notification run. Production dashboards continue
using their existing failure handling. No schema migration or manual enable
flag is needed.

## Testing and validation

Use test-first coverage for the following boundaries:

- 29 distinct positive Hand Build dates are not ready.
- 30 distinct positive Hand Build dates are ready.
- Multiple employees or stations on one date count once.
- Zero-unit dates do not count.
- Existing GOAT categories retain their current behavior.
- The New-Leaderboard omits the Hand Build GOAT chip before readiness and
  includes it at readiness.
- Notification finalization creates no Hand Build alert before readiness.
- Global GOAT badges do not expose a Hand Build override before readiness.
- Rankings, history, and ribbons remain unchanged.

Run focused GOAT, New-Leaderboard, notification, and Hand Build integration
tests, followed by Ruff and the broad local test suite with production database
URLs disabled.
