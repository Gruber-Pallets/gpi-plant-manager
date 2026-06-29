# Forklift Settings Redesign — Sliders + Algorithm Baseline

- **Date:** 2026-06-27
- **Status:** Approved (brainstorm) — pending spec review
- **Builds on:** the forklift advisor + the first forklift settings page (`docs/superpowers/specs/2026-06-26-forklift-demand-staffing-design.md`, shipped via PRs #3/#8/#10/#11).

## 1. Background & Goal

The forklift advisor recommends how many dedicated drivers to staff. The first settings page (PR #11) exposed the algorithm's parameters as plain number inputs. Dale wants it **(a) friendlier, (b) more things adjustable as sliders, and (c) to always show — discreetly — what the algorithm itself would recommend**, so he can see the data-driven number even when he overrides it.

Approved design direction (from visual brainstorm): **"a slider per factor"** (direction B), with a grey tick on each slider marking the algorithm's own value and a one-tap "reset to algorithm," and the headline showing **"Recommend 4 · the algorithm would recommend 6 · match it."**

## 2. Core concept: two recommendations + per-knob auto/override

The system computes **two** numbers for the target day:
- **Algorithm baseline** — the recommendation using the algorithm's *own* parameter values (data-derived where possible, sensible defaults otherwise). This is what shows discreetly everywhere.
- **Your recommendation** — the recommendation using your overrides where you've set them, else the algorithm's values.

Each tunable knob is **auto or overridden**:
- **Auto** (default): the knob follows the algorithm's value. Slider thumb sits on the grey tick.
- **Overridden**: you've moved the slider. Thumb is at your value; grey tick still shows the algorithm's value; **↺ reset** returns the knob to auto.

This is why only one mechanism is needed for "always show the algorithm's number, let me override, and snap back."

## 3. The knobs (4 sliders + 2 toggles + 1 advanced)

| Slider (friendly label) | Underlying parameter | Algorithm's value (the tick) | Range |
|---|---|---|---|
| **Driver speed** — calls one driver clears/hr | `throughput` (calls/hr) | **Data-derived** from `forklift_driver_daily` (fleet calls ÷ on-call hours, recent window); fallback 16 if thin data | 5–30 |
| **Safety slack** — spare capacity (tight ↔ roomy) | `target_utilization` (headroom) | Sensible default 0.65 (≈35% slack) | util 40–100% |
| **Plan for** — typical hour ↔ busiest hour | `plan_for_percentile` (which hour of the day to size to) **(new)** | Default 1.0 = busiest hour (current behavior) | 0.5 (median/typical) – 1.0 (busiest) |
| **History window** — how far back to learn | `history_samples` (same-weekday snapshots) | Default 8 | 2–20 |

- Effective per-driver throughput = `throughput × target_utilization`. The **Safety slack** slider stores `target_utilization`: its *Tight* end = high utilization → fewer drivers; its *Roomy* end = low utilization → more drivers (grey tick at the default 0.65). So sliding toward *Roomy* raises the recommendation.
- `recommended = max(1, ceil(plan_for_demand / effective_throughput))`, where `plan_for_demand` is the per-hour demand at the chosen percentile (1.0 = busiest hour = today's behavior; lower = a more typical hour, which is the easy way to bring the number down).

**Toggles:** Show-the-advisor (`enabled`); Count `Loading/Jockeying` toward coverage (`include_loading_jockeying`).
**Advanced (collapsed):** Cold-start assumed calls/day (`coldstart_calls_per_day`, 0 = auto from weekly trends).

Honesty note for the UI: only **Driver speed** is truly *data-derived*; Safety slack / Plan for / History window ticks are the algorithm's **sensible defaults**. The baseline *recommendation*, though, always reflects the data (predicted demand from history + derived throughput).

## 4. Data model

Revise the `forklift_settings` singleton (table created in PR #11) to store **nullable overrides** (NULL = auto / follow the algorithm):
- `enabled BOOLEAN NOT NULL DEFAULT TRUE`
- `throughput_override NUMERIC NULL`
- `utilization_override NUMERIC NULL`
- `plan_for_percentile_override NUMERIC NULL`
- `history_samples_override INTEGER NULL`
- `include_loading_jockeying BOOLEAN NOT NULL DEFAULT FALSE`
- `coldstart_calls_per_day NUMERIC NOT NULL DEFAULT 0`

Migration (guarded, idempotent): `ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS ...` for the four nullable override columns; the prior non-null param columns (`calls_per_hour`, `target_utilization`, `history_samples`) are superseded — leave them in place (harmless) and stop reading them; a later cleanup can drop them. The table is one day old (singleton, effectively default data), so no real data is lost.

`forklift_settings.py`: keep the cached-dataclass pattern, but the dataclass now carries the override fields (Optional) plus a resolver. Add a method/function `resolve(algo_values) -> ResolvedParams` that picks override-or-algorithm per knob, and `effective_throughput`. Each override is independently None-able.

## 5. Algorithm changes

**`forklift_demand.py`**
- Add `demand_at_percentile(by_hour: dict[int,float], pct: float) -> (hour, calls)`: pct=1.0 → max hour (busiest); pct=0.5 → median hour; interpolate over the sorted per-hour values. Used instead of the hard-coded peak.
- `DemandForecast` already carries `by_hour`; the percentile is applied at recommendation time (so the same forecast serves any percentile — needed for the live preview).
- `recommend_drivers(demand, effective_throughput)` unchanged.

**`forklift_store.py`**
- Add `recent_driver_throughput(days: int = 28) -> float | None`: `sum(calls) / sum(on_call_ms/3.6e6)` over `forklift_driver_daily` in the window; returns None if total on-call hours below a small floor (avoid noise). This is the data-derived Driver-speed tick.

**`forklift_advisor.py`**
- Compute the **algorithm values** for the day: `throughput = recent_driver_throughput() or DEFAULT`, `utilization = DEFAULT`, `percentile = DEFAULT(1.0)`, `history_samples = DEFAULT(8)`.
- Build the forecast once (using the *resolved* history window — note: if the user overrides the window, the forecast for "yours" uses their window; the algorithm baseline uses the default window. Compute up to two forecasts only if windows differ — otherwise reuse).
- Compute `algo_recommended` (all algorithm values) and `recommended` (resolved values).
- `build_advisor(target_day, scheduled, backups)` returns, in addition to today's keys: `algo_recommended: int|None`, plus `algo` values for the ticks. Never raises (existing defensive reads).
- `demand_summary(target_day)` returns everything the settings page needs: both recommendations, each knob's algorithm value + current effective value + range, the predicted demand, and the **per-hour demand array** (for the JS live preview's percentile math).

**`routes/staffing.py`**: unchanged logic; `_forklift_scheduled_counts` still reads the WC set from settings (`include_loading_jockeying`).

## 6. UI

### Settings page (`/settings` → Forklift) — direction B
Per the approved mockup:
- **Headline card:** big "Recommend **N** dedicated drivers — *[next working day]*", then discreet grey "↳ the algorithm would recommend **M** · *match it*" (link sets all knobs to auto). Coverage line (✅/⚠️ N scheduled on Tablets · backups). Demand line ("~X calls, busiest hour ~Y · based on N recent <weekday>s").
- **Four sliders**, each: friendly label + current value + one-line plain-English help; a track with the **yellow thumb (your/effective value)** and a **grey ▾ tick (algorithm value)**; a **↺ reset** that snaps the knob to auto; end labels.
- **Toggles:** Show advisor; Count Loading/Jockeying.
- **Advanced ▾:** cold-start calls/day.
- **Save** + **↺ Reset all to algorithm**.
- **Live preview:** as any slider moves, the headline "Recommend N" updates client-side. JS has the per-hour demand array + the formula (`ceil(demand_at(pct) / (speed × slack)))`); the grey "algorithm: M" stays fixed. (If demand can't be predicted yet, show "recommendation builds as history accrues" and disable the preview.)

### Scheduler card (`templates/staffing.html`)
Add the discreet baseline: when `algo_recommended` is present, render "Recommend **N** dedicated drivers" with a grey "· algorithm: **M**" beside it (only call attention when M ≠ N). Everything else on the card unchanged.

## 7. Components / boundaries
- `forklift_settings.py` — overrides + resolver (no algorithm logic).
- `forklift_store.recent_driver_throughput` — the only new data read.
- `forklift_demand.demand_at_percentile` — pure; the only model addition.
- `forklift_advisor` — orchestrates: algorithm values + resolved values → two recommendations + the settings summary. Single source of truth for both the card and the settings page (so they never disagree).
- `routes/settings.py` + `templates/settings.html` — the page; `templates/staffing.html` — the card baseline.

## 8. Testing
- `forklift_demand.demand_at_percentile`: pct=1.0→max, 0.5→median, interpolation, empty→0.
- `forklift_settings`: override resolution (auto→algorithm value; set→override); effective throughput; nullable round-trip (DB-gated).
- `forklift_store.recent_driver_throughput`: DB-gated; None on thin data.
- `forklift_advisor`: returns both `recommended` and `algo_recommended`; they match when all knobs auto; diverge when overridden; defensive fallback when no DB/data.
- `routes/settings` POST: set override, set auto (reset), reset-all, toggles, clamps; 303 redirect. Render: sliders show thumb+tick, headline shows both numbers (template render via Jinja env, no DB).
- `staffing.html` card: renders "Recommend N · algorithm: M".

## 9. Out of scope
- Increment B (people-level "dedicate X" suggestions) and Stage 2 (forklift leaderboards/trophies) remain parked.
- No new auth on the forklift API (separate, other-app concern).
