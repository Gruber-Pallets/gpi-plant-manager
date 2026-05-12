# Certification Badges — Design

**Date:** 2026-04-29
**Status:** Approved (brainstorming → implementation planning)

## Context

Odoo tracks employee certifications as a third skill_type
(`"Certifications"`) alongside the existing `"Production Skills"` and
`"Supervisor Skills"` types we already pull. Each certification is a row
under that type in `hr.skill`, and per-employee assignments live in
`hr.employee.skill_ids`.

Dale wants those certifications surfaced as small icon badges next to
every employee name across the dashboard, so anyone reading the
scheduler, matrix, or leaderboards can see at a glance who is forklift /
spotter / CDL / DOT certified.

The five certifications in Odoo today, by exact name:

- `Forklift Certified`
- `CDL (Automatics) Certified`
- `CDL (Manuals) Certified`
- `DOT Certified`
- `Spotter Truck Certified`

## Goals

1. Pull the `Certifications` skill_type through the same Odoo sync that
   already handles Production / Supervisor skills — no new model,
   schema, or sync code.
2. Render small monochrome icon badges immediately after each operator
   name, on every page where names appear (scheduler, People Matrix,
   leaderboards, past schedules).
3. Hover tooltip on each badge shows the cert's exact Odoo name, so a
   user can confirm `CDL (Automatics)` vs `CDL (Manuals)` without a
   custom icon.
4. Hardcoded cert→icon mapping for the five known certs; unmapped
   certs degrade gracefully to a small text pill so a new Odoo cert is
   never invisible.

## Non-goals

- Settings UI for editing the cert→icon mapping. Five entries don't
  justify a dedicated panel; adding a new cert is a one-line code
  change.
- Per-cert color theming. All badges inherit the page's text color
  (matches Lucide style + existing dashboard look).
- Expiration tracking. Odoo's `hr.skill` model doesn't store an expiry
  date; if a cert appears in `hr.employee.skill_ids`, the badge shows.
  If/when expiry tracking is added in Odoo, that's a separate feature.
- Treating certifications as proficiency-graded. Every cert is binary
  (have it or not); the Odoo level is ignored for badge display.
- Showing certifications as columns in the People Matrix. The matrix
  stays focused on proficiency-graded skills; certs surface only as
  badges.

## Design

### Data path

`src/zira_dashboard/odoo_client.py`:

```python
SKILL_TYPE_NAMES = ("Production Skills", "Supervisor Skills", "Certifications")
```

That single tuple change is the entire sync extension. The existing
`fetch_skill_columns_with_types()`, the `sync_skills()` upsert into the
local `skills` table, and the `person_skills` assignment sync all flow
through unchanged. Cert rows land with `skill_type = 'Certifications'`.

### People Matrix exclusion

`src/zira_dashboard/routes/skills.py` (the matrix route) currently
sources its column list from the `skills` table. Filter that query to
exclude `skill_type = 'Certifications'`:

```python
"SELECT name, skill_type FROM skills "
"WHERE skill_type IN ('Production Skills', 'Supervisor Skills') "
"ORDER BY skill_type, lower(name)"
```

This keeps the matrix grid focused; certifications only appear as
badges.

### Cert lookup helper

Per-request preload of `{person_name: [cert_name, ...]}`:

`src/zira_dashboard/cert_lookup.py` (new):

```python
from . import db

def load_person_certs() -> dict[str, list[str]]:
    """Return {person_name: [cert_name, ...]} for everyone with at least
    one certification. Cert list is alphabetical. Single query, no joins
    beyond skills + person_skills + people.

    Treats any person_skills row whose skill has skill_type='Certifications'
    as a binary 'has this cert' fact — level is ignored.
    """
    sql = """
        SELECT p.name AS person, s.name AS cert
        FROM person_skills ps
        JOIN skills s  ON s.id  = ps.skill_id
        JOIN people p  ON p.id  = ps.person_id
        WHERE s.skill_type = 'Certifications'
        ORDER BY p.name, s.name
    """
    out: dict[str, list[str]] = {}
    with db.cursor() as cur:
        cur.execute(sql)
        for row in cur.fetchall():
            out.setdefault(row["person"], []).append(row["cert"])
    return out
```

Cheap (one indexed query). Called once at the top of any route that
renders names. Result is plain Python dict — no caching layer needed
(query is small and fast).

### Cert → icon module

