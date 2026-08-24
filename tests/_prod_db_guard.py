"""Refuse to run DB-backed tests against a production (Railway) database.

DB-backed tests DELETE rows in setup/teardown: a full-suite run against prod
wiped the forklift tables on 2026-07-20, and on 2026-08-24 repeated local runs
kept flipping the Auto-Lunch mode Off in production (the repo-root ``.env``
carried the prod ``DATABASE_URL`` and ``load_dotenv()`` injected it into every
pytest run). ``conftest.py`` aborts the session when ``forbidden_database_host``
matches — use the embedded pgserver recipe or a localhost Postgres instead.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_FORBIDDEN_HOST_SUFFIXES = (".rlwy.net", ".railway.internal", ".railway.app")

_KEYWORD_HOST_RE = re.compile(r"(?:^|\s)host=([^\s]+)")


def forbidden_database_host(dsn: str | None) -> str | None:
    """Return the DSN's hostname when it points at a Railway database.

    Returns None for empty, localhost/loopback, unix-socket, and unparseable
    DSNs (those cannot reach production on their own).
    """
    if not dsn:
        return None
    host: str | None = None
    if "://" in dsn:
        try:
            host = urlsplit(dsn).hostname
        except ValueError:
            host = None
    else:
        match = _KEYWORD_HOST_RE.search(dsn)
        host = match.group(1) if match else None
    if not host:
        return None
    if host.lower().endswith(_FORBIDDEN_HOST_SUFFIXES):
        return host
    return None
