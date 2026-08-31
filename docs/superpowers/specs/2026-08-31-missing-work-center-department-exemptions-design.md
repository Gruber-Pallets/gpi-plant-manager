# Missing Work Center Department Exemptions Design

## Goal

Stop the Exception Inbox from flagging hourly Maintenance, Transportation, and Supervisor employees when their Odoo attendance has no work center. When an attendance record has no department, use the employee's current Odoo home department as a fallback.

## Confirmed behavior

- Maintenance does not require a work center.
- Transportation does not require a work center.
- Supervisor does not require a work center.
- Other departments continue to require a work center unless an administrator explicitly changes their saved department policy.
- An explicit administrator choice remains authoritative and is never overwritten by a default migration or later Odoo department sync.
- The attendance department wins when it is present because it describes where the employee worked for that attendance.
- The employee's current Odoo home department is used only when the attendance department is blank.
- If both department sources are blank or unknown, the safe behavior is to keep requiring a work center.

## Root cause

The legacy Missing Work Center path fetches attendance rows with an empty Odoo work-center field and then filters only for active hourly employees. It has no department input, so valid Maintenance and Supervisor attendances become urgent false positives.

The newer attendance-timeline path is department-aware, but its initial defaults exempt only Maintenance and Supervisor. Transportation is currently stored as requiring a work center. It also treats a blank attendance department as requiring a work center, even when the employee master record identifies the employee as Transportation.

The production examples that established the required behavior were:

- Trent's 6:32 AM attendance was Supervisor.
- Trent's 6:09 AM attendance was Maintenance.
- Gerald's 5:45 AM Friday attendance had no attendance department, while Gerald's Odoo employee record identified him as Transportation.

## Considered approaches

### 1. Shared local department resolution — selected

Mirror each employee's Odoo home department locally. Resolve an effective department from attendance department first and employee home department second, then apply the same saved department policy in both inbox paths.

This keeps request handling local, avoids new Odoo calls on the inbox hot path, fixes both the legacy and timeline implementations, and gives one durable rule to test.

### 2. Enrich only the legacy cache

The legacy warmer could make another Odoo employee lookup and suppress exempt rows before caching them. This would fix the current screenshot but leave the newer timeline with separate behavior and would add a recurring network dependency.

### 3. Suppress named employees

Specific employees could be excluded or their current rows dismissed. This would not protect new employees, future attendance records, or department transfers and would encode personnel names instead of the business rule.

## Architecture

### Employee department mirror

Add a nullable department-name column to the local `people` table. Request `department_id` in the existing active-employee Odoo fetch and persist its display name during the normal roster sync. A missing Odoo department remains `NULL`.

The roster sync remains the owner of employee master data. Inbox requests do not contact Odoo.

### Effective department resolution

Add one pure resolution rule:

1. Normalize and use the attendance department when it is nonblank.
2. Otherwise normalize and use the employee home department when it is nonblank.
3. Otherwise return no department.

Numbered Odoo labels continue to normalize to the clean local department name before policy lookup. An attendance department must take precedence so a temporary cross-department assignment is classified by the work performed that day rather than by the employee's home team.

### Legacy Missing Work Center path

Include the configured attendance-department field in the existing missing-work-center Odoo fetch and cache the normalized department display name with each attendance row. Pass each cached attendance department and the locally synced employee home department through the effective-department rule.

Suppress a row only when the resulting saved policy says that department does not require a work center. Continue applying the existing active, hourly, monitoring-boundary, resolved-row, and locally-unmapped filters unchanged.

### Attendance timeline path

Before projecting mirrored attendance rows, fill only blank attendance department names from the local employee home-department map. Do not replace a department already stored on the attendance. The existing timeline policy and `exempt_no_location` status then handle the row without changing the projection API.

### Department defaults and migration

Change the default exempt set to Maintenance, Transportation, and Supervisor. Update the idempotent schema bootstrap so an existing department row is changed to `requires_work_center = FALSE` only when `requires_work_center_explicit = FALSE`.

This updates today's non-explicit Transportation row while preserving any deliberate administrator override. New Odoo department syncs use the same default helper.

## Data flow

```text
Odoo hr.employee.department_id
        |
        v
people.department_name --------------------+
                                           |
Odoo attendance department --> attendance-first resolver
                                           |
                                           v
                              departments.requires_work_center
                                           |
                         +-----------------+-----------------+
                         |                                   |
                  legacy Missing WC                 attendance timeline
                         |                                   |
                         +------------> Exception Inbox <----+
```

## Failure and safety behavior

- If Odoo roster sync is temporarily unavailable, the last successfully mirrored home department remains available.
- If the attendance department field is unavailable, the employee home department can still supply the fallback.
- If neither source identifies a department, the row remains actionable instead of being silently suppressed.
- Existing resolved or dismissed attendance rows remain resolved.
- The fix does not write to Odoo attendance, dismiss inbox items, or change employee records in Odoo.
- A later employee department transfer can affect the fallback classification of an older attendance whose own department was blank. This accepted tradeoff is limited to records missing their authoritative attendance department.

## Test design

Add focused regression coverage for:

- Maintenance, Transportation, and Supervisor default to not requiring a work center, including numbered Odoo labels.
- Explicit administrator department choices survive schema bootstrap and department sync.
- Employee Odoo fetch and roster sync retain the home department.
- Attendance department wins over a different employee home department.
- Blank attendance department falls back to the employee home department.
- Trent-like Maintenance and Supervisor rows are suppressed by the legacy inbox.
- A Gerald-like blank attendance with a Transportation home department is suppressed.
- Production and fully unknown departments remain actionable.
- Timeline projection uses the home-department fallback only for blank attendance departments.

Focused tests must be observed failing before implementation. After they pass, run the related missing-work-center, Odoo sync, attendance timeline, department-policy, and Exception Inbox suites, followed by the complete project test and lint commands used by the repository.

## Deployment result

Schema bootstrap adds the nullable employee department column and updates only non-explicit Transportation policy rows. The next normal Odoo roster sync fills employee home departments. The missing-work-center warmer refreshes its cache with attendance departments, and exempt false positives disappear without a manager assigning or dismissing them.

New What's New text will explain in plain language that Maintenance, Transportation, and Supervisor employees no longer receive work-center alerts when they do not need a work center.
