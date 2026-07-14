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

## Running

```bash
zira-dashboard                      # console script
# or: uvicorn zira_dashboard.app:app
```

Set `AUTH_DISABLED=1` to bypass login during local development.

## Tests

```bash
pytest -v
```

Pure-logic tests run anywhere; tests that touch Postgres are skipped unless
`DATABASE_URL` is set.

## Layout

- `src/zira_dashboard/` — the app. `routes/` holds feature routers;
  `*_store.py` = Postgres-backed persistence; `*_sync.py` = external sync
  (Odoo); `*_client.py` = API clients.
- `src/zira_probe/` — standalone Zira API capability-probe CLI; its
  `client.py` is also the dashboard's Zira client.
- `docs/object-api.md` — server-to-server Odoo-like API for internal apps.
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
5. **Start a training block carefully.** A level-0 block requires a green
   (level-3) day-one trainer; the trainer pairs in on day one and the trainee
   works solo on later attended days. A full-day absence does not consume a
   training day, so the block extends automatically.
6. **Confirm promotion.** After the final attended day, the trainee is promoted
   from level 0 to level 1 in the target skill automatically — verify it landed
   on the People Matrix.

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
