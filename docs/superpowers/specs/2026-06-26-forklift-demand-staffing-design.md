# Forklift Demand & Staffing Integration — Stage 1 Design

- **Date:** 2026-06-26
- **Status:** Approved (brainstorm) — pending spec review
- **Author:** Dale + Claude
- **Topic:** Integrate the GPI Forklift app (`gpiforklift.com`) into the Plant Manager so the scheduler is aware of forklift demand and recommends how to staff it.

---

## 1. Background & Context

GPI runs a second app, **`gpiforklift.com`**, that operates a **forklift call-and-dispatch queue** for the plant. Stations on the floor press a button to call a forklift ("deliver pallets," "dump the tipper," "switchover," etc.). Forklift drivers claim calls off a priority queue and complete them. Every call is timestamped end-to-end (created → claimed → completed) and tagged with station, bay/area, request type, priority, driver, and a late reason.

The goal is to bring that demand and performance signal into the Plant Manager so that **when Dale staffs the next working day, the scheduler shows the expected forklift demand and recommends how many forklift-certified people to dedicate as drivers** (with backups for peaks). A later stage will surface forklift **performance** in the existing leaderboards/trophy case.

This spec covers **Stage 1** only (demand → staffing). Stage 2 (performance metrics) is a separate spec that reuses the foundation built here.

### Staffing model (confirmed with Dale)
- **Dedicated + backups.** A few forklift-certified people are dedicated drivers for the shift; additional certified people act as backups ("overload responders") who jump in at peak.
- **Coverage target: avoid overload/neglect.** "Enough drivers" means the queue rarely tips into *overload* or leaves calls *neglected* — both already measured by the forklift app.

### Existing Plant Manager facts this builds on
- FastAPI + Jinja2 + htmx; raw psycopg2 `ThreadedConnectionPool` (`db.py`); schema bootstrapped from `_schema.py`; background work via in-process asyncio **warmers** registered in `app.py` `_WARMERS`.
- People + skills + certifications sync from **Odoo**; production stats are snapshotted daily into **`production_daily`** by a warmer + backfill, and power leaderboards/trophies/GOAT.
- Forklift competencies already exist in the roster: graded production skills **`Forklift: Load/Jockey`** and **`Forklift: Tablets`**, plus a binary **`Forklift Certified`** certification (with a forklift badge icon).
- The scheduler (`/staffing`, `routes/staffing.py` + `templates/staffing.html`) assigns people to ~22 work centers (`LOCATIONS` in `staffing.py`). **Two of those work centers are the dedicated-driver slots:** `Location("Loading/Jockeying", "Forklift: Load/Jockey", "Forklift", "Supervisor", None)` and `Location("Tablets", "Forklift: Tablets", "Forklift", "Supervisor", None, max_ops=None)`.
- REST-integration pattern to copy: **`slack_client.py`** (env-var key, `requests`, explicit timeouts, `raise_for_status`, custom `*Error`). Secrets live in `.env` / Railway env, registered in `.env.example`.

---

## 2. Discovered API map (gpiforklift.com)

Discovered via a read-only probe (no source available; app is a Railway-hosted SPA). **The API is unauthenticated for reads** — every endpoint returned identical data with and without the key and with every common auth header tried. A handful of `/api/*` admin routes return `401 {"message":"Admin authentication required"}`. The key (`FORKLIFT_API_KEY`, prefix `gpifk__`) made no difference; it is stored and sent as a header anyway for future-proofing. **See the security note (§10).**

Base: `https://www.gpiforklift.com` (apex 301s to `www`). API under `/api/*`; unknown routes fall through to the SPA shell (HTML 200).

**Endpoints we will consume (read-only):**

| Endpoint | Shape | Notes |
|---|---|---|
| `GET /api/dashboard` | dict | `driverLeaderboard[]`, `stationLeaderboard[]`, `top5LongestCalls[]`, `workstationAvgs{avgClaim,avgService,avgTotal}`, `hourlyClaimAvgs[]`, `lateReasonCounts{}` — **all "today".** |
| `GET /api/queue/history` | list | All of **today's** calls (~400/day). **Date/range params are ignored** — today only. |
| `GET /api/queue/completed` | list | Today's completed calls (subset of history). |
| `GET /api/drivers` | list | 12 drivers: `{id, name, isOverloadResponder, skills[]}`. |
| `GET /api/skills` | list | Call-skill defs: `sk-1 New Work Stations`, `sk-2 Recycled Work Stations`, `sk-3 Unload Lumber & Nails`, `sk-5126… CPUs & VDOs`, `sk-bbad… Trailer Jockeying`. |
| `GET /api/workstations` | list | 19 stations: `{id, name, area, isForkliftEnabled, odooWorkCenterId, requestTypes[], ...}`. **`odooWorkCenterId` is null on all real stations** (only a test row set); join to plant WCs is **by name**. |
| `GET /api/report/weekly-trends` | dict | 8 weeks: `weeks[]{weekEnding, avgClaimMs, claimedCalls, alertState, alertCount, overloadCount, neglectedCount}` + trend %. **The cold-start history source.** |
| `GET /api/report/requests` | dict | `rows[]{requestLabel, workstationName, area, avgMs, avgClaimToCompleteMs, total}`. |

