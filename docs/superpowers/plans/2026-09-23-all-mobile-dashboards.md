# Complete Mobile Manager Dashboards Implementation Plan

**Approval:** Dale approved the Recycling leaderboard mockup and requested all remaining Performance dashboards use that design and the shipped mobile Recycling design. Continue through deployment and live verification without another approval gate.

**Goal:** All nine manager Performance views work on phones while laptop and TV views remain unchanged.

**Architecture:** Share a phone navigation shell at <=760px on non-TV Performance pages. Reuse Recycling's 120%-goal production and connected downtime presentation for New/Operator. Give the two department leaderboards the approved operator-name-first, two-average row layout and compact monthly awards. Reflow People, general Leaderboards, Forklift, and Trophies without changing data, permissions, actions, or live refresh.

## Global constraints

- Only Performance views are in scope: Recycling, New, Operator, People, Recycling leaderboard, New leaderboard, Leaderboards, Forklift, Trophies.
- Desktop and TV appearance and behavior remain unchanged, including narrow TV viewports.
- Phone presentation applies at widths <=760px; prevent mobile or breakpoint transitions from saving desktop grid layouts.
- Keep all metrics and authorized actions available, including history, range controls, warnings, dialogs and operator links. Use existing server values; do not invent goals, ranks, eligibility, or attribution.
- Controls have usable touch targets, long names wrap, pages and dialogs fit 320px without page-level horizontal scrolling.
- No production data edits are required. Use local fixture data for testing.
- Preserve unrelated untracked files. Commit and push to origin/main. Verify deployed assets and live pages, and wait for required CI.

## Task 1 — Shared shell and integration (coordinator)

Files: `_base_app.html`, new `performance-mobile.css/js`, `dashboard-grid.js`, tests for shell/navigation and integration.

- [ ] Capture immutable pre-change templates/static for desktop/TV screenshot comparisons.
- [ ] Add inactive-by-default phone assets; activate only on Performance pages with sub-navigation and never TV.
- [ ] Reuse authorized subnav links for the dashboard selector; compact top navigation into a touch-sized Menu. Preserve the already shipped Recycling shell.
- [ ] Generalize grid mobile guard only to explicitly opted-in New and Operator pages.
- [ ] Verify menu, switching pages, ranges, and breakpoint transitions.

## Task 2 — Recycling and New leaderboards (independent implementer)

Files: department leaderboard templates, new leaderboard-mobile assets/partial and isolated tests.

- [ ] Match approved mockup: full operator name above year-to-date/last30-day averages, readable day counts, qualifying thresholds in accessible details.
- [ ] Keep Repairs/Dismantlers and New work-center families, real eligibility messages, full dates, and monthly awards. Stack awards and remove empty fixed-height areas on phones only.
- [ ] Use shared phone shell; preserve desktop and TV markup/presentation.
- [ ] Browser-check 320/390/760px and baseline desktop/TV screenshots.

## Task 3 — New and Operator production (independent implementer)

Files: New/Operator templates, phone-specific production templates/assets and isolated tests.

- [ ] Adapt approved Recycling patterns: operator-first labels with muted work centers; Now at100% on a120% goal track; red below goal, green at/above; neutral missing goal. Historical targets labelled Goal.
- [ ] Preserve operators/stint attribution, links, work-center/date selection, all production/progress/uptime/downtime/GOAT/ribbon metrics in usable cards/details.
- [ ] New/Operator opt into grid guard with `data-production-mobile` on non-TV html.
- [ ] Browser-check mobile, missing data, multi-operator data, and baseline desktop/TV screenshots.

## Task 4 — People, general Leaderboards, Forklift, Trophies (independent implementer)

Files: view-specific templates and new phone-only CSS/JS, isolated tests.

- [ ] Stack People timelines full width under names; preserve filters, live updates and warning/assignment actions.
- [ ] Make general/Forklift ranking rows readable with explicit labels and preserve sorting, drill-down, dates, categories and all metrics.
- [ ] Contain trophy cards, controls, award dialogs, long names and month/year pickers on phones.
- [ ] Verify at320/390/760, all authorized actions and modal bounds; compare desktop rendering unchanged.

## Task 5 — Verification and delivery (coordinator)

- [ ] Review each implementer's diff and resolve material findings.
- [ ] Run all new browser/template tests and applicable existing suites with an isolated local PostgreSQL database; run lint and JS syntax checks.
- [ ] Compare desktop and TV against the captured baseline and inspect phone screenshots for every view.
- [ ] Add plain-language What's New notes, commit scoped implementation and push origin/main.
- [ ] Wait for CI and Railway deployment matching the pushed commit. Verify health, exact deployed mobile assets, and real authenticated page rendering where available.
- [ ] Deliver only when live verification succeeds, or report a genuine external blocker with precise evidence.