`src/zira_dashboard/cert_icons.py` (new):

```python
"""Hardcoded cert-name → inline-SVG icon mapping. Case-insensitive
exact-match on the trimmed cert name. Returns None when no mapping
exists; the template renders a text pill in that case."""

_SVG_FORKLIFT = '<svg ...>...</svg>'        # Lucide "forklift"
_SVG_SEMI     = '<svg ...>...</svg>'        # Lucide "truck"
_SVG_WRENCH   = '<svg ...>...</svg>'        # Lucide "wrench"
_SVG_SPOTTER  = '<svg ...>...</svg>'        # Lucide "truck" variant or hand-picked yard-truck

_CERT_ICONS: dict[str, str] = {
    "forklift certified":           _SVG_FORKLIFT,
    "cdl (automatics) certified":   _SVG_SEMI,
    "cdl (manuals) certified":      _SVG_SEMI,
    "dot certified":                _SVG_WRENCH,
    "spotter truck certified":      _SVG_SPOTTER,
}

def icon_for(cert_name: str) -> str | None:
    return _CERT_ICONS.get(cert_name.strip().lower())
```

All five SVGs are inlined as constants — no HTTP fetch, no icon font,
no CDN. SVG `<svg fill="currentColor">` so the badge inherits the
surrounding text color, keeping it theme-friendly.

For the spotter truck specifically: Lucide has no exact match. Options
during implementation are (a) a generic `truck` icon styled differently
from the CDL truck (e.g., a thicker outline or yard-truck silhouette),
or (b) a small custom SVG. Either is fine; both certs that share the
plain truck icon (CDL Automatics / Manuals) are differentiated only by
hover today, so the spotter truck icon must be visually distinct from
them at a glance.

### Jinja macro and partial

`src/zira_dashboard/templates/_cert_badges.html` (new):

```jinja
{% macro cert_badges(name, person_certs) -%}
  {%- set certs = person_certs.get(name, []) -%}
  {%- if certs -%}
    <span class="cert-badges">
      {%- for cert in certs -%}
        {%- set svg = cert_icon_svg(cert) -%}
        {%- if svg -%}
          <span class="cert-badge" title="{{ cert }}">{{ svg|safe }}</span>
        {%- else -%}
          <span class="cert-pill" title="{{ cert }}">{{ cert.split()[0] }}</span>
        {%- endif -%}
      {%- endfor -%}
    </span>
  {%- endif -%}
{%- endmacro %}
```

CSS (added to the shared base stylesheet — pick whichever the templates
already share):

```css
.cert-badges {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: 6px;
  vertical-align: middle;
}
.cert-badge svg {
  width: 14px; height: 14px;
  display: block;
  color: var(--accent, currentColor);
  opacity: 0.85;
}
.cert-pill {
  font-size: 0.65rem;
  font-weight: 600;
  padding: 1px 5px;
  border-radius: 999px;
  border: 1px solid var(--border, #ccc);
  color: var(--muted, #666);
  text-transform: uppercase;
  letter-spacing: 0.02em;
}
```

`cert_icon_svg` is registered as a Jinja global in `app.py` (one-line
`app.jinja_env.globals["cert_icon_svg"] = cert_icons.icon_for`). The
macro is imported wherever needed:

```jinja
{% from "_cert_badges.html" import cert_badges %}
...
{{ display_name }}{{ cert_badges(display_name, person_certs) }}
```

### Per-page integration

For each route handler that renders names, add at the top:

```python
from .. import cert_lookup
person_certs = cert_lookup.load_person_certs()
```

…and pass `person_certs` into the template context. Templates import
the macro and call it next to each name.

Templates touched (and where in each):

- `staffing.html`
  - left-rail Unscheduled list items (`.dd-item`)
  - left-rail Reserves list items
  - WC card name labels (each assignment cell)
  - time-off chips
- `skills.html`
  - matrix grid name column (first cell of each row)
- `leaderboards.html`
  - operator name in each top-5 row (both WC and group sections)
- `past_schedules.html`
  - any name labels in the day's snapshot view

### Naming + lookup edge cases

- Lookup keys for `person_certs` use the same `display_name` form the
  templates already render (post-truncation to first two tokens via
  `_short_name`). Since `cert_lookup.load_person_certs()` reads
  `people.name`, and the rest of the app already stores the truncated
  form there, lookups match without extra normalization.