**Key fields used:**
- *Call (queue) record:* `workstationId/Name`, `area`, `requestTypeId/Label`, `priority` (normal/urgent), `createdAt`/`claimedAt`/`completedAt` (ms epoch), `status` (completed/canceled/...), `claimedBy`/`completedBy` (driverId), `lateReason`, `requiredSkillId`.
- *Driver leaderboard row:* `driverId`, `name`, `total`, `onTime`, `late`, `avgMs`, `maxMs`, `lateReasons[]`, `utilizationPct`, `totalOnCallMs`, `availableMs`.
- *Hourly slot:* `slot`, `avgMinutes`, `calls`, `callsAdjusted`, `overloadCount`, `neglectedCount`.

**Drivers (12), first-name matches to the plant roster:** Trent, Iban, Jesus, Pascual, Isidro, Lauro, Dale, Ian, Francisco, Louie, Juan, Luke. **Overload responders (backups): Louie, Juan, Luke.**

**Critical constraint:** the API exposes **only today** in detail. To build day-of-week demand patterns and historical performance, the Plant Manager must **snapshot daily** (same approach as `production_daily`). Cold start bootstraps from `weekly-trends` + today's `hourlyClaimAvgs`.

---

## 3. Goals / Non-Goals

**Goals (Stage 1):**
1. Snapshot forklift demand + per-driver performance daily into the Plant Manager DB.
2. On the scheduler, show the **predicted demand** for the day being scheduled and a **recommended dedicated-driver count** sized to avoid overload/neglect.
3. Show a **coverage check** against who's actually scheduled (dedicated slots + certified people + overload-responder backups).
4. (Increment B) Recommend **which specific** certified people to dedicate vs. keep as backups, flagging named peak gaps.

**Non-Goals:**
- Auto-assigning people (full optimizer) — deferred until the model is proven.
- Any **writes** to gpiforklift.com.
- Stage 2 (leaderboards/trophies) — separate spec.
- Fixing the forklift API's open authentication — separate task (§10).

---

## 4. Architecture & Components

All new modules follow existing Plant Manager conventions.

1. **`forklift_client.py`** — REST client modeled on `slack_client.py`.
   - Reads `FORKLIFT_API_KEY` / `FORKLIFT_BASE_URL` via `os.environ.get`; `class ForkliftError(Exception)`.
   - GET-only helpers: `fetch_dashboard()`, `fetch_queue_history()`, `fetch_drivers()`, `fetch_workstations()`, `fetch_weekly_trends()`.
   - `requests` with explicit `timeout=`, `raise_for_status()`, JSON parse; sends `X-API-Key` header (best-effort/future-proof).

2. **Schema additions (`_schema.py`)** — idempotent `CREATE TABLE IF NOT EXISTS`:
   - **`forklift_calls_daily`** (demand): `day DATE PK`, `total_calls INT`, `urgent_calls INT`, `overload_count INT`, `neglected_count INT`, `by_hour JSONB` (slot→{calls,overload,neglected,avgMinutes}), `by_station JSONB` (workstationName→calls), `by_skill JSONB`, `computed_at TIMESTAMPTZ`.
   - **`forklift_driver_daily`** (performance; seeds Stage 2): PK `(day, driver_id)`; `driver_id TEXT`, `name TEXT`, `calls INT`, `on_time INT`, `late INT`, `avg_ms BIGINT`, `max_ms BIGINT`, `utilization_pct NUMERIC`, `on_call_ms BIGINT`, `available_ms BIGINT`, `computed_at TIMESTAMPTZ`.
   - **`forklift_name_map`** (overrides): `forklift_name TEXT`, `kind TEXT` ('driver'|'workstation'), `plant_name TEXT` (person name or WC name), PK `(kind, forklift_name)`.

3. **`forklift_store.py`** — read/write the three tables (`*_store.py` pattern): `upsert_calls_daily()`, `upsert_driver_daily()`, range readers, name-map getters with a small cached dict.

4. **`forklift_demand.py`** — **pure** functions (no I/O), unit-tested:
   - `predict_demand(snapshots, weekly_trends, target_day, scheduled_wcs) -> DemandForecast` (total calls, hourly shape, peak window).
   - `recommend_drivers(forecast, throughput) -> int`.
   - `assess_coverage(recommended, scheduled_state) -> Coverage` (status OK/short + gap + which backups present).

5. **Daily warmer** — `_tick_forklift()` added to `app.py` `_WARMERS`.
   - Pulls `dashboard` + `queue/history` + `drivers`, computes the day's demand & per-driver rows, upserts both snapshot tables (`asyncio.to_thread` for blocking work). Runs periodically (e.g. every ~10 min to keep "today" fresh) and the last run of the working day captures the full day. Swallows exceptions (a warmer must never die).

