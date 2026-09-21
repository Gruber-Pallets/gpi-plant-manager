# Invited Users Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Require invitations and enforce Owner/Admin, HR, Manager, and Visitor access.

**Architecture:** Store active access in Postgres and check it for every personal request. Use an explicit route policy plus narrow HR field filtering on mixed screens. Keep TV and bearer integration authentication separate.

**Tech Stack:** Existing FastAPI, Starlette, Jinja, psycopg2, Postgres, pytest.

## Global Constraints

- Shop-floor TVs must continue exactly as they do today.
- Personal access requires both a gruberpallets.com Microsoft identity and an active invitation.
- Initial owner: dale@gruberpallets.com. No automatic access for other domain users.
- Role/access changes apply on the next request. Do not cache access rows.
- Never remove the last active admin or restore revoked users on restart.
- Preserve unrelated working-tree files. Commit and push to origin/main.

### Task 1: Durable users and admin screen

Files: create `src/zira_dashboard/user_access.py`, `src/zira_dashboard/routes/user_access.py`, `src/zira_dashboard/templates/user_access.html`, `tests/test_user_access.py`; modify `_schema.py` and app router registration.

Interfaces: `user_access.lookup_active(email) -> dict | None`, `list_users() -> list[dict]`, `save_user(email, role, active, actor) -> None`; roles `admin`, `hr`, `manager`, `visitor`. Route `/admin/users` uses request.state.user_role and request.state.user_upn. The main auth integration assigns those fields after lookup.

- [ ] Write failing tests for email/role validation, inactive users, one-time seed, last-admin concurrency protection, actor audit, and CSRF.
- [ ] Add idempotent table DDL with a one-time migration marker and initial owner seed. Serialize administrative changes with a transaction advisory lock; audit changes in the same transaction.
- [ ] Implement normalized exact-email invitation, role changes, and revocation. Validate actor is an active admin inside the write transaction.
- [ ] Build the administration page with role explanations, active/inactive status, invite/edit forms, and shareable sign-in link. Require signed-in admin and session-bound CSRF token.
- [ ] Run `DATABASE_URL='' .venv/bin/python -m pytest tests/test_user_access.py -q`; validate DB behavior against a local test database if available.

### Task 2: Request authorization and privacy

Files: create `src/zira_dashboard/permissions.py`, `tests/test_permissions.py`; modify `auth.py`, `routes/auth.py`, `_http_cache.py`, `deps.py`, shared navigation, sensitive route/rendering boundaries.

- [ ] Write failing tests using signed sessions and fake active rows for uninvited/revoked identities, role changes, domain enforcement, allowed operational writes, forbidden HR/admin access, read-only Visitors, and unchanged TV behavior.
- [ ] Resolve current user on every request and at login; respond with access-denied or unavailable pages without leaking data. Enforce normalized paths and registered route patterns with deny-by-default non-admin classification.
- [ ] Define explicit read/write policy for every registered personal route. Preserve the TV bypass and server-to-server bearer API.
- [ ] Make current role available to Jinja and partition rendered caches by role. Mark signed-in responses private/no-store.
- [ ] Filter HR/pay fields from Manager staffing and exception models and block sensitive alternatives/export routes. Restrict settings and global layout mutations to admins.
- [ ] Render navigation and controls consistent with permissions. Test real templates, middleware, and cache behavior.
- [ ] Run `DATABASE_URL='' .venv/bin/python -m pytest tests/test_permissions.py tests/test_auth_middleware.py tests/test_auth_session.py -q` and relevant existing rendering tests.

### Task 3: Review, validation, and delivery

- [ ] Review complete diff for bypasses, sensitive data leakage, unsafe writes, cache mixing, and TV regressions. Resolve actionable findings.
- [ ] Run the full suite with a non-production DATABASE_URL and Ruff on changed Python files. Record actual results and any pre-existing failures.
- [ ] Add plain-language What's New notes and access-management documentation.
- [ ] Commit only this task's files and push origin/main. Verify remote commit. Report user-facing result, tests, and deployment limitations honestly.
