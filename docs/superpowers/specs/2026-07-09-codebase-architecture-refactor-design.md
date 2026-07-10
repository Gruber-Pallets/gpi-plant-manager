# Codebase architecture refactor — design

**Date:** 2026-07-09<br>
**Branch:** `codex/codebase-architecture-refactor`<br>
**Goal:** Make GPI Plant Manager easier and safer to extend by decomposing its
highest-friction internals while preserving every user-visible behavior, route,
integration contract, and rendered appearance.

## Baseline and survey

The repository already completed the July 6 cleanup that introduced
`CachedSingleton`, deduplicated time parsing, added small performance fixes, and
modernized safe Python idioms. This pass will not repeat that work.

Current local safety baseline:

- `ruff check src tests scripts`: clean.
- `pytest -q`: 1,413 passed, 294 skipped, 5 third-party deprecation warnings.
- The skipped tests require Postgres and are run by the existing GitHub Actions
  job with a Postgres 16 service.
- The existing user-facing stack is server-rendered FastAPI + Jinja + HTMX,
  with plain JavaScript and CSS assets. No framework migration is warranted.

The highest-cost maintenance seams are:

- `odoo_client.py` is 1,629 lines and combines XML-RPC transport, authentication,
  skills, employee calendars, attendance, time off, and feedback-task behavior.
- Several route modules mix HTTP concerns with large context builders or command
  workflows, particularly settings, recycling dashboards, and exception-inbox
  breakdown actions.
- The expanded Ruff audit reports concentrated complexity in those orchestration
  functions, while the configured high-signal `F` rules remain clean.
- Existing tests and application modules monkeypatch names on
  `zira_dashboard.odoo_client`, including private cache and helper seams. A naive
  package split would silently bypass those patches and is therefore unsafe.

## Chosen approach

Use compatibility-first decomposition. Existing modules remain stable facades;
focused internal modules receive dependencies explicitly. This creates clearer
boundaries without forcing a repository-wide import migration or changing runtime
behavior.

Rejected alternatives:

1. A full internal rewrite would produce a cleaner theoretical end state but
   carries excessive risk in live Odoo, timeclock, and time-off workflows.
2. A repository-wide formatter and expanded-lint sweep would touch roughly 330
   files, bury architectural changes in noise, and provide little immediate
   product value.
3. Splitting files solely by line count would create arbitrary modules. Large
   cohesive files stay intact unless the extraction creates a named, testable
   responsibility.

## Compatibility contract

This refactor must preserve:

- Every HTTP route path, method, parameter, redirect, response code, JSON shape,
  template name, and template context key.
- All rendered HTML structure, static asset contents, CSS behavior, JavaScript
  behavior, and display layout.
- `zira_dashboard.odoo_client` public functions, exception classes, constants,
  and established monkeypatch seams used by application code and tests.
- Database schema, SQL results, cache lifetimes, background-job cadence, external
  API call ordering, and error-degradation behavior.
- Existing Python 3.11+ and deployment requirements.

No template, CSS, or JavaScript file will be edited unless a failing
characterization test proves that an extraction requires a compatibility repair.
Any such repair must preserve the generated output and receive a focused test.

## Architecture

### 1. Stable Odoo facade with domain internals

`odoo_client.py` remains the only supported application-facing Odoo module. It
continues to own environment configuration, XML-RPC proxy construction,
authentication, mutable caches, and public wrappers.

Focused private modules will hold domain algorithms and request construction:

- `_odoo_skills.py`: skill columns, level buckets, and employee-skill writes.
- `_odoo_calendars.py`: work schedules, calendar hours, lunch windows, and
  employee resource-calendar derivation.
- `_odoo_attendance.py`: attendance reads, datetime normalization, work-center
  fields, punches, and transfers.
- `_odoo_time_off.py`: leave types, balances, leave reads/writes, holidays, and
  duplicate detection.
- `_odoo_feedback.py`: feedback project, stage, tag, task, message, and attachment
  operations.

Internal functions accept `execute` and other patchable dependencies as
parameters. Public facade wrappers pass the facade's current objects on every
call. This preserves tests and callers that monkeypatch `odoo_client.execute`,
`_department_id_for_wc`, or cache variables after import.

