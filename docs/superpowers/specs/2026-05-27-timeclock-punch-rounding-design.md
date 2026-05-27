# Timeclock Punch Rounding — Design

**Date:** 2026-05-27
**Status:** Approved (brainstorming → implementation planning)

## Context

The timeclock kiosk writes punches to `kiosk_punches_log` and to Odoo
`hr.attendance`. Today every punch is recorded at the exact moment the
employee taps the button — including punches a few minutes before
scheduled shift start (people who show up early) and a few minutes
after scheduled shift end (people who linger before clocking out).

StratusTime had a "Round To Schedule" feature that pulled punches
within a configurable window toward the scheduled shift boundary,
matching how payroll actually wants to count time. Dale wants the
same behavior in the GPIPlantmanager timeclock, with the rounded value
applied to the punches that land in Odoo `hr.attendance`.

The StratusTime screenshot driving this:

```
Round To Schedule

IN                                       OUT
Up to [20] min before clock-in time      Up to [ 0] min before clock-out time
Up to [ 0] min after  clock-in time      Up to [20] min after  clock-out time
```

Window semantics: rounding always pulls **toward** the scheduled
boundary. A 20-min IN-before window means a punch up to 20 min before
shift start rounds UP to scheduled start. An employee can't game it by
punching early or staying late — within the window, they're recorded
at scheduled time. Outside the window, the raw punch time stands.

## Goals

1. A plant-wide settings page lets Dale configure four numbers — IN
   before, IN after, OUT before, OUT after — matching the StratusTime
   UI. Effective immediately on save; no historical re-rounding.
2. Each kiosk `clock_in` / `clock_out` punch stores both the raw
   timestamp (`occurred_at`) and a rounded timestamp (`rounded_at`).
3. Odoo `hr.attendance` receives the rounded timestamp on every kiosk
   sync. Payroll runs against rounded times.
4. The kiosk success page, the dashboard "Clocked in at …" line, and
   the late-arrival detector all use the rounded time. The kiosk
   display and Odoo agree.
5. `transfer_in` / `transfer_out` punches are never rounded —
   transfers are mid-shift events, not shift boundaries.

## Non-goals

- **No per-person rounding rules.** Plant-wide policy only.
- **No per-WC rounding rules.** The forklifts and the repair line all
  round to the same plant shift.
- **No effective-date history.** The settings row stores the current
  rules. If Dale ever needs historical changes, a future enhancement
  can add an `effective_from` column; not in scope here.
- **No automatic re-rounding of historical punches** when settings
  change. New punches use the new rules; old punches keep their
  stored values. A one-off maintenance script can re-round if needed.
- **No display indicator that a punch was rounded.** The kiosk just
  shows the rounded time; the raw `occurred_at` is kept in the log
  for audit but not surfaced in the UI.
- **No overnight-shift support.** The plant shift is 7:00 AM – 3:30
  PM. The rounding helper assumes shift_start and shift_end fall on
  the same site-local date as the punch. Documented as an assumption
  in the helper.
- **No rounding around lunch / break boundaries.** Lunch is an
  unpaid auto-deduction off the schedule, not a real punch.

## Design

### 1. Settings model

One singleton row in a new `rounding_settings` table:

| column | type | default | meaning |
| --- | --- | --- | --- |
| `id` | INT PK | 1 | singleton constraint |
| `in_before_min` | INT NOT NULL | 0 | punch up to N min BEFORE shift_start rounds UP |
| `in_after_min` | INT NOT NULL | 0 | punch up to N min AFTER shift_start rounds DOWN |
| `out_before_min` | INT NOT NULL | 0 | punch up to N min BEFORE shift_end rounds UP |
| `out_after_min` | INT NOT NULL | 0 | punch up to N min AFTER shift_end rounds DOWN |
| `updated_at` | TIMESTAMPTZ | now() | for audit |

Ships disabled (all zeros = no rounding). Dale flips on whichever
sides he wants on the settings page. Validation: 0 ≤ each value ≤ 60.

A new `rounding_store.py` module wraps load/save with a module-level
cache + RLock, mirroring the pattern in `schedule_store.py` — settings
are read on every punch, so a hot in-process cache matters. `save()`
invalidates the cache; `current()` returns the cached `RoundingSettings`
dataclass.

### 2. Schema changes

Additive migrations in `db.py`:

```sql
CREATE TABLE IF NOT EXISTS rounding_settings (
  id              INT PRIMARY KEY DEFAULT 1,
  in_before_min   INT NOT NULL DEFAULT 0,
  in_after_min    INT NOT NULL DEFAULT 0,
  out_before_min  INT NOT NULL DEFAULT 0,
  out_after_min   INT NOT NULL DEFAULT 0,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT rounding_settings_singleton CHECK (id = 1)
);
INSERT INTO rounding_settings (id) VALUES (1) ON CONFLICT DO NOTHING;

ALTER TABLE kiosk_punches_log
  ADD COLUMN IF NOT EXISTS rounded_at TIMESTAMPTZ;
```

