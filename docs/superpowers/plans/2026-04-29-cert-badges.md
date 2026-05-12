# Certification Badges Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render small icon badges next to every employee name across the dashboard, sourced from Odoo's `Certifications` skill_type, so anyone can see at a glance who is forklift / spotter / CDL / DOT certified.

**Architecture:** Reuse the existing `hr.skill` sync pipeline by adding `"Certifications"` to the skill-type filter. A new `cert_lookup.load_person_certs()` returns `{person_name: [cert_name, ...]}` per request. A hardcoded `cert_icons.icon_for()` maps each cert to an inline SVG. A shared Jinja partial defines a `cert_badges(name, person_certs)` macro that every name-rendering template calls.

**Tech Stack:** FastAPI · Jinja2 · psycopg2 · PostgreSQL · inline Lucide-style SVGs

---

## File Structure

**New files:**
- `src/zira_dashboard/cert_icons.py` — hardcoded `{cert_name → SVG}` dict + `icon_for()` lookup
- `src/zira_dashboard/cert_lookup.py` — `load_person_certs() → {name: [cert,...]}` query
- `src/zira_dashboard/templates/_cert_badges.html` — Jinja partial with `cert_badges` macro and `cert_badges_css` macro
- `tests/test_cert_icons.py` — pure-function tests
- `tests/test_cert_lookup.py` — Postgres-backed tests

**Modified files:**
- `src/zira_dashboard/odoo_client.py` — extend `SKILL_TYPE_NAMES`
- `src/zira_dashboard/app.py` — register `cert_icon_svg` Jinja global
- `src/zira_dashboard/routes/skills.py` — filter matrix columns; pass `person_certs`
- `src/zira_dashboard/routes/staffing.py` — pass `person_certs`
- `src/zira_dashboard/routes/leaderboards.py` — pass `person_certs`
- `src/zira_dashboard/routes/past_schedules.py` — pass `person_certs`
- `src/zira_dashboard/templates/skills.html` — call macro in name column
- `src/zira_dashboard/templates/staffing.html` — call macro at every name render point
- `src/zira_dashboard/templates/leaderboards.html` — call macro in top-5 rows
- `src/zira_dashboard/templates/past_schedules.html` — call macro at name labels

---

## Task 1: Extend Odoo skill-type filter

**Files:**
- Modify: `src/zira_dashboard/odoo_client.py:78`

- [ ] **Step 1: Update SKILL_TYPE_NAMES**

Edit `src/zira_dashboard/odoo_client.py` line 78:

```python
SKILL_TYPE_NAMES = ("Production Skills", "Supervisor Skills", "Certifications")
```

- [ ] **Step 2: Verify test still passes**

Run: `pytest tests/test_odoo_client.py -v`
Expected: PASS (any test that previously passed should still pass — the change just lengthens an iterable; existing assertions about Production/Supervisor are unaffected).

If the test asserts an exact tuple length or contents and fails, update its expectation to include `"Certifications"`.

- [ ] **Step 3: Commit**

```bash
git add src/zira_dashboard/odoo_client.py tests/test_odoo_client.py
git commit -m "feat(odoo-sync): pull Certifications skill_type"
```

---

## Task 2: Cert icons module

**Files:**
- Create: `src/zira_dashboard/cert_icons.py`
- Test: `tests/test_cert_icons.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cert_icons.py`:

