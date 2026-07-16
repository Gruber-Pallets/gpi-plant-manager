"""Generic key → JSON-value access for the ``app_settings`` table.

One place that knows how ``app_settings`` stores values: psycopg2 may hand a
JSONB column back as an already-decoded Python object OR as raw JSON text
depending on adapter config, so reads normalize both; writes use the
``::jsonb`` + ``updated_at = now()`` upsert convention. ``settings_store``
(targets, time-off settings) builds its typed getters/setters on top of these.

CAVEAT — scalar strings: a JSONB string value decodes to a bare Python ``str``
in "decoded" mode, which ``get_setting`` then cannot tell apart from raw JSON
text and would fail to ``json.loads``. The only such value today is
``odoo_sync``'s ``odoo_last_sync`` (an isoformat string), which keeps its own
purpose-built decode. Values stored via this module should be dict/list/number
shapes (which round-trip cleanly in both psycopg2 modes).
"""
from __future__ import annotations

import json
from typing import Any


def get_setting(key: str) -> Any | None:
    """Return the decoded JSON value for ``key``, or ``None`` if the row is
    missing or its raw text won't decode."""
    from . import db
    rows = db.query("SELECT value FROM app_settings WHERE key = %s", (key,))
    if not rows:
        return None
    raw = rows[0]["value"]
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw


def set_setting(key: str, value: Any, *, cur=None) -> None:
    """Upsert ``key`` → ``value`` (JSON-encoded), stamping ``updated_at``."""
    from . import db
    sql = (
        "INSERT INTO app_settings (key, value, updated_at) "
        "VALUES (%s, %s::jsonb, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
    )
    params = (key, json.dumps(value))
    if cur is not None:
        cur.execute(sql, params)
    else:
        db.execute(sql, params)
