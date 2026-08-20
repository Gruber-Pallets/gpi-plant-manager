# Auto-Lunch Setting Guard Design

## Problem

Auto-Lunch stopped deducting lunches because its persisted mode was Off. The
worker did exactly what that setting requested, but the app provided no active
warning and no history showing who changed the mode or when. A restart did not
cause the change, and no background job writes the setting, but a future
Settings save or direct database edit could disable it again without a visible
operational signal.

## Goals

- Show one urgent Exception Inbox item whenever the persisted Auto-Lunch mode
  is Off or Observe-only.
- Remove the active item automatically only after the persisted mode returns to
  Live.
- Keep an append-only history of mode and flex-rule changes, including the old
  values, new values, time, actor when known, and change source.
- Detect a direct database change that bypasses the Settings form on the normal
  Inbox refresh, which runs about every 20 seconds.
- Refresh the worker's in-process settings cache from that database check so
  the worker and Inbox use the same source-of-truth value.
- Keep the existing Auto-Lunch behavior, schedule rules, and current
  production Live configuration unchanged.

## Non-Goals

- Do not send Slack messages, email, or other outside notifications.
- Do not prevent an authenticated manager from deliberately selecting Off or
  Observe-only.
- Do not add a manual dismiss action to the warning.
- Do not reconstruct the actor for historical changes made before this guard
  was deployed.
- Do not promise to capture a direct database change that is changed back
  between two monitor reads. The guard records and alerts on the persisted
  value it observes.

## Approaches Considered

### Chosen: database observation plus append-only audit history

Read the persisted singleton on every Inbox refresh, compare it with the last
audited value, record an outside-app change when they differ, and shape the
current value into an Inbox item. This covers both normal Settings saves and
the direct-edit failure mode without adding another notification system.

### Rejected: audit only the Settings form

This is smaller, but a database edit or another process could bypass both the
history and the alert. It does not cover the incident's main trust concern.

### Rejected: dedicated monitor worker and durable alert queue

A separate worker, state table, and alert lifecycle would work, but the app
already refreshes the Exception Inbox every 20 seconds. Reusing that refresh
keeps the feature smaller and avoids another independent lifecycle.

## Data Model

Add an append-only `auto_lunch_setting_events` table:

- `id BIGSERIAL PRIMARY KEY`
- nullable `before_enabled`, `before_observe_only`,
  `before_flex_after_hours`, and `before_flex_minutes`
- non-null `after_enabled`, `after_observe_only`,
  `after_flex_after_hours`, and `after_flex_minutes`
- nullable `actor_upn` and `actor_name`
- non-null `source`, restricted to `settings`, `external`, or `baseline`
- `changed_at TIMESTAMPTZ NOT NULL DEFAULT now()`

The first observation inserts a baseline row with null before-values. Baseline
rows establish the comparison point and render as "Monitoring started" rather
than pretending a change occurred. Settings-form changes use `source=settings`.
When the observer finds a persisted value different from the most recent
event's after-values, it inserts `source=external` with no actor.

Repeated observations of the same four-value signature create no new row.
Changing only the flex rule still creates history but does not create an Inbox
warning while the mode remains Live.

## Settings Save Flow

Extend `auto_lunch_settings.save` with optional actor metadata and a source.
Within one database transaction it:

1. Locks the singleton settings row.
2. Reads the current four values.
3. Returns without writing an event when all four requested values are equal.
4. Updates the singleton.
5. Inserts the matching audit event.
6. Commits, then updates the process cache.

The Settings route supplies `request.state.user_upn` and `user_name`. If the
database update or audit insert fails, the transaction rolls back and the
setting remains unchanged. Tests and trusted callers may omit actor metadata;
those events display as an unknown/system actor.

## Direct-Change Observation

Add a focused `auto_lunch_guard` module. Its observation function rereads the
singleton directly from Postgres instead of trusting the process cache and
updates that cache with the valid result. It then attempts the following
best-effort audit reconciliation:

1. Lock the singleton settings row to serialize concurrent Inbox refreshes.
2. Reread the current four-value signature.
3. Read the latest event.
4. Insert a baseline if history is empty, or insert one `external` event when
   the latest audited after-values differ.

The audit reconciliation is isolated from alert shaping. If its insert fails,
the observer logs the failure but still returns the valid persisted setting so
the urgent warning cannot be hidden. If the initial database read itself
fails, the Inbox records a degraded source and does not guess a mode; the
worker's last valid cached setting remains in place.

Because the existing Inbox warmer runs every 20 seconds, a direct change that
remains persisted is normally observed within about 20 seconds. That same
observation refreshes the shared settings cache before the Auto-Lunch worker's
next read.

## Exception Inbox Experience

Both `exception_inbox.build_summary` and `build_snapshot` include the guard as
a captured source. When the observed mode is not Live, they contribute one
urgent item and increment both total and urgent counts:

- Name: `Auto-Lunch`
- Label: `Off` or `Observe only`
- Detail: `Lunch deductions are not being written. Restore Live mode.`
- Badge: `Timeclock`
- Link: `/settings?section=timeclock#auto-lunch-form`
- Stable item key: `auto_lunch:setting`

The row has no dismiss or resolve action. Since it is derived from the current
setting, restoring Live removes it automatically on the next Inbox refresh.

## Settings History Experience

Show the newest 20 audit rows below the Auto-Lunch form. Each entry displays
the site-local date and time, before and after summaries, and the actor:

- Authenticated Settings save: the manager's name, falling back to their UPN.
- Outside-app detection: `Outside app / detected automatically`.
- Baseline: `Monitoring started`.

The summaries use plain labels such as `Live · 5 hours · 30 minutes` and
`Off · 5 hours · 30 minutes`. History remains after the active Inbox item
clears.

## Failure Handling

- Settings saves are atomic with their audit entry; an audit failure prevents
  an untracked app-originated change.
- External audit reconciliation is best-effort and cannot suppress the active
  warning.
- Row locking and signature comparison prevent duplicate external events from
  concurrent Inbox refreshes.
- A failed persisted-setting read is exposed through the Inbox source-error
  mechanism. It does not force a false Off or Live state.
- The Inbox item cannot be dismissed while the unsafe mode remains persisted.

## Testing

Use test-first development for each behavior:

- Schema tests for the append-only event fields and source constraint.
- Settings-store tests for atomic update plus audit, actor capture, flex-only
  changes, unchanged saves, and cache update only after commit.
- Guard tests for baseline creation, external-change detection, deduplication,
  cache refresh, and returning an alert even when external audit insertion
  fails.
- Exception Inbox tests for the urgent row, summary counts, link, source-error
  degradation, and automatic removal in Live mode.
- Settings route tests proving request actor metadata reaches the store.
- Settings template/context tests for recent history labels and site-local
  timestamps.

Production verification must not deliberately disable Auto-Lunch. After the
deployment, verify the new schema, the persisted `enabled=true` and
`observe_only=false` values, one baseline history row, healthy Inbox responses,
and a successful Railway health check.

## Rollout

Deploy through the normal `main` branch Railway build. Schema bootstrap creates
the audit table idempotently. The first post-deploy Inbox refresh records the
current Live baseline without changing the setting. No new environment
variables or manual production toggles are required.
