# Mobile Dashboards Implementation Plan

> **For agentic workers:** Use subagent-driven-development for isolated tasks; the coordinator integrates and validates.

**Goal:** Implement Dale's approved mobile Recycling dashboard design first.

**Architecture:** Add phone-only styles and markup consuming existing server data. Keep desktop and TV markup intact; never initialize the desktop layout editor on a phone. Scope all phone rules to non-TV Performance pages at max-width 760px.

**Tech Stack:** FastAPI, Jinja, CSS, vanilla JavaScript, pytest, Playwright.

## Global Constraints

- Desktop and TV appearance and behavior remain unchanged.
- Only Recycling is in this build. All other views are deferred by Dale until after Recycling review.
- Current server goal and attribution data remain authoritative.
- Mobile must not write saved desktop layouts.
- Preserve permissions, links, date ranges, live refresh, and access to details.
- Do not alter unrelated untracked files.

### Task 1: Phone dashboard shell and production cards

Files: `recycling.html`, new `recycling-mobile.css` and `recycling-mobile.js`, `dashboard-grid.js`, new `_recycling_mobile.html`, `tests/test_mobile_recycling.py`, `tests/test_mobile_recycling_browser.py`.

- [x] Add fixture-rendered tests for 120% track, Now marker, strict goal colors, empty goals, escaped/wrapped names, and TV exclusion.
- [x] Render mobile production and connected downtime macros from existing context next to desktop-only content. Scale actual/goal to 120%, sort downtime descending with a shared scale, label historical targets Goal.
- [x] Add non-TV Performance shell hooks and phone menu/dashboard selector. Keep desktop markup visually unchanged.
- [x] Skip GridStack on phones and reload on crossing the breakpoint so viewport changes cannot save resized layouts. Leave other dashboards, including the Operator work-center selector, on their existing initialization path.
- [x] Style cards, charts, toolbars, and controls only inside the phone breakpoint. Preserve all widgets; provide tap-accessible chart detail.
- [x] Run `.venv/bin/python -m pytest tests/test_mobile_recycling.py tests/test_new_dashboard_template.py tests/test_wc_dashboard.py tests/test_dashboards_polish.py`.

### Task 2: Integration and delivery

- [x] Run browser fixtures for Recycling, including actual navigation, details, and no mobile layout writes. Capture mobile screenshots and compare desktop/TV before and after.
- [x] Run relevant existing pytest suites and syntax/diff checks. Review the complete diff independently and fix material findings.
- [x] Add plain-language What's New notes. Commit only scoped files and push to origin/main.
- [x] Report verified results and any remaining limitation accurately.

## Verification record

- Deterministic browser fixtures exercised Recycling at 320, 390, and 760px. No horizontal overflow and no desktop layout writes on phone use or breakpoint transitions.
- Before/after desktop (1440px), TV (1920px), and narrow TV (390px) screenshots matched pixel-for-pixel.
- Independent review findings resolved: phone assignment popup is contained with 44px controls; existing summary metrics and operator links remain available in expanded details.
- Database-backed dashboard checks used a fresh temporary local PostgreSQL database, with no production access: 36 passed, 4 existing skips.
- Other Performance dashboards remain deferred by explicit user request.
- Final checks: 140 pure/template tests passed; 13 browser tests passed; 36 database-backed tests passed with 4 existing skips. Ruff and JavaScript syntax checks passed.