- If a person's name doesn't appear in `person_certs` (no cert rows in
  Odoo), the macro renders nothing — empty `<span>` is omitted by the
  `{% if certs %}` guard, so no extra whitespace.
- A cert in Odoo that isn't in `_CERT_ICONS` renders as a text pill
  whose label is the **first whitespace-separated word** of the cert
  name (e.g., a cert named `"Reach Truck Certified"` would show a pill
  labelled `REACH`). Crude but readable, and trivially upgradable to a
  proper icon by adding one entry.

## Acceptance criteria

- Running the existing "Refresh from Odoo" pulls in the
  `Certifications` skill_type rows and per-person assignments.
- The People Matrix grid does **not** show certification columns.
- A person who has `Forklift Certified` shows a forklift icon next to
  their name on every page that renders names.
- A person who has both `CDL (Automatics) Certified` and
  `CDL (Manuals) Certified` shows **two** semi-truck icons; hovering
  each shows the full cert name.
- A person who has `DOT Certified` shows a wrench icon.
- A person who has `Spotter Truck Certified` shows a spotter-truck
  icon that is visually distinct from the CDL semi-truck.
- A person with no certifications shows their name as it does today,
  with no extra spacing or empty span.
- A new certification added in Odoo (e.g., `"Reach Truck Certified"`)
  appears next to certified employees as a text pill labelled
  `REACH` until an icon is added in `cert_icons.py`.
- Badges scale visually with surrounding text on dense pages (matrix)
  and roomier pages (scheduler) without breaking row layout.

## Risks

- **Spotter truck icon distinctness.** Lucide has no native spotter /
  yard truck icon. The chosen icon must be visually distinct from the
  semi-truck used for CDL or operators may misread it at a glance.
  Acceptable mitigation: a hand-rolled SVG silhouette (yard tractor
  shape) baked into `cert_icons.py`. About 30 lines of SVG.
- **Multi-cert rendering on tight rows.** A person with all five
  certifications would render five 14px icons after their name (~80px
  + gaps). On the People Matrix the name column has plenty of room;
  on the scheduler's narrow Unscheduled rail, this could push the row
  wider. If it looks crowded in practice, follow-up mitigation: cap at
  3 + a `+N` chip with the remaining certs revealed on hover. Not
  building this now — YAGNI until we see it crowd.
- **Name match drift.** If Odoo's cert name is later renamed (e.g.,
  `"DOT Certified"` → `"DOT (FMCSA) Certified"`), the dict lookup
  silently misses and the cert falls back to a text pill. Mitigation:
  the pill behavior makes this visible (rather than the badge silently
  disappearing), and the fix is a one-line dict edit.

## File touch list

- New: `src/zira_dashboard/cert_icons.py`
- New: `src/zira_dashboard/cert_lookup.py`
- New: `src/zira_dashboard/templates/_cert_badges.html`
- Modified: `src/zira_dashboard/odoo_client.py` (extend
  `SKILL_TYPE_NAMES` tuple)
- Modified: `src/zira_dashboard/app.py` (register
  `cert_icon_svg` Jinja global)
- Modified: `src/zira_dashboard/routes/skills.py` (filter matrix
  columns to exclude `Certifications`; pass `person_certs` into
  template context)
- Modified: `src/zira_dashboard/routes/staffing.py` (pass
  `person_certs` into context for scheduler templates)
- Modified: `src/zira_dashboard/routes/leaderboards.py` (pass
  `person_certs` into context)
- Modified: `src/zira_dashboard/routes/past_schedules.py` (pass
  `person_certs` into context)
- Modified: `src/zira_dashboard/templates/staffing.html` (call macro
  in left-rail items, WC card name labels, time-off chips)
- Modified: `src/zira_dashboard/templates/skills.html` (call macro in
  matrix name column)
- Modified: `src/zira_dashboard/templates/leaderboards.html` (call
  macro in top-5 rows for both WC and group sections)
- Modified: `src/zira_dashboard/templates/past_schedules.html` (call
  macro in day-snapshot name labels)
- Modified: shared base stylesheet (add `.cert-badges`, `.cert-badge`,
  `.cert-pill` styles)
- New: `tests/test_cert_lookup.py` (round-trip: insert person with cert
  in test DB → load_person_certs returns expected mapping)
- New: `tests/test_cert_icons.py` (case-insensitivity, trimming,
  unmapped → None)
