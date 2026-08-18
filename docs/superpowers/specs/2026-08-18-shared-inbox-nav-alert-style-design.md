# Shared Inbox Navigation Alert Style — Design

## Goal

Show the Inbox link and its count in red on every normal desktop page whenever
the Exception Inbox has one or more open items. TV pages remain unchanged.

## Current behavior

`_topnav.html` already renders the Inbox count and adds `has-open` when the
server summary has a positive total. The red `has-open` CSS rule currently
lives in `footer.css`. Several normal dashboard and leaderboard templates
intentionally omit the shared footer, so they receive the count but not the
red visual treatment.

## Design

Move the Inbox navigation layout and state rules from `footer.css` to
`topnav.css`. `_base_app.html` loads `topnav.css` for every normal page that
uses `_topnav.html`, including the affected dashboards and leaderboards.

The existing server-rendered classes and client-side summary logic remain
unchanged. This keeps the change limited to presentation and does not add the
footer's changelog, feedback UI, or unrelated scripts to pages that suppress
the footer.

## Verification

Add a regression test that establishes `topnav.css` as the owner of the
`has-open` red styling and verifies the old footer stylesheet no longer owns
it. Run the focused top-nav Inbox test file, then the relevant broader test
suite and lint checks before the implementation commit.

## Scope limits

- No changes to Inbox counting, urgency, polling, or API responses.
- No changes to TV headers or TV display styling.
- No dashboard-specific CSS duplication.
