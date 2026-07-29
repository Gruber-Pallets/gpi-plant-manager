# GOAT Slack Celebrations — Design

**Date:** 2026-07-29  
**Status:** Approved for planning  
**Destination:** `#MGMT-Sups`

## 1. Goal

After a production shift is finalized, Plant Manager will automatically post a
short celebration to `#MGMT-Sups` for each newly established GOAT record. The
post celebrates the person, identifies the work center and final pallet total,
shows when the new and previous records were set, and prompts supervisors to
congratulate the person.

The post must use final production data only. It must not appear while a shift
is in progress, and it must not be duplicated after an application restart or
a temporary Slack failure.

## 2. GOAT categories

The day-close job will evaluate these five production categories:

| Department | Category | Included work centers |
| --- | --- | --- |
| Recycling | Repairs | Repair 1–5 |
| Recycling | Dismantlers | Dismantler 1–4 |
| New | Juniors | Junior 1–3 |
| New | Woodpecker | Woodpecker #1 |
| New | Hand Build | Hand Build #1, Hand Build #2, Big Build #1 |

The category definitions will be centralized and reuse the same work-center
membership rules as the existing Recycling and New GOAT leaderboards. This
prevents the dashboard and Slack from computing different records.

All five categories are enabled from the first release. A category is skipped
when it has no usable Zira camera production data; it begins participating
automatically as mapped work centers receive data. At launch, this means
Repairs, Dismantlers, and Juniors are expected to be the only functional
sources. No setting or deployment change is needed later to activate
Woodpecker or Hand Build.

## 3. Finalization and winner selection

### Day-close job

Plant Manager will run a lightweight, in-process day-close check after the
configured shift end. It will run periodically and again when the application
starts, so an app restart or a short outage cannot lose a record. It replaces
the current dashboard-visit-only trigger for the Slack path: no one needs to
open a dashboard for the post to happen.

For each eligible category, the job:

1. Reads the completed day's production and scheduled credited operator.
2. Finds the category's best final person/work-center result.
3. Compares it with the all-time record while excluding the completed day.
4. Creates a new GOAT alert only when the final total is strictly greater than
   the previous record; a tie is not a new GOAT.
5. Creates a pending Slack delivery for that new alert.

At most one celebration is created for a category on a day. If more than one
work center exceeds the old record, the final category winner is celebrated.
Existing GOAT tie-breaking determines a deterministic single winner for an
equal final total.

Existing dashboard banners continue to display NEW GOAT alerts. Slack is an
additional surface, not a replacement for the banner or its dismiss control.

## 4. Reliable Slack delivery

### Configuration

Add a dedicated deployment setting, `GOAT_SLACK_CHANNEL_ID`, containing the
channel ID for `#MGMT-Sups`. It is intentionally separate from
`SLACK_CHANNEL_ID`, which remains the destination for staffing schedule PDFs.
The existing bot token is reused; the bot must be a member of `#MGMT-Sups` and
have its existing `chat:write` scope.

### Outbox

Each new GOAT alert receives one durable delivery row, uniquely keyed to that
alert. The row records pending/sent status, Slack's message timestamp and
permalink when available, retry attempts, and the most recent delivery error.

The day-close job posts pending deliveries using Slack's `chat.postMessage`.
On a network or Slack API failure it saves the failure and retries later. A
successful delivery is never posted again, including after process restarts or
when more than one app process reaches the check at the same time. New delivery
rows are created only for alerts finalized after this feature is deployed;
pre-existing banner alerts are not back-posted to Slack.

## 5. Slack message design

Use Block Kit so the new record is visually dominant on desktop and mobile:

1. A `header` block for the announcement. Slack renders headers larger.
2. A `section` block for the person, final pallet count, work center, and new
   record date. The person and pallet count are bold.
3. A `context` block for the previous record. Slack treats this as secondary
   information; the application cannot set arbitrary font sizes or colors.
4. A short closing section encouraging a personal congratulations.

Example:

> 🏆 NEW REPAIRS GOAT!  
> **Jose O.** — **898 pallets** at Repair 3 on Jul 28, 2026  
> *(previous = Jose Ochoa · 891 · Jun 10, 2026)*  
> Congratulate Jose when you see them! 🎉

The application sends a complete plain-text fallback alongside the blocks so
notification previews and assistive technology contain the same information.
Names are displayed as roster names, not Slack @mentions; this keeps the
celebration focused and avoids requiring a separate person-to-Slack-user map.

## 6. Error handling and observability

- A missing GOAT channel ID, missing bot token, Slack API rejection, or network
  timeout never prevents GOAT finalization or the dashboard banner.
- Each failed post remains pending with its error for retry and application-log
  visibility.
- The normal day-close run retries due deliveries without creating another GOAT
  alert or another delivery row.
- Malformed or unavailable production data causes only that category to be
  skipped for the run; other categories continue.

## 7. Validation

Automated tests will cover:

- category membership and automatic eligibility as camera data becomes
  available;
- final totals, strict-greater-than behavior, previous-holder/total/date, and
  deterministic same-day winner selection;
- no post before finalization and one post after finalization;
- message blocks and complete accessibility fallback text;
- successful posting, Slack errors, retry behavior, and no duplicate posts
  across repeated checks or a simulated restart;
- isolation from the staffing PDF Slack channel and from existing dashboard
  banner dismissal behavior.

## 8. Out of scope

- Posting a live, mid-shift contender alert.
- Backfilling Slack posts for historical records or existing visible alerts.
- Slack @mentions, reactions, interactive buttons, or a manual resend UI.
- Forklift GOAT notifications; that recognition path remains separate until it
  has a finalized GOAT-alert source.
