# Odoo Employees + Skills Integration — Design

**Date:** 2026-04-28
**Status:** Approved (brainstorming → implementation planning)

## Context

The People Matrix currently runs from a hand-maintained `roster.json` file in
the repo. Each `Person` has a name, `active`/`reserve` flags, and a flat
`skills: dict[str, int]` map (11 hardcoded skill columns, levels 0–3). Edits
go through the matrix UI; the file is the source of truth.

Dale's HR data lives in Odoo (`hr.employee` + `hr_skills` modules), and he
wants Odoo to drive the People Matrix instead — both employees and skills.
This avoids duplicate maintenance and lets Odoo administrators control plant
roster changes without touching the dashboard.

**Mode chosen during brainstorming:** Odoo read-only, local cache. The
matrix becomes view-only for skills (edit in Odoo, refresh on the
dashboard). Two-way sync is a possible future step that this design does
not preclude — the Odoo client is decoupled from the routes so write
methods can be added later without restructuring.

## Goals

1. Pull `hr.employee` records from Odoo, filtered to active employees, as
   the canonical roster.
2. Pull skills from Odoo skill types **"Production"** and **"Supervisor"**;
   each skill in those types becomes a matrix column.
3. Map Odoo's named skill levels (per type's `hr.skill.level` records) to
   the existing 0–3 integer scale via `level_progress` bucketing.
4. Standardize on Odoo's `hr.employee.name` everywhere — including a
   one-time migration of historical schedule files.
5. Refresh on a 1-hour TTL with a manual "Refresh from Odoo" button on the
   matrix page.
6. Preserve the existing `reserve` flag as a local-only override.

## Non-goals

- Two-way sync (writes to Odoo). Out of scope; explicitly the next iteration.
- Time-off pull from Odoo HR. Separate feature, separate spec.
- Authentication via OAuth or session. XML-RPC with API key is enough for
  this scale and is the universal Odoo auth.
- Settings UI for Odoo connection. Connection lives in env vars on Railway.

## Design

### Connection + auth

A new module `src/zira_dashboard/odoo_client.py` owns the Odoo XML-RPC
client. Reads four env vars at import time (or on first call):

- `ODOO_URL` — base URL like `https://gruber-pallets.odoo.com` (no `/odoo`)
- `ODOO_DB` — database name (`Production`)
- `ODOO_LOGIN` — username (email)
- `ODOO_API_KEY` — API key from Odoo Settings → Users → Account Security

Public functions:

```python
def authenticate() -> int: ...                  # returns uid; cached
def execute(model: str, method: str, *args, **kwargs) -> Any: ...
def fetch_employees() -> list[dict]: ...        # active hr.employee records
def fetch_skills_for(employee_ids: list[int]) -> dict[int, list[dict]]: ...
def fetch_skill_columns() -> list[str]: ...     # Production + Supervisor types
def fetch_skill_level_buckets() -> dict[int, int]: ...  # level_id → 0/1/2/3
```

