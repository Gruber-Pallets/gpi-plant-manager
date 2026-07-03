# Odoo-like object API for trusted apps

**Date:** 2026-07-03
**Status:** Approved (brainstorming -> implementation planning)

## Problem

The Plant Manager app has useful operational objects - people, work centers,
schedules, time off, dashboards, settings, inbox items, and production-derived
records - but other internal apps cannot consume or update them through one
durable API. The current JSON endpoints are built for browser screens, are
session-cookie authenticated, and are shaped around individual UI workflows.

The user wants a powerful API that feels like Odoo's external API: a compact
object surface where clients can discover models and fields, search records,
read selected fields, and perform writes through a consistent vocabulary. The
API must be safe to share with internal server-side apps and must not become a
public attack surface for outsiders.

Odoo reference: https://www.odoo.com/documentation/18.0/developer/reference/external_api.html

## Goals

1. Add a server-to-server API for trusted apps using private API keys.
2. Make the API feel Odoo-like: models, domains, field lists, pagination,
   ordering, `fields_get`, `search`, `read`, `search_read`, `search_count`,
   `create`, `write`, and guarded `unlink`.
3. Keep the API safe by exposing only registered models and adapter methods,
   never raw SQL tables.
4. Allow each external app to have its own revocable key, hashed at rest.
5. Record an audit trail of every API call, including app identity, optional
   actor metadata, model, method, target domain or ids, status, and error.
6. Start with a small set of high-value Plant Manager models, then add more
   models without changing the API shape.

## Non-goals

- Public third-party OAuth or per-user delegated auth.
- Exposing every database table automatically.
- Re-enabling public FastAPI `/docs`, `/redoc`, or `/openapi.json`.
- Replacing existing browser routes.
- Building a full Odoo XML-RPC compatibility layer.
- Allowing arbitrary method calls on Python modules or store objects.

## Decision

Build one Odoo-inspired object API under `/api/v1/object/*`, protected by
per-app bearer tokens.

The primary endpoint is:

```http
POST /api/v1/object/execute
Authorization: Bearer gpi_live_<token>
Content-Type: application/json
```

Request shape:

```json
{
  "model": "plant.person",
  "method": "search_read",
  "args": [[["active", "=", true]]],
  "kwargs": {
    "fields": ["id", "name", "department", "skills"],
    "limit": 50,
    "offset": 0,
    "order": "name asc"
  },
  "context": {
    "actor": "Dale",
    "source": "new-crm"
  }
}
```

Response shape:

```json
{
  "ok": true,
  "result": [
    {
      "id": 123,
      "name": "Jane Employee",
      "department": "Recycling",
      "skills": {"Repair": 3}
    }
  ]
}
```

Errors use a stable envelope:

```json
{
  "ok": false,
  "error": {
    "code": "access_denied",
    "message": "API key does not allow object:write",
    "details": {}
  }
}
```

## API endpoints

### `POST /api/v1/object/execute`

Runs one model method. This is the endpoint most apps use.

Supported methods in v1:

- `fields_get`: returns public field metadata for a model.
- `search`: returns record ids matching a domain.
- `search_count`: returns a count for a domain.
- `read`: returns selected fields for ids.
- `search_read`: combines `search` and `read`.
- `create`: creates one record and returns its id.
- `write`: updates records by id and returns `true`.
- `unlink`: deletes or archives records by id and returns `true`; requires an
  explicit `object:unlink` scope and may be disabled per model.

### `GET /api/v1/object/models`

Lists registered models the key may see:

```json
{
  "ok": true,
  "models": [
    {"model": "plant.person", "name": "People", "read": true, "write": true},
    {"model": "plant.work_center", "name": "Work Centers", "read": true, "write": false}
  ]
}
```

### `GET /api/v1/object/models/{model}/fields`

Convenience wrapper around `fields_get` for clients that want simple discovery
without constructing an execute payload.

## Authentication and key storage

Each trusted app gets a separate API key.

Key format:

