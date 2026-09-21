# GPI Plant Manager

Plant operations platform for Gruber Pallets: a daily staffing scheduler,
work-center dashboards, recycling production goals, leaderboards/recognition,
and a timeclock kiosk. Server-rendered FastAPI + HTMX, backed by Postgres,
wired to the Zira.us telematics API (live production metrics) and Odoo (HR:
employees, skills, attendance, time-off). Deployed on Railway.

## Stack

- **Web:** FastAPI + uvicorn, Jinja2 templates, HTMX (server-rendered HTML).
- **Data:** Postgres (single source of truth) via psycopg2.
- **Integrations:** Zira.us API (production), Odoo XML-RPC (HR), Slack
  (schedule share), Microsoft Entra ID (OIDC login), Playwright (PDF render).

## Setup

Requires Python 3.11+ and a Postgres database.

```bash
pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and fill in the values (Postgres `DATABASE_URL`,
Odoo + Zira credentials, auth secrets, etc.). The schema is created
automatically on startup by `db.bootstrap_schema()`.

### Auto salaried punch

Salaried (Fixed Wage) employees get automatic attendance punches
(6:00–11:00 and 11:30–15:30, department "Sustaining") so their sustaining/
maintenance hours are trackable — see
`docs/superpowers/specs/2026-08-26-auto-salaried-punch-design.md`.

- `AUTO_SALARIED_DRY_RUN=1` — simulate: log intended punches, write nothing.
- `AUTO_SALARIED_ENABLED=1` — live. Dry-run wins if both are set.
- Requires `ODOO_KIOSK_DEPARTMENT_FIELD` for department tagging and an Odoo
  `hr.department` whose name contains "Sustaining".
- "Needs a human" flags: `/auto-salaried/flags`.
- Flip modes (dry-run ↔ live) outside plant hours (before 6:00 AM or after
  3:30 PM Central); mid-day flips can strand an open attendance or produce
  noise flags.
- If the Sustaining department was missing in Odoo and you create it,
  restart/redeploy the app — the department lookup caches misses for the
  process lifetime.

## Running

```bash
zira-dashboard                      # console script
# or: uvicorn zira_dashboard.app:app
```

Set `AUTH_DISABLED=1` to bypass login during local development.

### Users and access

Personal sign-in requires an active invitation as well as a
`@gruberpallets.com` Microsoft account. On the first database migration,
`dale@gruberpallets.com` becomes Owner/Admin. Other domain accounts receive
no automatic access. Existing TV displays keep their current access.

Open **Users & Access** in the main menu to grant access to an exact email,
choose its role, or revoke access. Copy the sign-in link shown there and
share it with the person; granting access does not send an email.

| Role | Access |
| --- | --- |
| Owner/Admin | All pages and actions, including users and system settings |
| HR | Operations and sensitive employee information, including HR edits |
| Manager | Operations and scheduling edits, without payroll, leave details, or employee notice history |
| Visitor | Read-only operational dashboards |

Role changes and revocation take effect on the next request. The last active
admin cannot be removed or demoted. Restarts never restore revoked access.
Keep `AUTH_DISABLED` unset in production: it is a development bypass of all
personal sign-in and permissions. `SUPER_ADMIN_UPNS` no longer grants access
or controls roles for authenticated users; the database role is authoritative.

The bearer-authenticated object API retains its separate credentials. Only
admins can manage those credentials. New personal endpoints must be listed
in `permissions.py`; non-admin access is denied until explicitly classified.

## Tests

```bash
pytest -v
```

Pure-logic tests run anywhere; tests that touch Postgres are skipped unless
`DATABASE_URL` is set.

### Optional Saturday recruiting

When optional Saturday work is needed, open the Saturday Scheduler, turn on
the work centers you plan to run, and click the blue **Recruit for X work
centers** action. Each enabled center requests its configured minimum crew and
the offer immediately appears in Timeclock. Accepted people appear in
**Unassigned** for normal assignment; green **Publish** remains the final
step. Recruiting closes automatically at the nearest prior workday's start
time. Partial commitments use 30-minute increments. Employees whose exact
Spanish skill level is 3 see personalized Timeclock screens Spanish-first.

## Layout

- `src/zira_dashboard/` — the app. `routes/` holds feature routers;
  `*_store.py` = Postgres-backed persistence; `*_sync.py` = external sync
  (Odoo); `*_client.py` = API clients.
- `src/zira_probe/` — standalone Zira API capability-probe CLI; its
  `client.py` is also the dashboard's Zira client.
- `docs/object-api.md` — server-to-server Odoo-like API for internal apps.
- `docs/odoo-2s-feedback-operations.md` — approval and safety runbook for the
  one-way shared feedback mirror.
- `docs/superpowers/` — design specs and implementation plans.

## Automatic schedule rotations

The scheduler can auto-build enabled work centers with safe, explainable
suggestions. Day-to-day manager workflow:

1. **Set scheduling preferences.** On the People Matrix, open each person's
   Scheduling Preferences icon. It lists only the qualified grouped and
   standalone targets; choose `primary`, `regular`, `occasional`, or `never`
   (missing means `regular`). Those choices influence enabled **Auto** work
   centers alongside skill level and rotation history.
2. **Choose the Auto work centers.** On Staffing, each work center has an
   **Auto** checkbox. The first run initializes these from recently used
   schedules; after that, the checked list is saved globally until changed.
3. **Pick a goal, then rebuild.** Choose **Optimized** (favor strongest
   coverage), **Normal** (balance coverage, preference, and rotation history —
   the default), or **Training** (develop level-1/2 operators paired with a
   green) before rebuilding the enabled Auto work centers.
4. **Review, then adjust.** Generated picks can show reason badges for useful
   context like primary operator, training pair, or least-recent center. Green
   names do not get a redundant badge. Manual assignments and saved default
   people are locked and survive rebuilds.
5. **Start training.** On Staffing, use **Training** below Notes. Choose the
   trainee, a level 3 trainer, the exact work center, start date, and attended
   days. The trainee can already have some required skills as long as at least
   one is missing. The trainer needs level 3 in every skill being taught. Both
   people must already hold any required certificates.
6. **Schedule and follow progress.** Turn on Auto for the selected work center.
   On day one (the first attended day), the scheduler reserves the trainee and
   trainer together. Later days reserve the trainee. Add the trainer manually when continued pairing
   is needed. If scheduling fails, the saved plan stays visible. Correct the
   schedule warning and click **Retry scheduling**, rather than starting again.
   Missed, conflicting, and paused days do not consume attended days. Use
   **Edit**, **Pause**, and **Resume** on the card as needed. Once days have
   been earned, start a new plan to change the work center or start date.
7. **Finish and verify.** The plan finishes after its last attended day, or you
   can click **Complete** early. Only missing skills rise to level 1; higher
   skills and certificates stay unchanged. Check the **Skills Matrix** afterward.
   **End** stops the plan without raising skills. If saving to Odoo fails, the
   plan stays open with a retry message. The explicit **Posted** view is read-only;
   use **Draft** to manage training.

### Automatic Repair and Dismantle skill levels

Repair and Dismantle levels can update themselves from production. On the
**People Matrix**, hover the **Repair** or **Dismantle** header and click the
settings gear. Each group keeps its own thresholds — level 3 defaults to 90% of
goal, level 2 to 80%, and level 1 to 70%; anything lower is level 0. The modal
previews how many units per day each threshold works out to for every work
center, both for a solo operator and for two people sharing a center.

Scoring looks back 30 calendar days. A person needs at least two days with four
or more hours in the group before automation will move their level; on each day
the center's goal and output are split equally among that day's operators, and
partial days are normalized to a full shift before comparing to goal. **Save &
Recalculate** applies the new thresholds immediately, and a daily run after the
shift ends keeps eligible employees in sync. Every change is written to Odoo
first, so a rejected write leaves the level unchanged and is reported in the
run summary. Manual matrix edits still work; a later automated run may promote
or demote the same two skills.

### Time-off approval emails

New final approvals of current or future time off send an email with the approved
dates and hours in English and Spanish. Plant Manager uses the employee's personal
email in Odoo, then their work email if no valid personal address is saved. Keep
these addresses current in Odoo. The time-clock notices continue to work.

Approvals in Plant Manager and Odoo are covered, including the local approval
fallback. Pending first approvals, denied or cancelled requests, past leave, and
historical imports do not send approval emails. Each request gets at most one
approval email. The message leaves out the leave type and private notes.

Operators can set `TIME_OFF_APPROVAL_EMAIL_RECIPIENT` to `personal_then_work`
(default), `personal`, or `work`. `TIME_OFF_APPROVAL_EMAIL_ENABLED=0` pauses the
worker; approvals continue to be saved for processing when it resumes. It does
not recall mail already accepted by Odoo. Delivery uses the configured Odoo user's
default sender address, displayed as **GPI Plant Manager**, and Odoo's normal mail
queue. The API user needs read access to the exact employee's email fields and
read/create access to `mail.mail` (plus cancellation access for stale queued mail).

The `time_off_approval_email` table records each request's delivery status. The
worker checks it every minute. `queued` means Odoo accepted the email; `sent`
means Odoo reports it sent, not proof that it reached the inbox. Missing addresses
retry hourly; read failures and definite queue rejections retry after five minutes.
Odoo delivery failures remain `attention` and must be reviewed in Odoo's outgoing
mail list. Logs include request IDs and fixed reasons, never email addresses or
message bodies. A useful read-only status query is:

```sql
SELECT request_id, state, last_error, odoo_mail_id, updated_at
FROM time_off_approval_email
WHERE state NOT IN ('sent', 'skipped')
ORDER BY updated_at;
```

An uncertain queue response stays `sending` with `delivery_unknown`. The worker
looks for its stable Message-ID every 15 minutes without creating another email.
Investigate that exact message in Odoo before taking further action. Do not reset
or delete the delivery row to retry an uncertain send. Odoo mail records are kept
after delivery so restarts can read back the same message.
