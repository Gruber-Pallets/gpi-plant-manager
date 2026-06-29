# Runbook: move app + DB from EU West → US (fixes site-wide slow page loads)

## Why
The `web` app and its Postgres are deployed in **Railway EU West (Amsterdam)**, but
**every user and dependency is in the US**:

| Component | Location | Evidence |
|---|---|---|
| Shop-floor users / kiosks | Lake Elmo, **Minnesota** | `65.144.157.18` in access logs |
| Odoo (HR / attendance / time-off) | Council Bluffs, **Iowa** (GCP) | `gruber-pallets.odoo.com` → `34.16.105.31` |
| Zira API (production meters) | Boardman, **Oregon** (AWS us-west-2) | `api.zira.us` → `44.239.190.246` |
| Railway edge PoP | **San Francisco** | `69.46.46.94` |
| **App + Postgres** | **EU West / Amsterdam** ❌ | `railway status`; DB `postgres.railway.internal` |

A no-op `/healthz` measures **~170 ms TTFB** with only ~25 ms TCP connect → ~145 ms
is the round trip to the Amsterdam origin, paid on **every request**. Data pages then
cross the Atlantic again to Zira/Odoo. Moving app **and** DB (they must stay
co-located) to a US region collapses all three latency legs.

**Target region: US East (Virginia / `us-east4`)** — balanced for MN users, IA Odoo,
OR Zira. (US West is an alternative if Zira latency matters most; it's slightly worse
for the Minnesota users.)

## Known values (already gathered)
- Project: **GPI-Plant-Manager**, environment **production**
- App service: **`web`** (deploys from GitHub `dalesgruber/gpi-plant-manager`, domain `gpiplantmanager.com`)
- DB service: **`Postgres`**, database `railway`
  - private host `postgres.railway.internal:5432`
  - public proxy `mainline.proxy.rlwy.net:43228` (this is `OLD_URL`)
- `web.DATABASE_URL` currently a literal pointing at the private host.

## What does NOT change
- **Odoo / Zira config** — already US; calls just get faster.
- **Microsoft Entra (Azure AD) OIDC** — the custom domain `gpiplantmanager.com` follows
  the `web` service, so redirect URIs are unchanged. No auth reconfig.
- **DNS / custom domain** — attached to `web`, follows it across regions.
- **App code** — none. `db.bootstrap_schema()` is idempotent on boot.

---

## Step 0 — Prep (do ahead of the window, no downtime)
1. Install Postgres client tools (major ≥ the server's — Railway is currently PG 16):
   ```
   brew install postgresql@16
   echo 'export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
   pg_dump --version   # confirm 16.x+
   ```
2. Grab `OLD_URL`:
   ```
   ~/.local/bin/railway variables --service Postgres --kv | grep DATABASE_PUBLIC_URL
   ```
3. Check DB size (sets your downtime expectation):
   ```
   psql "<OLD_URL>" -tAc "select pg_size_pretty(pg_database_size('railway'));"
   ```
   (< 1 GB → dump/restore is a few minutes.)

## Step 1 — Pick the window
Off-shift, Minnesota time, when nobody is punching (overnight / before first punch).
Budget **30–45 min**. Notify that kiosks/TVs will be down briefly.

## Step 2 — Create the new US Postgres
- Railway → GPI-Plant-Manager project → **New → Database → Add PostgreSQL**.
- Set its **region to US East (Virginia)** *before/at* creation (volumes are
  region-pinned and can't be moved later). If Railway defaults it to EU West,
  set the project/workspace default region to US East first, then create.
- Open the new service → **Variables** → copy its `DATABASE_PUBLIC_URL` → this is `NEW_URL`.
- (Optional) rename the service, e.g. `Postgres-US`, so the reference variable is clear.

## Step 3 — Pause writes
- `web` service → **Settings** → pause the deployment (or remove the active deploy).
  This stops the background warmers and the kiosk, so no writes are in flight.
- Confirm no one is mid-punch.

## Step 4 — Migrate the data
```
OLD_URL='<EU public url>' NEW_URL='<US public url>' ./scripts/migrate_db_region.sh
```
Confirm the row-count sanity block shows matching old/new counts. Keep the `.pgc` dump.

## Step 5 — Repoint + move the app
- `web` → **Variables** → set `DATABASE_URL` to a **reference** to the new service
  (e.g. `${{Postgres-US.DATABASE_URL}}`), replacing the old literal. A reference
  auto-tracks the right private host.
- `web` → **Settings → Region** → **US East (Virginia)**.
- Re-enable / redeploy `web` (the region + var change triggers a fresh deploy in US).

## Step 6 — Verify (the proof it worked)
- `curl -w 'ttfb=%{time_starttransfer}s total=%{time_total}s\n' -o /dev/null -s https://gpiplantmanager.com/healthz`
  → TTFB should drop from ~0.17 s toward ~0.03–0.06 s.
- Log in (Azure AD) → load `/staffing`, `/recycling`, `/exceptions` → noticeably snappier.
- Do a **test kiosk punch** → confirm it lands in Odoo.
- `~/.local/bin/railway logs` → no `PoolError`, no "warmer tick failed" storms.

## Step 7 — Rollback (if anything is off)
Writes were paused during cutover, so the EU DB is still authoritative.
- Revert `web` `DATABASE_URL` back to the EU `Postgres` reference + region back to EU West, redeploy.
- No data lost.

## Step 8 — Decommission
After a few days of healthy US operation: delete the **EU `Postgres`** service and the local `.pgc` dump.

---

### Notes / gotchas
- App and DB **must** be in the **same US region** — moving only the app would make the
  10+ per-page DB queries transatlantic (worse). Keep them co-located.
- The new Postgres must be in the **same project + environment** as `web` so
  `*.railway.internal` private networking resolves.
- If `pg_restore` errors on a missing extension, create it on the new DB
  (`CREATE EXTENSION IF NOT EXISTS <name>;`) and re-run the restore.
- Independent, optional code win (not required for this migration): the inbox top-nav
  sub-caches (`_ASSIGNMENTS_TODO_CACHE`, `_LATE_REPORT_CACHE`, Zira `_TODAY_CACHE`) use a
  30 s TTL but `page_warmer.warm_once` runs every 45 s, so `exception_inbox.build_summary()`
  cold-recomputes on many page loads. Shorten the warmer interval to < 30 s or have the
  warmer prime those caches.