```python
from zira_dashboard import cert_icons


def test_icon_for_known_forklift_returns_svg():
    svg = cert_icons.icon_for("Forklift Certified")
    assert svg is not None
    assert svg.startswith("<svg")
    assert "</svg>" in svg


def test_icon_for_case_insensitive():
    a = cert_icons.icon_for("Forklift Certified")
    b = cert_icons.icon_for("FORKLIFT CERTIFIED")
    c = cert_icons.icon_for("forklift certified")
    assert a is not None
    assert a == b == c


def test_icon_for_strips_surrounding_whitespace():
    a = cert_icons.icon_for("Forklift Certified")
    b = cert_icons.icon_for("  Forklift Certified  ")
    assert a is not None
    assert a == b


def test_icon_for_unknown_returns_none():
    assert cert_icons.icon_for("Reach Truck Certified") is None
    assert cert_icons.icon_for("") is None


def test_cdl_automatics_and_manuals_share_icon():
    a = cert_icons.icon_for("CDL (Automatics) Certified")
    m = cert_icons.icon_for("CDL (Manuals) Certified")
    assert a is not None
    assert a == m


def test_dot_uses_wrench_distinct_from_others():
    dot = cert_icons.icon_for("DOT Certified")
    fork = cert_icons.icon_for("Forklift Certified")
    assert dot is not None
    assert dot != fork


def test_spotter_icon_distinct_from_cdl():
    spotter = cert_icons.icon_for("Spotter Truck Certified")
    cdl = cert_icons.icon_for("CDL (Automatics) Certified")
    assert spotter is not None
    assert cdl is not None
    assert spotter != cdl
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cert_icons.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'zira_dashboard.cert_icons'`).

- [ ] **Step 3: Create the module**

Create `src/zira_dashboard/cert_icons.py`:

```python
"""Hardcoded cert-name → inline-SVG icon mapping.

Lookup is case-insensitive on the trimmed cert name. Returns None when
no mapping exists; callers render a small text pill in that case so a
new Odoo cert is never silently invisible.

All SVGs use stroke="currentColor" so badges inherit the surrounding
text color (theme-friendly). Sized via CSS, not via SVG width/height
attributes.
"""

from __future__ import annotations

# Lucide "forklift"
_SVG_FORKLIFT = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 12H5a2 2 0 0 0-2 2v5"/>'
    '<circle cx="13" cy="19" r="2"/>'
    '<circle cx="5" cy="19" r="2"/>'
    '<path d="M8 19h3m5-17v17h6M6 12V7c0-1.1.9-2 2-2h3l5 5"/>'
    '</svg>'
)

# Lucide "truck" — used for both CDL variants
_SVG_SEMI = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M14 18V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v11a1 1 0 0 0 1 1h2"/>'
    '<path d="M15 18H9"/>'
    '<path d="M19 18h2a1 1 0 0 0 1-1v-3.65a1 1 0 0 0-.22-.624l-3.48-4.35'
    'A1 1 0 0 0 17.52 8H14"/>'
    '<circle cx="17" cy="18" r="2"/>'
    '<circle cx="7" cy="18" r="2"/>'
    '</svg>'
)

# Lucide "wrench"
_SVG_WRENCH = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77'
    'a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91'
    'a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>'
    '</svg>'
)

# Hand-rolled spotter / yard-truck silhouette: stubby cab-forward + flat
# bed. Visually distinct from the semi-truck used for CDL.
_SVG_SPOTTER = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="2" y="8" width="6" height="8" rx="1"/>'
    '<rect x="8" y="11" width="13" height="5"/>'
    '<circle cx="5" cy="18" r="2"/>'
    '<circle cx="17" cy="18" r="2"/>'
    '<line x1="9" y1="18" x2="15" y2="18"/>'
    '</svg>'
)

_CERT_ICONS: dict[str, str] = {
    "forklift certified": _SVG_FORKLIFT,
    "cdl (automatics) certified": _SVG_SEMI,
    "cdl (manuals) certified": _SVG_SEMI,
    "dot certified": _SVG_WRENCH,
    "spotter truck certified": _SVG_SPOTTER,
}


def icon_for(cert_name: str) -> str | None:
    """Return the inline SVG for the given cert name, or None if unmapped.

    Match is case-insensitive on the trimmed name.
    """
    if not cert_name:
        return None
    return _CERT_ICONS.get(cert_name.strip().lower())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cert_icons.py -v`
Expected: PASS (all seven tests).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/cert_icons.py tests/test_cert_icons.py
git commit -m "feat(cert-badges): cert_icons module with hardcoded SVG mapping"
```

---

## Task 3: Cert lookup helper

**Files:**
- Create: `src/zira_dashboard/cert_lookup.py`
- Test: `tests/test_cert_lookup.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cert_lookup.py`:

```python
import os
import pytest

from zira_dashboard import db, cert_lookup


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)


