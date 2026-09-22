# Inbox reconciliation and skills-cache fixes — September 22, 2026

Both reported baseline failures were reproduced against disposable local Postgres before editing production code.

## Root causes and changes

- The quick-punch section entered the open-item mirror under its raw section ID but was absent from the complete-source mapping. It could therefore never report a resolved departure. New rows now use the existing quick-punch audit kind; both that kind and the legacy stored kind can reconcile.
- Quick-punch reads swallowed exceptions and returned an empty list. That would make wiring reconciliation unsafe: an unavailable source could appear successfully empty. Read errors now reach the existing snapshot error capture so the source remains incomplete and cannot resolve stored alerts. Normal grace periods and human-event checks remain unchanged.
- The skills-cache test inspected a pre-permissions raw key. Cache keys are now scoped by role. The updated test uses the cache API, verifies admin/manager/system caches exist after reads, checks the save response, and confirms every role's cached page is invalidated. Test setup and teardown clear the stable bucket to avoid order dependence. Production cache and permission behavior is unchanged.

## Verification

Ten new quick-punch regressions cover canonical audit kind, legacy/current departures, truncated/errored sources, and errors at job/event/acknowledgment reads. Before the fix, six failed and four safety cases passed. After the fix, all ten pass. Related inbox/cache/access-control checks: 142 passed.

Full database-backed suite in the isolated checkout: **7,287 passed, 21 skipped, zero failures**. Ruff across src/tests/scripts and whitespace checks passed. The concurrent timeclock-lightbulb commit was incorporated before delivery. Final integration checks after incorporating that commit: **148 passed**.

Implementation `45ac653f` was pushed to `origin/main`. [CI run 35744688224](https://github.com/Gruber-Pallets/gpi-plant-manager/actions/runs/35744688224) succeeded: **7,279 passed, 33 skipped, zero failures**; lint and attendance transaction checks also passed. Railway deployment `2fe1e86c-7be5-49dc-864f-6d142760d822` reached SUCCESS for the exact implementation commit. The live health endpoint returned `{"ok":true}`. The temporary local QA database container was stopped.
