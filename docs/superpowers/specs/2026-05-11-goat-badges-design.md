# GOAT Badges — System-Wide

**Date:** 2026-05-11
**Status:** Draft — pending user review
**Goal:** Show a 🐐 icon next to every employee name in the dashboard for each group they currently hold the all-time GOAT record in. Hover reveals which group. Updates daily (and within ~5 min of any GOAT change).

## Problem

Today the GOAT (all-time best single-day units per group) is only visible on the `/trophies` page and in the player card's "Trophy case" section. An operator who walks the floor or scans the scheduler has no way to recognize a GOAT holder at a glance. Dale wants the same visual cue everywhere a name appears, with hover text identifying the group(s), and stacked icons when one person holds GOAT in multiple groups.

## Strategy

Mirror the existing certification-badge pattern (`_cert_badges.html`):

1. A single helper computes `{name: [group_name, ...]}` for every current GOAT.
2. A reusable Jinja macro renders one 🐐 per group with `title="GOAT — <group>"` for the hover tooltip.
3. The data is injected as a Jinja global so every template gets it without per-route plumbing.
4. Every existing `cert_badges(name, person_certs)` callsite gets a sibling `goat_badges(name, goat_holders)` call.

The daily-update guarantee is structural: `awards.goat(group)` reads from `production_daily`, which the nightly job and live warmer keep fresh. A 5-minute in-process TTL cache on the helper keeps per-render cost negligible while still reflecting same-day changes within minutes.

## Components

### Data layer — `awards.goat_holders_map()`

New function in `src/zira_dashboard/awards.py`:

```python
def goat_holders_map() -> dict[str, list[str]]:
    """{operator_name: [group_name, ...]} for every current GOAT.

    Iterates registered groups, calls goat(g), applies overrides
    (so manual reassignments / deletes flow through), and inverts.
    Groups where the GOAT slot is empty (no data or override-deleted)
    contribute nothing.

    Cached in-process for 5 minutes.
    """
```

- Overrides honored via `apply_overrides_single(scope="award_goat", group_name=g)`.
- TTL cache wraps the body so per-request templates don't pay per-group lookup.
- Returned shape is the same kind of dict as `person_certs` — a name-keyed lookup ready for the macro.

### Macro — `templates/_goat_badges.html`

New file mirroring `_cert_badges.html`:

```jinja
{% macro goat_badges_css() -%}
.goat-badges {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  margin-left: 4px;
  vertical-align: middle;
  line-height: 1;
}
.goat-badge {
  font-size: 0.95em;
  cursor: help;
  user-select: none;
}
{%- endmacro %}

{% macro goat_badges(name, goat_holders) -%}
{%- set groups = (goat_holders or {}).get(name, []) -%}
{%- if groups -%}
<span class="goat-badges">
{%- for g in groups -%}
<span class="goat-badge" title="GOAT — {{ g }}">🐐</span>
{%- endfor -%}
</span>
{%- endif -%}
{%- endmacro %}
```

- Inline-flex container so multiple 🐐 emoji sit cleanly side-by-side.
- `font-size: 0.95em` keeps the emoji proportionate to surrounding name text.
- `cursor: help` cues that the tooltip is interactive.
- Empty list → empty macro output (no markup), so it's safe to call unconditionally next to every name.

### Jinja global — `goat_holders` callable

In `src/zira_dashboard/app.py`, alongside the existing `cert_icon_svg` registration:

```python
templates.env.globals["goat_holders"] = awards.goat_holders_map
```

Registering the function (not its return value) means it evaluates per-render and benefits from the TTL cache. Templates invoke it explicitly: `{{ goat_badges(name, goat_holders()) }}`. Mirrors how `cert_icon_svg` is wired — a callable global that templates call. The macro itself takes the dict as a normal argument, matching the `cert_badges(name, person_certs)` shape.

### Template callsites