@pytest.fixture(autouse=True)
def _clean():
    db.execute("DELETE FROM person_skills WHERE person_id IN "
               "(SELECT id FROM people WHERE name LIKE 'TestCertPerson%')")
    db.execute("DELETE FROM people WHERE name LIKE 'TestCertPerson%'")
    db.execute("DELETE FROM skills WHERE name LIKE 'TestCert%'")
    yield
    db.execute("DELETE FROM person_skills WHERE person_id IN "
               "(SELECT id FROM people WHERE name LIKE 'TestCertPerson%')")
    db.execute("DELETE FROM people WHERE name LIKE 'TestCertPerson%'")
    db.execute("DELETE FROM skills WHERE name LIKE 'TestCert%'")


def _insert_person(name: str) -> int:
    rows = db.query(
        "INSERT INTO people (name, active) VALUES (%s, TRUE) RETURNING id",
        (name,),
    )
    return rows[0]["id"]


def _insert_skill(name: str, skill_type: str) -> int:
    rows = db.query(
        "INSERT INTO skills (name, skill_type) VALUES (%s, %s) RETURNING id",
        (name, skill_type),
    )
    return rows[0]["id"]


def _link(person_id: int, skill_id: int, level: int = 3) -> None:
    db.execute(
        "INSERT INTO person_skills (person_id, skill_id, level) "
        "VALUES (%s, %s, %s)",
        (person_id, skill_id, level),
    )


def test_load_person_certs_empty_returns_empty_dict():
    result = cert_lookup.load_person_certs()
    test_rows = {k: v for k, v in result.items() if k.startswith("TestCertPerson")}
    assert test_rows == {}


def test_load_person_certs_groups_certs_by_person():
    pid = _insert_person("TestCertPerson1")
    sid_a = _insert_skill("TestCertA", "Certifications")
    sid_b = _insert_skill("TestCertB", "Certifications")
    _link(pid, sid_a)
    _link(pid, sid_b)

    result = cert_lookup.load_person_certs()
    assert "TestCertPerson1" in result
    assert sorted(result["TestCertPerson1"]) == ["TestCertA", "TestCertB"]


def test_load_person_certs_excludes_non_certification_skill_types():
    pid = _insert_person("TestCertPerson2")
    sid_skill = _insert_skill("TestCertProdSkill", "Production Skills")
    sid_cert = _insert_skill("TestCertReal", "Certifications")
    _link(pid, sid_skill)
    _link(pid, sid_cert)

    result = cert_lookup.load_person_certs()
    assert result.get("TestCertPerson2") == ["TestCertReal"]


def test_load_person_certs_returns_alphabetical_within_person():
    pid = _insert_person("TestCertPerson3")
    sid_z = _insert_skill("TestCertZebra", "Certifications")
    sid_a = _insert_skill("TestCertAlpha", "Certifications")
    _link(pid, sid_z)
    _link(pid, sid_a)

    result = cert_lookup.load_person_certs()
    assert result["TestCertPerson3"] == ["TestCertAlpha", "TestCertZebra"]