`occurred_at` stays the raw punch time (unchanged). `rounded_at` is
set at write time — equals `occurred_at` when rounding doesn't apply,
equals the rounded value when it does. Every consumer reads
`COALESCE(rounded_at, occurred_at)` so historical rows where the
column is NULL keep working without a backfill.

### 3. Rounding logic

A pure function in a new `rounding.py` module — easy to unit-test:

```python
from dataclasses import dataclass
from datetime import datetime, time

@dataclass(frozen=True)
class RoundingSettings:
    in_before_min: int
    in_after_min: int
    out_before_min: int
    out_after_min: int


def apply_rounding(
    action: str,            # "clock_in" | "clock_out" | "transfer_in" | "transfer_out"
    occurred_at: datetime,  # aware, UTC
    shift_start: time,      # plant-wide for the punch's local date
    shift_end: time,
    settings: RoundingSettings,
) -> datetime:
    """Return the rounded timestamp, or occurred_at unchanged when no
    rounding applies. Transfers always pass through. Punches outside
    the configured window pass through."""
```

Algorithm:

1. If `action in ("transfer_in", "transfer_out")` → return `occurred_at`.
2. Convert `occurred_at` to site-local (`SITE_TZ` from `shift_config`).
3. Combine the local date with `shift_start` → `scheduled_in` (tz-aware);
   combine with `shift_end` → `scheduled_out`.
4. For `action == "clock_in"`:
   - If `(scheduled_in - in_before_min) ≤ occurred_at ≤ (scheduled_in + in_after_min)`
     → return `scheduled_in` (converted back to UTC).
   - Else return `occurred_at`.
5. For `action == "clock_out"`:
   - If `(scheduled_out - out_before_min) ≤ occurred_at ≤ (scheduled_out + out_after_min)`
     → return `scheduled_out`.
   - Else return `occurred_at`.

Examples (shift 7:00 AM – 3:30 PM, settings 20/0/0/20):

| action | punch | rounded |
| --- | --- | --- |
| clock_in | 6:50 AM | 7:00 AM |
| clock_in | 6:38 AM (outside 20-min window) | 6:38 AM |
| clock_in | 7:05 AM (after = 0) | 7:05 AM |
| clock_in | 9:00 AM (forgot, came in late) | 9:00 AM |
| clock_out | 3:35 PM | 3:30 PM |
| clock_out | 3:55 PM (outside 20-min window) | 3:55 PM |
| clock_out | 1:00 PM (early leave) | 1:00 PM |
| transfer_in | 3:35 PM | 3:35 PM (transfers never rounded) |

`shift_start` and `shift_end` come from
`shift_config.shift_start_for(day)` / `shift_end_for(day)` so per-day
custom hours (published Saturdays, etc.) are honored automatically.

### 4. Integration points

Four places switch from raw `occurred_at` to rounded:

**a. Kiosk write path** (`routes/kiosk.py:_open_log_row`)

After the INSERT, compute the rounded time and write it back in the
same transaction:

```python
def _open_log_row(person_odoo_id, action, wc_name):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO kiosk_punches_log (person_odoo_id, action, wc_name) "
            "VALUES (%s, %s, %s) RETURNING id, occurred_at",
            (person_odoo_id, action, wc_name),
        )
        row = cur.fetchone()
        local_date = row["occurred_at"].astimezone(shift_config.SITE_TZ).date()
        rounded = rounding.apply_rounding(
            action,
            row["occurred_at"],
            shift_config.shift_start_for(local_date),
            shift_config.shift_end_for(local_date),
            rounding_store.current(),
        )
        cur.execute(
            "UPDATE kiosk_punches_log SET rounded_at = %s WHERE id = %s",
            (rounded, row["id"]),
        )
        return row["id"], rounded
```

The handlers (`kiosk_clock_in`, `kiosk_clock_out`, `kiosk_transfer`)
take the returned `rounded` and pass it to `_fmt_time(rounded)` on the
success page, so the kiosk shows "Clocked in at 7:00 AM" even when
the tap was at 6:50.

**b. Odoo sync** (`kiosk_sync.py` → `odoo_client.clock_in/clock_out`)

The SELECT in `retry_unsynced_punches` and `sync_one_by_id` switches
from `occurred_at` to `COALESCE(rounded_at, occurred_at) AS ts`.
`_retry_one` passes that `ts` to `odoo_client.clock_in/clock_out`. Odoo
`hr.attendance.check_in` and `check_out` record the rounded values.

**c. Dashboard state read** (`routes/kiosk.py:_current_state`)

