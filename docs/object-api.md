# Object API

The object API is a server-to-server API for trusted internal apps. It mirrors
the best part of Odoo's XML-RPC/JSON-RPC style: call a named model, pass a
method, `args`, `kwargs`, and receive a consistent JSON envelope.

Use this API from backend services only. Never put a `gpi_live_...` key in a
browser, mobile app, spreadsheet, or public repository.

## Setup

1. Open Settings -> API.
2. Create a key. Admin keys can read and write every exposed object.
3. Copy the key once and store it in the calling app's server-side environment.
4. Revoke the key immediately if it is exposed.

Optional allowed IPs can be a comma-separated list of exact IPs or CIDR ranges.
When `REQUIRE_API_HTTPS=1`, calls must arrive over HTTPS or through a proxy that
sets `X-Forwarded-Proto: https`.

## Authentication

Send the key as a bearer token:

```http
Authorization: Bearer gpi_live_...
```

Keys are stored as HMAC-SHA256 hashes. The full secret is shown only once.

## Execute

```http
POST /api/v1/object/execute
Content-Type: application/json
Authorization: Bearer gpi_live_...
```

```json
{
  "model": "plant.person",
  "method": "search_read",
  "args": [[["active", "=", true]]],
  "kwargs": {
    "fields": ["id", "name", "skills"],
    "limit": 50,
    "order": "name asc"
  },
  "context": {
    "actor": "new-crm"
  }
}
```

Successful responses:

```json
{
  "ok": true,
  "result": [
    {"id": 1, "name": "Dale", "skills": {"Assembly": 3}}
  ]
}
```

Errors:

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

## Discovery

- `GET /api/v1/object/models`
- `GET /api/v1/object/models/plant.person/fields`

You can also call `fields_get` through `/execute`.

## Methods

- `fields_get`: returns field metadata.
- `search`: returns matching record IDs.
- `search_count`: returns the number of matching records.
- `read`: reads records by ID.
- `search_read`: searches and returns records in one call.
- `create`: creates a record when the model supports it.
- `write`: updates records when the model exposes writable fields.
- `unlink`: disabled unless a model explicitly enables deletes.

`search` and `search_read` support `limit`, `offset`, `fields`, and `order`.
The default limit is 100 and the maximum limit is 1000.

## Domains

Domains are implicit AND lists:

```json
[["name", "ilike", "dale"], ["active", "=", true]]
```

Supported operators: `=`, `!=`, `in`, `not in`, `ilike`, `not ilike`, `>`,
`>=`, `<`, `<=`.

## Models

### `plant.person`

People from the roster.

Readable fields: `id`, `odoo_id`, `name`, `active`, `reserve`, `excluded`,
`wage_type`, `spanish_speaker`, `skills`, `departments`.

Writable fields: `active`, `reserve`, `excluded`, `spanish_speaker`.

### `plant.work_center`

Configured plant work centers.

Readable fields: `id`, `name`, `bay`, `department`, `groups`,
`required_skills`, `default_people`, `goal_per_day`, `min_ops`, `max_ops`,
`note`.

Writable fields: `department`, `groups`, `required_skills`, `default_people`,
`goal_per_day`, `min_ops`, `max_ops`, `note`.

### `plant.schedule`

Daily schedule records, keyed by ISO date.

Readable fields: `id`, `day`, `published`, `assignments`, `notes`,
`work_center_notes`, `testing_day`.

Writable fields: `day`, `published`, `assignments`, `notes`,
`work_center_notes`, `testing_day`.

### `plant.time_off_request`

Time off requests from Odoo and local kiosk requests.

Readable fields: `id`, `person_odoo_id`, `person_name`, `start_date`,
`end_date`, `shape`, `hour_from`, `hour_to`, `status`, `source`.

This model is read-only.

## Examples

Read active people:

```sh
curl -s "$GPI_BASE_URL/api/v1/object/execute" \
  -H "Authorization: Bearer $GPI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "plant.person",
    "method": "search_read",
    "args": [[["active", "=", true]]],
    "kwargs": {"fields": ["id", "name"], "limit": 100}
  }'
```

Update a person's local flags:

```sh
curl -s "$GPI_BASE_URL/api/v1/object/execute" \
  -H "Authorization: Bearer $GPI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "plant.person",
    "method": "write",
    "args": [[123], {"reserve": true}]
  }'
```

Python helper:

```python
import os
import requests

BASE_URL = os.environ["GPI_BASE_URL"]
API_KEY = os.environ["GPI_API_KEY"]


def execute(model, method, *args, **kwargs):
    response = requests.post(
        f"{BASE_URL}/api/v1/object/execute",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"model": model, "method": method, "args": list(args), "kwargs": kwargs},
        timeout=20,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(body["error"]["message"])
    return body["result"]


people = execute(
    "plant.person",
    "search_read",
    [["active", "=", True]],
    fields=["id", "name"],
    limit=100,
)
```

## Security Notes

- Prefer one key per external app so each app can be revoked independently.
- Use allowed IPs for production services with stable egress.
- Keep admin keys server-side and out of logs.
- Use read-only keys for reporting tools.
- Check `api_audit_log` when investigating external writes or failed calls.