def test_load_person_certs_ignores_level():
    pid = _insert_person("TestCertPerson4")
    sid = _insert_skill("TestCertLevelZero", "Certifications")
    _link(pid, sid, level=0)

    result = cert_lookup.load_person_certs()
    assert result.get("TestCertPerson4") == ["TestCertLevelZero"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cert_lookup.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'zira_dashboard.cert_lookup'`).

- [ ] **Step 3: Create the module**

Create `src/zira_dashboard/cert_lookup.py`:

```python
"""Per-request lookup: who has which certifications.

Reads from the local Postgres tables that the Odoo sync populates:
- `skills` rows with skill_type='Certifications' are the cert master list.
- `person_skills` rows link a person to a cert. Any link counts as
  'has this cert' — the level value is ignored (binary semantics).

Cheap single indexed query. Call once per request from any route that
renders names; pass the result into the template context as
`person_certs`.
"""

from __future__ import annotations

from . import db


def load_person_certs() -> dict[str, list[str]]:
    """Return {person_name: [cert_name, ...]} for everyone with at least
    one certification record. Cert lists are alphabetical."""
    sql = """
        SELECT p.name AS person, s.name AS cert
        FROM person_skills ps
        JOIN skills s ON s.id = ps.skill_id
        JOIN people p ON p.id = ps.person_id
        WHERE s.skill_type = 'Certifications'
        ORDER BY p.name, lower(s.name)
    """
    out: dict[str, list[str]] = {}
    for row in db.query(sql):
        out.setdefault(row["person"], []).append(row["cert"])
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `DATABASE_URL=$DATABASE_URL pytest tests/test_cert_lookup.py -v`
Expected: PASS (all five tests). If `DATABASE_URL` isn't set in the local env, all tests are skipped — that's acceptable; the CI/Railway environment runs them.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/cert_lookup.py tests/test_cert_lookup.py
git commit -m "feat(cert-badges): cert_lookup.load_person_certs query"
```

---

## Task 4: Jinja partial + global registration

**Files:**
- Create: `src/zira_dashboard/templates/_cert_badges.html`
- Modify: `src/zira_dashboard/app.py:46-47` (after the `app = FastAPI(...)` line)

- [ ] **Step 1: Create the partial**

Create `src/zira_dashboard/templates/_cert_badges.html`:

```jinja
{# Reusable certification badges.

Usage:
  {% from "_cert_badges.html" import cert_badges, cert_badges_css %}
  ...
  <head>...{{ cert_badges_css() }}</head>
  ...
  <span class="name">{{ name }}</span>{{ cert_badges(name, person_certs) }}

`cert_icon_svg` is registered as a Jinja global in app.py — it returns
the inline SVG string for a known cert, or None for an unmapped cert
(macro then falls back to a text pill).
#}

{% macro cert_badges_css() -%}
<style>
.cert-badges {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: 6px;
  vertical-align: middle;
}
.cert-badge {
  display: inline-flex;
  align-items: center;
}
.cert-badge svg {
  width: 14px;
  height: 14px;
  display: block;
  color: var(--accent, currentColor);
  opacity: 0.85;
}
.cert-pill {
  display: inline-block;
  font-size: 0.65rem;
  font-weight: 600;
  padding: 1px 5px;
  border-radius: 999px;
  border: 1px solid var(--border, #ccc);
  color: var(--muted, #666);
  text-transform: uppercase;
  letter-spacing: 0.02em;
  line-height: 1.3;
}
</style>
{%- endmacro %}

{% macro cert_badges(name, person_certs) -%}
{%- set certs = (person_certs or {}).get(name, []) -%}
{%- if certs -%}
<span class="cert-badges">
{%- for cert in certs -%}
  {%- set svg = cert_icon_svg(cert) -%}
  {%- if svg -%}
    <span class="cert-badge" title="{{ cert }}">{{ svg|safe }}</span>
  {%- else -%}
    <span class="cert-pill" title="{{ cert }}">{{ cert.split()[0]|upper }}</span>
  {%- endif -%}
{%- endfor -%}
</span>
{%- endif -%}
{%- endmacro %}
```

- [ ] **Step 2: Register the Jinja global**

Edit `src/zira_dashboard/app.py`. After the `app = FastAPI(...)` line at line 46, add:

```python
from . import cert_icons
from .deps import templates

templates.env.globals["cert_icon_svg"] = cert_icons.icon_for
```

(Inserted after line 46 and before the `@app.middleware(...)` block. `deps.templates` is the existing `Jinja2Templates` instance the app already uses.)

- [ ] **Step 3: Verify the import does not break startup**

Run: `python -c "from zira_dashboard.app import app; print(app.title)"`
Expected output: `Zira Station Dashboard`. No tracebacks.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/templates/_cert_badges.html src/zira_dashboard/app.py
git commit -m "feat(cert-badges): Jinja partial + global cert_icon_svg helper"
```

---

## Task 5: People Matrix — exclude certifications + render badges

**Files:**
- Modify: `src/zira_dashboard/routes/skills.py:32-37` (column query) and the same handler's template context (around line 43-61)
- Modify: `src/zira_dashboard/templates/skills.html` — name column rendering + import macro + include CSS

- [ ] **Step 1: Filter the matrix column query**

Edit `src/zira_dashboard/routes/skills.py`. Change the `skill_rows` query (currently lines 32–35) to:

```python
    # Columns directly from skills table — exclude certifications
    # (those surface as badges next to names, not as matrix columns).
    skill_rows = db.query(
        "SELECT name, skill_type FROM skills "
        "WHERE skill_type IN ('Production Skills', 'Supervisor Skills') "
        "ORDER BY skill_type, lower(name)"
    )
```

- [ ] **Step 2: Inject person_certs into context**

In the same handler (`staffing_skills`), add the import + lookup at the top of the function, and add `person_certs` to the template context dict.

Add near the top of the function (after the existing `from .. import odoo_sync, ...` line):

```python
    from .. import cert_lookup
    person_certs = cert_lookup.load_person_certs()
```

Add to the `TemplateResponse` context dict (alongside other keys like `"people": roster`):

```python
            "person_certs": person_certs,
```

- [ ] **Step 3: Wire macro into the template**

Edit `src/zira_dashboard/templates/skills.html`. At the top (with the other `{% extends %}` / `{% block %}` directives, or right at file top if there are none), add:

```jinja
{% from "_cert_badges.html" import cert_badges, cert_badges_css %}
```

In the template's `<head>` (or wherever the existing `<style>` block lives), add:

```jinja
{{ cert_badges_css() }}
```

Find the matrix's name column cell — it's the `<th>` or `<td>` that renders `{{ p.name }}` (around line 320, near the existing `active-badge` span). Replace the bare `{{ p.name }}` occurrence with:

```jinja
{{ p.name }}{{ cert_badges(p.name, person_certs) }}
```

Leave the existing `active-badge` span untouched — the cert badges go between the name text and that badge.

- [ ] **Step 4: Smoke test the page renders**

Run the dev server (`python -m zira_dashboard.app` or whatever local command starts it), open `/staffing/skills` in a browser, and verify:
- Page renders with no template error.
- Certification skill_type does NOT appear as a column.
- A person known to have a certification shows the icon next to their name.

If the page errors on `cert_icon_svg is undefined`, the Jinja global registration in Task 4 didn't take effect — verify the `app.py` edit landed.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/routes/skills.py src/zira_dashboard/templates/skills.html
git commit -m "feat(cert-badges): exclude certs from matrix grid + render badges"
```

---

## Task 6: Scheduler — render badges across all name surfaces

**Files:**
- Modify: `src/zira_dashboard/routes/staffing.py` — pass `person_certs` into context for the main scheduler GET handler
- Modify: `src/zira_dashboard/templates/staffing.html` — import macro, include CSS, call macro at every name render point (Unscheduled list, Reserves list, WC card name labels, time-off chips)

- [ ] **Step 1: Inject person_certs into the scheduler route**

Edit `src/zira_dashboard/routes/staffing.py`. Find the main scheduler GET handler (the one that renders `staffing.html`). Add at the top of that handler (next to existing imports/lookups):

```python
    from .. import cert_lookup
    person_certs = cert_lookup.load_person_certs()
```

In the `TemplateResponse` context dict for `staffing.html`, add:

```python
            "person_certs": person_certs,
```

- [ ] **Step 2: Import macro + CSS into the template**

Edit `src/zira_dashboard/templates/staffing.html`. At the top:

```jinja
{% from "_cert_badges.html" import cert_badges, cert_badges_css %}
```

In the existing `<head>` `<style>` area:

```jinja
{{ cert_badges_css() }}
```

- [ ] **Step 3: Add badges to every name render point**

Search the template for places that render a person's name as visible text. Each one becomes:

```jinja
{{ name_var }}{{ cert_badges(name_var, person_certs) }}
```

The render points to update (search for these patterns):
1. **Unscheduled list items** — the `.dd-item` (or equivalent) blocks in the left rail. Each item displays a person name; add the macro right after.
2. **Reserves list items** — the corresponding reserves block. Same treatment.
3. **WC card assignment cells** — wherever an assigned person's name renders inside a work-center card. Some of those are JS-injected; if a name-rendering JS block exists, leave it for Step 4.
4. **Time-off chips** — the rendered chips on the time-off panel/list view.

For each location, preserve any surrounding markup (links, badges, etc.) and append `{{ cert_badges(name, person_certs) }}` immediately after the visible name text.

- [ ] **Step 4: Handle JS-injected names (if any)**

If the scheduler template injects names via JavaScript (e.g., the addToUnscheduled / addToReserves / addBackToCorrectList helpers), those rendered DOM nodes will not pick up the badges automatically.

For each JS function that creates a name-displaying DOM node, expose `person_certs` to JS by emitting a `<script>` block in the template body:

```jinja
<script>
  window.PERSON_CERTS = {{ person_certs|tojson }};
</script>
```

Then, in each helper that builds a name node, append cert badge spans:

```javascript
function appendCertBadges(parentEl, name) {
  const certs = (window.PERSON_CERTS || {})[name] || [];
  if (!certs.length) return;
  const wrap = document.createElement('span');
  wrap.className = 'cert-badges';
  for (const cert of certs) {
    const span = document.createElement('span');
    span.className = 'cert-badge-js';
    span.title = cert;
    span.textContent = cert.split(' ')[0].toUpperCase();
    span.style.cssText =
      'font-size:0.65rem;font-weight:600;padding:1px 5px;' +
      'border-radius:999px;border:1px solid #ccc;color:#666;' +
      'text-transform:uppercase;margin-left:4px;';
    wrap.appendChild(span);
  }
  parentEl.appendChild(wrap);
}
```

(The JS helper uses text pills only — keeping it simple. Server-rendered HTML uses real SVGs via the macro. Live-added rows after a drag/drop revert to text pills until next page reload; acceptable trade-off.)

Call `appendCertBadges(itemEl, name)` from `addToUnscheduled`, `addToReserves`, and `addBackToCorrectList` after the name node is appended.

- [ ] **Step 5: Smoke test**

Open `/staffing` in a browser. Verify:
- Unscheduled rail shows badges next to names with certs.
- Reserves rail shows badges.
- Assigning a person to a WC keeps their badges visible.
- Removing a person (clicking the X) and watching them re-appear in Unscheduled or Reserves: text-pill badges appear via the JS helper.
- Time-off chips show badges.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/staffing.py src/zira_dashboard/templates/staffing.html
git commit -m "feat(cert-badges): scheduler renders cert badges everywhere names appear"
```

---

## Task 7: Leaderboards — render badges in top-5 rows

**Files:**
- Modify: `src/zira_dashboard/routes/leaderboards.py` — pass `person_certs` into context
- Modify: `src/zira_dashboard/templates/leaderboards.html` — import macro, include CSS, call macro after operator names

- [ ] **Step 1: Inject person_certs into the route**

Edit `src/zira_dashboard/routes/leaderboards.py`. In the GET handler that renders `leaderboards.html`, add:

```python
    from .. import cert_lookup
    person_certs = cert_lookup.load_person_certs()
```

Add to the `TemplateResponse` context dict:

```python
            "person_certs": person_certs,
```

- [ ] **Step 2: Wire macro into the template**

Edit `src/zira_dashboard/templates/leaderboards.html`. At the top:

```jinja
{% from "_cert_badges.html" import cert_badges, cert_badges_css %}
```

In `<head>`'s `<style>` section:

```jinja
{{ cert_badges_css() }}
```

Find each top-5 row that renders an operator name (search for `row.person` or whatever the variable is named — likely something like `{{ row.person }} ({{ row.name_count }})`). Replace with:

```jinja
{{ row.person }}{{ cert_badges(row.person, person_certs) }} ({{ row.name_count }})
```

Apply this to **both** the WC sections and the group sections — both render the same row shape, so two render-point edits.

- [ ] **Step 3: Smoke test**

Open `/staffing/leaderboards` in a browser. Verify each top-5 row that lists a certified operator shows the badge next to their name, before the `(N)` days suffix.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/routes/leaderboards.py src/zira_dashboard/templates/leaderboards.html
git commit -m "feat(cert-badges): leaderboards top-5 rows show cert badges"
```

---

## Task 8: Past Schedules — render badges at name labels

**Files:**
- Modify: `src/zira_dashboard/routes/past_schedules.py` — pass `person_certs` into context
- Modify: `src/zira_dashboard/templates/past_schedules.html` — import macro, include CSS, call macro at name labels

- [ ] **Step 1: Inject person_certs into the route**

Edit `src/zira_dashboard/routes/past_schedules.py`. In each GET handler that renders `past_schedules.html` (or the day-snapshot view), add:

```python
    from .. import cert_lookup
    person_certs = cert_lookup.load_person_certs()
```

Add to each `TemplateResponse` context dict:

```python
            "person_certs": person_certs,
```

- [ ] **Step 2: Wire macro into the template**

Edit `src/zira_dashboard/templates/past_schedules.html`. At the top:

```jinja
{% from "_cert_badges.html" import cert_badges, cert_badges_css %}
```

In `<head>`'s `<style>` section:

```jinja
{{ cert_badges_css() }}
```

Find every place a person name renders in the day snapshot view (assignment cells, time-off chips). For each visible name, append:

```jinja
{{ name_var }}{{ cert_badges(name_var, person_certs) }}
```

- [ ] **Step 3: Smoke test**

Open `/staffing/past-schedules` (or whatever the route path is) and view a day's snapshot. Verify badges appear next to certified operators.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/routes/past_schedules.py src/zira_dashboard/templates/past_schedules.html
git commit -m "feat(cert-badges): past schedules show cert badges"
```

---

## Task 9: Full smoke test + verification

- [ ] **Step 1: Trigger Odoo refresh**

In the dashboard UI, click **Refresh from Odoo** on the People Matrix page. Wait for the success indicator. This populates the local `skills` table with the `Certifications` rows and the `person_skills` links.

- [ ] **Step 2: Verify Postgres state**

Run a quick query against the live DB:

```bash
railway connect Postgres
```

Then in psql:

```sql
SELECT skill_type, COUNT(*) FROM skills GROUP BY skill_type;
SELECT s.name FROM skills s WHERE s.skill_type = 'Certifications' ORDER BY name;
```

Expected: a row for `Certifications` in the count, and the five known cert names in the list.

- [ ] **Step 3: Walk every page**

For each page below, verify that at least one certified employee is showing a badge with the correct icon (check tooltip on hover):

- `/staffing/skills` — People Matrix: badges next to names; certifications NOT in column header list.
- `/staffing` — Scheduler: badges in Unscheduled, Reserves, WC card assignments, time-off chips.
- `/staffing/leaderboards` — Top-5 rows in both WC and group sections.
- `/staffing/past-schedules` — Day snapshot view.

- [ ] **Step 4: Verify each cert icon renders distinctly**

Find a person with each cert (or temporarily assign for testing) and visually confirm:
- Forklift cert → forklift icon (with mast/forks visible)
- CDL Automatics → semi-truck icon (cab + box trailer profile)
- CDL Manuals → same semi-truck icon (hover differentiates)
- DOT cert → wrench icon
- Spotter Truck cert → stubby truck icon (visually distinct from CDL semi)

- [ ] **Step 5: Verify unmapped-cert fallback**

In Odoo, temporarily add a new cert (e.g., `Reach Truck Certified`) to one employee and refresh. Verify the dashboard shows a small text pill with `REACH` next to that person's name. Remove the test cert in Odoo afterward.

- [ ] **Step 6: Commit any final adjustments**

If steps 1–5 surfaced cosmetic issues (size, gap, alignment) tweak the CSS in `_cert_badges.html` and commit:

```bash
git add src/zira_dashboard/templates/_cert_badges.html
git commit -m "fix(cert-badges): tune badge sizing/spacing after smoke test"
```

If everything renders cleanly, no commit needed — feature is shipped.

---

## Acceptance Recap

After all tasks merge:

- ✅ Odoo `Certifications` skill_type rows pulled into local `skills` table.
- ✅ People Matrix grid does NOT show certification columns.
- ✅ Every page that renders an operator name shows their cert badges immediately after the name.
- ✅ Forklift / CDL (both) / DOT / Spotter Truck each render with their hardcoded SVG.
- ✅ Hover tooltip on each badge shows the full cert name.
- ✅ A cert added in Odoo without a code mapping renders as a small uppercase text pill.
- ✅ Person with no certs shows their name unchanged.