If any env var is missing, `authenticate()` raises a clear
`OdooConfigError`. If the connection fails, the higher-level `sync()`
catches it, logs, and falls back to the existing cache (so a temporary
Odoo outage doesn't break the dashboard).

### Skill column derivation

`fetch_skill_columns()` calls Odoo:

```python
type_ids = execute("hr.skill.type", "search",
                   [[("name", "in", ["Production", "Supervisor"])]])
skills = execute("hr.skill", "search_read",
                 [[("skill_type_id", "in", type_ids)]],
                 {"fields": ["id", "name", "skill_type_id"]})
```

The resulting skill names (sorted by skill_type then name) become the
matrix column order. The two-type ordering is implicit — Production skills
appear first, Supervisor skills second.

### Skill level bucketing

Odoo's `hr.skill.level` records have a `level_progress` integer (0–100).
Rank-based mapping: for each skill type, fetch all levels and sort by
`level_progress` ascending, then assign rank index 0..N-1. Map rank to
0–3 via `bucket = round(rank * 3 / (N - 1))` (with `bucket = 0` when
N == 1):

- 4 levels (typical: Beginner/Intermediate/Advanced/Expert) → 0, 1, 2, 3 ✓
- 3 levels → 0, 2, 3
- 5 levels → 0, 1, 2, 2, 3
- 1 level → 0
- 0 levels → empty map (cells render as 0)

This avoids the standard-Odoo bug where progress thresholds 0/33/67/100
would skip bucket 2 (33% maps to 1, 67% maps to 3 with no path through 2).
Rank-based is stable regardless of how Odoo distributes `level_progress`
values.

`fetch_skill_level_buckets()` returns a `{level_id: bucket}` map for fast
lookup at sync time.

### Roster sync

A new module `src/zira_dashboard/odoo_sync.py` owns the cache and refresh
loop:

```python
ROSTER_PATH = Path("roster.json")          # existing
ODOO_LAST_SYNC_PATH = Path(".odoo_last_sync")  # ISO timestamp
TTL_SECONDS = 3600

def sync(force: bool = False) -> SyncResult:
    """Refresh roster.json from Odoo. Returns a SyncResult with counts +
    any errors. If `force` is False and last sync is within TTL, no-op."""
```

`SyncResult` is a dataclass:

```python
@dataclass
class SyncResult:
    ok: bool
    refreshed: bool                # True if we actually called Odoo
    employee_count: int
    skill_column_count: int
    last_sync_at: datetime
    error: str | None = None
```

Sync logic:

1. If `not force` and `(now - last_sync_at) < TTL`, return early with
   `refreshed=False`.
2. Authenticate to Odoo.
3. Fetch active employees (`hr.employee.search_read([("active","=",true)])`),
   their skills, the relevant skill columns, and level buckets.
4. Build a fresh `Person` list. For each Odoo employee:
   - `name` = `employee.name`
   - `active` = `employee.active`
   - `skills` = dict mapping each column name → bucketed level (0 if
     employee has no record for that skill)
   - `reserve` = preserved from existing `roster.json` if a person with the
     same name already exists; else `False`
5. Atomic write of `roster.json` (write to `roster.json.tmp` then rename).
6. Touch `.odoo_last_sync` with the current timestamp.

### One-time schedule name migration

A new script `scripts/migrate_schedule_names_to_odoo.py`:

1. Run a fresh Odoo sync, but write to `roster.json.next` instead of
   replacing `roster.json`.
2. Build a name mapping `{old_local_name: odoo_name}`:
   - Exact match first (case-insensitive, trimmed).
   - For unmatched locals: fuzzy-match against Odoo names (Levenshtein
     distance ≤ 3 OR shared prefix of length ≥ 5).
   - For unmatched Odoo names: list as "new people coming in".
   - For unmatched local names: list as "in scheduler but not in Odoo".
3. Print the proposed mapping + the two unmatched lists. Prompt the user
   to confirm (`y/N`) before writing.
4. On confirmation:
   - Move `schedules/` to `schedules.bak/`.
   - Walk `schedules.bak/`, rewrite every person name in every JSON file
     using the mapping, write the result to `schedules/`.
   - Replace `roster.json` with `roster.json.next`.
   - Touch `.odoo_last_sync`.
5. Print a summary: N schedules migrated, N people renamed, M people not
   in Odoo (preserved as-is in their original schedules — they'll just
   never show up in new ones).

The migration is run once locally (or in a Railway shell). After it
succeeds, normal sync (TTL or manual) handles all future drift.

### Matrix UI changes

`templates/skills.html` becomes view-only for skill cells:

- The `<select class="skill-sel">` dropdowns become `<span class="skill-display">`
  showing the same level/color, no edit affordance.
- Each cell gets a small "edit in Odoo" link icon (only visible on hover)
  that opens the Odoo employee profile in a new tab. URL pattern:
  `${ODOO_URL}/web#id={employee_id}&model=hr.employee&view_type=form`
- The `Active` checkbox becomes a static badge (✅ / ❌). Reserve checkbox
  stays editable since it's a local-only override.
- The "+ Add person" / "Remove person" buttons disappear (handled in Odoo).
- A new header bar above the table: "Last synced: HH:MM · [Refresh from Odoo]"
- The Refresh button POSTs to `/staffing/skills/refresh`, which calls
  `odoo_sync.sync(force=True)` and redirects back with a flash message
  showing the SyncResult counts.

POST `/staffing/skills` (existing) keeps handling the `reserve` and (for
backward compat during transition) `active`/skill writes — but the
template no longer renders inputs for those, so nothing is submitted for
them. The handler ignores any submitted `skill__*` keys and writes only
`reserve__*` to `roster.json`.

### Page-load sync trigger

The existing `/staffing/skills` GET handler calls `odoo_sync.sync()` (TTL-
checked, non-forcing) before reading `roster.json`. So:

- First request after TTL expiry triggers a sync (slow request once an
  hour for one user)
- Subsequent requests within TTL hit the cache (fast)
- If sync fails, the cache is still served and a small `⚠ Sync failed`
  badge appears next to "Last synced"

### Error handling

| Failure mode | Behavior |
|---|---|
| Env vars missing | `/staffing/skills` shows full table from cache + a banner: "Odoo connection not configured. Edit env vars and redeploy." Manual refresh button shows the same error. |
| Odoo unreachable / auth fail | Cache served; refresh button shows error toast; "Last synced" reflects the prior successful sync. |
| Skill type renamed in Odoo | Sync runs; that type contributes 0 columns. Logged warning. |
| Skill level bucket fails (zero levels) | All cells for that type render as level 0. Logged warning. |
| Employee renamed in Odoo | New name appears in matrix; old historical schedules keep displaying old name. (One-time migration only happens once.) |
| Employee deleted in Odoo | Person disappears from matrix. Historical schedules keep displaying them. |

## Acceptance criteria

- Setting the four env vars on Railway and clicking "Refresh from Odoo"
  populates the matrix from Odoo with employees from the two skill types.
- Skill columns match the names in Odoo's "Production" + "Supervisor"
  types; their order is stable across refreshes.
- Levels render 0–3 correctly via `level_progress` bucketing.
- Active matrix is read-only; toggling reserve still saves to
  `roster.json` and persists across refreshes.
- The migration script runs once, prompts for confirmation, backs up
  `schedules/`, and rewrites historical names.
- After migration, every schedule file uses Odoo names; new schedules use
  Odoo names by default.
- Auto-sync triggers once an hour on first page load past TTL; manual
  button forces an immediate refresh.
- An Odoo outage doesn't break `/staffing/skills` — the cached matrix
  still renders, with a warning banner.

## Risks

- **Skill type "Production" / "Supervisor" must exist in Odoo with the
  expected names.** If they're named differently, the columns come up
  empty. Mitigated by clear logging in sync; flagged in setup docs.
- **Level bucketing edge cases.** A skill type with 5+ levels compresses
  awkwardly. Acceptable for now; if Dale defines such a type, we revisit.
- **Migration script idempotency.** Running it twice could double-rename
  if the mapping changes between runs. Mitigated by using `schedules.bak/`
  as a one-shot snapshot — second run sees existing `schedules.bak/` and
  refuses to proceed.
- **Railway filesystem persistence.** `roster.json` writes don't survive
  redeploys without a persistent volume. Pre-existing concern; flagged in
  brainstorm. The TTL refresh masks this in practice (Odoo reseeds within
  an hour), but the `reserve` overrides would be lost. Out of scope to fix
  here; address as a follow-up if it bites.
- **API rate limits.** Odoo Online has soft limits; one sync = ~5 calls.
  At 1/hour TTL plus manual presses, we're well under any plausible cap.

## File touch list

- New: `src/zira_dashboard/odoo_client.py`
- New: `src/zira_dashboard/odoo_sync.py`
- New: `scripts/migrate_schedule_names_to_odoo.py`
- New: `tests/test_odoo_client.py` (with mocked XML-RPC)
- New: `tests/test_odoo_sync.py`
- Modified: `src/zira_dashboard/routes/skills.py` — wire `odoo_sync.sync()`
  on GET, drop skill-write logic on POST, add `/staffing/skills/refresh`
- Modified: `src/zira_dashboard/templates/skills.html` — view-only skill
  cells, refresh button, last-sync header, edit-in-Odoo links
- Modified: `src/zira_dashboard/staffing.py` — note (no code change) that
  `roster.json` is now Odoo-derived; `Person.active` is no longer
  edited locally
- Modified: `README.md` (or new `docs/odoo-setup.md`) — env var setup,
  migration script usage
- Updated: `.gitignore` — add `.odoo_last_sync` and `roster.json.next`

## Open questions deferred to plan

- Exact field paths in the XML-RPC responses (verified against actual
  Odoo data on first sync; stub fixtures in tests).
- Whether to cache Odoo employee IDs locally for the "edit in Odoo" link
  (probably yes — store on each Person in `roster.json`).
- Whether the level-bucket math should be configurable per skill type
  (probably no until Dale hits a case where it matters).