Mutable caches stay facade-owned unless their identity is fully private and a
characterization test proves moving them is transparent. Cache invalidation
continues through named facade helpers rather than new cross-module assignments.

### 2. Thin HTTP routes around named services

Route handlers retain request parsing, authentication context, response creation,
and router registration. Pure or integration-heavy work moves behind explicit
functions with ordinary Python inputs and outputs.

Targeted seams:

- Settings: extract GET-page context assembly into a settings context module.
  POST routes remain in place unless a command has a clearly independent model.
- Recycling: extract multi-day aggregation and range-summary construction from
  `routes/departments.py`; single-day data loading and template rendering retain
  their current contracts.
- Exception inbox: extract machine-breakdown transfer, snooze, dismiss, report,
  and undo command logic from `routes/exceptions.py`; HTTP wrappers keep current
  validation and JSON responses.

The extraction order is based on coupling and regression risk, not file size.
If characterization reveals that one candidate depends on route-module globals
or patch-by-name behavior that cannot be preserved cleanly, it stays in place and
the reason is documented in the implementation plan.

### 3. Targeted efficiency and hygiene

Only high-confidence changes with observable maintenance or runtime value are in
scope:

- Replace repeated transformed-list loops when the resulting order and side
  effects are identical.
- Make `zip` truncation intent explicit at the two flagged route sites.
- Remove genuinely unused suppressions or assignments only after reference and
  test verification.
- Add expanded Ruff rules only for categories made clean by this work and useful
  enough to prevent regressions. Do not enable noisy rules globally merely to
  raise a score.

Cache TTL changes, speculative memoization, broad query rewrites, and automatic
formatting are out of scope because they could change freshness, call ordering,
or reviewability without a measured problem.

## Data and control flow

The request and integration paths remain:

1. FastAPI route parses and validates the request.
2. Route calls an existing facade or a newly extracted named service.
3. Service computes a context/result and delegates external I/O through injected
   current dependencies.
4. Route renders the same template or returns the same response shape.

For Odoo operations:

1. Caller invokes `odoo_client.<operation>` exactly as before.
2. Facade evaluates current cache and helper state.
3. Facade passes its current `execute` function and callbacks to the domain
   implementation.
4. Domain implementation builds the same Odoo model/method/domain/fields call.
5. Facade returns the same result or propagates the same exception type.

This indirection is deliberate: dependency lookup at call time keeps monkeypatch
and retry behavior intact.

## Error handling

- Existing custom Odoo exceptions and `xmlrpc.client.Fault` behavior remain
  unchanged.
- Best-effort operations remain best effort; extractions cannot turn a logged
  warning into a request failure or hide an existing failure.
- Route-level friendly error messages and status codes remain at the route
  boundary.
- Internal modules do not catch broad exceptions unless the existing code did so
  at the same boundary.
- Background warmers keep their current isolation: one failed integration tick
  is logged and cannot stop unrelated warmers.

## Testing and verification

Each extraction follows this sequence:

1. Add or identify characterization tests for call arguments, return shapes,
   cache behavior, monkeypatch seams, route responses, and rendered contracts.
2. Run those tests and record the passing pre-change baseline.
3. Move one responsibility while keeping its facade or route wrapper stable.
4. Run the focused tests immediately.
5. Run `ruff check src tests scripts` and the full local suite after each logical
   wave.

Final verification includes:

- The complete locally available test suite.
- Configured Ruff checks plus any focused expanded rules adopted by the refactor.
- Import/compile smoke checks for every extracted module.
- Browser smoke checks of representative scheduler, recycling, settings,
  exception-inbox, and timeclock pages when the local environment can serve them
  safely without touching production data.
- A final diff review specifically checking for route, template, static-asset,
  schema, and dependency changes.
- The existing Postgres-backed GitHub Actions suite must pass before merge; local
  success alone is not evidence for the 294 database-gated tests.

## Delivery shape

Work lands as small, independently reviewable commits:

1. Characterization tests and compatibility helpers.
2. Odoo domain extractions, one domain per commit or tightly related pair.
3. Route/service extractions, one seam per commit.
4. Targeted efficiency and lint-policy updates.
5. Final verification notes and documentation updates, if the architecture or
   developer workflow materially changed.

The branch is ready only when local verification is green, the final diff shows
no user-facing asset or contract drift, and remaining database-gated verification
is called out explicitly for CI.