Add a sibling `{{ goat_badges(name, goat_holders()) }}` after every existing `{{ cert_badges(name, person_certs) }}` callsite. Per the audit:

- `leaderboards.html` — 12 callsites across daily, weekly, monthly, yearly, all-time, and per-WC sections.
- `staffing.html` — 5 callsites: Unassigned list, Time Off rows, scheduler-row name dropdown items (×2 places), and assigned-operator names.
- `skills.html` — 1 callsite in the matrix's name column.
- `past_schedules.html` — 1 callsite in the operators-per-WC inline list.

Plus a new callsite where cert_badges doesn't currently exist:

- `player_card.html` — the operator's name at the top of the page (next to the name picklist dropdown).

Every callsite imports the macro:

```jinja
{% from "_goat_badges.html" import goat_badges, goat_badges_css %}
```

And every template includes `{{ goat_badges_css() }}` inside its existing `<style>` block (alongside the existing `{{ cert_badges_css() }}` call).

### Out of scope

- The `/trophies` page itself — names there are already grouped under GOAT/trophy sections, so an inline 🐐 next to each name would be redundant.
- Trophy Case section on player cards — the operator's awards are listed below in plain text; the GOAT line already starts with "🐐 GOAT — Repairs" today.
- The retro-attribution dropdowns, recycling per-WC widgets, and dashboards (Recycled / New VS top-units widgets) — these don't currently render cert badges either; matching that omission keeps the visual treatment consistent across both badge families.

## Daily-update guarantee

The chain is structural — no scheduled job is needed beyond the precompute pipeline that already exists:

1. `production_daily` is refreshed daily by the nightly precompute job and continuously by the 45 s warmer (today's row only).
2. `awards.goat(group)` reads `production_daily` via the existing `_all_time_range()` + `person_days_in_group()` chain.
3. `goat_holders_map()` caches the result for 5 minutes in-process.
4. After 5 minutes the cache expires and the next render recomputes — picking up any new GOAT.

End-to-end: a new GOAT achieved today shows up across the system within 5 minutes (warmer + cache expiry). A GOAT lost via manual override propagates the same way. A GOAT lost because someone beat the record propagates after the nightly recompute writes the new top day into `production_daily`.

## Error handling

- `goat_holders_map()` wraps each `goat(group)` call in its own try/except. A single broken group does not poison the whole map; the broken group simply contributes nothing. (This matches the pattern in `awards_earned_by`.)
- The macro is purely Jinja with no Python calls beyond the dict lookup. A `None` or missing `goat_holders` argument renders as empty — safe for any template even if the global is somehow unset.
- The TTL cache catches and re-raises so a transient DB failure does not pin a poisoned cache entry.

## Testing

- **`awards.goat_holders_map()`:** unit tests with mocked `goat()` and `apply_overrides_single` exercising: empty groups, single-group GOAT, multi-group GOAT for one person, override-deleted slot dropped, override-replaced name substituted.
- **TTL cache:** test that two back-to-back calls produce one computation; test that bypass after the TTL re-evaluates.
- **Macro rendering:** snapshot-style tests that render the macro with representative inputs (zero / one / two groups) and assert exact HTML output including the `title` attribute.
- **Smoke:** for one representative template per family (`leaderboards.html`, `staffing.html`, `player_card.html`), render against a fixture roster with a known GOAT and assert the 🐐 appears the expected number of times in the right rows.

## Rollout

One ship, one commit chain on `main`:

1. Add `awards.goat_holders_map()` + tests.
2. Add `_goat_badges.html` macro + tests.
3. Register Jinja global in `app.py`.
4. Add the `{% from %}` import + `{{ goat_badges_css() }}` + `{{ goat_badges(...) }}` calls across the seven templates listed above.
5. Smoke-test live page.
6. CHANGELOG entry + push.

No data migration, no schema change, no new endpoints.

## Open questions

None — all decisions made above. If a future surface (e.g., a new dashboard) needs the badge, it adds one import + one macro call.
