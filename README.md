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

## Recycled rotations

The scheduler can auto-build the Recycled area (Dismantler, Repair, Trim Saw)
with safe, explainable suggestions. Day-to-day manager workflow:

1. **Set preferences.** On the People Matrix, give each person a Dismantler,
   Repair, and Trim Saw preference — `primary`, `regular`, `occasional`, or
   `never` (missing means `regular`).
2. **Pick a goal, then rebuild.** On Staffing, choose **Optimized** (maximize
   level-3 coverage), **Normal** (balance coverage, preference, and rotation
   history — the default), or **Training** (develop level-1/2 operators paired
   with a green) before rebuilding a Recycled schedule.
3. **Review, then adjust.** Each generated pick shows a reason badge; watch for
   the warning banner. Make manual changes as needed — manual assignments are
   locked and survive every rebuild.
4. **Start a training block carefully.** A level-0 block requires a green
   (level-3) day-one trainer; the trainer pairs in on day one and the trainee
   works solo on later attended days. A full-day absence does not consume a
   training day, so the block extends automatically.
5. **Confirm promotion.** After the final attended day, the trainee is promoted
   from level 0 to level 1 in the target skill automatically — verify it landed
   on the People Matrix.
