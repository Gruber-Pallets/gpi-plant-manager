# Past-Absence PTO in the Exception Inbox

**Date:** 2026-09-01  
**Status:** Approved design

## Problem

An employee can successfully request one PTO day for a recorded past absence, but the request does not appear in the Exception Inbox. Managers therefore have to know that the request is waiting at the bottom of the separate Time Off page.

The live August 31 request proved the gap: it exists in `absence_pto_requests`, is pending, has a known sufficient balance, and has a valid approval action, while the live Exception Inbox reports zero pending time-off items. The Inbox currently reads only ordinary rows from `time_off_requests`.

## Goal

Put every open past-absence PTO request in the existing **Pending Time Off** Inbox section. A manager must be able to finish the appropriate action without leaving the Inbox.

## Non-goals

- Do not change employee eligibility, PTO balances, pay-period rules, or Odoo conversion behavior.
- Do not approve, deny, or mark a request handled automatically.
- Do not remove the Time Off page's existing approval panel.
- Do not combine ordinary Odoo time-off rows with past-absence PTO storage.

## Manager experience

### Pending request

The Inbox row shows:

- employee name;
- missed date;
- **Past absence PTO** label;
- **1 PTO day** and **Waiting for approval** detail;
- **Approve** and **Deny** actions.

Approve uses the existing past-absence PTO approval endpoint. Deny first reveals a required reason field and then uses the existing denial endpoint.

### Payroll-review request

When a request is in `needs_review`, the row stays visible and says that payroll review is needed. Approve and Deny are replaced with a required note field and **Mark handled**, using the existing manual-resolution endpoint.

### Action results

- A successful approval, denial, or manual resolution removes the row and refreshes the shared Inbox count.
- If approval moves the request to payroll review, the page reloads so the row changes to the correct **Mark handled** state.
- A temporary or revalidation failure leaves the row visible and shows the server's safe warning or error.
- Buttons and inputs are disabled only while an action is running.

## Inbox data design

Past-absence PTO remains a separate local-mirror source. `exception_inbox.py` will add a bounded loader and count query for `absence_pto_requests` states `pending` and `needs_review`.

The ordinary and past-absence counts are added together for:

- the **Pending Time Off** section;
- the Inbox total;
- the shared navigation count;
- the JSON summary used by polling.

The two row lists are merged into the existing section and sorted deterministically. Each source may contribute up to the Inbox's bounded row limit so a busy ordinary time-off queue cannot hide every past-absence request.

Past-absence rows use their own stable identity, `absence_pto:<request id>`, matching the audit events already written by the approval, denial, and manual-resolution workflows. Their action metadata uses a distinct `absence_pto` type so browser code cannot accidentally call an ordinary time-off endpoint.

## Failure isolation

The Inbox continues to render from local PostgreSQL mirrors and makes no new Odoo call while loading. Ordinary time off and past-absence PTO are captured separately. If either source cannot be read, the page shows that source as unavailable instead of reporting a false all-clear or hiding the other source.

All manager mutations continue through the existing authenticated routes and domain services. Those services re-check the request, balance, pay period, source absence, and Odoo state before changing anything.

## Files expected to change

- `src/zira_dashboard/exception_inbox.py`
- `src/zira_dashboard/inbox_keys.py`
- `src/zira_dashboard/templates/exceptions.html`
- `src/zira_dashboard/static/exceptions.js`
- focused Exception Inbox tests
- `CHANGELOG.md`

The existing `absence_pto_admin.py` routes and conversion services should not require behavior changes.

## Verification

Automated coverage will prove:

1. pending and payroll-review past-absence requests are counted;
2. their Inbox rows have stable keys, clear copy, and the correct action metadata;
3. ordinary and past-absence rows share the Pending Time Off section without hiding either source;
4. summary and navigation counts include both sources;
5. pending rows render Approve and Deny, with a required denial reason;
6. payroll-review rows render Mark handled, with a required note;
7. browser actions call only the existing past-absence endpoints and preserve failed rows;
8. ordinary time-off Inbox behavior remains unchanged;
9. the focused test set, full suite, lint, and JavaScript syntax checks pass.

After deployment, a read-only production Inbox snapshot must show the August 31 request with an `absence_pto` action. The manager remains responsible for pressing Approve or Deny.