The SELECT for `occurred_at` becomes `COALESCE(rounded_at, occurred_at)
AS occurred_at`. The "Clocked in at 7:00 AM" line on the kiosk
dashboard matches what's in Odoo.

**d. Late report** (`late_report.py`)

Wherever `attendance_for_day` reads the punch time used to compare
against `shift_start + 15 min`, switch to rounded. A 6:50 AM punch
that rounds to 7:00 AM no longer trips the late detector. Same
COALESCE pattern.

### 5. Settings UI

Extend the existing `templates/settings.html` with a new Rounding
section, modeled on the StratusTime screenshot:

```
┌─ Rounding ─────────────────────────────────────────────────────┐
│ Note: Some jurisdictions may limit or prohibit rounding.       │
│                                                                │
│ Round To Schedule                                              │
│ When clocking in or out, the employee's time can be rounded    │
│ to the plant shift if the entry falls within an acceptable     │
│ "window". Enter the values for the window:                     │
│                                                                │
│  IN                                  OUT                       │
│  Up to [20] min before clock-in      Up to [ 0] min before     │
│  Up to [ 0] min after  clock-in      Up to [20] min after      │
│                                                                │
│ Effective immediately — punches from this point forward use    │
│ these values. Historical punches are unchanged.                │
│                                                                │
│ [Save Rounding]                                                │
└────────────────────────────────────────────────────────────────┘
```

Four `<input type="number" min="0" max="60">` boxes, one save button,
posts to a new `POST /settings/rounding` route. Server validates 0 ≤
each value ≤ 60 and updates the singleton row + invalidates the
`rounding_store` cache.

No JS framework — plain HTML form, same pattern the existing settings
page uses. Add a `GET /settings/rounding` route (or extend the
existing settings GET) to seed current values into the inputs.

### 6. Components and data flow

```
[Employee taps Clock In at 6:50 AM]
        ↓
routes/kiosk.py:kiosk_clock_in
        ↓
_open_log_row inserts row, then:
  rounding.apply_rounding(
    action="clock_in",
    occurred_at=2026-05-27T06:50:00,
    shift_start=07:00,
    shift_end=15:30,
    settings=RoundingSettings(20, 0, 0, 20),
  ) → 2026-05-27T07:00:00
        ↓
UPDATE kiosk_punches_log SET rounded_at = '07:00' WHERE id = ...
        ↓
Success page renders "Clocked in at 7:00 AM"
        ↓
BackgroundTask kiosk_sync.sync_one_by_id(id)
        ↓
SELECT COALESCE(rounded_at, occurred_at) AS ts → 07:00
        ↓
odoo_client.clock_in(emp, wc, ts=07:00)
        ↓
hr.attendance.create({check_in: '07:00', ...})

[Later, same employee opens the kiosk dashboard]
        ↓
_current_state SELECT COALESCE(rounded_at, occurred_at) → 07:00
        ↓
Dashboard shows "Clocked in at 7:00 AM" — matches Odoo
```

## Testing

**Unit tests** (`tests/test_rounding.py`, new):

1. `test_clock_in_within_before_window_rounds_to_start` — 6:50 AM,
   scheduled 7:00, in_before=20 → 7:00.
2. `test_clock_in_outside_before_window_unchanged` — 6:38 AM,
   in_before=20 → 6:38 (22 min early, outside window).
3. `test_clock_in_within_after_window_rounds_to_start` — 7:05 AM,
   in_after=10 → 7:00.
4. `test_clock_in_outside_after_window_unchanged` — 9:00 AM,
   in_after=10 → 9:00.
5. `test_clock_out_within_after_window_rounds_to_end` — 3:35 PM,
   out_after=20 → 3:30 PM.
6. `test_clock_out_within_before_window_rounds_to_end` — 3:25 PM,
   out_before=10 → 3:30 PM.
7. `test_clock_out_early_leave_unchanged` — 1:00 PM, scheduled 3:30
   → 1:00 PM.
8. `test_transfer_in_never_rounded` — transfer_in at 6:50 AM with
   in_before=20 → 6:50 AM unchanged.
9. `test_transfer_out_never_rounded` — same for transfer_out.
10. `test_zero_window_disables_rounding` — all settings = 0, every
    `clock_in` / `clock_out` returns occurred_at unchanged.
11. `test_uses_per_day_custom_hours` — published Saturday with
    custom 8:00 AM start: 7:50 AM clock_in rounds to 8:00 AM, not
    the global 7:00 AM default.
12. `test_boundary_at_exact_window_edge` — 6:40 AM with in_before=20
    (exactly 20 min before 7:00) → rounds to 7:00 (inclusive bound).

**Route tests** (`tests/test_kiosk.py`, extend):

13. `test_clock_in_writes_both_occurred_and_rounded` — POST clock-in
    inside the window, check kiosk_punches_log row has both
    `occurred_at` and `rounded_at` populated and they differ.
