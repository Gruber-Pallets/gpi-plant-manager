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

Files: `recycling.html`, new `recycling-mobile.css` and `recycling-mobile.js`, `dashboard-grid.js`, new `_recycling_mobile.html`, `tests/test_mobile_recycling.py`.

- [ ] Add fixture-rendered tests for 120% track, Now marker, strict goal colors, empty goals, escaped/wrapped names, and TV exclusion.
- [ ] Render mobile production and connected downtime macros from existing context next to desktop-only content. Scale actual/goal to 120%, sort downtime descending with a shared scale, label historical targets Goal.
- [ ] Add non-TV Performance shell hooks and phone menu/dashboard selector. Keep desktop markup visually unchanged.
- [ ] Skip GridStack on phones and reload on crossing the breakpoint so viewport changes cannot save resized layouts. Preserve work-center selection before the phone early return.
- [ ] Style cards, charts, toolbars, and controls only inside the phone breakpoint. Preserve all widgets; provide tap-accessible chart detail.
- [ ] Run `.venv/bin/python -m pytest tests/test_mobile_recycling.py tests/test_new_dashboard_template.py tests/test_wc_dashboard.py tests/test_dashboards_polish.py`.

### Task 2: Integration and delivery

- [ ] Run browser fixtures for Recycling, including actual navigation, details, and no mobile layout writes. Capture mobile screenshots and compare desktop/TV before and after.
- [ ] Run relevant existing pytest suites and syntax/diff checks. Review the complete diff independently and fix material findings.
- [ ] Add plain-language What's New notes. Commit only scoped files and push to origin/main.
- [ ] Report verified results and any remaining limitation accurately.