- Plaintext shown once: `gpi_live_<random>`.
- Database stores only a prefix, hash, app name, scopes, timestamps,
  optional allowed IP list, and revocation timestamp.
- Hash uses HMAC-SHA256 with `SESSION_SECRET`. This is appropriate for v1
  because API keys are generated as high-entropy random tokens and are never
  user-chosen passwords.

Recommended schema:

```sql
CREATE TABLE api_keys (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  key_prefix TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,
  scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
  allowed_ips JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ
);
```

Scopes:

- `object:read`
- `object:write`
- `object:unlink`
- `model:<model>:read`
- `model:<model>:write`
- `model:<model>:unlink`
- `admin:*` for fully trusted internal apps

V1 can issue `admin:*` keys while still enforcing the scope machinery so future
keys can be narrowed without redesigning auth.

## Request security

The API is not part of the browser session system.

Security controls:

- Require `Authorization: Bearer ...` on every object API route.
- Add `/api/v1/object/` to the session-auth middleware bypass list so these
  routes do not redirect to Microsoft login; the object API routes must then
  reject every request that lacks a valid bearer key.
- Reject requests without HTTPS in production, using `X-Forwarded-Proto` behind
  Railway's proxy.
- Optional IP allowlist per key.
- Constant-time key comparison.
- Maximum request body size for `/api/v1/object/*`.
- Maximum `limit` cap for reads, defaulting to 100 and capped at 1000.
- Domain depth and clause count caps.
- Method allowlist per model.
- Field allowlist per model.
- No dynamic imports or arbitrary method names.
- No raw SQL from request input.
- `unlink` disabled by default per model.
- Audit every request before returning the response.

## Audit log

Recommended schema:

```sql
CREATE TABLE api_audit_log (
  id BIGSERIAL PRIMARY KEY,
  api_key_id INTEGER REFERENCES api_keys(id),
  app_name TEXT NOT NULL,
  actor TEXT,
  model TEXT,
  method TEXT,
  request_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL,
  error_code TEXT,
  duration_ms INTEGER,
  client_ip TEXT,
  user_agent TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

The audit log stores summaries, not full payloads, to avoid collecting
unnecessary employee or business data. For example: field names, ids count,
domain clauses count, limit, offset, and error code.

## Model registry

The API core owns dispatch, auth, validation, domains, and audit. Models are
registered adapters with explicit fields and methods.

Adapter contract:

```python
class ObjectModel:
    name: str
    display_name: str
    fields: dict[str, FieldSpec]
    default_order: str
    allow_unlink: bool = False

    def search(self, domain, *, limit, offset, order, context) -> list[int]: ...
    def search_count(self, domain, *, context) -> int: ...
    def read(self, ids, *, fields, context) -> list[dict]: ...
    def create(self, values, *, context) -> int: ...
    def write(self, ids, values, *, context) -> bool: ...
    def unlink(self, ids, *, context) -> bool: ...
