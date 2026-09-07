# Stability and performance audit — September 7, 2026

## Scope and findings

This pass inspected shared TTL/HTTP caches, database pool lifecycle and bounded
connection waiting, app startup/background warmers, the object API, and samples
of staffing, layout, feedback and sharing routes. It also ran the repository's
lint and full test suite. It is not an exhaustive review of every workflow.

Fixed issues:

1. **Stale cache publication after a save (P1).** `TTLCache.get_or_compute`
   released its lock while reading and then published unconditionally. A save
   that cleared the cache during that read could be undone by its completion.
   An in-flight read could also overwrite an explicit newer `set`. A generation
   counter now suppresses publication across a write or invalidation. The
   already-running caller still receives its own read result; subsequent reads
   fetch fresh data. This applies to TTLCache computations, not separate HTTP
   response render/store sequences.
2. **Object API stalls unrelated requests (P1).** Bearer-key verification,
   model execution and audit writes performed synchronous database work on the
   event loop. These operations now run in Starlette's worker thread pool.
   Metadata GET handlers are synchronous so FastAPI also offloads them.
   Authorization, execution and auditing retain their order and responses.
3. **Refreshed cache entries evicted too soon (P2).** Updating an existing key
   did not move it to the end of the eviction order. A recently refreshed value
   could be evicted before older entries. Writes now refresh that order.

## Evidence

- Four added regression tests failed against the original implementation for
  the intended reasons, then passed after the fixes.
- The concurrency test blocks each object API I/O stage in turn and verifies
  that an unrelated request can release it before the blocking call times out.
- Focused cache, HTTP-cache and object API tests: **24 passed**.
- Full local suite on current `origin/main` (`a8d4b8df`), with
  `DATABASE_URL=''`: **6,160 passed, 476 skipped**. The initial older checkout
  also passed its suite: 3,888 passed, 399 skipped.
  The sandbox baseline could not launch Chromium; repeating outside the
  sandbox passed, including browser preview geometry checks.
- `ruff check src tests scripts`: passed. `git diff --check`: passed.

## Limits and follow-up candidates

- No production load measurements were taken. The concurrency test proves the
  removed stall; it does not establish a percentage speedup in production.
- Database-dependent tests were skipped locally. PostgreSQL integration is a
  separate CI gate; no production database lifecycle or physical kiosk was
  exercised in this audit.
- Several other async mutation routes still call synchronous work directly
  (for example feedback status updates). Those need a separate bounded review
  of transaction and authorization behavior before moving their work.
- HTTP response caches use separate lookup/render/store calls, so the TTLCache
  generation guard does not cover a render that races a response-cache clear.
  A future change would need to carry a generation token across that API.
- The generation counter is deliberately cache-wide and bounded in size. A
  write to another key can suppress one in-flight cache fill; it does not
  invalidate unrelated stored entries. Same-key cold reads can still compute
  concurrently. Request coalescing was not introduced.
- No changes to attendance/payroll rules, Odoo write gates, or test-debt skips.
