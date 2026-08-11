# Odoo + Postgres Setup

The dashboard runs against two backends:

- **Postgres** — owns app state (people, schedules, work-center config, layouts, customizations, sync state). Survives deploys.
- **Odoo** — source of truth for HR data (employees + skills). Synced into Postgres on a 1-hour TTL.

## Required env vars (Railway)

Set these on the **web** service → Variables:

- `ODOO_URL` — base URL like `https://gruber-pallets.odoo.com` (no `/odoo`)
- `ODOO_DB` — database name. For Odoo.sh this is the long form like
  `odoo-ps-psus-gruber-pallets-production-30069198`. To find it, hit
  `https://<your-subdomain>.odoo.com/web/database/list` (no auth needed)
  and use the value returned.
- `ODOO_LOGIN` — username (email)
- `ODOO_API_KEY` — API key from Odoo Settings → Users → Account Security → New API Key
- `DATABASE_URL` — auto-injected by Railway when you reference the Postgres add-on; do NOT set manually

Never commit any of the above.

## Attendance work-center field

`ODOO_KIOSK_WC_FIELD` is optional. When this release is deployed, set it to
`x_studio_work_center`, an `hr.attendance` Many2one field that links to
`mrp.workcenter` (Odoo Manufacturing Work Centers).

Before enabling it, map every kiosk-selectable app work center to one active
Odoo Manufacturing Work Center in Settings. Enable the Railway variable only
with this release, because the application and the setting must go out together
so the app can write Odoo work-center IDs safely.

### Production rollout checklist

After deployment, complete every check below before treating the rollout as
complete:

```text
[ ] Every kiosk-selectable app work center has one active Odoo Manufacturing Work Center mapping.
[ ] Railway's web service has ODOO_KIOSK_WC_FIELD=x_studio_work_center.
[ ] A normal kiosk clock-in shows the selected Odoo Work Center on hr.attendance.
[ ] A kiosk transfer creates the new attendance with its selected Odoo Work Center.
[ ] An Auto-Lunch return creates the afternoon attendance with the pre-lunch Work Center.
[ ] The Missing Work Center inbox remains empty after the three checks.
```

## First-time setup

1. **Add the Postgres add-on** in Railway: + New → Database → PostgreSQL.
2. **Reference `DATABASE_URL`** into the web service: web service → Variables → + New Variable → Add Reference → Postgres → DATABASE_URL.
3. **Set the four `ODOO_*` env vars** as above.
4. Push code. Schema bootstraps automatically on app startup.
5. **Migrate existing JSON state into Postgres** (one-shot):
   ```powershell
   $env:DATABASE_URL = "<paste from Railway → Postgres → DATABASE_PUBLIC_URL>"
   python -m scripts.migrate_json_to_postgres
   ```
   Reads the local JSON files (`roster.json`, `schedules/`, `work_centers.json`, `settings.json`, `layouts.json`, `widget_customizations.json`, `schedule.json`, `skill_filter.json`) and bulk-inserts into Postgres.

## Skill type setup in Odoo

Two skill types feed the People Matrix: **"Production Skills"** and **"Supervisor Skills"**. Skills under those types become matrix columns. Levels are bucketed to 0–3 by rank within each type.

- To add a column: add a skill to one of those types in Odoo.
- To add a level: add it under the type in Odoo. Bucket math re-distributes 0–3 across all levels.

## Routine refresh

- Auto-refresh from Odoo runs on first `/staffing/skills` GET past the 1-hour TTL.
- "Refresh from Odoo" button on the matrix forces an immediate sync.
- Sync writes to the `people`, `skills`, `person_skills` tables. The local `reserve` flag is preserved.

## Persistence behavior

All saves now hit Postgres. Reserve toggles, scheduler drafts, custom hours, settings — all survive deploys. The previous JSON-file model wiped on every redeploy; that's gone.

## What if Odoo is down?

The dashboard serves the cached Postgres data with a warning banner in the sync header. Refresh button retries on demand. The app continues to work for everything except pulling fresh employee/skill changes from Odoo.

## What if Postgres is down?

The app fails to boot until Postgres comes back. Routes return 500 on DB errors. Mitigation: Railway's managed Postgres has high uptime; check Railway status if you see persistent 500s.

## Two-way sync (planned)

The schema includes `odoo_id`, `last_pulled_at`, `last_pushed_at`, `local_dirty` columns and a `sync_outbox` table — all reserved for the eventual two-way sync (writes back to Odoo for people, skills, time off, etc.). Not yet wired up. The outbox will be drained by a background worker that pushes changes to Odoo on a periodic interval.
