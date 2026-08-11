# Odoo Roster Sync Inbox Alert Design

**Status:** Approved for implementation

## Goal

Tell managers in the Exception Inbox whenever Plant Manager rejects an unsafe
Odoo employee roster update, before it can affect the timeclock.

## Design

The Odoo roster sync will store one durable alert when it rejects a payload
whose `active` value is not the Boolean `true`. The stored alert includes the
safe, manager-facing reason and the time it was detected. A successful fresh
Odoo roster sync clears that alert; cache-only TTL hits do not.

The Exception Inbox will read that state from Postgres and render one urgent
`Timeclock roster` row. It will say that the names remain available from the
last good update and link to the People Matrix, where a manager can run the
existing Refresh from Odoo action. The row counts toward the Inbox badge and
clears automatically after the next verified successful roster sync.

## Boundaries

- Only unsafe/malformed active-status payloads create this alert.
- The alert does not write to Odoo or alter the roster.
- Existing sync rejection remains the safety boundary: no people rows change
  when the alert is created.
- The Inbox reconciler records the automatic clearing in its archive using a
  stable item key.

## Validation

Tests will prove that an unsafe roster payload writes the durable alert without
writing employee rows, a successful fresh sync clears it, and the alert
appears as one urgent Inbox item until it is cleared.
