# Odoo Work-Center Continuity for Attendance

## Goal

Every attendance interval created by the kiosk has the correct linked Odoo
Manufacturing Work Center. When Auto-Lunch signs a person back in, it must
continue the exact app work center they had immediately before lunch.

The Odoo attendance field is `x_studio_work_center`. Live metadata confirms it
is an optional Many2one field to `mrp.workcenter`; it must receive an Odoo
record ID, never an app or Odoo display name.

## Why an explicit mapping is required

The kiosk and Odoo use different names for many otherwise corresponding work
centers. For example, the kiosk has `Repair 1`, while Odoo has `Repair #1`.
Only 7 of the kiosk's 23 configured work centers currently match active Odoo
work-center names exactly. Name cleanup or fuzzy matching could put time on
the wrong station.

The application will therefore maintain a deliberate, validated mapping:

```
app work-center name -> active mrp.workcenter ID and display name
```

Settings owns this mapping. It presents only active Odoo Manufacturing Work
Centers, records the selected Odoo ID plus its display name for auditability,
and refuses to save a target that is no longer active. The mapping is
one-to-one for kiosk work centers; the UI reports every unmapped kiosk option
as a readiness gap.

## Write flow

Create one shared work-center resolver in the Odoo attendance client, next to
the existing Kiosk Department resolver. Given an app work-center name, it
returns its mapped `mrp.workcenter` ID, if any.

Every attendance-opening path must call that same resolver:

1. kiosk clock-in;
2. kiosk transfer-in;
3. Auto-Lunch automatic clock-in after the break;
4. the immediate and retry paths in `timeclock_sync`;
5. manual Missing Work Center assignment of an existing Odoo attendance.

When there is a valid mapping, `clock_in` sends the ID with the normal create
payload, alongside the existing Kiosk Department value:

```python
{
    "x_studio_work_center": odoo_workcenter_id,
}
```

No caller writes an Odoo work-center name into this field.

## Break continuity

Auto-Lunch already captures the worker's active app work center when it writes
the automatic clock-out. It persists that value on `auto_lunch_runs.wc_name`.
It also falls back to that person's latest local clock-in or transfer-in when
the live Odoo snapshot has no work-center value.

The design retains that state machine. At the scheduled break end, Auto-Lunch
writes its normal local clock-in using the persisted app work-center name; the
shared attendance writer resolves the corresponding Odoo work-center ID when
it syncs to Odoo. A transfer before lunch is therefore continued after lunch.

Existing safeguards remain intact: a stale or absent live-attendance snapshot
causes no automatic action; an employee clocking out during the lunch gap
cancels the owed return; and the run state makes return punching idempotent.

## Read flow

Odoo reads a Many2one value as `[id, display_name]`. The application must not
treat that display name as a kiosk work-center name: it can differ from the
kiosk label.

All Odoo attendance readers that surface `wc_name` will unwrap the Odoo ID and
reverse-resolve it through the saved mapping to the exact app work-center
name. This includes open-attendance cache refreshes and attendance intervals
used for dashboards and attribution. An unknown Odoo ID becomes `None`, which
allows the existing local-punch fallback and correction workflow to operate
safely.

## Failure policy and operational safety

Correct timekeeping takes priority over filling an optional attribution field.
When an app work center is missing a mapping, or its selected Odoo target is
invalid:

1. never infer or fuzzy-match a target;
2. create the attendance interval without `x_studio_work_center` rather than
   preventing the employee from being clocked in;
3. surface the blank interval in the existing Missing Work Center alert so a
   manager can correct it;
4. show the mapping gap in Settings until it is repaired.

The production environment must set:

```dotenv
ODOO_KIOSK_WC_FIELD=x_studio_work_center
```

This setting must be enabled only with the application change that sends
Many2one IDs. Enabling it first would make the current name-based attendance
writes invalid.

## Validation and rollout

Automated tests must cover:

- loading and saving explicit mappings, including inactive-target rejection;
- attendance creation carrying an Odoo ID rather than a work-center name;
- kiosk clock-in, transfer-in, Auto-Lunch return, and asynchronous retry;
- assigning a Missing Work Center correction through the mapping;
- reverse mapping `[id, display_name]` to the kiosk label in open-attendance
  and attendance-interval reads;
- unmapped or invalid targets creating untagged attendance and surfacing the
  correction alert;
- Auto-Lunch retaining the pre-break work center, including its existing
  local-log fallback and no-double-return behavior.

Before production rollout:

1. map and validate every kiosk-selectable app work center against an active
   Odoo Manufacturing Work Center;
2. deploy the code and production `ODOO_KIOSK_WC_FIELD` setting together;
3. verify one normal clock-in, one transfer, and one automatic lunch return
   in Odoo; and
4. monitor Missing Work Center alerts immediately after deployment.

## Out of scope

This change does not rename work centers, infer mappings from Staffing, alter
Auto-Lunch scheduling, or modify historical attendance records in bulk.