14. `test_clock_in_success_page_shows_rounded_time` — same setup,
    success page HTML contains the rounded time, not the raw.
15. `test_dashboard_shows_rounded_clock_in_time` — after a rounded
    clock-in, `/kiosk/dashboard/{token}` renders rounded time.
16. `test_kiosk_sync_passes_rounded_to_odoo` — mock odoo_client,
    confirm `clock_in(...)` is called with the rounded timestamp.
17. `test_transfer_punches_persist_raw_only` — transfer_in row has
    `rounded_at == occurred_at`. (`apply_rounding` returns
    `occurred_at` unchanged for transfers; the writer always
    populates `rounded_at`, so transfers store the equal value
    rather than NULL. NULL is reserved for historical pre-feature
    rows.)

**Settings tests** (`tests/test_settings.py` or new):

18. `test_settings_rounding_get_seeds_current_values` — defaults all
    zero on first load.
19. `test_settings_rounding_post_persists_and_invalidates_cache` —
    POST 20/0/0/20, confirm DB row updated and
    `rounding_store.current()` returns new values.
20. `test_settings_rounding_rejects_negative_or_oversized` — POST
    `in_before_min=-5` or `=999` → 400, no DB change.

**Manual / visual:**

- Set rounding to 20/0/0/20 on the settings page. Clock in at 6:50
  AM via the kiosk. Confirm the success page shows 7:00 AM.
- Open the kiosk dashboard for that employee. Confirm "Clocked in at
  7:00 AM."
- Check Odoo `hr.attendance` directly — `check_in` should equal 7:00 AM.
- Set all four rounding settings to 0. Clock out at 3:35 PM. Confirm
  success page and Odoo both record 3:35 PM (no rounding applied).
- Late report: don't punch in by 7:00 AM, settings still 20/0/0/20.
  At 7:16 AM you should appear in the late report. At 7:14 AM you
  should not (still inside 15-min grace).

## Files touched

- `src/zira_dashboard/db.py` — DDL for `rounding_settings` table,
  ALTER TABLE for `kiosk_punches_log.rounded_at`.
- `src/zira_dashboard/rounding.py` *(new)* — `RoundingSettings`
  dataclass, `apply_rounding(...)` pure function.
- `src/zira_dashboard/rounding_store.py` *(new)* — load/save singleton
  with module-level cache + RLock, mirrors `schedule_store`.
- `src/zira_dashboard/routes/kiosk.py` — `_open_log_row` returns
  rounded time; handlers display rounded on success page; `_current_state`
  uses `COALESCE(rounded_at, occurred_at)`.
- `src/zira_dashboard/kiosk_sync.py` — SELECT uses
  `COALESCE(rounded_at, occurred_at)`.
- `src/zira_dashboard/late_report.py` — late detection switches to
  rounded time.
- `src/zira_dashboard/routes/settings.py` — new `GET` / `POST`
  `/settings/rounding`.
- `src/zira_dashboard/templates/settings.html` — Rounding section
  with four number inputs + save button.
- `tests/test_rounding.py` *(new)* — 12 unit tests for the pure function.
- `tests/test_kiosk.py` — extend with rounding-aware route tests.
- `tests/test_settings.py` — extend (or create) with settings tests.
- `CHANGELOG.md` — entry for the deploy.

## Implementation notes

- The schema changes are additive (`CREATE TABLE IF NOT EXISTS`,
  `ALTER TABLE … ADD COLUMN IF NOT EXISTS`). Safe to deploy without
  migration coordination.
- `rounding_store.current()` returns a cached frozen dataclass; reads
  are sub-microsecond. The settings page `POST` handler calls
  `rounding_store.invalidate()` so the next punch sees the new values.
- The `COALESCE(rounded_at, occurred_at)` pattern means we never need
  to backfill historical rows — they just keep their raw timestamps.
  New punches always populate both columns.
- `apply_rounding` is a pure function (no DB, no side effects). All
  edge cases are testable without fixtures.
- The function works in UTC internally but converts to site-local
  (`SITE_TZ`) to derive the date and combine with `time` objects.
  This matters around DST transitions and for clarity when reading
  test cases.
- Overnight shifts are not supported — `shift_start` and `shift_end`
  must fall on the same site-local date as the punch. If GPI ever
  adds a 2nd or 3rd shift that crosses midnight, this helper needs
  to be extended (compute scheduled_in / scheduled_out on potentially
  different dates).
- Existing punches with `rounded_at = NULL` continue to work via
  COALESCE. No backfill script needed.
- If Dale later wants to re-round history after a settings change,
  a one-off script can iterate `kiosk_punches_log` rows with
  `synced_to_odoo = TRUE`, recompute rounded with current settings,
  and re-sync to Odoo (would need an Odoo `write` on existing
  `hr.attendance` ids). Not in scope here.