```

Field metadata mirrors the useful parts of Odoo's `fields_get`:

```json
{
  "name": {"type": "char", "string": "Name", "readonly": false, "required": true},
  "active": {"type": "boolean", "string": "Active", "readonly": false},
  "skills": {"type": "json", "string": "Skills", "readonly": false}
}
```

V1 field types:

- `integer`
- `float`
- `char`
- `text`
- `boolean`
- `date`
- `datetime`
- `selection`
- `json`
- `many2one`
- `one2many`
- `many2many`

Relational types can start as read-only JSON/id fields where the local store
does not yet have clean relational write APIs.

## Domain language

Use Odoo-like prefix-free clause arrays:

```json
[["active", "=", true], ["name", "ilike", "dale"]]
```

V1 supports implicit AND across clauses.

Operators:

- `=`
- `!=`
- `in`
- `not in`
- `ilike`
- `not ilike`
- `>`
- `>=`
- `<`
- `<=`

Rejected in v1:

- Arbitrary SQL fragments.
- Nested boolean operators `|`, `&`, and `!`.
- Domain fields not exposed by the model.

Nested boolean domains can be added later after the simple domain compiler is
well tested.

## Initial models

Start with models that are valuable to other internal apps and have reasonably
clear existing store boundaries:

### `plant.person`

Backed by the roster and related people/skills helpers.

Read fields:

- `id`
- `name`
- `active`
- `department`
- `skills`
- `certifications`

Write fields:

- `active`
- `department`

Skill and certification writes are out of v1. They can be added later as a
separate adapter expansion once the existing skill update path is reviewed.

### `plant.work_center`

Backed by work center store/config.

Read fields:

- `id`
- `name`
- `group`
- `department`
- `required_skills`
- `active`

Write fields:

- Read-only in v1.

### `plant.schedule`

Backed by schedule store.

Read fields:

- `id`
- `day`
- `published`
- `assignments`
- `notes`
- `work_center_notes`
- `testing_day`

Write fields:

- `assignments`
- `notes`
- `work_center_notes`
- `testing_day`

Writes must reuse the same schedule validation and cache invalidation paths as
the existing `/staffing` workflow.

### `plant.time_off_request`

Backed by time-off request/audit helpers.

Read fields:

- `id`
- `person_name`
- `start_date`
- `end_date`
- `shape`
- `hour_from`
- `hour_to`
- `status`
- `source`

Write fields:

- Read-only in v1.

## Cache and side effects

Object writes must call the same invalidation hooks used by browser routes.

Examples:

- Schedule changes invalidate today's staffing/dashboard response caches.
- Work center changes invalidate page caches and related settings views.
- Time-off writes, once enabled, must trigger the same Odoo sync/audit behavior
  as in-app requests.

Adapters own these side effects because they understand the underlying model.

## Error codes

Stable codes:

- `auth_required`
- `invalid_api_key`
- `key_revoked`
- `ip_not_allowed`
- `access_denied`
- `model_not_found`
- `method_not_allowed`
- `invalid_request`
- `invalid_domain`
- `invalid_field`
- `record_not_found`
- `validation_error`
- `conflict`
- `server_error`

HTTP status mapping:

- `401`: missing or invalid key.
- `403`: valid key lacks scope or IP is not allowed.
- `404`: model not found.
- `400`: malformed request, domain, method arguments, or fields.
- `409`: write conflict.
- `422`: validation error on create/write values.
- `500`: unexpected server error with a redacted message.

## Admin/key management

V1 can use a small CLI script to avoid adding UI scope:

```bash
python -m zira_dashboard.api_keys create "New CRM" --scope admin:*
python -m zira_dashboard.api_keys revoke <id>
python -m zira_dashboard.api_keys list
```

The CLI prints the plaintext key once. Later, a settings/admin panel can wrap
the same functions.

## Testing

Test at three layers:

1. Pure unit tests for domain validation, field validation, ordering parsing,
   error envelopes, scope checks, and key hashing/verification.
2. Adapter tests for each registered model, using monkeypatched store helpers
   where possible so tests do not require Postgres.
3. Route tests with `TestClient`, covering missing key, invalid key, revoked
   key, read success, write denied without scope, write success with scope,
   audit logging, model listing, and field discovery.

Security-specific tests:

- Unknown model cannot dispatch.
- Unknown method cannot dispatch.
- Private fields cannot be read by naming them explicitly.
- Domain on private/unknown field is rejected.
- `limit` is capped.
- `unlink` is rejected without `object:unlink`.
- Browser session cookies do not authenticate object API requests.

## Rollout

1. Add schema for `api_keys` and `api_audit_log`.
2. Add key creation/list/revoke helpers and CLI.
3. Add object API auth dependency and request audit helper.
4. Add domain/field/method validation core.
5. Add the object route module and include it in `app.py`.
6. Register the first read-capable models.
7. Enable write methods model by model only when they reuse existing
   validation, persistence, and cache invalidation paths.
8. Document examples for the internal apps that will call the API.

This rollout gives other apps useful read access early, while write access
arrives through adapters that have been deliberately reviewed.