6. **Scheduler integration:**
   - `routes/staffing.py` `staffing_page()` builds a `forklift_advisor` render model from `forklift_demand` + scheduled assignments (degrade to `None`/"unavailable" on error).
   - `templates/staffing.html` renders the **advisor card in the right `day-context` aside, under the "Notes for the day" textarea** (approved mockup).

7. **Settings (optional, minimal):** an enable/observe toggle + coverage sensitivity, stored under an `app_settings` key (or a singleton-row table à la `auto_lunch_settings.py`) and surfaced in `routes/settings.py`. Can be deferred if not needed for v1.

---

## 5. Demand Model & Recommendation (heart of Stage 1)

1. **Predict** the next working day's **total calls** and **hourly shape**:
   - Primary: median of recent **same-weekday** `forklift_calls_daily` snapshots (e.g. last 4–8 matching weekdays).
   - **Cold start (few/no snapshots):** derive a per-day baseline from `weekly-trends` (`claimedCalls`/operating days) and the hourly *shape* from the latest `dashboard.hourlyClaimAvgs`.
2. **Schedule-responsive scaling:** adjust the baseline by **which lines run** tomorrow. Using the workstation→WC name map and each station's historical share of calls (`by_station`), drop demand for stations whose WC is unstaffed in the draft schedule. This is what makes the advisor feel "automatic."
3. **Per-driver throughput:** calls/driver/hour derived from history (completed calls ÷ driver on-call hours), with a sane default until data accumulates.
4. **Recommended dedicated count** = smallest N such that the **busiest predicted hour** stays out of overload/neglect: `ceil(peak_hour_calls / throughput_per_driver_per_hour)`, calibrated so historical `overloadCount`/`neglectedCount` ≈ 0. Backups (overload responders) absorb spikes above the dedicated baseline.
5. **Coverage assessment:** compare recommendation to (a) people assigned to Loading/Jockeying + Tablets, (b) forklift-certified people scheduled anywhere, (c) overload-responder backups present → `✅ OK` or `⚠️ short` with the numeric gap. A "based on N days" confidence note is shown while history is thin.

---

## 6. Mapping
- **Driver → plant person:** match by first name; `forklift_name_map` (kind='driver') overrides mismatches.
- **Workstation → plant WC:** match by name (normalize trailing `#N`); `forklift_name_map` (kind='workstation') overrides.
- **Dedicated drivers** in the schedule = assignments to the **Loading/Jockeying** and **Tablets** work centers.

---

## 7. UI

Advisor card in the right `day-context` rail, under "Notes for the day" (approved). Contents (Increment A): 🚜 header, predicted calls + peak window, small hourly sparkline, **recommended dedicated count**, coverage badge (✅/⚠️ + gap), backups present, a "based on N days" note, and an expandable hour-by-hour detail. Increment B adds named "who to dedicate / keep as backup" suggestions and named peak-gap flags. Degrades to a quiet "forklift demand data unavailable" line when there's no data.

---

## 8. Reliability & Degradation
- Warmer swallows all exceptions and logs; never blocks the event loop (blocking calls via `asyncio.to_thread`).
- Snapshot upserts idempotent (`ON CONFLICT` on PK).
- Scheduler render never fails because of the advisor — a failed/empty advisor model renders the "unavailable" state.

---

## 9. Testing
- **`forklift_client`:** monkeypatch `forklift_client.requests.get` with a fake-response iterator (copy `tests/test_slack_client.py`).
- **`forklift_demand`:** pure unit tests over synthetic snapshots/trends — prediction, schedule-responsive scaling, recommendation thresholds, coverage OK/short, cold-start path.
- **`forklift_store`:** DB-gated tests (self-bootstrap schema; skip locally when `DATABASE_URL` unset, like existing store tests).
- **Scheduler render:** staffing route/template test with a stubbed `forklift_advisor` model (card renders; "unavailable" fallback renders).
- **Config:** add `FORKLIFT_API_KEY` (+ `FORKLIFT_BASE_URL`) dummy to CI env if any module import requires it.

---

## 10. Security note (separate task)
The forklift API serves operational data (drivers, full call history, leaderboard) **with no authentication**. Recommend locking it down **on the forklift-app side** (require the API key / admin auth on read endpoints). The forklift app's source is not accessible from this repo, so this can't be fixed here — it is tracked in this spec and should be addressed by whoever maintains gpiforklift.com. Out of scope for Stage 1.

---

## 11. Config / Secrets
- `FORKLIFT_API_KEY` and `FORKLIFT_BASE_URL=https://www.gpiforklift.com` in `.env` (done) and Railway env; register both in `.env.example` under a `# ---- Forklift ----` block.

---

## 12. Future (Stage 2 — separate spec)
Once `forklift_driver_daily` accumulates, feed per-driver metrics (calls, on-time %, avg response, utilization) into the existing leaderboard / trophy / GOAT machinery as a new "Forklift" category — mostly name-mapping + one new metric path through `awards.py` / `routes/leaderboards.py`.
