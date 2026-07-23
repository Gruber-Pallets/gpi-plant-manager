# Kid-Friendly What's New Notes Design

**Date:** 2026-07-23

## Goal

Make every future What's New entry easy for a 10-year-old to understand.

## Scope

This applies to the patch notes added for every push to `main`. Existing
historical entries stay as they are.

## Design

Add the same permanent writing standard in two places:

1. Project instructions, so people and coding assistants follow it whenever
   they prepare a push to `main`.
2. The `CHANGELOG.md` format comment, so the rule is visible beside the notes
   themselves.

Every new entry will:

- Say what changed in plain words.
- Say how the change helps the person using the app when useful.
- Use short sentences and common words.
- Avoid developer-only details, code names, routes, and implementation steps.
- Explain any necessary unfamiliar word immediately.

For example, instead of "The kiosk is htmx-boosted and now swaps non-2xx
responses," write "Time-off requests now show an explanation when something
needs fixing, instead of looking like the button did nothing."

## Alternatives Considered

- **Instruction only:** easier to add but can be missed during direct
  changelog edits.
- **Automated readability gate:** rigid and likely to reject clear, useful
  notes based on subjective wording.

Keeping the rule in both the project instructions and the changelog is the
recommended approach because it is clear at both decision points without
blocking releases.

## Validation

Review the final instructions and changelog comment to confirm they require
plain, user-focused wording for future main-branch patch notes. No application
behavior changes or runtime tests are needed.
