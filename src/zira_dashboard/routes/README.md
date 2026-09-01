# Route modules, grouped by nav section

The route files in this folder are mounted flat in `app.py` (each declares its
own full URL paths — no router prefixes). This index groups them by where
they surface in the UI so a newcomer can find the code behind a page.

> **Why flat, not folders?** The whole test suite monkeypatches these
> modules by dotted path (`monkeypatch.setattr(staffing, ...)` across ~40
> tests in 59 files), so moving files into packages would break imports
> everywhere for no user-facing gain. The names are already descriptive;
> this map is the "grouping" without the churn. See
> `docs/UI-ARCHITECTURE.md` for the page/URL picture.

## Performance section (topnav "Performance")
Dashboards, leaderboards, and the trophy case — all under
`_performance_subnav.html`.

| Module | Serves |
|---|---|
| `dashboard.py` | `/` → redirect, `/work-centers` → redirect, `/api/leaderboard`, `/tv/ping` |
| `departments.py` | `/recycling`, `/new`, and their `/tv/*` variants |
| `wc_dashboard.py` | `/wc/{slug}`, `/tv/wc/{slug}`, `/operator` (per-work-center dashboards) |
| `people_performance.py` | `/people-performance`, `/people-performance/rows` (people-first day dashboard + live row partial) |
| `recycling_leaderboard.py` | `/recycling-leaderboard` (+ `/tv/`) |
| `new_leaderboard.py` | `/new-leaderboard` (+ `/tv/`) |
| `leaderboards.py` | `/staffing/leaderboards` (plant leaderboards) |
| `forklift_leaderboards.py` | `/staffing/forklift` |
| `trophies.py` | `/trophies` (GOATs, annual trophies, ribbons) |
| `goat_watch.py` | GOAT-alert dismiss API used by the dashboards |
| `tv_displays.py` | `/tv/{slug}` registry dispatch → the render fns above |

## Staffing section (topnav "Staffing")
| Module | Serves |
|---|---|
| `staffing.py` | `/staffing` Plant Scheduler (the big one — solver wiring, save paths) |
| `rotations.py` | Recycled auto-rotation APIs used by the scheduler |
| `saturday_recruiting.py` | Saturday recruiting APIs (`/api/staffing/saturday-recruiting/*`) |
| `share.py` | `/staffing/share-to-slack` schedule PDF/permalink |
| `time_off.py` | `/staffing/time-off` calendar + merged approvals panel |
| `time_off_approvals.py` | approvals payload helpers (page URL now 301s to time_off) |
| `absence_pto_admin.py` | manager actions for PTO linked to recorded absences |
| `skills.py` | `/staffing/skills` People/Skills Matrix + automation goals |
| `people.py` | `/staffing/people/{name}` player cards |
| `past_schedules.py` | `/staffing/past` published-schedule browser |

## Inbox section (topnav "Inbox")
| Module | Serves |
|---|---|
| `exceptions.py` | `/exceptions` unified queue + its APIs |
| `late_report.py` | late/absence exception source + actions |
| `missing_wc.py` | missing-work-center exception source |
| `missed_punch_out.py` | missed-punch-out exception source |

## Settings section (topnav "Settings")
| Module | Serves |
|---|---|
| `settings.py` | `/settings?section=…` (all nine panels) |
| `changelog.py` | What's-New modal fragment + `/changelog/latest` (page URL 301s to `/`) |
| `feedback.py` | feedback widget → Odoo task API |

## Kiosk (shop-floor timeclock — separate audience)
| Module | Serves |
|---|---|
| `timeclock.py` | `/timeclock` punch flows + session tokens |
| `timeclock_saturday.py` | Saturday offer/commit kiosk flow |
| `timeclock_time_off.py` | kiosk time-off request/mine/calendar/whos-out |
| `timeclock_absence_pto.py` | kiosk requests to use PTO for a recorded past absence |

## Cross-cutting / infrastructure
| Module | Serves |
|---|---|
| `auth.py` | Azure AD OIDC login/callback + the RequireAuth middleware |
| `admin.py` | `/admin/*` debug + `/admin/page-usage` (linked from Settings → Diagnostics) |
| `api_layout.py` | dashboard widget layout persistence + `/healthz` |
| `object_api.py` | `/api/v1/object/*` bearer-authed server-to-server API |
