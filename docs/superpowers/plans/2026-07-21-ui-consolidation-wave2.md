# UI Consolidation Wave 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the five TV-shared dashboard templates (`recycling.html`, `new_dept.html`, `wc_dashboard.html`, `new_leaderboard_tv.html`, `recycling_leaderboard_tv.html`) to extend `_base_app.html`, one template per commit, without any behavior change on the live plant-floor TVs.

**Architecture:** Same chrome-block refactor as Wave 1, plus three additive hooks on `_base_app.html` that the TV pages need: `html_attrs` (conditional `data-tv-theme`), `body_attrs` (page body classes), and an overridable `title_tag` (the leaderboard TV pages use suffix-less titles). TV pages override `header`/`subnav`/`footer` wholesale to preserve their `tv_mode` conditionals byte-for-byte. Body markup moves verbatim.

**Live-TV gates (binding, from the spec):** per template — ratchet red→green, the template's existing render tests for BOTH desktop and TV variants, `tests/test_recycling_scaling_static.py` where applicable, a new TV chrome-absence test, and preview screenshots of desktop + TV variants before commit. Deploy of these commits happens off-shift (TVs repaint within 60s).

**Spec:** `docs/superpowers/specs/2026-07-21-ui-consolidation.md`

**Test env:** `DATABASE_URL=<local pgserver> ZIRA_API_KEY=test .venv/bin/python -m pytest`

---

### Task 1: Base hooks for TV pages

**Files:** Modify `src/zira_dashboard/templates/_base_app.html`

- [ ] **Step 1: Add the three hooks** — change the `<html>`, `<title>`, and `<body>` lines to:

```jinja
<html lang="en"{% block html_attrs %}{% endblock %}>
...
{% block title_tag %}<title>{% block title %}Home{% endblock %} — GPI Plant Manager</title>{% endblock %}
...
<body{% block body_attrs %}{% endblock %}>
```

- [ ] **Step 2: Verify Wave 1 pages unaffected**

Run: `pytest tests/test_base_app_template.py tests/test_exception_inbox.py tests/test_page_usage_route.py -q` → all pass (blocks default to empty / identical output).

- [ ] **Step 3: Commit** — `feat: base hooks for TV page conversion (html_attrs, body_attrs, title_tag)`

---

### Task 2: Convert `recycling.html`

**Files:** Modify `src/zira_dashboard/templates/recycling.html`, `tests/test_base_app_template.py` (allowlist), `tests/test_tv_dashboards_vs.py` (TV chrome test)

