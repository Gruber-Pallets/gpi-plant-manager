# GPI Plant Manager — StratusTime API Integration

**Contact:** Dale Gruber, Gruber Pallets (dale@gruberpallets.com)
**Date prepared:** 2026-05-15

This document describes the StratusTime web-services endpoints that the GPI Plant Manager (a custom internal dashboard for Gruber Pallets, hosted on Railway) calls during normal operation, with the exact date parameters and call frequency. Shared with the StratusTime engineering team in response to a request for context on date-handling errors we may be triggering.

---

## Identity / auth

- **App name:** GPI Plant Manager (custom internal tool)
- **Customer alias:** `gruberpallets`
- **Web-services user:** `wsuser`
- **Hosting:** single Railway worker (one process); no horizontal scaling
- **Auth flow:**
  - `POST /service/ws-json/2.0/CreateToken` with body
    ```json
    {
      "CustomerAlias": "gruberpallets",
      "CustomerAliasExternal": "",
      "SharedKey": "<UUID>",
      "UserName": "wsuser",
      "UserPass": "<password>"
    }
    ```
  - Returned token is cached in-process for 30 minutes and refreshed on expiry or on a non-2xx response from any downstream call.

## Date format

All `StartDate`, `EndDate`, and `EffectiveDate` parameters are sent in Microsoft WCF date format:

```
/Date(<epoch_ms>+0000)/
```

where `<epoch_ms>` is the **UTC-midnight** of the target date as Unix epoch milliseconds. Example for 2026-05-15: `/Date(1747008000000+0000)/`.

The code that builds these strings (Python 3.12):

```python
from datetime import datetime, timezone

def _wcf_date(epoch_ms: int) -> str:
    return f"/Date({epoch_ms}+0000)/"

def _epoch_ms(d):  # datetime.date
    dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)
```

## Endpoints called

All endpoints are v2.0 unless noted. `SELECT-ALL` / `SELECT-EMPID` refer to the `DataAction.Name` field on each request.

| Endpoint | Frequency | Date parameters | Purpose |
|---|---|---|---|
| `CreateToken` | every ~30 min (token refresh only) | none | auth |
| `PingTest` | on health-check page only (rare) | none | connectivity check |
| `GetUserBasic` (SELECT-ALL) | every 5-30 min via cache + warmer | `EffectiveDate` = now | full employee roster |
| `GetUserTimeOffRequest` (SELECT-ALL) | every ~3 min via warmer + on user demand | `StartDate` = today − 3 days (UTC midnight)<br>`EndDate` = today + 3 days (UTC midnight) | approved time-off requests in a 7-day rolling window centered on today |
| `GetUserSchedule` (SELECT-ALL) | every ~60 s when computing derived absences | `StartDate` = today (UTC midnight)<br>`EndDate` = today + 1 day (UTC midnight) | who is scheduled to work today |
| `GetUserTimeOnStatusBoard` (SELECT-EMPID with list of `EmpIdentifier`s) | every 45 s | none (returns latest-punch state) | current attendance / clock-in status |
| `/service/ws-json/1.0/TimeGetPunchesByEmpIdentifier` (v1 — no v2 equivalent we could find) | every ~60 s + on user demand | `StartDate` = today − 3 days<br>`EndDate` = today + 3 days<br>`AfterModifiedOnDate` = `/Date(0+0000)/` (epoch zero) | "manual non-work shift" entries that are not exposed by `GetUserTimeOffRequest` |

### Background warmers (our side)

The Plant Manager process runs three async loops to keep caches warm:

1. **45-second loop** — refreshes attendance (`GetUserTimeOnStatusBoard`) and a composite "time-off for today" payload (which fans out to `GetUserTimeOffRequest` + `TimeGetPunchesByEmpIdentifier`).
2. **3-minute loop** — re-warms `GetUserBasic` and `GetUserTimeOffRequest` for today.
3. **30-second loop** — talks to Zira (the production meter API), not StratusTime. Listed here only to confirm it's not us.

A 12-worker `ThreadPoolExecutor` fans out the three time-off / non-work / schedule fetches in parallel when a request handler needs all three together. So you may see up to ~5 concurrent requests from us in a burst, then ~45 s of quiet.

## Example request body

`POST /service/ws-json/2.0/GetUserTimeOffRequest` for today = 2026-05-15:

```json
{
  "AuthToken": "<token>",
  "StartDate": "/Date(1746748800000+0000)/",
  "EndDate":   "/Date(1747267200000+0000)/",
  "DateTimeSchema": 0,
  "IgnoreDeletedRequests": true,
  "IgnoreDetails": false,
  "DataAction": {"Name": "SELECT-ALL", "Values": []}
}
```

## Possible sources of "date errors" worth flagging

1. **UTC midnight, not local midnight.** We send epoch ms representing UTC midnight of the target date. If StratusTime is interpreting these in the customer's local timezone (America/Chicago, UTC−5/−6), the effective day could shift by one calendar day depending on DST.
2. **±3-day padding.** Most requests for "today" actually send a 7-day window. If the error fires on a particular calendar offset (e.g. day-of-week edge cases) inside that window, our logs can show which ones.
3. **`TimeGetPunchesByEmpIdentifier` is the v1 endpoint.** We could not find a v2 equivalent that exposes the "manual non-work shift" rows. If a v2 endpoint exists for those, we'd switch.
4. **`AfterModifiedOnDate = /Date(0+0000)/`** on the v1 punches call — epoch zero. If your backend doesn't like that, we can send a more recent floor.

## Next steps

If your team can share which endpoint + which date parameter is producing the errors (and ideally a request timestamp or `CustomerAlias` hit), we can correlate it against our logs and replay the exact request body. Happy to wire up a request-body capture toggle on our side for a few hours if that would help.

— Dale