Mapping (body markup verbatim):
- Imports (`{% from %}` lines 15–21, plus `_tv_header.html` import currently inside the body's tv branch) → top of file after `{% extends %}`.
- `html_attrs` ← `{% if tv_mode %} data-tv-theme="{{ tv_theme or 'dark' }}"{% endif %}`
- `title` ← `{% if tv_mode %}TV · {% endif %}Departments — Recycling`
- `head` ← gridstack css, recycling.css, goat_watch.css, conditional tv-mode.css, `<style>{{ goat_badges_css() }}</style>`, dashboards-subnav.css. (favicon/viewport/topnav.css come from the base — delete.)
- `header` (whole-block override) ← the `{% if tv_mode %}{% call tv_header(...) %}...{% endcall %}{% else %}<header>topnav</header>{% endif %}` conditional.
- `subnav` ← `{% if not tv_mode %}` dashboards subnav include + `.rc-toolbar` form + goat-watch banner `{% endif %}`.
- Top-level macros (`widget_attrs` etc.) stay top-level after the imports.
- `content` ← everything currently inside `<main>…</main>`.
- `footer` ← `{% if not tv_mode %}{% include '_footer.html' %}{% endif %}`.
- `scripts` ← gridstack-all.js, dashboard-grid.js, conditional assign-popover.js, conditional tv-refresh.js, `{{ hover_tip_clamp_script() }}`.

- [ ] Shrink allowlist (`recycling.html` out) → ratchet red.
- [ ] Add to `tests/test_tv_dashboards_vs.py` (reuse its `_stub_data`):

```python
def test_tv_recycling_has_no_desktop_chrome(monkeypatch):
    _stub_data(monkeypatch)
    client = TestClient(app)
    html = client.get("/tv/recycling").text
    assert 'data-tv-theme="dark"' in html
    assert 'class="brand-row"' not in html      # no topnav
    assert "changelog-modal" not in html        # no footer
    assert html.lower().count("<!doctype") == 1
```

- [ ] Convert; run `pytest tests/test_base_app_template.py tests/test_tv_dashboards_vs.py tests/test_dashboards_polish.py tests/test_recycling_scaling_static.py -q` → green.
- [ ] Preview screenshots: regenerate snapshots (dump script pattern from Wave 1 / `scripts/preview_recycling.py`) for `/recycling` and `/tv/recycling?theme=dark`; visually compare chrome.
- [ ] Commit — `refactor: recycling dashboard extends _base_app`

---

### Task 3: Convert `new_dept.html`

Same mapping as Task 2 with: `title` ← `{% if tv_mode %}TV · {% endif %}Departments — New`; toolbar form action `/new`; extra import `_cumulative_progress_chart.html`; tv_header args `("New", crumb="DEPARTMENTS · TODAY")`.

- [ ] Allowlist shrink → red; TV chrome test for `/tv/new` (same asserts, `_empty_new_day` stubs in test_tv_dashboards_vs.py).
- [ ] Convert; run `pytest tests/test_base_app_template.py tests/test_tv_dashboards_vs.py tests/test_new_dashboard_data.py -q` → green.
- [ ] Preview screenshots `/new` + `/tv/new`.
- [ ] Commit — `refactor: new-department dashboard extends _base_app`

---

### Task 4: Convert `wc_dashboard.html`

Mapping deltas: imports + macros already top-level (keep, after `{% extends %}`); `body_attrs` ← ` class="wc-dashboard"`; `title` ← `{% if tv_mode %}TV · {% endif %}{{ wc_name }}`; `head` ← wc_dashboard.css + recycling.css + goat_watch.css + conditional (tv-mode.css **and tv-refresh.js**) + dashboards-subnav.css + goat_badges_css style; `header` ← the tv_header/topnav conditional; `subnav` ← `{% if not tv_mode %}` subnav include + unified chrome strip `{% endif %}`; `content` ← `<main>` innards; `footer` ← empty override (`{% block footer %}{% endblock %}` — this page has never had a footer); `scripts` ← gridstack-all.js + dashboard-grid.js + hover_tip_clamp_script.

- [ ] Allowlist shrink → red; TV chrome test in `tests/test_wc_dashboard.py` (reuse `_stub_wc`): `/tv/wc/repair-1` has `data-tv-theme`, no brand-row, single doctype.
- [ ] Run `pytest tests/test_base_app_template.py tests/test_wc_dashboard.py tests/test_tv_displays_routes.py -q` → green (dispatch-path test included — the July 10 incident regression guard).
- [ ] Preview screenshots `/wc/{slug}` + `/tv/wc/{slug}`.
- [ ] Commit — `refactor: operator wc dashboard extends _base_app`

---

### Task 5: Convert `new_leaderboard_tv.html`

TV-first page: `{% set is_tv = tv_mode | default(true) %}` stays top-level after `{% extends %}` (verify blocks see it; if not, set inside each block). Mapping: `html_attrs` ← `{% if is_tv %} data-tv-theme=...{% endif %}`; **`title_tag` full override** to preserve the exact current titles (TV: `New-Leaderboard`, desktop: `New-Leaderboard - GPI Plant Manager`); `head` ← conditional tv-mode.css / (topnav.css + dashboards-subnav.css) + recycling_leaderboard.css + new_leaderboard.css + conditional tv-refresh — **note: desktop branch must keep topnav.css conditional exactly as today; the base also emits topnav.css unconditionally, which is harmless (one duplicate link on TV) — instead DELETE the child's topnav.css line and rely on the base's**; `body_attrs` ← the class conditional; `header` ← tv_header call / desktop topnav conditional; `subnav` ← `{% if not is_tv %}{% include "_dashboards_subnav.html" %}{% endif %}`; `main_attrs` ← ` class="rlb-main"`; `content` ← main innards; `footer` ← empty override (page has no footer today).

- [ ] Allowlist shrink → red; chrome tests in `tests/test_new_leaderboard_routes.py`: TV variant no brand-row + data-tv-theme; desktop variant single brand-row.
- [ ] Run `pytest tests/test_base_app_template.py tests/test_new_leaderboard_routes.py tests/test_new_leaderboard_static.py -q` → green.
- [ ] Preview screenshots both variants.
- [ ] Commit — `refactor: new-leaderboard page extends _base_app`

---

### Task 6: Convert `recycling_leaderboard_tv.html`

Identical pattern to Task 5 (titles `Recycling-leaderboard` / `Recycling-leaderboard - GPI Plant Manager`; only recycling_leaderboard.css; `range_meta` set-block stays inside the `header` override with the tv_header call).

- [ ] Allowlist shrink → red; chrome tests in `tests/test_recycling_leaderboard_tv.py`.
- [ ] Run `pytest tests/test_base_app_template.py tests/test_recycling_leaderboard_tv.py tests/test_recycling_leaderboard_static.py -q` → green.
- [ ] Preview screenshots both variants.
- [ ] Commit — `refactor: recycling-leaderboard page extends _base_app`

---

### Task 7: Wave 2 wrap-up

- [ ] Full suite; compare to the Wave 1 baseline (2,373 passed / 20 skipped / 4 pre-existing failures).
- [ ] Screenshot all five TV variants + desktop variants from snapshots; confirm chrome identical.
- [ ] Report to Dale: commits, test counts, deploy-off-shift reminder, unpushed-commits check (`git log origin/main..HEAD`).
